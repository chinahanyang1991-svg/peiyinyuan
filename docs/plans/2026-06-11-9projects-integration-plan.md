# 9大项目整合实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将9个开源项目整合到配音员程序中，新增18文件、修改15文件、新增6个测试文件

**Architecture:** 保持现有 backend → services → modules 三层架构，通过扩展现有类/接口实现增量整合

**Tech Stack:** Python/FastAPI/SQLAlchemy, TypeScript/React/Vite/Electron, anime.js, ffmpeg

---

## 里程碑 1: TTS引擎 + 电子书管道 (v2.0.0)

### Task 1.1: 新建 M4B 构建器

**Files:**
- Create: `modules/tts_engine/m4b_builder.py`
- Create: `tests/test_m4b_builder.py`

- [ ] **Step 1: 创建 M4B Builder 模块**

```python
# modules/tts_engine/m4b_builder.py
"""
M4B 有声书构建器
参考 ebook2audiobook + audiblez 的 M4B 封装逻辑
使用 ffmpeg 将 WAV 音频段合并为 M4B 格式有声书（含章节标记）
"""

import os
import subprocess
import tempfile
import json
from pathlib import Path
from typing import Optional


class M4BBuilder:
    """M4B 有声书构建器"""

    def __init__(self):
        self._check_ffmpeg()

    def _check_ffmpeg(self):
        """检测 ffmpeg 是否可用"""
        try:
            subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True, timeout=5
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            print("[M4B] 警告: ffmpeg 未安装，M4B 输出将不可用。安装: https://ffmpeg.org/download.html")

    @property
    def available(self) -> bool:
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def build(
        self,
        audio_segments: list,
        metadata: dict,
        output_path: str,
        sample_rate: int = 48000,
    ) -> str:
        """
        将音频段合并为 M4B 有声书

        Args:
            audio_segments: [{"audio_data": bytes, "chapter_title": "第x章", "duration_ms": 12000}, ...]
            metadata: {"title": "书名", "author": "作者", "year": "2024"}
            output_path: 输出文件路径 (.m4b)
            sample_rate: 采样率

        Returns:
            str: 输出文件路径
        """
        if not self.available:
            raise RuntimeError("ffmpeg 未安装，无法生成 M4B。请安装 ffmpeg: https://ffmpeg.org/download.html")

        if not audio_segments:
            raise ValueError("audio_segments 为空")

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="m4b_build_") as tmpdir:
            tmpdir = Path(tmpdir)

            # 1. 写入每个音频段为 WAV 文件
            wav_files = []
            for i, seg in enumerate(audio_segments):
                data = seg.get("audio_data", b"")
                if not data:
                    continue
                wav_path = tmpdir / f"seg_{i:04d}.wav"
                wav_path.write_bytes(data)
                wav_files.append(str(wav_path))

            if not wav_files:
                raise ValueError("无有效音频数据")

            # 2. 写入 concat 列表
            concat_list = tmpdir / "concat.txt"
            with open(concat_list, "w", encoding="utf-8") as f:
                for wf in wav_files:
                    f.write(f"file '{wf}'\n")

            # 3. 写入 ffmetadata 章节标记
            metadata_path = tmpdir / "ffmetadata.txt"
            self._write_ffmetadata(audio_segments, metadata, str(metadata_path))

            # 4. ffmpeg 合并 + 封装 M4B
            temp_output = tmpdir / "output.m4b"

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-i", str(metadata_path),
                "-map_metadata", "1",
                "-c:a", "aac", "-b:a", "128k",
                "-ar", str(sample_rate),
                "-movflags", "+faststart",
                str(temp_output),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg 失败: {result.stderr[:500]}")

            # 5. 移动到目标路径
            import shutil
            shutil.move(str(temp_output), output_path)

        return output_path

    def _write_ffmetadata(self, audio_segments: list, metadata: dict, output_path: str):
        """写入 ffmpeg 章节元数据"""
        lines = [";FFMETADATA1"]

        title = metadata.get("title", "有声书")
        author = metadata.get("author", "")
        year = metadata.get("year", "")

        if title:
            lines.append(f"title={title}")
        if author:
            lines.append(f"artist={author}")
        if year:
            lines.append(f"date={year}")

        # 累计时间追踪章节边界
        cumulative_ms = 0
        for seg in audio_segments:
            duration_ms = seg.get("duration_ms", 0)
            if duration_ms <= 0:
                continue
            chapter_title = seg.get("chapter_title", "")

            lines.append("[CHAPTER]")
            lines.append("TIMEBASE=1/1000")
            lines.append(f"START={cumulative_ms}")
            cumulative_ms += duration_ms
            lines.append(f"END={cumulative_ms}")
            if chapter_title:
                lines.append(f"title={chapter_title}")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
```

- [ ] **Step 2: 验证模块可导入**

```bash
cd d:\程序开发\配音员\peiyinyuan && python -c "from modules.tts_engine.m4b_builder import M4BBuilder; b = M4BBuilder(); print(f'ffmpeg available: {b.available}')"
```

---

### Task 1.2: 新建 TextNormalizer

**Files:**
- Create: `modules/text_processor/text_normalizer.py`

- [ ] **Step 1: 从 VoxCPM2 移植 TextNormalizer**

