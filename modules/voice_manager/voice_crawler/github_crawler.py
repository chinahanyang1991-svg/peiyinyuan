"""
GitHub TTS 项目爬虫
搜索 GitHub 上开源的 TTS/语音合成相关项目

VOICE-ONLINE (#89)：
  * 超时由 `online_net` 统一给（连接 5s / 读 10s / 总预算），不再 30 秒裸等。
  * 失败返回结构化 status（含真实 http_code 与错误文本），不再"返回空列表当成功"。
  * 删掉了旧 `_fallback_search` 的手写假仓库（3 条、stars 都是编的）。
  * GitHub 仓库里的样本音频也能真下载：先取 default_branch，再用
    git/trees?recursive=1 挑音频，最后走 raw.githubusercontent.com。
"""

import logging
from typing import Optional, List, Tuple
from pathlib import Path

from modules.voice_manager.voice_crawler import online_net
from modules.voice_manager.voice_crawler.online_net import EndpointPool

logger = logging.getLogger(__name__)

HAS_HTTPX = online_net.HAS_HTTPX


class GitHubCrawler:
    """GitHub TTS 项目爬虫"""

    API_URL = "https://api.github.com"
    RAW_URL = "https://raw.githubusercontent.com"
    TTS_TOPICS = ["text-to-speech", "tts", "voice-synthesis", "voice-cloning"]

    def __init__(self):
        # 单端点：api.github.com 实测可达；池子只用来如实记录失败状态与冷却。
        self.pool = EndpointPool([self.API_URL])
        self.client = online_net.make_client({
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "PeiYinYuan/1.0",
        })
        self.last_status: dict = {}

    def api_get(self, path: str, params: dict = None, deadline_s: float = None) -> tuple:
        url = f"{self.API_URL}{path}"
        return online_net.get_json(self.client, url, params=params,
                                   deadline_s=deadline_s or online_net.SEARCH_DEADLINE_S)

    # ------------------------------------------------------------ 搜索
    def search_with_status(
        self,
        query: str = "tts",
        language: str = None,
        sort: str = "stars",
        limit: int = 20,
        deadline_s: float = None,
    ) -> dict:
        """搜索 GitHub 仓库，并把这次请求的真实结果一并返回。

        Args:
            query: 搜索关键词
            language: 编程语言筛选 ("python", "javascript", etc.)
            sort: 排序 ("stars", "forks", "updated")
            limit: 返回数量上限
        """
        q_parts = [query or "tts", "tts"]
        if language:
            q_parts.append(f"language:{language}")

        params = {
            "q": "+".join(q_parts),
            "sort": sort,
            "order": "desc",
            "per_page": max(1, min(int(limit or 20), 50)),
        }
        data, record = self.api_get("/search/repositories", params=params,
                                    deadline_s=deadline_s)
        status = {
            "source": "github",
            "ok": bool(record.get("ok")),
            "endpoint": self.API_URL,
            "http_code": record.get("http_code", 0),
            "elapsed_ms": record.get("elapsed_ms", 0.0),
            "reason": record.get("reason", ""),
            "error": record.get("error", ""),
            "attempts": record.get("attempts", []),
            "endpoint_status": self.pool.status(),
        }
        if not status["ok"] or not isinstance(data, dict):
            status.update(results=[], count=0)
            self.last_status = status
            return status

        results = []
        for repo in data.get("items", []) or []:
            full_name = repo.get("full_name", "")
            results.append({
                "name": full_name,
                "model_id": full_name,          # 与 HF 条目统一的服务层字段
                "description": (repo.get("description") or "")[:300],
                "stars": repo.get("stargazers_count", 0),
                # GitHub 仓库没有"下载量"这个指标：如实给 0，另用 metric 告诉前端该显示 stars。
                "downloads": 0,
                "metric": "stars",
                "likes": repo.get("stargazers_count", 0),
                "forks": repo.get("forks_count", 0),
                "language": repo.get("language", ""),
                "url": repo.get("html_url", ""),
                "topics": repo.get("topics", []) or [],
                "updated_at": repo.get("updated_at", ""),
                "default_branch": repo.get("default_branch") or "main",
                "source": "github",
            })
        status.update(results=results, count=len(results))
        self.last_status = status
        return status

    def search(self, query: str = "tts", language: str = None,
               sort: str = "stars", limit: int = 20) -> list:
        """兼容旧签名：只要条目列表（失败＝空列表，不再有假仓库）。"""
        return self.search_with_status(query, language, sort, limit)["results"]

    # ------------------------------------------------------------ 仓库音频
    def resolve_repo(self, repo: str, deadline_s: float = None) -> Tuple[str, dict]:
        """拿默认分支。返回 (branch, status)。"""
        data, record = self.api_get(f"/repos/{repo}", deadline_s=deadline_s)
        branch = "main"
        if isinstance(data, dict) and data.get("default_branch"):
            branch = data["default_branch"]
        return branch, record

    def audio_candidates(self, repo: str, deadline_s: float = None) -> Tuple[List[dict], dict]:
        """列出仓库里可直接下载的音频文件（raw 路径已备好）。

        大仓库的 tree API 会 `truncated: true` —— 那也如实上报，不假装列全了。
        """
        branch, rec = self.resolve_repo(repo, deadline_s=deadline_s)
        if not rec.get("ok"):
            return [], {"repo": repo, "ok": False, "reason": rec.get("reason", "unreachable"),
                        "error": rec.get("error", ""), "http_code": rec.get("http_code", 0)}
        data, record = self.api_get(f"/repos/{repo}/git/trees/{branch}",
                                    params={"recursive": "1"}, deadline_s=deadline_s)
        if not record.get("ok") or not isinstance(data, dict):
            return [], {"repo": repo, "ok": False, "branch": branch,
                        "reason": record.get("reason", "unreachable"),
                        "error": record.get("error", ""),
                        "http_code": record.get("http_code", 0)}
        tree = data.get("tree", []) or []
        picked = online_net.pick_audio_paths(tree)
        for p in picked:
            p["raw_path"] = f"/{repo}/{branch}/{p['path']}"
            p["resolve_path"] = p["raw_path"]
        return picked, {"repo": repo, "ok": True, "branch": branch,
                        "entries": len(tree), "truncated": bool(data.get("truncated")),
                        "http_code": record.get("http_code", 0),
                        "elapsed_ms": record.get("elapsed_ms", 0.0)}

    def download_sample(self, repo: str, dest: Path, file_path: str = None,
                        branch: str = None, max_bytes: int = None,
                        deadline_s: float = None) -> dict:
        """从 raw.githubusercontent.com 下载仓库里的一个音频文件。"""
        dest = Path(dest)
        candidates: List[dict] = []
        listing: dict = {}
        if not branch:
            branch, _ = self.resolve_repo(repo, deadline_s=deadline_s)
        if file_path:
            chosen = {"path": file_path.lstrip("/")}
        else:
            candidates, listing = self.audio_candidates(repo, deadline_s=deadline_s)
            if not candidates:
                return {"ok": False, "reason": (listing or {}).get("reason", "no_audio_in_repo"),
                        "error": (listing or {}).get("error", "该仓库没有可下载的音频文件"),
                        "repo": repo, "file_path": "", "candidates": [], "bytes": 0,
                        "elapsed_ms": 0.0, "listing": listing}
            chosen = candidates[0]
        path = f"/{repo}/{branch}/{chosen['path']}"
        url = f"{self.RAW_URL}{path}"
        ok, record = online_net.download_file(
            self.client, url, dest, max_bytes=max_bytes or online_net.MAX_DOWNLOAD_BYTES,
            deadline_s=deadline_s or online_net.DOWNLOAD_DEADLINE_S)
        record.update(repo=repo, file_path=chosen["path"], branch=branch,
                      candidates=[c["path"] for c in (candidates or [])[:8]],
                      listing=listing or None)
        return record

    async def search_tts_projects(self, query: str = "tts", limit: int = 30) -> list:
        """search() 的别名，用于统一接口（同步实现，调用方负责 to_thread）。"""
        import inspect
        result = self.search(query, None, "stars", limit)
        if inspect.iscoroutine(result):
            return await result
        return result
