from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from shutil import which

ROOT = Path(__file__).resolve().parents[1]

# Prefer this conda env when PYTHON is unset.
DEFAULT_CONDA_PYTHON = Path.home() / "miniconda3/envs/python3/bin/python"


def _load_dotenv_file(path: Path, *, override: bool = False) -> bool:
    """Minimal .env loader (works without python-dotenv)."""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"\"", "'"}:
            val = val[1:-1]
        if not override and key in os.environ:
            continue
        os.environ[key] = val
    return True


def load_env(project_root: Path | None = None) -> list[Path]:
    root = project_root or ROOT
    candidates = [
        root / ".env",
        Path.cwd() / ".env",
        Path.home() / ".config" / "youtube-zh-dub" / ".env",
    ]
    # also try python-dotenv if present (same semantics)
    try:
        from dotenv import load_dotenv as _dotenv_load
    except ImportError:
        _dotenv_load = None

    loaded: list[Path] = []
    seen: set[Path] = set()
    for p in candidates:
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp in seen or not rp.is_file():
            continue
        seen.add(rp)
        if _dotenv_load is not None:
            _dotenv_load(rp, override=False)
        else:
            _load_dotenv_file(rp, override=False)
        # always also run builtin to be safe if dotenv missing keys edge cases
        _load_dotenv_file(rp, override=False)
        loaded.append(rp)
    return loaded


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value.strip()))).resolve()


def _is_exec(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_tool(env_key: str, cmd: str, extra: list[Path] | None = None) -> str:
    raw = (os.getenv(env_key) or "").strip()
    if raw:
        p = _expand(raw)
        if not _is_exec(p):
            raise SystemExit(
                f"{env_key} is set but not executable: {p}\n"
                f"Fix .env or unset {env_key} for auto-detect."
            )
        return str(p)

    hit = which(cmd)
    if hit:
        return hit

    fallbacks = list(extra or [])
    fallbacks += [
        Path.home() / "miniconda3/envs/python3/bin" / cmd,
        Path.home() / "miniconda3/bin" / cmd,
        Path.home() / "anaconda3/envs/python3/bin" / cmd,
        Path.home() / "anaconda3/bin" / cmd,
        Path("/opt/homebrew/opt/ffmpeg-full/bin") / cmd,
        Path("/opt/homebrew/bin") / cmd,
        Path("/usr/local/bin") / cmd,
    ]
    for p in fallbacks:
        if _is_exec(p):
            return str(p)
    raise SystemExit(
        f"missing dependency: {cmd}\n"
        f"Install it, or set {env_key}=/absolute/path in .env"
    )


def resolve_python() -> str:
    """Prefer .env PYTHON, then conda env python3, then PATH."""
    raw = (os.getenv("PYTHON") or "").strip()
    if raw:
        p = _expand(raw)
        if not _is_exec(p):
            raise SystemExit(f"PYTHON is set but not executable: {p}")
        return str(p)
    if _is_exec(DEFAULT_CONDA_PYTHON):
        return str(DEFAULT_CONDA_PYTHON)
    hit = which("python3") or which("python")
    if hit:
        return hit
    raise SystemExit("missing python3 (expected conda env: miniconda3/envs/python3)")


@dataclass
class Settings:
    root: Path
    api_key: str
    model: str
    proxy: str
    voice: str
    quality: str
    workdir: Path
    python: str
    yt_dlp: str
    ffmpeg: str
    ffprobe: str
    edge_tts: str
    modelscope_base_url: str
    translate_batch_size: int
    translate_max_retries: int
    translate_concurrency: int
    translate_refill_max_rounds: int
    tts_concurrency: int
    tts_max_rate: int
    cover_image: Path | None
    env_files: list[Path]

    @classmethod
    def load(cls, project_root: Path | None = None) -> "Settings":
        root = (project_root or ROOT).resolve()
        env_files = load_env(root)
        workdir_raw = (os.getenv("WORKDIR") or str(root / "work")).strip()
        workdir = Path(os.path.expanduser(workdir_raw))
        if not workdir.is_absolute():
            workdir = (root / workdir).resolve()

        quality = (os.getenv("QUALITY") or "720").strip().lower()
        if quality in {"720p"}:
            quality = "720"
        elif quality in {"1080p"}:
            quality = "1080"
        elif quality in {"max", "highest", "source"}:
            quality = "best"
        if quality not in {"720", "1080", "best"}:
            raise SystemExit("QUALITY must be 720, 1080, or best")

        api_key = (os.getenv("API_KEY") or "").strip()
        model = (os.getenv("MODEL") or "").strip()
        if not api_key or not model:
            raise SystemExit(f"缺少 API_KEY / MODEL，请写在 {root / '.env'}")

        cover_raw = (os.getenv("COVER_IMAGE") or "").strip()
        cover_image: Path | None = None
        if cover_raw:
            cover_path = Path(os.path.expanduser(cover_raw))
            if not cover_path.is_absolute():
                cover_path = (root / cover_path).resolve()
            else:
                cover_path = cover_path.resolve()
            cover_image = cover_path

        return cls(
            root=root,
            api_key=api_key,
            model=model,
            proxy=(os.getenv("PROXY") or "http://127.0.0.1:7890").strip(),
            voice=(os.getenv("VOICE") or "zh-CN-YunyangNeural").strip(),
            quality=quality,
            workdir=workdir,
            python=resolve_python(),
            yt_dlp=resolve_tool("YT_DLP", "yt-dlp"),
            ffmpeg=resolve_tool(
                "FFMPEG",
                "ffmpeg",
                [Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")],
            ),
            ffprobe=resolve_tool(
                "FFPROBE",
                "ffprobe",
                [Path("/opt/homebrew/opt/ffmpeg-full/bin/ffprobe")],
            ),
            edge_tts=resolve_tool("EDGE_TTS", "edge-tts"),
            modelscope_base_url=(
                os.getenv("MODELSCOPE_BASE_URL")
                or "https://api-inference.modelscope.cn/v1"
            ).strip(),
            translate_batch_size=int(os.getenv("TRANSLATE_BATCH_SIZE") or "100"),
            translate_max_retries=int(os.getenv("TRANSLATE_MAX_RETRIES") or "5"),
            translate_concurrency=max(
                1, int(os.getenv("TRANSLATE_CONCURRENCY") or "2")
            ),
            translate_refill_max_rounds=max(
                0, int(os.getenv("TRANSLATE_REFILL_MAX_ROUNDS") or "2")
            ),
            tts_concurrency=max(1, int(os.getenv("TTS_CONCURRENCY") or "2")),
            tts_max_rate=max(0, int(os.getenv("TTS_MAX_RATE") or "30")),
            cover_image=cover_image,
            env_files=env_files,
        )