```python
# modules/text_processor/text_normalizer.py
"""
文本规范化器
参考 VoxCPM2 的 text_normalize.py，规范化数字、符号、缩写等
"""

import re


class TextNormalizer:
    """文本规范化：数字、符号、特殊字符标准化"""

    # 中文数字读法映射
    CN_DIGITS = {
        "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
        "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
    }

    CN_UNITS = ["", "十", "百", "千", "万", "十万", "百万", "千万", "亿"]

    def normalize(self, text: str) -> str:
        """主规范化入口"""
        if not text:
            return text

        text = self._normalize_whitespace(text)
        text = self._normalize_numbers(text)
        text = self._normalize_punctuation(text)
        text = self._normalize_abbreviations(text)
        return text.strip()

    def _normalize_whitespace(self, text: str) -> str:
        """统一空白字符"""
        # 多个空格合并为一个
        text = re.sub(r"[ \t]+", " ", text)
        # 多个换行合并
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    def _normalize_numbers(self, text: str) -> str:
        """规范化数字（保留原样，供 TTS 引擎处理）"""
        # 移除数字间的逗号: 1,234,567 → 1234567
        text = re.sub(r"(\d),(\d)", r"\1\2", text)
        # 百分号: 50% → 百分之五十
        text = re.sub(r"(\d+)%", r"百分之\1", text)
        return text

    def _normalize_punctuation(self, text: str) -> str:
        """规范化标点符号"""
        # 英文引号 → 中文引号
        text = text.replace('"', "\u201c").replace('"', "\u201d")
        # 连续标点合并
        text = re.sub(r"[。！？]{2,}", lambda m: m.group()[0], text)
        # 中英文之间的空格
        text = re.sub(r"([\u4e00-\u9fff])([A-Za-z])", r"\1 \2", text)
        text = re.sub(r"([A-Za-z])([\u4e00-\u9fff])", r"\1 \2", text)
        return text

    def _normalize_abbreviations(self, text: str) -> str:
        """展开常见缩写"""
        replacements = {
            "Mr.": "Mister",
            "Mrs.": "Misses",
            "Dr.": "Doctor",
            "etc.": "et cetera",
            "e.g.": "for example",
            "i.e.": "that is",
            "vs.": "versus",
        }
        for abbr, full in replacements.items():
            text = text.replace(abbr, full)
        return text

    def quick_normalize(self, text: str) -> str:
        """快速规范化（仅空白和基础标点）"""
        text = re.sub(r"[ \t]+", " ", text)
        text = text.strip()
        return text
```

- [ ] **Step 2: 验证可导入**

```bash
cd d:\程序开发\配音员\peiyinyuan && python -c "from modules.text_processor.text_normalizer import TextNormalizer; t=TextNormalizer(); print(t.normalize('Hello   世界！50% test'))"
```

---

### Task 1.3: 升级 VoxCPM Adapter — 新增 LoRA 接口

**Files:**
- Modify: `modules/tts_engine/engines/voxcpm_adapter.py`

- [ ] **Step 1: 在 VoxCPMAdapter 类末尾新增 LoRA 方法**

在 `_mock_synthesize` 方法之前插入以下方法：

```python
    # ---- LoRA 微调接口 (参考 voxcpm/core.py L317-365) ----

    def load_lora(self, lora_weights_path: str) -> tuple:
        """
        加载 LoRA 微调权重

        Args:
            lora_weights_path: .pth 文件或包含 lora_weights.ckpt 的目录路径

        Returns:
            tuple: (loaded_keys, skipped_keys) 加载和跳过的参数名列表
        """
        if not self._loaded:
            self.load_model()

        if self._model is None:
            raise RuntimeError("VoxCPM 模型未加载，无法加载 LoRA 权重")

        if hasattr(self._model, 'load_lora'):
            return self._model.load_lora(lora_weights_path)
        raise RuntimeError("当前 VoxCPM 模型不支持 LoRA")

    def unload_lora(self):
        """卸载 LoRA 权重，恢复基础模型"""
        if self._model and hasattr(self._model, 'unload_lora'):
            self._model.unload_lora()

    def set_lora_enabled(self, enabled: bool):
        """启用/禁用 LoRA（不卸载权重）"""
        if self._model and hasattr(self._model, 'set_lora_enabled'):
            self._model.set_lora_enabled(enabled)

    @property
    def lora_enabled(self) -> bool:
        """检查是否配置了 LoRA"""
        if self._model and hasattr(self._model, 'lora_enabled'):
            return self._model.lora_enabled
        return False

    def get_lora_state_dict(self) -> dict:
        """获取当前 LoRA 参数"""
        if self._model and hasattr(self._model, 'get_lora_state_dict'):
            return self._model.get_lora_state_dict()
        return {}
```

插入位置：在 `_mock_synthesize` 方法定义之前（即 `def _mock_synthesize` 之前）。

---

### Task 1.4: 升级 MOBI 解析器 — 章节拆分

**Files:**
- Modify: `modules/book_parser/parsers/mobi_parser.py`

- [ ] **Step 1: 在 `_try_mobi_lib` 中添加章节拆分逻辑**

替换 `_try_mobi_lib` 方法中的文本提取部分，在 BeautifulSoup 解析后添加章节拆分：

在 `_try_mobi_lib` 方法中，找到以下代码段（BeautifulSoup 解析 HTML 之后）:

```python
                        for tag in soup(['script', 'style', 'nav']):
                            tag.decompose()
                        text = soup.get_text()
                        lines = [l.strip() for l in text.split('\n') if l.strip()]
                        clean_text = '\n'.join(lines)

                        if len(clean_text) > 50:
                            chapters.append({
                                "title": f"章节 {idx + 1}",
                                "index": idx,
                                "content": clean_text,
                                "char_count": len(clean_text),
                            })
                            idx += 1
```

替换为：

```python
                        for tag in soup(['script', 'style', 'nav']):
                            tag.decompose()

                        # 尝试按标题标签拆分章节
                        sub_chapters = self._split_by_headings(soup)
                        if sub_chapters:
                            for ch in sub_chapters:
                                chapters.append({
                                    "title": ch["title"],
                                    "index": idx,
                                    "content": ch["content"],
                                    "char_count": ch["char_count"],
                                })
                                idx += 1
                        else:
                            # 回退：整本作为一章
                            text = soup.get_text()
                            lines = [l.strip() for l in text.split('\n') if l.strip()]
                            clean_text = '\n'.join(lines)
                            if len(clean_text) > 50:
                                chapters.append({
                                    "title": f"章节 {idx + 1}",
                                    "index": idx,
                                    "content": clean_text,
                                    "char_count": len(clean_text),
                                })
                                idx += 1
```

