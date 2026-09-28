"""
HuggingFace TTS 模型爬虫
自动搜索 HuggingFace 上的公开 TTS 模型

VOICE-ONLINE (#89) 的三处硬约束，全部落在本文件 + `online_net`：
  * 每次请求都有真实超时与总预算（旧实现 30s×3 + 指数退避 ≈ 93 秒才失败一次）。
  * 端点可达性由**当次实测**决定：默认候选顺序是镜像 `hf-mirror.com` 在前、
    官方 `huggingface.co` 在后（本机实测官方端点 code=000 连不上、镜像 200）。
    不可用的端点在 `search_with_status()["endpoint_status"]` 里如实上报，
    不做"看起来成功了"的伪装。
  * 拿不到数据就返回空列表 + reason，**不再返回手写的 well-known 假条目**
    （旧 `_fallback_search` 会编 downloads=999999 的 10 条，前端搜到、下载必炸）。
"""

import os
import logging
from typing import Optional, List
from pathlib import Path
from dataclasses import dataclass, field

from modules.voice_manager.voice_crawler import online_net
from modules.voice_manager.voice_crawler.online_net import EndpointPool

logger = logging.getLogger(__name__)

HAS_HTTPX = online_net.HAS_HTTPX

# 语言代码规范化映射
LANGUAGE_ALIASES = {
    "zh": ["zh", "chi", "zho", "chinese", "中文", "汉语", "普通话", "cmn"],
    "en": ["en", "eng", "english", "英文", "英语"],
    "ja": ["ja", "jpn", "japanese", "日文", "日语"],
    "ko": ["ko", "kor", "korean", "韩文", "韩语"],
    "fr": ["fr", "fra", "fre", "french", "法文", "法语"],
    "de": ["de", "deu", "ger", "german", "德文", "德语"],
    "es": ["es", "spa", "spanish", "西班牙文", "西班牙语"],
    "ru": ["ru", "rus", "russian", "俄文", "俄语"],
    "ar": ["ar", "ara", "arabic", "阿拉伯文"],
    "pt": ["pt", "por", "portuguese", "葡萄牙文"],
    "it": ["it", "ita", "italian", "意大利文"],
    "th": ["th", "tha", "thai", "泰文"],
    "vi": ["vi", "vie", "vietnamese", "越南文"],
    "tr": ["tr", "tur", "turkish", "土耳其文"],
}
ALIAS_TO_LANG = {}
for code, aliases in LANGUAGE_ALIASES.items():
    for alias in aliases:
        ALIAS_TO_LANG[alias] = code
KNOWN_LANG_TAGS = set(LANGUAGE_ALIASES.keys())

# 模型页的规范 URL 仍是官方站（镜像只是访问路径），条目里另给 mirrored_from。
CANONICAL_BASE = "https://huggingface.co"


def default_endpoints() -> List[str]:
    """候选端点顺序：env 覆盖 → 镜像 → 官方。顺序会被实测结果重排。"""
    raw = os.getenv("VOICE_ONLINE_HF_ENDPOINTS", "").strip()
    if raw:
        return [b.strip().rstrip("/") for b in raw.split(",") if b.strip()]
    return ["https://hf-mirror.com", "https://huggingface.co"]


@dataclass
class OnlineVoiceModel:
    """在线音色模型信息"""
    name: str
    author: str
    downloads: int = 0
    likes: int = 0
    language: str = ""
    license: str = ""
    url: str = ""
    description: str = ""
    tags: list = field(default_factory=list)
    source: str = "huggingface"


