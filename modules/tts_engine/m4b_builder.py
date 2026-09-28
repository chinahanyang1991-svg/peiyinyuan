"""
M4B 有声书构建器
参考 ebook2audiobook + audiblez 的 M4B 封装逻辑
使用 ffmpeg 将 WAV 音频段合并为 M4B 格式有声书（含章节标记）

修复 (round-1b):
    - PY-302: 不再裸调 PATH 里的 `ffmpeg`，统一走 `ffmpeg_utils.resolve_ffmpeg()`
      (Config.FFMPEG_PATH -> PATH -> imageio-ffmpeg 自带可执行文件)
    - PY-125: 段字典契约收敛为 audio_data(WAV bytes) / chapter_title / duration_ms，
      缺键时明确告警，而不是静默产出无章节文件
    - PY-126: 章节时间轴与 concat 列表同源（按 WAV 真实时长 + 段间 gap 累加），
      不再出现"所有章节起点都是 0"；结果记录在 `last_chapters` 供回归校验
    - PY-127: ffmpeg 缺失时抛 FFmpegUnavailableError（带修复建议），不再是裸报错
    - WF-18: 封装显式写 `-brand "M4B "`（仅靠 .m4b 扩展名只会落到 major_brand=M4A），
      构建后用 read_ftyp() 自检品牌并记入 last_major_brand
"""

import io
import logging
import os
import struct
import tempfile
import shutil
import wave
from pathlib import Path

from modules.tts_engine import ffmpeg_utils

logger = logging.getLogger(__name__)


def _wav_duration_ms(data: bytes, fallback_sr: int = 48000) -> float:
    """从 WAV 字节量出真实时长（毫秒）；解析失败返回 0.0，由调用方兜底。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            fr = wf.getframerate() or fallback_sr
            return wf.getnframes() / float(fr) * 1000.0
    except Exception:
        return 0.0


def _make_silent_wav(frames: int, sample_rate: int, channels: int = 1) -> bytes:
    """生成 frames 个采样点的 16-bit 单声道静音 WAV（章节之间的间隔）。"""
    frames = max(0, int(frames))
    data = b"\x00" * (frames * channels * 2)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate, sample_rate * channels * 2,
        channels * 2, 16,
        b"data", len(data),
    )
    return header + data


def _build_chapters(entries: list, gap_ms: int = 0) -> list:
    """由 (wav_path, duration_ms, title) 列表累计出章节时间轴。

    gap 与 concat 列表保持一致：加在每两段之间（最后一段之后无 gap）。

    title 为空的条目只是"章节内的下一句"，不开新章节 (PY-126)：
    生产的 audio_arrays 是句子粒度，每个句子都带 chapter_title，若逐段开章节
    会把一章切成几百个"章节标记"。因此 package 只在每个章节的首句上打标题。
    """
    spans = []
    cursor = 0.0
    for i, (_path, duration_ms, title) in enumerate(entries):
        start = cursor
        end = start + max(0.0, float(duration_ms))
        spans.append((start, end, str(title or "").strip()))
        cursor = end + (float(gap_ms) if i < len(entries) - 1 else 0.0)
    total = cursor

    marks = [i for i, s in enumerate(spans) if s[2]]
    chapters = []
    for n, i in enumerate(marks):
        start = spans[i][0]
        end = spans[marks[n + 1]][0] if n + 1 < len(marks) else total
        chapters.append({"title": spans[i][2], "start_ms": start, "end_ms": end,
                         "duration_ms": end - start, "segment_index": i})
    return chapters


def _escape_meta(value: str) -> str:
    """ffmetadata 值转义（允许中文标题，需转义 = ; # \\ 与换行）。"""
    out = str(value or "")
    for ch in ("\\", "=", ";", "#", "\n", "\r"):
        out = out.replace(ch, "\\" + ch)
    return out


# WF-18: 容器品牌。输出文件名是 .m4b 时 ffmpeg 已自动选用 ipod muxer，
# 但音频-only 的默认 major_brand 仍是 "M4A "（实测 ffmpeg 7.1：`-f ipod` 单加
# 也不改变品牌），必须显式 `-brand "M4B "` 才会写成 ftyp/M4B + compatible
# brands "M4B isomiso2"。严格识别品牌的有声书播放器（部分车机/有声书屋）
# 会把 M4A 当纯音乐处理。
BRAND_ARGS = ["-brand", "M4B "]