- [ ] **Step 2: 新增 `_split_by_headings` 方法**

在 `_decode_html` 方法之后（或类中任意位置）添加：

```python
    def _split_by_headings(self, soup) -> list:
        """按 HTML h1/h2/h3 标签拆分章节（参考 ebook2audiobook）"""
        chapters = []
        current_title = ""
        current_lines = []

        if not soup.body:
            return []

        for elem in soup.body.descendants:
            # 标题标签：标记新章节
            if elem.name in ('h1', 'h2', 'h3') and elem.get_text(strip=True):
                # 保存前一个章节
                if current_lines and len('\n'.join(current_lines)) > 30:
                    chapters.append({
                        "title": current_title or f"章节 {len(chapters) + 1}",
                        "content": '\n'.join(current_lines),
                        "char_count": sum(len(l) for l in current_lines),
                    })
                current_title = elem.get_text(strip=True)
                current_lines = []
            # 文本节点
            elif elem.name is None:
                text = elem.strip()
                if text:
                    current_lines.append(text)

        # 最后一个章节
        if current_lines and len('\n'.join(current_lines)) > 30:
            chapters.append({
                "title": current_title or f"章节 {len(chapters) + 1}",
                "content": '\n'.join(current_lines),
                "char_count": sum(len(l) for l in current_lines),
            })

        return chapters if len(chapters) > 1 else []  # 至少2章才拆分
```

- [ ] **Step 3: 验证章节拆分**

```bash
cd d:\程序开发\配音员\peiyinyuan && python -c "
import sys; sys.path.insert(0,'.')
from modules.book_parser.parsers.mobi_parser import MobiParser
p = MobiParser(r'data\books\cba48c45-f02e-4ebe-81f7-f0ce15932079.mobi')
data = p.parse()
print(f'章节数: {len(data.chapters)}')
for i, ch in enumerate(data.chapters[:5]):
    print(f'  [{i}] {ch[\"title\"][:30]} ({ch[\"char_count\"]}字)')
"
```

---

### Task 1.5: 升级 EPUB 解析器 — 章节拆分

**Files:**
- Modify: `modules/book_parser/parsers/epub_parser.py`

- [ ] **Step 1: 读取当前 EPUB 解析器**

```bash
# 确认文件路径和当前代码
```

- [ ] **Step 2: 在 EPUB 解析器中应用同样的 `_split_by_headings` 逻辑**

由于 EPUB 解析器已有独立的解析流程，将 `_split_by_headings` 提取到共享位置或复制到 epub_parser.py 中。

在 `epub_parser.py` 中，修改文档遍历逻辑：每个 item 解析 HTML 后，调用 `_split_by_headings` 按标题拆分，而不是整页作为一章。

---

### Task 1.6: 新建 HTML 解析器

**Files:**
- Create: `modules/book_parser/parsers/html_parser.py`
- Modify: `modules/book_parser/parser_factory.py`

- [ ] **Step 1: 创建 HTML 解析器**

```python
# modules/book_parser/parsers/html_parser.py
"""HTML 格式电子书解析器"""

import re
from pathlib import Path
from modules.book_parser.parser_factory import BookData


class HtmlParser:
    """HTML 格式解析器"""

    def __init__(self, file_path: str):
        self.file_path = file_path

    def parse(self) -> BookData:
        data = BookData()
        data.format = "html"
        data.file_path = self.file_path
        data.title = Path(self.file_path).stem

        try:
            from bs4 import BeautifulSoup
        except ImportError:
            data.chapters = [{"title": data.title, "index": 0, "content": "（需要安装 beautifulsoup4）", "char_count": 0}]
            return data

        with open(self.file_path, "r", encoding="utf-8", errors="replace") as f:
            html_content = f.read()

        soup = BeautifulSoup(html_content, "html.parser")

        # 提取标题
        title_tag = soup.find("title")
        if title_tag:
            data.title = title_tag.get_text(strip=True)

        # 去除脚本和样式
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        # 提取正文
        body = soup.find("body")
        if not body:
            body = soup

        # 按 h1/h2/h3 拆分章节
        chapters = self._split_by_headings(body)
        if not chapters:
            text = body.get_text()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            chapters = [{
                "title": data.title,
                "content": "\n".join(lines),
                "char_count": sum(len(l) for l in lines),
            }]

        data.chapters = []
        for i, ch in enumerate(chapters):
            data.chapters.append({
                "title": ch["title"],
                "index": i,
                "content": ch["content"],
                "char_count": ch["char_count"],
            })

        data.total_chars = sum(ch["char_count"] for ch in data.chapters)
        data.language = self._detect_language(
            data.chapters[0]["content"][:500] if data.chapters else ""
        )
        return data

    def _split_by_headings(self, soup) -> list:
        """按 HTML 标题标签拆分章节"""
        chapters = []
        current_title = ""
        current_lines = []

        for elem in soup.descendants:
            if elem.name in ("h1", "h2", "h3") and elem.get_text(strip=True):
                if current_lines and len("\n".join(current_lines)) > 30:
                    chapters.append({
                        "title": current_title or f"章节 {len(chapters) + 1}",
                        "content": "\n".join(current_lines),
                        "char_count": sum(len(l) for l in current_lines),
                    })
                current_title = elem.get_text(strip=True)
                current_lines = []
            elif elem.name is None:
                text = elem.strip()
                if text:
                    current_lines.append(text)

        if current_lines and len("\n".join(current_lines)) > 30:
            chapters.append({
                "title": current_title or f"章节 {len(chapters) + 1}",
                "content": "\n".join(current_lines),
                "char_count": sum(len(l) for l in current_lines),
            })

        return chapters if len(chapters) > 1 else (
            [{"title": "", "content": "\n".join(current_lines),
              "char_count": sum(len(l) for l in current_lines)}] if current_lines else []
        )

    def _detect_language(self, text: str) -> str:
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        return "zh" if chinese_chars > len(text) * 0.3 else "en"
```

