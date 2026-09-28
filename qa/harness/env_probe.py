"""
[env] 环境探测: Python / 依赖 / ffmpeg / GPU / 后端可启动性 / 引擎可加载性
运行: python qa/harness/env_probe.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

MODS = [
    ("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("sqlalchemy", "sqlalchemy"),
    ("soundfile", "soundfile"), ("numpy", "numpy"), ("torch", "torch"),
    ("torchaudio", "torchaudio"), ("transformers", "transformers"),
    ("ebooklib", "ebooklib"), ("fitz/PyMuPDF", "fitz"), ("mobi", "mobi"),
    ("bs4", "bs4"), ("jieba", "jieba"), ("pypinyin", "pypinyin"),
    ("cn2an", "cn2an"), ("librosa", "librosa"), ("websockets", "websockets"),
    ("pydub", "pydub"), ("chardet", "chardet"), ("PIL", "PIL"),
]


def main():
    with H.Tee(H.LOGS / "env_probe.log"):
        H.log("=== 环境探测 ===")
        H.kv("platform", sys.platform)
        H.kv("python", sys.version.replace("\n", " "))
        H.kv("executable", sys.executable)
        H.kv("cwd", str(Path.cwd()))
        H.kv("PYTHONIOENCODING", __import__("os").environ.get("PYTHONIOENCODING"))

        H.log("\n--- 依赖 ---")
        missing = []
        for label, mod in MODS:
            try:
                m = __import__(mod)
                v = getattr(m, "__version__", "?")
                H.kv(label, f"OK {v}")
            except Exception as e:
                H.kv(label, f"MISSING ({type(e).__name__}: {e})")
                missing.append(label)

        H.log("\n--- 外部二进制 ---")
        for exe in ("ffmpeg", "ffprobe", "calibre", "ebook-convert"):
            p = H.which(exe)
            H.kv(exe, p or "NOT FOUND in PATH")

        H.log("\n--- GPU ---")
        try:
            import torch
            H.kv("torch", torch.__version__)
            H.kv("cuda.available", torch.cuda.is_available())
            H.kv("cpu_threads", torch.get_num_threads())
        except Exception as e:
            H.kv("torch", f"ERR {e}")

        H.log("\n--- 模型与路径 ---")
        sys.path.insert(0, str(H.PROJECT_ROOT))
        from backend.config import Config
        H.kv("Config.TTS_ENGINE", Config.TTS_ENGINE)
        H.kv("Config.VOXCPM_MODEL_PATH", Config.VOXCPM_MODEL_PATH)
        H.kv("  exists", Path(Config.VOXCPM_MODEL_PATH).exists())
        real_dir = H.PROJECT_ROOT / "data/models/voxcpm2"
        H.kv("adapter default model dir", str(real_dir))
        H.kv("  exists", real_dir.exists())
        for f in ("config.json", "audiovae.pth", "model.safetensors"):
            fp = real_dir / f
            H.kv(f"  {f}", fp.stat().st_size if fp.exists() else "MISSING")
        H.kv("Config.validate()", Config.validate())
        H.kv("OUTPUT_DIR", f"{H.OUTPUT_DIR} exists={H.OUTPUT_DIR.exists()}")
        H.kv("DB_PATH", f"{H.DB_PATH} exists={H.DB_PATH.exists()} "
                        f"size={H.DB_PATH.stat().st_size if H.DB_PATH.exists() else 0}")

        H.log("\n--- 后端可启动性 (import 层) ---")
        t = time.time()
        try:
            import backend.main  # noqa: F401
            H.kv("import backend.main", f"OK ({time.time()-t:.1f}s)")
        except Exception as e:
            H.kv("import backend.main", f"FAIL {type(e).__name__}: {e}")

        H.log("\n--- DB 现状 (只读) ---")
        try:
            for t_ in ("books", "chapters", "conversion_tasks", "voices", "projects"):
                rows = H.db_rows(f"SELECT COUNT(*) c FROM {t_}")
                H.kv(t_, rows[0]["c"])
        except Exception as e:
            H.kv("db", f"ERR {e}")

        H.log("\n--- 真实 TTS 引擎加载 ---")
        t = time.time()
        try:
            from modules.tts_engine.engine_registry import engine_registry
            eng = engine_registry.get_loaded("voxcpm")
            H.kv("engine", type(eng).__name__ if eng else "None")
            H.kv("load_seconds", round(time.time() - t, 1))
            if eng is None:
                H.log("  [结论] 引擎不可用 -> 全部合成走静音兜底 (P0)")
            elif eng._model is None:
                H.log("  [结论] DEGRADED: _model is None -> _mock_synthesize 静音兜底")
            else:
                H.kv("_model", type(eng._model).__name__)
                inner = getattr(eng._model, "tts_model", None)
                H.kv("tts_model.sample_rate", getattr(inner, "sample_rate", "?"))
                H.kv("adapter.sample_rate", eng.sample_rate)
                H.kv("device", eng.device)
                H.log("  [结论] 真实模型已加载, 可进入 P7 实测")
        except Exception as e:
            H.kv("engine load", f"FAIL {type(e).__name__}: {e}")

        H.log("\n=== env probe done ===")


if __name__ == "__main__":
    main()