def read_ftyp(path: str) -> dict:
    """读容器头 ftyp box，返回 {major_brand, compatible_brands, error}。

    纯 Python 解析，不再 spawn ffmpeg —— 用于封装后自检品牌 (WF-18)。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError as e:
        return {"major_brand": "", "compatible_brands": [], "error": str(e)}

    i = head.find(b"ftyp")
    if i < 4:
        return {"major_brand": "", "compatible_brands": [], "error": "未找到 ftyp box"}
    try:
        size = int.from_bytes(head[i - 4:i], "big")
    except Exception as e:  # pragma: no cover - 防御性
        return {"major_brand": "", "compatible_brands": [], "error": str(e)}
    if size < 8 or size > len(head) - (i - 4):
        size = min(len(head) - (i - 4), 32)
    body = head[i - 4:i - 4 + size]
    major = body[8:12].decode("latin-1").strip()
    compat = [body[n:n + 4].decode("latin-1").strip()
              for n in range(16, len(body) - 3, 4)
              if body[n:n + 4].isascii() and body[n:n + 4].strip()]
    return {"major_brand": major, "compatible_brands": compat, "error": None}


class M4BBuilder:
    """M4B 有声书构建器"""

    def __init__(self):
        # 构建成功后记录章节时间轴 [{"title","start_ms","end_ms","duration_ms"}] (PY-126)
        self.last_chapters = []
        # 构建成功后记录容器品牌 (WF-18)，期望 "M4B"
        self.last_major_brand = ""
        self.last_compatible_brands = []
        self._check_ffmpeg()

    def _check_ffmpeg(self):
        """检测 ffmpeg 是否可用（只探测，不抛错）"""
        if not ffmpeg_utils.is_available():
            print(f"[M4B] 警告: {ffmpeg_utils.unavailable_reason()}")

    @property
    def available(self) -> bool:
        """ffmpeg 是否可用（统一探测，含 imageio-ffmpeg 兜底）。"""
        return ffmpeg_utils.is_available()

    @property
    def ffmpeg_path(self):
        """解析出的 ffmpeg 可执行文件绝对路径（可能为 None）。"""
        return ffmpeg_utils.resolve_ffmpeg()

    def build(
        self,
        audio_segments: list,
        metadata: dict,
        output_path: str,
        sample_rate: int = 48000,
        gap_ms: int = 0,
    ) -> str:
        """
        将音频段合并为 M4B 有声书

        Args:
            audio_segments: [{"audio_data": wav bytes, "chapter_title": "第x章",
                              "duration_ms": 12000}, ...]
                `audio_data` 为 WAV 字节（必需）；`chapter_title` 用作章节名；
                `duration_ms` 仅在 WAV 头解析失败时兜底，真实时长以 WAV 为准。
            metadata: {"title": "书名", "author": "作者", "year": "2024"}
            output_path: 输出文件路径 (.m4b)
            sample_rate: 采样率
            gap_ms: 章节之间插入的静音长度，与 wav/mp3 成品的段间隔对齐

        Returns:
            str: 输出文件路径

        Raises:
            FFmpegUnavailableError: 环境无可用 ffmpeg (PY-302/PY-127)
            ValueError: audio_segments 为空 / 无有效音频数据
            RuntimeError: ffmpeg 编码失败（消息含 stderr 片段）
        """
        if not self.available:
            raise ffmpeg_utils.FFmpegUnavailableError("m4b")

        if not audio_segments:
            raise ValueError("audio_segments 为空")

        self.last_chapters = []
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="m4b_build_") as tmpdir:
            tmpdir = Path(tmpdir)

            # 1. 每段写成 WAV，同时量出真实时长 —— 章节时间轴的唯一来源
            raw_titles = [str(s.get("chapter_title") or "").strip() for s in audio_segments]
            any_title = any(raw_titles)
            entries = []
            for i, seg in enumerate(audio_segments):
                data = seg.get("audio_data") or b""
                if not data:
                    logger.warning(f"[M4B] 段 {i} 缺少 audio_data，跳过且不计入章节时间轴 "
                                   f"(PY-125 契约: 键名必须是 audio_data)")
                    continue
                wav_path = tmpdir / f"seg_{i:04d}.wav"
                wav_path.write_bytes(data)
                dur = _wav_duration_ms(data, sample_rate) or float(seg.get("duration_ms") or 0.0)
                title = raw_titles[i] if any_title else f"Chapter {i + 1}"
                entries.append((str(wav_path), dur, title))

            if not entries:
                raise ValueError("无有效音频数据")

            # 2. concat 列表（章节之间按需插静音）
            gap_wav = None
            if gap_ms and gap_ms > 0:
                gap_wav = tmpdir / "gap.wav"
                gap_wav.write_bytes(_make_silent_wav(int(gap_ms * sample_rate / 1000),
                                                     sample_rate))

            concat_list = tmpdir / "concat.txt"
            with open(concat_list, "w", encoding="utf-8") as f:
                for n, (wf, _dur, _title) in enumerate(entries):
                    f.write(f"file '{wf}'\n")
                    if gap_wav and n < len(entries) - 1:
                        f.write(f"file '{gap_wav}'\n")

            # 3. ffmetadata 章节标记（与 concat 列表同源）
            chapters = _build_chapters(entries, gap_ms)
            self.last_chapters = chapters
            metadata_path = tmpdir / "ffmetadata.txt"
            self._write_ffmetadata(chapters, metadata, str(metadata_path))

            # 4. ffmpeg 合并 + 封装 M4B（章节随 map_chapters 写入）
            temp_output = tmpdir / "output.m4b"
            args = [
                "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-i", str(metadata_path),
                "-map_metadata", "1",
                "-map_chapters", "1",
                "-c:a", "aac", "-b:a", "128k",
                "-ar", str(sample_rate),
                "-movflags", "+faststart",
            ]
            # WF-18: 显式写 major_brand=M4B；旧 ffmpeg 不认 -brand 时去掉它再跑一次，
            # 品牌退化可以接受，产物没有则不可接受。
            try:
                ffmpeg_utils.run_ffmpeg(args + BRAND_ARGS + [str(temp_output)],
                                        desc="M4B 封装", want_format="m4b")
            except ffmpeg_utils.FFmpegUnavailableError:
                raise
            except RuntimeError as e:
                logger.warning(f"[M4B] 带 -brand 的封装失败，退回不带品牌的参数重试: {e}")
                ffmpeg_utils.run_ffmpeg(args + [str(temp_output)],
                                        desc="M4B 封装(无 -brand)", want_format="m4b")

            # 5. 移动到目标路径（Windows 下 shutil.move 不覆盖已存在文件）
            if os.path.exists(output_path):
                os.remove(output_path)
            shutil.move(str(temp_output), output_path)

        # 6. 封装自检：容器品牌 (WF-18)
        ftyp = read_ftyp(output_path)
        self.last_major_brand = ftyp.get("major_brand", "")
        self.last_compatible_brands = ftyp.get("compatible_brands", [])
        if self.last_major_brand != "M4B":
            logger.warning(f"[M4B] {output_path} 的 major_brand={self.last_major_brand!r} "
                           f"而非 'M4B'（compatible_brands={self.last_compatible_brands}），"
                           f"严格识别品牌的播放器可能按纯音乐处理")

        tail = chapters[-1]["end_ms"] / 1000.0 if chapters else 0.0
        logger.info(f"[M4B] 已写出 {output_path}: 章节 {len(chapters)} 个, "
                    f"时间轴总长 {tail:.1f}s")
        return output_path

    def build_chapters(self, audio_segments: list, sample_rate: int = 48000,
                       gap_ms: int = 0) -> list:
        """只计算章节时间轴（不跑 ffmpeg），供打包阶段回传/回归校验使用。"""
        raw_titles = [str(s.get("chapter_title") or "").strip() for s in (audio_segments or [])]
        any_title = any(raw_titles)
        entries = []
        for i, seg in enumerate(audio_segments or []):
            data = seg.get("audio_data") or b""
            if not data:
                continue
            dur = _wav_duration_ms(data, sample_rate) or float(seg.get("duration_ms") or 0.0)
            title = raw_titles[i] if any_title else f"Chapter {i + 1}"
            entries.append((None, dur, title))
        return _build_chapters(entries, gap_ms)

    def _write_ffmetadata(self, chapters: list, metadata: dict, output_path: str):
        """写入 ffmpeg 章节元数据。chapters 已含 start_ms/end_ms，此处不再累加。"""
        lines = [";FFMETADATA1"]

        title = metadata.get("title", "有声书")
        author = metadata.get("author", "")
        year = metadata.get("year", "")

        if title:
            lines.append(f"title={_escape_meta(title)}")
        if author:
            lines.append(f"artist={_escape_meta(author)}")
        if year:
            lines.append(f"date={_escape_meta(year)}")

        for ch in chapters:
            start = int(round(ch["start_ms"]))
            end = int(round(ch["end_ms"]))
            lines.append("[CHAPTER]")
            lines.append("TIMEBASE=1/1000")
            lines.append(f"START={start}")
            lines.append(f"END={max(start + 1, end)}")
            lines.append(f"title={_escape_meta(ch.get('title') or '')}")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