- [ ] **Step 2: 在 parser_factory.py 中注册 HTML 解析器**

读取 `parser_factory.py`，在 `get_parser()` 方法中添加 `.html` / `.htm` 扩展名映射：

```python
from modules.book_parser.parsers.html_parser import HtmlParser

# 在 get_parser() 中添加:
if ext in ('.html', '.htm'):
    return HtmlParser(file_path)
```

---

## 里程碑 2: 爬虫 + 工作流 (v2.1.0)

### Task 2.1: 升级 HF 爬虫 — 反反爬 + 自适应解析

**Files:**
- Modify: `modules/voice_manager/voice_crawler/hf_crawler.py`

- [ ] **Step 1: 升级 httpx 客户端配置**

在 `HuggingFaceCrawler.__init__` 中升级配置：

```python
def __init__(self):
    self.client = None
    if HAS_HTTPX:
        self.client = httpx.Client(
            timeout=30.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
            follow_redirects=True,
        )
    self._user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]
    self._ua_index = 0

def _rotate_ua(self):
    """轮换 User-Agent"""
    self._ua_index = (self._ua_index + 1) % len(self._user_agents)
    if self.client:
        self.client.headers["User-Agent"] = self._user_agents[self._ua_index]
```

- [ ] **Step 2: 升级 `search()` 方法添加重试**

在 `search()` 方法的 httpx 请求处添加重试：

```python
import time

# 在 search() 方法中，将:
resp = self.client.get(f"{self.API_URL}/models", params=params)

# 替换为:
max_retries = 3
for attempt in range(max_retries):
    try:
        resp = self.client.get(f"{self.API_URL}/models", params=params)
        resp.raise_for_status()
        break
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429 and attempt < max_retries - 1:
            time.sleep(2 ** attempt)
            self._rotate_ua()
            continue
        raise
```

---

### Task 2.2: 工作流模型 + API

**Files:**
- Create: `backend/models/workflow.py`
- Create: `modules/workflow/workflow_service.py`
- Modify: `backend/api/workflows.py`
- Modify: `backend/main.py`

- [ ] **Step 1: 创建工作流数据模型**

```python
# backend/models/workflow.py
"""工作流模板数据模型"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, JSON, DateTime
from backend.models.base import Base


class WorkflowTemplate(Base):
    __tablename__ = "workflow_templates"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    description = Column(String, default="")
    stages = Column(JSON, default=list)  # [{"name":"import","config":{}},...]
    engine = Column(String, default="voxcpm")
    output_format = Column(String, default="m4b")
    settings = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "stages": self.stages,
            "engine": self.engine,
            "output_format": self.output_format,
            "settings": self.settings,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
```

- [ ] **Step 2: 创建工作流服务**

```python
# modules/workflow/workflow_service.py
"""工作流模板管理服务"""

import uuid
from datetime import datetime
from typing import Optional
from backend.models.base import get_session
from backend.models.workflow import WorkflowTemplate


class WorkflowTemplateService:
    """工作流模板 CRUD"""

    def create(self, name: str, description: str = "",
               stages: list = None, engine: str = "voxcpm",
               output_format: str = "m4b", settings: dict = None) -> dict:
        session = get_session()
        try:
            template = WorkflowTemplate(
                id=str(uuid.uuid4()),
                name=name,
                description=description,
                stages=stages or self._default_stages(),
                engine=engine,
                output_format=output_format,
                settings=settings or {},
            )
            session.add(template)
            session.commit()
            session.refresh(template)
            return template.to_dict()
        finally:
            session.close()

    def get(self, template_id: str) -> Optional[dict]:
        session = get_session()
        try:
            t = session.query(WorkflowTemplate).filter_by(id=template_id).first()
            return t.to_dict() if t else None
        finally:
            session.close()

    def list_all(self, limit: int = 50) -> list:
        session = get_session()
        try:
            templates = (
                session.query(WorkflowTemplate)
                .order_by(WorkflowTemplate.updated_at.desc())
                .limit(limit).all()
            )
            return [t.to_dict() for t in templates]
        finally:
            session.close()

    def update(self, template_id: str, **kwargs) -> bool:
        session = get_session()
        try:
            t = session.query(WorkflowTemplate).filter_by(id=template_id).first()
            if not t:
                return False
            for key, val in kwargs.items():
                if hasattr(t, key):
                    setattr(t, key, val)
            t.updated_at = datetime.utcnow()
            session.commit()
            return True
        finally:
            session.close()

    def delete(self, template_id: str) -> bool:
        session = get_session()
        try:
            t = session.query(WorkflowTemplate).filter_by(id=template_id).first()
            if not t:
                return False
            session.delete(t)
            session.commit()
            return True
        finally:
            session.close()

    @staticmethod
    def _default_stages() -> list:
        return [
            {"name": "import", "label": "导入", "config": {}},
            {"name": "parse", "label": "解析", "config": {}},
            {"name": "clean", "label": "清洗", "config": {}},
            {"name": "split", "label": "分句", "config": {}},
            {"name": "analyze", "label": "分析", "config": {}},
            {"name": "assign", "label": "分配", "config": {}},
            {"name": "synthesize", "label": "合成", "config": {}},
            {"name": "postprocess", "label": "后处理", "config": {}},
            {"name": "package", "label": "封装", "config": {}},
        ]


workflow_template_service = WorkflowTemplateService()
```

