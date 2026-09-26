"""Assemble per-segment mp3 clips into one full-length narration wav."""

from __future__ import annotations

import array
import subprocess
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Settings
from .logutil import highlight, info, progress, warn
from .segments import Segment

NARRATION_SR = 24000


def _decode_pcm(settings: Settings, src: Path) -> array.array:
    cp = subprocess.run(
        [
            settings.ffmpeg, "-v", "error", "-i", str(src),
            "-f", "s16le", "-ac", "1", "-ar", str(NARRATION_SR), "-",
        ],
        check=True,
        capture_output=True,
    )
    samples = array.array("h")
    samples.frombytes(cp.stdout)
    return samples


def build_narration(
    settings: Settings, segs: list[Segment], out_wav: Path, total_dur: float
) -> int:
    """
    Place each clip at seg.start on a full-length mono timeline.

    Avoids ffmpeg amix with hundreds of inputs (OOM / exit 232) by
    assembling PCM in-process (slice paste; later clip wins on overlap).
    Returns number of clips placed.
    """
    clips = [s for s in segs if s.audio and Path(s.audio).is_file() and s.start < total_dur]
    info(f"旁白时间线  clips={len(clips)}  total={total_dur:.1f}s")
    n_samples = max(1, int(round(total_dur * NARRATION_SR)))
    # zeros: ~ total_dur * 24k * 2 bytes (e.g. 4000s ≈ 190MB)
    buf = array.array("h", bytes(n_samples * 2))

    t0 = time.time()
    placed = 0

    def decode(seg: Segment) -> tuple[Segment, array.array | None]:
        try:
            return seg, _decode_pcm(settings, Path(seg.audio))
        except Exception as e:  # noqa: BLE001
            warn(f"旁白解码失败 idx={seg.idx}: {e}")
            return seg, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, (seg, samples) in enumerate(pool.map(decode, clips), start=1):
            if samples:
                offset = int(round(seg.start * NARRATION_SR))
                n = min(len(samples), n_samples - offset)
                buf[offset : offset + n] = samples[:n]
                placed += 1
            if i == 1 or i == len(clips) or i % 100 == 0:
                progress(i, len(clips), f"seg_{seg.idx:04d}", label="旁白")

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(NARRATION_SR)
        out.writeframes(buf.tobytes())

    highlight(f"旁白完成  {time.time()-t0:.1f}s  clips={placed}/{len(clips)}  -> {out_wav.name}")
    return placed
