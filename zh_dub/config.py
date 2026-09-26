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
    """Minimal .env loader: runs before re-exec, when the interpreter may lack
    third-party packages such as python-dotenv."""
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
        _load_dotenv_file(rp, override=False)
        loaded.append(rp)
    return loaded


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value.strip()))).resolve()


def _project_path(raw: str, root: Path) -> Path:
    p = Path(os.path.expanduser(raw.strip()))
    return p.resolve() if p.is_absolute() else (root / p).resolve()


def normalize_quality(raw: str) -> str:
    q = (raw or "").strip().lower()
    if q in {"720", "720p"}:
        return "720"
    if q in {"1080", "1080p"}:
        return "1080"
    if q in {"best", "max", "highest", "source"}:
        return "best"
    raise SystemExit("quality must be 720, 1080, or best")


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
    api_keys: list[str]
    model: str
    proxy: str
    voice: str
    quality: str
    workdir: Path
    python: str
    yt_dlp: str
    ffmpeg: str
    ffprobe: str
    modelscope_base_url: str
    parakeet_model: str
    parakeet_silence_gap: float
    parakeet_chunk_duration: float
    parakeet_overlap_duration: float
    parakeet_decoding: str
    parakeet_beam_size: int
    translate_batch_size: int
    translate_max_retries: int
    translate_concurrency: int
    translate_refill_max_rounds: int
    tts_concurrency: int
    tts_max_rate: int
    tts_timeout: float
    cover_image: Path | None
    output_dir: Path | None
    log_dir: Path
    env_files: list[Path]

    @classmethod
    def load(cls, project_root: Path | None = None) -> "Settings":
        root = (project_root or ROOT).resolve()
        env_files = load_env(root)
        workdir = _project_path(os.getenv("WORKDIR") or "work", root)
        quality = normalize_quality(os.getenv("QUALITY") or "720")

        parakeet_decoding = (os.getenv("PARAKEET_DECODING") or "greedy").strip().lower()
        if parakeet_decoding not in {"greedy", "beam"}:
            raise SystemExit("PARAKEET_DECODING must be greedy or beam")

        api_keys = [
            k.strip()
            for k in re.split(r"[,\s]+", os.getenv("API_KEY") or "")
            if k.strip()
        ]
        seen_keys: set[str] = set()
        api_keys = [k for k in api_keys if not (k in seen_keys or seen_keys.add(k))]
        model = (os.getenv("MODEL") or "").strip()
        if not api_keys or not model:
            raise SystemExit(f"缺少 API_KEY / MODEL，请写在 {root / '.env'}")

        cover_raw = (os.getenv("COVER_IMAGE") or "").strip()
        cover_image = _project_path(cover_raw, root) if cover_raw else None
        output_raw = (os.getenv("OUTPUT_DIR") or "").strip()
        output_dir = _project_path(output_raw, root) if output_raw else None
        log_dir = _project_path(os.getenv("LOG_DIR") or "logs", root)

        return cls(
            root=root,
            api_keys=api_keys,
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
            tts_timeout=max(5.0, float(os.getenv("TTS_TIMEOUT") or "45")),
            parakeet_model=(
                os.getenv("PARAKEET_MODEL") or "mlx-community/parakeet-tdt-0.6b-v3"
            ).strip(),
            parakeet_silence_gap=max(
                0.0, float(os.getenv("PARAKEET_SILENCE_GAP") or "2.0")
            ),
            parakeet_chunk_duration=max(
                0.0, float(os.getenv("PARAKEET_CHUNK_DURATION") or "120")
            ),
            parakeet_overlap_duration=max(
                0.0, float(os.getenv("PARAKEET_OVERLAP_DURATION") or "15")
            ),
            parakeet_decoding=parakeet_decoding,
            parakeet_beam_size=max(1, int(os.getenv("PARAKEET_BEAM_SIZE") or "5")),
            cover_image=cover_image,
            output_dir=output_dir,
            log_dir=log_dir,
            env_files=env_files,
        )