- [ ] **Step 3: 升级 workflows API 路由**

读取当前的 `backend/api/workflows.py`，添加模板 CRUD 端点：

```python
# 在现有路由基础上新增:

from backend.models.workflow import WorkflowTemplate
from modules.workflow.workflow_service import workflow_template_service

class CreateTemplateRequest(BaseModel):
    name: str
    description: str = ""
    stages: Optional[list] = None
    engine: str = "voxcpm"
    output_format: str = "m4b"
    settings: dict = {}

@router.get("/templates")
async def list_templates(limit: int = Query(50)):
    templates = workflow_template_service.list_all(limit)
    return {"status": "ok", "templates": templates, "total": len(templates)}

@router.post("/templates")
async def create_template(req: CreateTemplateRequest):
    template = workflow_template_service.create(
        name=req.name, description=req.description,
        stages=req.stages, engine=req.engine,
        output_format=req.output_format, settings=req.settings,
    )
    return {"status": "ok", "template": template}

@router.get("/templates/{template_id}")
async def get_template(template_id: str):
    t = workflow_template_service.get(template_id)
    if not t:
        raise HTTPException(404, "模板不存在")
    return {"status": "ok", "template": t}

@router.put("/templates/{template_id}")
async def update_template(template_id: str, req: CreateTemplateRequest):
    ok = workflow_template_service.update(template_id, **req.dict(exclude_none=True))
    if not ok:
        raise HTTPException(404, "模板不存在")
    return {"status": "ok"}

@router.delete("/templates/{template_id}")
async def delete_template(template_id: str):
    ok = workflow_template_service.delete(template_id)
    if not ok:
        raise HTTPException(404, "模板不存在")
    return {"status": "ok"}
```

- [ ] **Step 4: 在 main.py 中注册 WorkflowTemplate 表**

```python
# 在 backend/main.py 的 startup 事件中添加:
from backend.models.workflow import WorkflowTemplate
from backend.models.base import Base, engine
Base.metadata.create_all(bind=engine)  # 已有这行则确保 WorkflowTemplate 已导入
```

---

## 里程碑 3: 记忆模块 + 前端 (v2.2.0)

### Task 3.1: 新建记忆模块完整后端

**Files:**
- Create: `backend/models/memory.py`
- Create: `backend/api/memory.py`
- Create: `backend/services/memory_service.py`
- Create: `modules/memory/__init__.py`
- Create: `modules/memory/memory_store.py`
- Create: `modules/memory/transcript_formatter.py`
- Modify: `backend/main.py`

- [ ] **Step 1: 创建 Memory 数据模型**

```python
# backend/models/memory.py
"""记忆数据模型"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, JSON, DateTime
from backend.models.base import Base


class Memory(Base):
    __tablename__ = "memories"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, nullable=True, index=True)
    memory_type = Column(String, default="general")  # project_config|voice_profile|operation_log
    title = Column(String, nullable=False)
    content = Column(Text, default="")
    tags = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "memory_type": self.memory_type,
            "title": self.title,
            "content": self.content,
            "tags": self.tags,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
```

- [ ] **Step 2: 创建 Memory API**

```python
# backend/api/memory.py
"""记忆管理 API"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from backend.services.memory_service import memory_service

router = APIRouter(prefix="/api/memory", tags=["memory"])


class CreateMemoryRequest(BaseModel):
    project_id: Optional[str] = None
    memory_type: str = "general"
    title: str
    content: str = ""
    tags: list = []


@router.get("/")
async def list_memories(
    project_id: Optional[str] = None,
    memory_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    memories = memory_service.list_all(project_id, memory_type, limit)
    return {"status": "ok", "memories": memories, "total": len(memories)}


@router.post("/")
async def create_memory(req: CreateMemoryRequest):
    memory = memory_service.create(
        project_id=req.project_id, memory_type=req.memory_type,
        title=req.title, content=req.content, tags=req.tags,
    )
    return {"status": "ok", "memory": memory}


@router.get("/search")
async def search_memories(q: str = Query(..., min_length=1), limit: int = 20):
    results = memory_service.search(q, limit)
    return {"status": "ok", "results": results, "total": len(results)}


@router.get("/{memory_id}")
async def get_memory(memory_id: str):
    m = memory_service.get(memory_id)
    if not m:
        raise HTTPException(404, "记忆不存在")
    return {"status": "ok", "memory": m}


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str):
    if not memory_service.delete(memory_id):
        raise HTTPException(404, "记忆不存在")
    return {"status": "ok"}
```

- [ ] **Step 3: 创建 Memory Service**

```python
# backend/services/memory_service.py
"""记忆管理服务"""

import uuid
from datetime import datetime
from typing import Optional
from backend.models.base import get_session
from backend.models.memory import Memory


class MemoryService:
    def create(self, project_id: str = None, memory_type: str = "general",
               title: str = "", content: str = "", tags: list = None) -> dict:
        session = get_session()
        try:
            m = Memory(
                id=str(uuid.uuid4()),
                project_id=project_id,
                memory_type=memory_type,
                title=title,
                content=content,
                tags=tags or [],
            )
            session.add(m)
            session.commit()
            session.refresh(m)
            return m.to_dict()
        finally:
            session.close()

    def get(self, memory_id: str) -> Optional[dict]:
        session = get_session()
        try:
            m = session.query(Memory).filter_by(id=memory_id).first()
            return m.to_dict() if m else None
        finally:
            session.close()

    def list_all(self, project_id: str = None, memory_type: str = None,
                 limit: int = 50) -> list:
        session = get_session()
        try:
            q = session.query(Memory)
            if project_id:
                q = q.filter_by(project_id=project_id)
            if memory_type:
                q = q.filter_by(memory_type=memory_type)
            memories = q.order_by(Memory.updated_at.desc()).limit(limit).all()
            return [m.to_dict() for m in memories]
        finally:
            session.close()

    def search(self, query: str, limit: int = 20) -> list:
        session = get_session()
        try:
            q = session.query(Memory).filter(
                Memory.title.contains(query) | Memory.content.contains(query)
            ).order_by(Memory.updated_at.desc()).limit(limit)
            return [m.to_dict() for m in q.all()]
        finally:
            session.close()

    def delete(self, memory_id: str) -> bool:
        session = get_session()
        try:
            m = session.query(Memory).filter_by(id=memory_id).first()
            if not m:
                return False
            session.delete(m)
            session.commit()
            return True
        finally:
            session.close()


memory_service = MemoryService()
```

