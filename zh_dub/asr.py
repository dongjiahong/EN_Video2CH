"""Parakeet ASR: transcribe the local source video into a punctual SRT.

Called in-process via the parakeet-mlx Python API (no CLI subprocess):

    from_pretrained(model) -> model.transcribe(wav, ...) -> result.sentences
    Each AlignedSentence has (text, start, end); we write those to asr.srt.

Layout concerns (model/API/import errors) are intentionally isolated in this
module so a bad parakeet install only affects the ``transcribe`` stage and
never download/translate/tts/compose.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Settings
from .logutil import highlight, info, warn
from .media import run_cmd
from .segments import seconds_to_ts

ASR_WAV = "_asr.wav"
MIN_CUE_SEC = 0.05


def _import_parakeet():
    """Lazy import so status/clean/translate never touch parakeet-mlx."""
    try:
        import parakeet_mlx  # noqa: F401
        from parakeet_mlx import DecodingConfig, SentenceConfig, from_pretrained  # noqa: F401
        return parakeet_mlx, DecodingConfig, SentenceConfig, from_pretrained
    except ImportError as e:
        raise RuntimeError(
            "parakeet-mlx 未安装，无法转录本地字幕。\n"
            "请在 conda python3 环境安装后重试:\n"
            "  pip install parakeet-mlx\n"
            "（需 Apple Silicon + ffmpeg；首次运行会从 HuggingFace 下载模型）"
        ) from e


def extract_asr_wav(settings: Settings, work: Path) -> Path:
    """source.mp4 -> 16kHz mono wav (Parakeet input spec)."""
    src = work / "source.mp4"
    if not src.is_file():
        raise RuntimeError(f"missing source.mp4 in {work}; run prepare_video first")
    wav = work / ASR_WAV
    run_cmd(
        [
            settings.ffmpeg,
            "-y",
            "-i",
            str(src),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(wav),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not wav.is_file() or wav.stat().st_size < 500:
        raise RuntimeError("ASR wav extraction produced empty output")
    return wav


def _build_decoding_config(settings: Settings):
    _, DecodingConfig, SentenceConfig, _ = _import_parakeet()
    kw: dict = {}
    if settings.parakeet_decoding == "beam":
        try:
            from parakeet_mlx import Beam
        except ImportError:
            Beam = None
        if Beam is not None:
            kw["decoding"] = Beam(beam_size=settings.parakeet_beam_size)
        else:
            warn("parakeet_mlx 未导出 Beam，本次回退 greedy 解码")
    gap = settings.parakeet_silence_gap
    kw["sentence"] = SentenceConfig(silence_gap=gap if gap > 0 else None)
    return DecodingConfig(**kw)


def transcribe_to_srt(settings: Settings, work: Path) -> tuple[Path, int]:
    """Transcribe work/source.mp4 into work/asr.srt. Returns (path, cue_count)."""
    _, _, _, from_pretrained = _import_parakeet()
    wav = extract_asr_wav(settings, work)

    info(
        f"ASR 转录  model={settings.parakeet_model}  "
        f"decoding={settings.parakeet_decoding}  "
        f"chunk={settings.parakeet_chunk_duration}s  "
        f"overlap={settings.parakeet_overlap_duration}s  "
        f"silence_gap={settings.parakeet_silence_gap}s"
    )
    model = from_pretrained(settings.parakeet_model)
    config = _build_decoding_config(settings)
    result = model.transcribe(
        str(wav),
        chunk_duration=settings.parakeet_chunk_duration,
        overlap_duration=settings.parakeet_overlap_duration,
        decoding_config=config,
    )

    sentences = list(getattr(result, "sentences", None) or [])
    if not sentences:
        raise RuntimeError(
            f"parakeet 结果里没有 sentences（text 有值但缺时间戳）。"
            f"result 可用字段: {sorted(k for k in dir(result) if not k.startswith('_'))}"
        )

    out = work / "asr.srt"
    lines: list[str] = []
    n = 0
    skipped = 0
    for s in sentences:
        text = (getattr(s, "text", "") or "").strip()
        start = float(getattr(s, "start", 0.0) or 0.0)
        end = float(getattr(s, "end", 0.0) or 0.0)
        if not text or end - start < MIN_CUE_SEC:
            skipped += 1
            continue
        n += 1
        lines.append(str(n))
        lines.append(
            f"{seconds_to_ts(start, True)} --> {seconds_to_ts(end, True)}"
        )
        lines.append(text)
        lines.append("")
    if n == 0:
        raise RuntimeError(f"ASR 转录结果为空（跳过 {skipped} 条无效 cue）")

    out.write_text("\n".join(lines), encoding="utf-8")
    try:
        wav.unlink(missing_ok=True)  # ~160MB/hr scratch; keep work dir lean
    except OSError:
        pass
    highlight(f"ASR 完成  cues={n}  skipped={skipped}  -> {out.name}")
    return out, n
