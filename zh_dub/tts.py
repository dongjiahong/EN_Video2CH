"""edge-tts synthesis with per-segment duration fitting and fingerprint cache."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .logutil import warn
from .media import clear_proxy_env, ffprobe_duration
from .segments import Segment, speakable

MIN_MP3_BYTES = 500
HEARTBEAT_SEC = 20.0
OnProgress = Callable[..., None]


def compress_zh(text: str) -> list[str]:
    cands = [text]
    t = text
    for a, b in [
        ("我们", ""),
        ("一个", ""),
        ("这个", ""),
        ("那个", ""),
        ("然后", ""),
        ("显然", ""),
        ("其实", ""),
        ("你看", ""),
        ("我会", "我"),
        ("进行", ""),
        ("的话", ""),
        ("一下", ""),
    ]:
        t2 = t.replace(a, b)
        if t2 != t:
            t = re.sub(r"[，。\s]{2,}", "，", t2).strip("，。 ")
            if t and t not in cands:
                cands.append(t)
    compact = re.sub(r"[，。！？、\s]+", "，", text).strip("，")
    if compact and compact not in cands:
        cands.append(compact)
    return cands


def _needed_rate_pct(dur: float, slot: float, max_rate: int) -> int:
    """Compute rate% so that dur/(1+rate/100) ~= slot."""
    if dur <= 0 or slot <= 0:
        return 0
    need = dur / slot - 1.0
    if need <= 0:
        return 0
    # small headroom for non-linear edge-tts rate
    pct = int(math.ceil(need * 100 * 1.05))
    return max(1, min(max_rate, pct))


def tts_key(text: str, voice: str, max_rate: int) -> str:
    raw = f"{text.strip()}|{voice}|{max_rate}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def audio_path(audio_dir: Path, seg: Segment) -> Path:
    return audio_dir / f"seg_{seg.idx:04d}.mp3"


def is_cached(seg: Segment, audio_dir: Path, voice: str, max_rate: int) -> bool:
    mp3 = audio_path(audio_dir, seg)
    return (
        bool(seg.tts_key)
        and seg.tts_key == tts_key(seg.zh, voice, max_rate)
        and mp3.is_file()
        and mp3.stat().st_size >= MIN_MP3_BYTES
    )


def _fail_note(err: BaseException) -> str:
    if isinstance(err, (TimeoutError, asyncio.TimeoutError)):
        return f"tts_timeout:{str(err)[-200:]}"
    return f"tts_fail:{str(err)[-200:]}"


def _discard(task: asyncio.Task) -> None:
    """Swallow result of a cancelled/abandoned synth so it cannot hang gather."""
    try:
        if task.cancelled():
            return
        task.exception()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def _run_async(coro):
    """Like asyncio.run, but do not wait forever for abandoned synth tasks."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        leftover = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in leftover:
            t.cancel()
            t.add_done_callback(_discard)
        if leftover:
            loop.run_until_complete(asyncio.wait(leftover, timeout=0.5))
        asyncio.set_event_loop(None)
        loop.close()


async def _edge_save(text: str, out_mp3: Path, voice: str, rate_pct: int) -> None:
    import edge_tts

    await edge_tts.Communicate(text, voice, rate=f"{rate_pct:+d}%").save(str(out_mp3))


async def synthesize_mp3_async(
    text: str,
    out_mp3: Path,
    voice: str,
    rate_pct: int,
    *,
    retries: int = 2,
    timeout: float = 45.0,
) -> None:
    """One synth: timeout, then one retry. retries=2 means original + 1 extra try.

    Uses asyncio.wait rather than wait_for: wait_for waits for cancellation
    to finish, which is exactly what a hung edge-tts websocket will not do.
    """
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    last_err: BaseException | None = None
    attempts = max(1, retries)
    for attempt in range(attempts):
        out_mp3.unlink(missing_ok=True)
        task = asyncio.create_task(_edge_save(text, out_mp3, voice, rate_pct))
        done, pending = await asyncio.wait({task}, timeout=timeout)
        if pending:
            task.cancel()
            task.add_done_callback(_discard)
            last_err = TimeoutError(f"tts timeout after {timeout:.0f}s")
        else:
            exc = task.exception()
            if exc is None:
                if out_mp3.is_file() and out_mp3.stat().st_size >= MIN_MP3_BYTES:
                    return
                last_err = RuntimeError(f"tts empty output: {out_mp3}")
            else:
                last_err = exc
        if attempt + 1 < attempts:
            await asyncio.sleep(1.2 * (attempt + 1))
    if isinstance(last_err, TimeoutError):
        raise last_err
    raise RuntimeError(f"tts failed after {attempts} tries: {last_err}")


def _apply_result(
    seg: Segment, mp3: Path, *, dur: float, rate: int, text: str, original: str, key: str
) -> None:
    seg.zh = text
    seg.rate_pct = rate
    seg.tts_dur = dur
    seg.audio = str(mp3)
    seg.tts_key = key
    seg.fitted = dur <= seg.slot * 1.15
    if not seg.fitted:
        seg.note = "overflow"
    else:
        seg.note = "ok" if text == original else "compressed"