- [ ] **Step 4: 创建转录格式化器**

```python
# modules/memory/transcript_formatter.py
"""转录文本格式化器（参考 claude-supermemory transcript-formatter）"""

import re


def format_transcript(raw_text: str, speaker_labels: list = None) -> str:
    """
    将原始转录文本格式化为结构化对话

    Args:
        raw_text: 原始转录文本（可能是一大段文本）
        speaker_labels: 说话人标签列表，如 ["说话人A", "说话人B"]

    Returns:
        str: 格式化的对话文本
    """
    if not raw_text:
        return ""

    # 按句号/问号/感叹号分句
    sentences = re.split(r"(?<=[。！？.!?])", raw_text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if speaker_labels and len(speaker_labels) >= 2:
        formatted = []
        for i, sent in enumerate(sentences):
            speaker = speaker_labels[i % len(speaker_labels)]
            formatted.append(f"{speaker}: {sent}")
        return "\n".join(formatted)

    return "\n".join(sentences)


def extract_timestamps(text: str) -> list:
    """从转录文本中提取时间戳"""
    pattern = r"\[(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]"
    return re.findall(pattern, text)


def clean_transcript(text: str) -> str:
    """清理转录文本中的噪声标记"""
    # 移除时间戳
    text = re.sub(r"\[\d{2}:\d{2}:\d{2}(?:\.\d+)?\]", "", text)
    # 移除重复空格
    text = re.sub(r"\s+", " ", text)
    return text.strip()
```

- [ ] **Step 5: 注册路由到 main.py**

在 `backend/main.py` 中添加：

```python
from backend.api.memory import router as memory_router
app.include_router(memory_router)
```

---

### Task 3.2: 前端 — 设计令牌 + 动画系统

**Files:**
- Modify: `frontend/src/styles/global.css`
- Create: `frontend/src/styles/animations.css`
- Create: `frontend/src/components/animations/useAnime.ts`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: 升级 global.css 设计令牌**

在 `:root {}` 块末尾添加新的设计令牌：

```css
  /* === v2.0 升级：间距系统 (参考 Linear) === */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-6: 24px;
  --space-8: 32px;

  /* === v2.0 升级：字重系统 (参考 Stripe) === */
  --font-normal: 400;
  --font-medium: 500;
  --font-semibold: 600;

  /* === v2.0 升级：动画缓动 (参考 Vercel) === */
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
  --duration-fast: 150ms;
  --duration-normal: 300ms;
  --duration-slow: 500ms;

  /* === v2.0 升级：阴影层级 (参考 Apple) === */
  --shadow-xs: 0 1px 2px rgba(0, 0, 0, 0.04);
  --shadow-sm: 0 1px 3px rgba(0, 0, 0, 0.08);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.1);
  --shadow-lg: 0 8px 24px rgba(0, 0, 0, 0.12);
```

- [ ] **Step 2: 创建 animations.css**

```css
/* frontend/src/styles/animations.css */
/* 动画系统（参考 anime-master + react-bits） */

/* 页面进入 */
.page-enter {
  opacity: 0;
  transform: translateY(20px);
}

/* 交错列表项 */
.stagger-item {
  opacity: 0;
  transform: translateX(-10px);
}

/* 淡入 */
@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

/* 上滑 */
@keyframes slideUp {
  from { opacity: 0; transform: translateY(16px); }
  to { opacity: 1; transform: translateY(0); }
}

/* 缩放进入 */
@keyframes scaleIn {
  from { opacity: 0; transform: scale(0.95); }
  to { opacity: 1; transform: scale(1); }
}

/* 脉冲 */
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

/* 闪光（骨架屏） */
@keyframes shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}

/* 打字机光标 */
@keyframes blink {
  0%, 100% { border-color: transparent; }
  50% { border-color: var(--color-primary); }
}

/* 应用动画的工具类 */
.animate-fade-in {
  animation: fadeIn var(--duration-normal) var(--ease-out) forwards;
}

.animate-slide-up {
  animation: slideUp var(--duration-normal) var(--ease-out) forwards;
}

.animate-scale-in {
  animation: scaleIn var(--duration-normal) var(--ease-out) forwards;
}

.animate-pulse {
  animation: pulse 2s ease-in-out infinite;
}

.animate-shimmer {
  background: linear-gradient(90deg, var(--color-bg) 25%, var(--color-border) 50%, var(--color-bg) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
}

.typewriter {
  border-right: 2px solid var(--color-primary);
  animation: blink 0.7s step-end infinite;
  white-space: nowrap;
  overflow: hidden;
}

/* 按钮 hover 提升 */
.btn-lift {
  transition: transform var(--duration-fast) var(--ease-out),
              box-shadow var(--duration-fast) var(--ease-out);
}
.btn-lift:hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow-md);
}

/* 卡片 hover 提升 */
.card-lift {
  transition: transform var(--duration-fast) var(--ease-out),
              box-shadow var(--duration-fast) var(--ease-out);
}
.card-lift:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-lg);
}
```