class HuggingFaceCrawler:
    """HuggingFace TTS 模型爬虫"""

    BASE_URL = CANONICAL_BASE
    API_URL = CANONICAL_BASE + "/api"
    TTS_PIPELINE_TAG = "text-to-speech"

    def __init__(self, endpoints: Optional[List[str]] = None):
        self.pool = EndpointPool(endpoints or default_endpoints(), env_var="VOICE_ONLINE_HF_ENDPOINTS")
        self.client = online_net.make_client({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        self.last_status: dict = {}

    # ------------------------------------------------------------ 基础请求
    def api_get(self, path: str, params: dict = None,
                deadline_s: float = None) -> tuple:
        """GET 一个 /api 相对路径，按端点顺序各试 1 次。返回 (data, record)。"""
        url = f"{self.pool.ordered()[0]}{path}" if self.pool.ordered() else path
        return online_net.get_json(self.client, url, params=params,
                                   deadline_s=deadline_s or online_net.SEARCH_DEADLINE_S,
                                   pool=self.pool, path=path)

    def endpoint_status(self) -> list:
        return self.pool.status()

    # ------------------------------------------------------------ 搜索
    def search_with_status(
        self,
        query: str = "",
        language: str = None,
        sort: str = "downloads",
        limit: int = 20,
        deadline_s: float = None,
    ) -> dict:
        """搜索并如实返回"这次到底通没通"。

        返回: {ok, source, results, endpoint, http_code, elapsed_ms, reason, error,
               attempts, endpoint_status}
        `ok=False` 时 results 一定是空列表 —— 不再拿假数据填位。
        """
        params = {
            "pipeline_tag": self.TTS_PIPELINE_TAG,
            "sort": sort,
            "direction": -1,
            "limit": max(1, min(int(limit or 20), 50)),
        }
        if query:
            params["search"] = query
        if language:
            params["filter"] = language

        data, record = self.api_get("/api/models", params=params, deadline_s=deadline_s)
        status = {
            "source": "huggingface",
            "ok": bool(record.get("ok")),
            "endpoint": record.get("url", "").split("/api")[0],
            "http_code": record.get("http_code", 0),
            "elapsed_ms": record.get("elapsed_ms", 0.0),
            "reason": record.get("reason", ""),
            "error": record.get("error", ""),
            "attempts": record.get("attempts", []),
            "endpoint_status": self.pool.status(),
        }
        if not status["ok"] or not isinstance(data, list):
            status.update(results=[], count=0)
            self.last_status = status
            return status

        results = []
        for m in data:
            model = OnlineVoiceModel(
                name=m.get("modelId", m.get("id", "")),
                author=m.get("author", ""),
                downloads=m.get("downloads", 0),
                likes=m.get("likes", 0),
                language=self._extract_language(m),
                license=(m.get("cardData") or {}).get("license", ""),
                url=f"{CANONICAL_BASE}/{m.get('modelId', m.get('id', ''))}",
                description=self._extract_description(m),
                tags=m.get("tags", []),
            )
            results.append(self._to_dict(model))
        status.update(results=results, count=len(results))
        self.last_status = status
        return status

    def search(
        self,
        query: str = "",
        language: str = None,
        sort: str = "downloads",
        limit: int = 20,
    ) -> list:
        """兼容旧调用方的签名：只要条目列表（失败＝空列表）。"""
        return self.search_with_status(query, language, sort, limit)["results"]

    # ------------------------------------------------------------ 详情 / 文件清单
    def get_model_details(self, model_name: str, deadline_s: float = None) -> dict:
        """模型详情。失败返回 {name, available: False, reason, error} —— 不编数据。"""
        path = f"/api/models/{model_name}"
        data, record = self.api_get(path, deadline_s=deadline_s)
        if not record.get("ok") or not isinstance(data, dict):
            return {"name": model_name, "source": "huggingface", "available": False,
                    "reason": record.get("reason", "unreachable"),
                    "error": record.get("error", ""),
                    "endpoint_status": self.pool.status()}
        return {
            "name": model_name,
            "source": "huggingface",
            "available": True,
            "author": data.get("author", ""),
            "downloads": data.get("downloads", 0),
            "likes": data.get("likes", 0),
            "last_modified": data.get("lastModified", ""),
            "languages": self._extract_language(data),
            "license": (data.get("cardData") or {}).get("license", ""),
            "tags": data.get("tags", []),
            "url": f"{CANONICAL_BASE}/{model_name}",
            "pipeline_tag": data.get("pipeline_tag", ""),
            "siblings": [s.get("rfilename") for s in data.get("siblings", [])],
            "endpoint": record.get("url", "").split("/api")[0],
        }

    def list_files(self, model_name: str, revision: str = "main",
                   deadline_s: float = None) -> tuple:
        """仓库文件清单（tree API；拿不到就退到 detail 的 siblings）。

        返回 ([{path, size}], status_dict)。
        """
        data, record = self.api_get(f"/api/models/{model_name}/tree/{revision}",
                                    params={"recursive": "true"}, deadline_s=deadline_s)
        if record.get("ok") and isinstance(data, list):
            entries = [{"path": e.get("path"), "size": (e.get("lfs") or {}).get("size")
                        or e.get("size") or 0} for e in data if e.get("type") == "file"]
            return entries, {**record, "listing_via": "tree"}
        detail = self.get_model_details(model_name, deadline_s=deadline_s)
        if detail.get("available"):
            entries = [{"path": p, "size": 0} for p in detail.get("siblings") or [] if p]
            return entries, {"ok": True, "listing_via": "siblings",
                             "reason": record.get("reason", ""), "attempts": record.get("attempts", [])}
        return [], {"ok": False, "listing_via": "none",
                    "reason": detail.get("reason") or record.get("reason") or "unreachable",
                    "error": detail.get("error") or record.get("error", "")}

    def audio_candidates(self, model_name: str, revision: str = "main",
                         deadline_s: float = None) -> tuple:
        """仓库里可直接试听的人声样本文件（resolve 相对路径已备好）。"""
        entries, listing = self.list_files(model_name, revision, deadline_s=deadline_s)
        picked = online_net.pick_audio_paths(entries)
        for p in picked:
            p["resolve_path"] = f"/{model_name}/resolve/{revision}/{p['path']}"
        return picked, listing

    def download_sample(self, model_name: str, dest: Path, file_path: str = None,
                        revision: str = "main", max_bytes: int = None,
                        deadline_s: float = None) -> dict:
        """下载仓库里的一个音频文件（不给 file_path 就自动挑最小的样本）。

        只写文件、不落 `.part` 残留；DB 注册由服务层做（本爬虫不碰数据库）。
        """
        dest = Path(dest)
        candidates: List[dict] = []
        listing: dict = {}
        if file_path:
            path = f"/{model_name}/resolve/{revision}/{file_path.lstrip('/')}"
            chosen = {"path": file_path, "size": 0}
        else:
            candidates, listing = self.audio_candidates(model_name, revision,
                                                        deadline_s=deadline_s)
            if not candidates:
                return {"ok": False, "reason": "no_audio_in_repo",
                        "error": f"{model_name} 仓库里没有 .wav/.flac/.mp3/.ogg 样本文件",
                        "repo": model_name, "file_path": "", "candidates": [],
                        "bytes": 0, "elapsed_ms": 0.0, "listing": listing}
            chosen = candidates[0]
            path = chosen["resolve_path"]

        url = f"{self.pool.ordered()[0]}{path}" if self.pool.ordered() else path
        ok, record = online_net.download_file(
            self.client, url, dest, pool=self.pool, path=path,
            max_bytes=max_bytes or online_net.MAX_DOWNLOAD_BYTES,
            deadline_s=deadline_s or online_net.DOWNLOAD_DEADLINE_S)
        record.update(
            repo=model_name,
            file_path=chosen["path"],
            candidates=[c["path"] for c in candidates[:8]],
            listing=listing or None,
        )
        return record

    # ------------------------------------------------------------ 解析工具
    def _extract_language(self, model_data: dict) -> str:
        """从模型数据中提取语言信息（多源提取）"""
        found_langs = set()
        card_data = model_data.get("cardData", {}) or {}

        # 来源1: cardData.language
        lang_field = card_data.get("language", "")
        if isinstance(lang_field, list):
            for l in lang_field:
                norm = self._normalize_language_code(str(l))
                if norm:
                    found_langs.add(norm)
        elif lang_field:
            for part in str(lang_field).replace(",", " ").split():
                norm = self._normalize_language_code(part.strip())
                if norm:
                    found_langs.add(norm)

        # 来源2: tags 中包含的语言代码（如 "zh", "en"）
        tags = model_data.get("tags", []) or []
        for tag in tags:
            tag_lower = tag.lower().strip()
            if tag_lower in KNOWN_LANG_TAGS:
                found_langs.add(tag_lower)
            elif tag_lower in ALIAS_TO_LANG:
                found_langs.add(ALIAS_TO_LANG[tag_lower])

        # 来源3: modelId 中的语言暗示
        model_id = str(model_data.get("modelId", model_data.get("id", ""))).lower()
        for lang_code in ["zh", "en", "ja", "ko"]:
            if lang_code in model_id:
                if f"-{lang_code}" in model_id or f"_{lang_code}" in model_id:
                    found_langs.add(lang_code)

        return ",".join(sorted(found_langs)) if found_langs else ""

    def _normalize_language_code(self, code: str) -> str:
        """规范化语言代码"""
        code = code.strip().lower()
        if code in KNOWN_LANG_TAGS:
            return code
        if code in ALIAS_TO_LANG:
            return ALIAS_TO_LANG[code]
        if "-" in code:
            base = code.split("-")[0]
            if base in KNOWN_LANG_TAGS:
                return base
        return ""

    def _extract_description(self, model_data: dict) -> str:
        """从多源提取模型描述"""
        card_data = model_data.get("cardData", {}) or {}
        desc = card_data.get("description", "")
        if desc and len(desc) > 10:
            return desc[:500]

        # 从 tags 生成
        tags = model_data.get("tags", []) or []
        pipeline = model_data.get("pipeline_tag", "")
        model_id = model_data.get("modelId", model_data.get("id", ""))

        parts = [f"模型: {model_id}"]
        if pipeline:
            parts.append(f"类型: {pipeline}")
        relevant_tags = [t for t in tags if t not in KNOWN_LANG_TAGS]
        if relevant_tags:
            parts.append(f"标签: {', '.join(relevant_tags[:6])}")
        return " | ".join(parts)

    def _to_dict(self, model: OnlineVoiceModel) -> dict:
        return {
            "name": model.name,
            "model_id": model.name,
            "metric": "downloads",
            "author": model.author,
            "downloads": model.downloads,
            "likes": model.likes,
            "language": model.language,
            "license": model.license,
            "url": model.url,
            "description": model.description,
            "tags": model.tags,
            "source": model.source,
        }

    async def search_tts_models(self, query: str = "tts", limit: int = 30) -> list:
        """search() 的别名，用于统一接口（同步实现，别在事件循环里长挂 —— 调用方
        要么用 asyncio.to_thread 包，要么走 `/online/search` 路由的线程池版本）。"""
        import inspect
        result = self.search(query, None, "downloads", limit)
        if inspect.iscoroutine(result):
            return await result
        return result