async def fit_segment_async(
    settings: Settings,
    seg: Segment,
    audio_dir: Path,
    voice: str,
    *,
    timeout: float | None = None,
) -> Segment:
    """
    Typical path: 1 TTS (rate=0). If over slot: 1 more TTS at computed rate.
    Only if still overflow after max rate: try compressed zh candidates.
    Network timeout/fail: retry once inside synthesize, then skip this sentence.
    """
    max_rate = settings.tts_max_rate
    limit = float(timeout if timeout is not None else getattr(settings, "tts_timeout", 45.0) or 45.0)
    text = seg.zh.strip()
    if not speakable(text):
        seg.note = "empty_zh"
        seg.fitted = True
        seg.audio = ""
        seg.tts_key = ""
        return seg
    if is_cached(seg, audio_dir, voice, max_rate):
        seg.note = "cached"
        return seg

    mp3 = audio_path(audio_dir, seg)
    slot = seg.slot
    best: tuple[float, int, str, bytes] | None = None

    async def synth(cand: str, rate: int) -> float:
        nonlocal best
        await synthesize_mp3_async(cand, mp3, voice, rate, timeout=limit)
        dur = await asyncio.to_thread(ffprobe_duration, settings, mp3)
        if best is None or dur < best[0]:
            best = (dur, rate, cand, mp3.read_bytes())
        return dur

    def finish(dur: float, rate: int, cand: str) -> Segment:
        _apply_result(
            seg, mp3, dur=dur, rate=rate, text=cand, original=text,
            key=tts_key(cand, voice, max_rate),
        )
        return seg

    def skip(err: BaseException) -> Segment:
        mp3.unlink(missing_ok=True)
        seg.note = _fail_note(err)
        seg.audio = ""
        seg.tts_key = ""
        return seg

    for cand in compress_zh(text):
        try:
            dur0 = await synth(cand, 0)
        except Exception as e:  # noqa: BLE001
            if best is None:
                return skip(e)
            break
        if dur0 <= slot * 1.02:
            return finish(dur0, 0, cand)
        rate = _needed_rate_pct(dur0, slot, max_rate)
        try:
            dur = await synth(cand, rate)
        except Exception:  # noqa: BLE001
            continue
        if dur <= slot * 1.15:
            return finish(dur, rate, cand)

    if best is not None:
        dur, rate, cand, data = best
        mp3.write_bytes(data)
        return finish(dur, rate, cand)
    seg.audio = ""
    seg.tts_key = ""
    return seg


def fit_segments(
    settings: Settings,
    segs: list[Segment],
    audio_dir: Path,
    voice: str,
    on_progress: OnProgress | None = None,
) -> None:
    clear_proxy_env()
    audio_dir.mkdir(parents=True, exist_ok=True)
    timeout = float(getattr(settings, "tts_timeout", 45.0) or 45.0)

    async def main() -> None:
        sem = asyncio.Semaphore(max(1, settings.tts_concurrency))
        done = 0
        inflight: dict[int, float] = {}
        stop = asyncio.Event()

        async def one(seg: Segment) -> None:
            nonlocal done
            async with sem:
                inflight[seg.idx] = time.monotonic()
                try:
                    await fit_segment_async(
                        settings, seg, audio_dir, voice, timeout=timeout
                    )
                except Exception as e:  # noqa: BLE001
                    audio_path(audio_dir, seg).unlink(missing_ok=True)
                    seg.note = _fail_note(e)
                    seg.audio = ""
                    seg.tts_key = ""
                finally:
                    inflight.pop(seg.idx, None)
            done += 1
            if on_progress:
                on_progress(done, len(segs), seg, inflight=len(inflight))

        async def heartbeat() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SEC)
                    return
                except asyncio.TimeoutError:
                    pass
                now = time.monotonic()
                stuck = [
                    (idx, now - t0)
                    for idx, t0 in list(inflight.items())
                    if now - t0 >= HEARTBEAT_SEC
                ]
                for idx, elapsed in stuck:
                    warn(f"TTS 等待 idx={idx:04d} 已 {elapsed:.0f}s")

        hb = asyncio.create_task(heartbeat())
        try:
            await asyncio.gather(*(one(s) for s in segs))
        finally:
            stop.set()
            await hb

    _run_async(main())


def validate_tts(
    segs: list[Segment], audio_dir: Path, voice: str, max_rate: int
) -> dict[str, Any]:
    need = [s for s in segs if speakable(s.zh)]
    missing = [s.idx for s in need if not is_cached(s, audio_dir, voice, max_rate)]
    return {
        "segments": len(segs),
        "need": len(need),
        "audio_ok": len(need) - len(missing),
        "audio_missing": missing,
        "overflow": sum(1 for s in segs if s.note == "overflow"),
        "pass": not missing,
    }