- [ ] **Step 3: 创建 useAnime.ts 动画 Hook**

```typescript
// frontend/src/components/animations/useAnime.ts
import { useEffect, useRef } from 'react';

/**
 * 页面进入动画 Hook（参考 anime-master）
 * 直接使用 CSS 动画类名，无需 anime.js 依赖
 */
export function usePageEnter(selector: string = '.page-enter') {
  useEffect(() => {
    const elements = document.querySelectorAll(selector);
    elements.forEach((el, i) => {
      const htmlEl = el as HTMLElement;
      htmlEl.style.animation = `fadeIn 400ms ease-out ${i * 30}ms forwards`;
    });
  }, []);
}

/**
 * 交错列表动画 Hook
 */
export function useStaggerList(items: any[], selector: string = '.stagger-item') {
  useEffect(() => {
    const elements = document.querySelectorAll(selector);
    elements.forEach((el, i) => {
      const htmlEl = el as HTMLElement;
      htmlEl.style.animation = `slideUp 300ms ease-out ${i * 50}ms forwards`;
    });
  }, [items.length]);
}

/**
 * 滚动触发动画 Hook
 */
export function useScrollReveal(selector: string = '.reveal') {
  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            (entry.target as HTMLElement).classList.add('animate-fade-in');
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.1 }
    );

    document.querySelectorAll(selector).forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, []);
}
```

- [ ] **Step 4: 在 App.tsx 中导入动画样式**

在 `App.tsx` 顶部添加：
```tsx
import './styles/animations.css';
```

---

### Task 3.3: 前端 — 新增页面

**Files:**
- Create: `frontend/src/routes/MemoryPage.tsx`
- Create: `frontend/src/routes/WorkflowPage.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: 创建 MemoryPage**

```tsx
// frontend/src/routes/MemoryPage.tsx
import React, { useState, useEffect } from 'react';
import { api } from '../services/api';

interface Memory {
  id: string;
  title: string;
  content: string;
  memory_type: string;
  tags: string[];
  created_at: string;
}

export default function MemoryPage() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [search, setSearch] = useState('');
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => { loadMemories(); }, []);

  const loadMemories = async () => {
    try {
      const res = await api.get('/memory/');
      setMemories(res.data.memories || []);
    } catch (e) { console.error(e); }
    setLoading(false);
  };

  const handleSearch = async () => {
    if (!search.trim()) return loadMemories();
    try {
      const res = await api.get(`/memory/search?q=${encodeURIComponent(search)}`);
      setMemories(res.data.results || []);
    } catch (e) { console.error(e); }
  };

  const handleCreate = async () => {
    if (!title.trim()) return;
    try {
      await api.post('/memory/', { title, content, tags: [] });
      setTitle(''); setContent('');
      loadMemories();
    } catch (e) { console.error(e); }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.delete(`/memory/${id}`);
      loadMemories();
    } catch (e) { console.error(e); }
  };

  if (loading) return <div className="page"><p>加载中...</p></div>;

  return (
    <div className="page page-enter memory-page">
      <div className="page-header">
        <h1>项目记忆</h1>
        <p className="page-desc">管理配音项目的配置、音色设置和操作历史</p>
      </div>

      <div className="search-bar" style={{ marginBottom: '1rem' }}>
        <input
          type="text" value={search} onChange={e => setSearch(e.target.value)}
          placeholder="搜索记忆..." onKeyDown={e => e.key === 'Enter' && handleSearch()}
        />
        <button onClick={handleSearch}>搜索</button>
      </div>

      <div className="memory-create" style={{ marginBottom: '1.5rem', padding: '1rem', background: 'var(--color-bg-card)', border: '1px solid var(--color-border)', borderRadius: 'var(--radius-md)' }}>
        <input
          type="text" value={title} onChange={e => setTitle(e.target.value)}
          placeholder="记忆标题" style={{ width: '100%', marginBottom: '0.5rem' }}
        />
        <textarea
          value={content} onChange={e => setContent(e.target.value)}
          placeholder="记忆内容（可保存配音设置、文本片段等）"
          rows={3} style={{ width: '100%', marginBottom: '0.5rem' }}
        />
        <button className="btn-primary" onClick={handleCreate}>保存记忆</button>
      </div>

      <div className="memory-list stagger-item">
        {memories.map(m => (
          <div key={m.id} className="card-lift" style={{ padding: '1rem', margin: '0.5rem 0', background: 'var(--color-bg-card)', border: '1px solid var(--color-border)', borderRadius: 'var(--radius-md)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'start' }}>
              <div>
                <h4>{m.title}</h4>
                <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.9rem' }}>{m.content.substring(0, 200)}</p>
                <small style={{ color: 'var(--color-text-muted)' }}>
                  {m.memory_type} · {new Date(m.created_at).toLocaleDateString('zh-CN')}
                </small>
              </div>
              <button onClick={() => handleDelete(m.id)}
                style={{ background: 'none', border: 'none', color: 'var(--color-error)', cursor: 'pointer', padding: '0.25rem 0.5rem' }}>
                删除
              </button>
            </div>
          </div>
        ))}
        {memories.length === 0 && <p className="empty-state">暂无记忆，创建一条吧</p>}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 创建 WorkflowPage**

```tsx
// frontend/src/routes/WorkflowPage.tsx
import React, { useState, useEffect } from 'react';
import { api } from '../services/api';

const STAGE_LABELS: Record<string, string> = {
  import: '导入', parse: '解析', clean: '清洗', split: '分句',
  analyze: '分析', assign: '分配', synthesize: '合成',
  postprocess: '后处理', package: '封装',
};

interface Template {
  id: string; name: string; description: string;
  stages: { name: string; label: string }[];
  engine: string; output_format: string;
}

export default function WorkflowPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => { loadTemplates(); }, []);

  const loadTemplates = async () => {
    try {
      const res = await api.get('/workflows/templates');
      setTemplates(res.data.templates || []);
    } catch (e) { console.error(e); }
    setLoading(false);
  };

  const handleDelete = async (id: string) => {
    try {
      await api.delete(`/workflows/templates/${id}`);
      loadTemplates();
    } catch (e) { console.error(e); }
  };

  if (loading) return <div className="page"><p>加载中...</p></div>;

  return (
    <div className="page page-enter workflow-page">
      <div className="page-header">
        <h1>工作流编排</h1>
        <p className="page-desc">可视化管理和配置电子书→有声书转换工作流</p>
      </div>

      <div className="workflow-list stagger-item">
        {templates.map(t => (
          <div key={t.id} className="card-lift" style={{
            padding: '1rem', margin: '0.75rem 0',
            background: 'var(--color-bg-card)', border: '1px solid var(--color-border)',
            borderRadius: 'var(--radius-md)',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div>
                <h4>{t.name}</h4>
                <p style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}>
                  引擎: {t.engine} · 输出: {t.output_format}
                </p>
              </div>
              <button onClick={() => handleDelete(t.id)}
                style={{ background: 'none', border: 'none', color: 'var(--color-error)', cursor: 'pointer' }}>
                删除
              </button>
            </div>
            <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.75rem', flexWrap: 'wrap' }}>
              {(t.stages || []).map((s, i) => (
                <span key={i} style={{
                  padding: '0.2rem 0.6rem', fontSize: '0.75rem',
                  background: 'var(--color-primary-light)', color: 'var(--color-primary)',
                  borderRadius: '999px',
                }}>
                  {s.label || STAGE_LABELS[s.name] || s.name}
                </span>
              ))}
            </div>
          </div>
        ))}
        {templates.length === 0 && (
          <p style={{ textAlign: 'center', color: 'var(--color-text-muted)', padding: '2rem' }}>
            暂无工作流模板。通过 API 创建后在此查看。
          </p>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: 在 App.tsx 添加路由**

在 `App.tsx` 中添加导入和路由：
```tsx
import MemoryPage from './routes/MemoryPage';
import WorkflowPage from './routes/WorkflowPage';

// 在路由配置中添加两个新路由
```

---

## 里程碑 4: 测试 + 文档 (v2.3.0)

### Task 4.1: 新增测试文件

**Files:**
- Create: `tests/test_m4b_builder.py`
- Create: `tests/test_memory.py`
- Create: `tests/test_html_parser.py`

- [ ] **Step 1: M4B Builder 测试**

```python
# tests/test_m4b_builder.py
import pytest
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def test_m4b_builder_import():
    from modules.tts_engine.m4b_builder import M4BBuilder
    b = M4BBuilder()
    assert isinstance(b.available, bool)

def test_m4b_builder_metadata_writing():
    from modules.tts_engine.m4b_builder import M4BBuilder
    import tempfile
    b = M4BBuilder()
    segments = [
        {"chapter_title": "第一章", "duration_ms": 5000, "audio_data": b"\x00" * 100},
        {"chapter_title": "第二章", "duration_ms": 3000, "audio_data": b"\x00" * 100},
    ]
    metadata = {"title": "测试书", "author": "测试作者"}
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8") as f:
        b._write_ffmetadata(segments, metadata, f.name)
        content = open(f.name).read()
    os.unlink(f.name)
    assert "title=测试书" in content
    assert "artist=测试作者" in content
    assert "第一章" in content
```

- [ ] **Step 2: Memory 测试**

```python
# tests/test_memory.py
import pytest
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def test_memory_model_import():
    from backend.models.memory import Memory
    assert Memory.__tablename__ == "memories"

def test_memory_service_import():
    from backend.services.memory_service import memory_service
    assert memory_service is not None

def test_transcript_formatter():
    from modules.memory.transcript_formatter import format_transcript, clean_transcript
    raw = "说话人A: 你好。说话人B: 你好啊！"
    formatted = format_transcript(raw, ["张三", "李四"])
    assert "张三" in formatted
    assert "李四" in formatted

    noisy = "[00:01:23] 测试  文本  "
    cleaned = clean_transcript(noisy)
    assert "[00:01:23]" not in cleaned
    assert "  " not in cleaned
```

- [ ] **Step 3: HTML Parser 测试**

```python
# tests/test_html_parser.py
import pytest
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def test_html_parser_basic():
    from modules.book_parser.parsers.html_parser import HtmlParser
    with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as f:
        f.write("<html><head><title>测试</title></head><body><h1>第一章</h1><p>内容一</p><h2>第二章</h2><p>内容二</p></body></html>")
        path = f.name
    try:
        parser = HtmlParser(path)
        data = parser.parse()
        assert data.format == "html"
        assert data.title == "测试"
        assert len(data.chapters) >= 1
        assert data.total_chars > 0
    finally:
        os.unlink(path)

def test_html_parser_in_parser_factory():
    from modules.book_parser.parser_factory import ParserFactory
    parser = ParserFactory.get_parser("test.html")
    assert parser is not None
```

- [ ] **Step 4: 运行全部测试**

```bash
cd d:\程序开发\配音员\peiyinyuan && python -m pytest tests/ -v
```

---

## 总结

| 里程碑 | 新增文件 | 修改文件 | 测试数 |
|--------|---------|---------|--------|
| v2.0.0 | 4 | 6 | +1 |
| v2.1.0 | 2 | 4 | 0 |
| v2.2.0 | 8 | 4 | 0 |
| v2.3.0 | 3 | 0 | +3 |
| **合计** | **17** | **14** | **127** |