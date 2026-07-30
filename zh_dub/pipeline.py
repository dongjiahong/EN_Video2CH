from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .captions import (
    Segment,
    load_cues,
    load_segments,
    merge_cues,
    resolve_subtitle,
    save_segments,
    write_srt,
    write_zh_ass,
)
from .config import Settings
from .logutil import log, stage, stage_done, stage_error
from .media import (
    clear_proxy_env,
    download_subs,
    download_video,
    ffprobe_duration,
    mp3_to_wav,
    prepare_source_video,
    resolve_video_id,
    run_cmd,
    synthesize_mp3_async,
)
from .state import STAGES, JobState
from .translate import translate_segments


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


def _segment_slot(seg: Segment, next_start: float | None) -> float:
    slot = seg.slot
    if next_start is not None and next_start > seg.end:
        slot += min(0.35, max(0.0, next_start - seg.end))
    return slot


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


def _promote_existing_audio(
    settings: Settings, seg: Segment, audio_dir: Path
) -> bool:
    """Reuse canonical or legacy trial mp3 (seg_XXXX_rYY.mp3)."""
    final_mp3 = audio_dir / f"seg_{seg.idx:04d}.mp3"
    candidates: list[Path] = []
    if final_mp3.is_file():
        candidates.append(final_mp3)
    if seg.audio:
        p = Path(seg.audio)
        if p.is_file():
            candidates.append(p)
    candidates.extend(sorted(audio_dir.glob(f"seg_{seg.idx:04d}_r*.mp3")))
    for src in candidates:
        if src.stat().st_size <= 500:
            continue
        try:
            if src.resolve() != final_mp3.resolve():
                final_mp3.write_bytes(src.read_bytes())
            dur = ffprobe_duration(settings, final_mp3)
            if dur > 0.05:
                seg.audio = str(final_mp3)
                seg.tts_dur = dur
                seg.fitted = True
                seg.note = seg.note or "cached"
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _apply_audio_result(
    seg: Segment,
    final_mp3: Path,
    *,
    dur: float,
    rate: int,
    slot: float,
    used_text: str,
    original_text: str,
) -> None:
    seg.zh = used_text
    seg.rate_pct = rate
    seg.tts_dur = dur
    seg.audio = str(final_mp3)
    if dur <= slot * 1.02:
        seg.fitted = True
        seg.note = "ok" if used_text == original_text else "compressed"
    elif dur <= slot * 1.15:
        seg.fitted = True
        seg.note = "ok" if used_text == original_text else "compressed"
    else:
        seg.fitted = False
        seg.note = "overflow"


async def _synth_and_measure(
    settings: Settings,
    text: str,
    out_mp3: Path,
    voice: str,
    rate_pct: int,
) -> float:
    await synthesize_mp3_async(text, out_mp3, voice, rate_pct)
    return await asyncio.to_thread(ffprobe_duration, settings, out_mp3)


async def fit_segment_async(
    settings: Settings,
    seg: Segment,
    audio_dir: Path,
    voice: str,
    next_start: float | None,
    *,
    force: bool = False,
) -> Segment:
    """
    Typical path: 1 TTS (rate=0). If over slot: 1 more TTS at computed rate.
    Only if still overflow after max rate: try compressed zh (same 1~2 calls).
    """
    text = seg.zh.strip()
    if not text:
        seg.note = "empty_zh"
        seg.fitted = True
        return seg

    final_mp3 = audio_dir / f"seg_{seg.idx:04d}.mp3"
    if not force:
        promoted = await asyncio.to_thread(
            _promote_existing_audio, settings, seg, audio_dir
        )
        if promoted:
            return seg

    slot = _segment_slot(seg, next_start)
    max_rate = settings.tts_max_rate
    original = text
    cands = compress_zh(text)
    best_meta: tuple[float, int, str] | None = None
    best_bytes: bytes | None = None

    def remember(dur: float, rate: int, cand: str) -> None:
        nonlocal best_meta, best_bytes
        if best_meta is None or dur < best_meta[0]:
            best_meta = (dur, rate, cand)
            if final_mp3.is_file():
                best_bytes = final_mp3.read_bytes()

    for cand_i, cand in enumerate(cands):
        try:
            dur0 = await _synth_and_measure(settings, cand, final_mp3, voice, 0)
        except Exception as e:  # noqa: BLE001
            seg.note = f"tts_fail:{str(e)[-200:]}"
            continue

        remember(dur0, 0, cand)
        if dur0 <= slot * 1.02:
            _apply_audio_result(
                seg,
                final_mp3,
                dur=dur0,
                rate=0,
                slot=slot,
                used_text=cand,
                original_text=original,
            )
            return seg

        rate = _needed_rate_pct(dur0, slot, max_rate)
        try:
            dur = await _synth_and_measure(settings, cand, final_mp3, voice, rate)
        except Exception as e:  # noqa: BLE001
            if best_bytes is not None:
                final_mp3.write_bytes(best_bytes)
            _apply_audio_result(
                seg,
                final_mp3,
                dur=dur0,
                rate=0,
                slot=slot,
                used_text=cand,
                original_text=original,
            )
            if seg.note != "overflow":
                seg.note = f"tts_rate_fail:{str(e)[-120:]}"
            return seg

        remember(dur, rate, cand)
        _apply_audio_result(
            seg,
            final_mp3,
            dur=dur,
            rate=rate,
            slot=slot,
            used_text=cand,
            original_text=original,
        )
        if seg.note != "overflow" or cand_i == len(cands) - 1:
            return seg

    if best_meta is not None and best_bytes is not None:
        final_mp3.write_bytes(best_bytes)
        _apply_audio_result(
            seg,
            final_mp3,
            dur=best_meta[0],
            rate=best_meta[1],
            slot=slot,
            used_text=best_meta[2],
            original_text=original,
        )
    elif not (seg.note or "").startswith("tts_"):
        seg.note = "no_audio"
    return seg


def fit_segment(
    settings: Settings,
    seg: Segment,
    audio_dir: Path,
    voice: str,
    next_start: float | None,
) -> Segment:
    return asyncio.run(
        fit_segment_async(settings, seg, audio_dir, voice, next_start)
    )


async def _fit_segments_concurrent(
    settings: Settings,
    windowed: list[Segment],
    audio_dir: Path,
    voice: str,
    *,
    force: bool = False,
    on_progress: Any | None = None,
) -> None:
    conc = max(1, settings.tts_concurrency)
    sem = asyncio.Semaphore(conc)
    total = len(windowed)
    done = 0
    lock = asyncio.Lock()

    async def one(i: int, seg: Segment) -> None:
        nonlocal done
        nxt = windowed[i + 1].start if i + 1 < total else None
        async with sem:
            await fit_segment_async(
                settings, seg, audio_dir, voice, nxt, force=force
            )
            cached = seg.note == "cached"
        async with lock:
            done += 1
            if on_progress:
                on_progress(done, total, seg, cached=cached)

    await asyncio.gather(*(one(i, s) for i, s in enumerate(windowed)))


def _segment_audio_candidates(work: Path, seg: Segment) -> list[Path]:
    audio_dir = work / "audio"
    out: list[Path] = []
    canon = audio_dir / f"seg_{seg.idx:04d}.mp3"
    if canon.is_file():
        out.append(canon)
    if seg.audio:
        p = Path(seg.audio)
        if p.is_file():
            out.append(p)
    out.extend(sorted(audio_dir.glob(f"seg_{seg.idx:04d}_r*.mp3")))
    # unique keep order
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        uniq.append(p)
    return uniq


def validate_tts(work: Path, segs: list[Segment]) -> dict[str, Any]:
    need = [s for s in segs if (s.zh or "").strip()]
    ok_idx: list[int] = []
    missing: list[int] = []
    bad: list[int] = []
    for s in need:
        found = False
        for p in _segment_audio_candidates(work, s):
            if p.is_file() and p.stat().st_size >= 500:
                found = True
                break
        if found:
            ok_idx.append(s.idx)
        else:
            missing.append(s.idx)
    report = {
        "segments": len(segs),
        "zh_nonempty": len(need),
        "audio_ok": len(ok_idx),
        "audio_missing": missing,
        "audio_bad": bad,
        "overflow": sum(1 for s in segs if s.note == "overflow"),
        "pass": len(missing) == 0 and len(need) > 0,
    }
    (work / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


NARRATION_SR = 24000


def _ensure_mono_s16_wav(settings: Settings, src: Path, dst: Path) -> Path:
    """Decode/convert to mono s16le 24kHz wav (idempotent if dst fresh enough)."""
    if dst.is_file() and dst.stat().st_size >= 500:
        if src.suffix.lower() == ".mp3":
            # reuse if not older than source
            if dst.stat().st_mtime >= src.stat().st_mtime:
                return dst
        else:
            return dst
    subprocess.run(
        [
            settings.ffmpeg,
            "-y",
            "-i",
            str(src),
            "-ac",
            "1",
            "-ar",
            str(NARRATION_SR),
            "-sample_fmt",
            "s16",
            str(dst),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return dst


def build_narration(
    settings: Settings,
    segs: list[Segment],
    audio_dir: Path,
    out_wav: Path,
    total_dur: float,
) -> None:
    """
    Place each clip at seg.start on a full-length mono timeline.

    Avoids ffmpeg amix with hundreds of inputs (OOM / exit 232) by
    assembling PCM in-process (slice paste; later clip wins on overlap).
    """
    import array
    import wave

    clips: list[tuple[Path, float]] = []
    for seg in segs:
        if not seg.audio:
            continue
        src = Path(seg.audio)
        if not src.is_file():
            continue
        wav = audio_dir / f"seg_{seg.idx:04d}_use.wav"
        try:
            wav = _ensure_mono_s16_wav(settings, src, wav)
        except Exception as e:  # noqa: BLE001
            log(f"narration decode fail idx={seg.idx}: {e}")
            continue
        clips.append((wav, float(seg.start)))

    log(f"build narration timeline clips={len(clips)} total_dur={total_dur:.1f}s")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    n_samples = max(1, int(round(float(total_dur) * NARRATION_SR)))
    # zeros: ~ total_dur * 24k * 2 bytes (e.g. 4000s ≈ 190MB)
    buf = array.array("h", bytes(n_samples * 2))

    t0 = time.time()
    placed = 0
    for i, (wav_path, start) in enumerate(clips, start=1):
        try:
            with wave.open(str(wav_path), "rb") as w:
                if (
                    w.getnchannels() != 1
                    or w.getsampwidth() != 2
                    or w.getframerate() != NARRATION_SR
                ):
                    raise RuntimeError(
                        f"bad wav format ch={w.getnchannels()} "
                        f"sw={w.getsampwidth()} sr={w.getframerate()}"
                    )
                raw = w.readframes(w.getnframes())
            samples = array.array("h")
            samples.frombytes(raw)
        except Exception as e:  # noqa: BLE001
            log(f"narration skip {wav_path.name}: {e}")
            continue

        offset = int(round(start * NARRATION_SR))
        if offset >= n_samples or not samples:
            continue
        n = min(len(samples), n_samples - offset)
        # bulk paste (C-speed slice); overlaps: later clip overwrites
        buf[offset : offset + n] = samples[:n]
        placed += 1
        if i == 1 or i == len(clips) or i % 100 == 0:
            log(f"narration place {i}/{len(clips)}")

    with wave.open(str(out_wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(NARRATION_SR)
        out.writeframes(buf.tobytes())

    log(
        f"narration ready in {time.time()-t0:.1f}s clips={placed}/{len(clips)} "
        f"-> {out_wav}"
    )


def mux_video(
    settings: Settings,
    video: Path,
    narration: Path,
    out: Path,
    ass_path: Path | None,
    original_volume: float = 0.0,
) -> None:
    video = video.resolve()
    narration = narration.resolve()
    out = out.resolve()
    workdir = out.parent
    # pin duration to source video so a shorter leftover out/partial encode cannot win
    video_dur = ffprobe_duration(settings, video)
    nar_dur = ffprobe_duration(settings, narration)
    log(f"mux inputs video={video_dur:.1f}s narration={nar_dur:.1f}s")
    if nar_dur + 1.0 < video_dur * 0.95:
        raise RuntimeError(
            f"narration shorter than video ({nar_dur:.1f}s < {video_dur:.1f}s); "
            "rebuild narration first"
        )

    cmd: list[str] = [settings.ffmpeg, "-y", "-i", str(video), "-i", str(narration)]
    if original_volume <= 0:
        cmd += ["-map", "0:v", "-map", "1:a"]
    else:
        cmd += [
            "-filter_complex",
            f"[0:a]volume={original_volume}[a0];[a0][1:a]amix=inputs=2:normalize=0[aout]",
            "-map",
            "0:v",
            "-map",
            "[aout]",
        ]
    if ass_path is not None and ass_path.exists():
        local = workdir / "_burn.ass"
        local.write_text(ass_path.read_text(encoding="utf-8"), encoding="utf-8")
        cmd += ["-vf", f"ass={local.name}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    else:
        cmd += ["-c:v", "copy"]
    # Use source duration explicitly. Avoid bare -shortest which can stop early
    # on odd container timestamps / partial previous encodes.
    cmd += [
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-t",
        f"{video_dur:.3f}",
        str(out),
    ]
    log(f"ffmpeg mux -> {out}")
    t0 = time.time()
    cp = subprocess.run(
        cmd,
        check=False,
        cwd=str(workdir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        print((cp.stderr or "ffmpeg failed")[-2500:], flush=True)
        raise subprocess.CalledProcessError(cp.returncode, cmd, stderr=cp.stderr)
    out_dur = ffprobe_duration(settings, out)
    log(f"mux finished in {time.time()-t0:.1f}s out_dur={out_dur:.1f}s")
    if out_dur + 2.0 < video_dur * 0.98:
        raise RuntimeError(
            f"mux output truncated: out={out_dur:.1f}s source={video_dur:.1f}s"
        )


class Pipeline:
    def __init__(self, settings: Settings, work: Path, *, end: float = 0.0, voice: str | None = None):
        self.settings = settings
        self.work = work.resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.end = float(end or 0.0)
        self.voice = voice or settings.voice
        self.state = JobState(self.work)
        self.state.update_meta(end=self.end, voice=self.voice, quality=settings.quality)
        self._bootstrap_state_from_artifacts()

    def _bootstrap_state_from_artifacts(self) -> None:
        """Infer done stages from existing files (first run on old workdirs)."""
        changed = False
        st = self.state.data["stages"]

        def mark(name: str, **detail: Any) -> None:
            nonlocal changed
            if st.get(name, {}).get("status") != "done":
                st[name] = {"status": "done", "detail": detail}
                changed = True

        if (self.work / "source_full.mp4").is_file() or (self.work / "source.mp4").is_file():
            if (self.work / "source.en.vtt").is_file() or (self.work / "source.en.srt").is_file():
                mark("download", inferred=True)
        if (self.work / "source.mp4").is_file():
            mark("prepare_video", inferred=True)

        seg_path = self.work / "segments.json"
        if seg_path.is_file():
            try:
                segs = load_segments(seg_path)
            except Exception:  # noqa: BLE001
                segs = []
            if segs:
                # ensure en checkpoint
                if not self.state.checkpoint_path("segments_en.json").is_file():
                    self.state.write_json(
                        "segments_en.json",
                        [
                            {
                                "idx": s.idx,
                                "start": s.start,
                                "end": s.end,
                                "en": s.en,
                                "zh": "",
                                "rate_pct": 0,
                                "audio": "",
                                "tts_dur": 0.0,
                                "fitted": False,
                                "note": "",
                            }
                            for s in segs
                        ],
                    )
                mark("prepare_cues", inferred=True, segments=len(segs))
                mark("merge", inferred=True, segments=len(segs))
                zh_n = sum(1 for s in segs if (s.zh or "").strip())
                if zh_n >= max(1, int(len(segs) * 0.98)):
                    mark("translate", inferred=True, total=len(segs), zh=zh_n)
                    # seed batch checkpoint so resume won't redo
                    if not list((self.state.checkpoints / "translate").glob("batch_*.json")):
                        mapping = {
                            str(i + 1): s.zh
                            for i, s in enumerate(segs)
                            if (s.zh or "").strip()
                        }
                        (self.state.checkpoints / "translate").mkdir(parents=True, exist_ok=True)
                        self.state.write_json(
                            "translate/batch_000.json",
                            {
                                "batch_index": 0,
                                "start": 0,
                                "expected": len(segs),
                                "got": len(mapping),
                                "mapping": mapping,
                                "inferred": True,
                            },
                        )
                report = validate_tts(self.work, segs)
                if report["pass"]:
                    mark("tts", inferred=True, audio_ok=report["audio_ok"])
        if (self.work / "narration.wav").is_file():
            mark("narration", inferred=True)
        out = self.work / "out.mp4"
        prev = self.work / "out_preview.mp4"
        cand = out if out.is_file() else (prev if prev.is_file() else None)
        if cand is not None:
            # do not mark compose done if output is clearly shorter than source
            ok_len = True
            src = self.work / "source.mp4"
            if src.is_file():
                try:
                    sd = ffprobe_duration(self.settings, src)
                    od = ffprobe_duration(self.settings, cand)
                    if sd > 0 and od + 2.0 < sd * 0.98:
                        ok_len = False
                        log(
                            f"bootstrap skip compose: {cand.name} "
                            f"{od:.1f}s << source {sd:.1f}s"
                        )
                except Exception:  # noqa: BLE001
                    pass
            if ok_len:
                mark("compose", inferred=True, out=str(cand))

        if changed:
            nxt = self.state.next_pending()
            self.state.data["stage"] = nxt or "compose"
            if nxt is None:
                self.state.data["status"] = "done"
            elif self.state.data.get("status") == "pending":
                self.state.data["status"] = "running"
            self.state.save()
            log("bootstrapped job_state from existing artifacts")

    def print_status(self) -> None:
        stage("status", str(self.work))
        for line in self.state.summary_lines():
            log(line)
        # live validation if segments exist
        seg_path = self.work / "segments.json"
        if seg_path.is_file():
            segs = load_segments(seg_path)
            zh_n = sum(1 for s in segs if (s.zh or "").strip())
            report = validate_tts(self.work, segs)
            log(
                f"live: segments={len(segs)} zh={zh_n} "
                f"audio_ok={report['audio_ok']} missing={len(report['audio_missing'])}"
            )
            if report["audio_missing"][:10]:
                log(f"missing idx sample: {report['audio_missing'][:10]}")

    def ensure_media_from_url(self, url: str) -> None:
        if self.state.is_done("download") and (
            (self.work / "source_full.mp4").is_file()
            or (self.work / "source.mp4").is_file()
        ):
            log("download already done")
            return
        self.state.set_running("download")
        try:
            clear_proxy_env()  # only yt-dlp uses explicit env proxy
            info = download_video(self.settings, url, self.work)
            sub = download_subs(self.settings, url, self.work)
            self.state.set_done("download", video=info, subs=sub)
        except Exception as e:  # noqa: BLE001
            stage_error("download", str(e))
            self.state.set_failed("download", str(e), retryable=True)
            raise

    def step_prepare_video(self) -> None:
        if self.state.is_done("prepare_video") and (self.work / "source.mp4").is_file():
            # if full job but source looks like stale preview, refresh
            if self.end <= 0:
                full = self.work / "source_full.mp4"
                src = self.work / "source.mp4"
                if full.is_file() and src.stat().st_size < full.stat().st_size * 0.9:
                    log("source.mp4 stale preview; refresh full copy")
                else:
                    log("prepare_video already done")
                    return
        self.state.set_running("prepare_video")
        try:
            info = prepare_source_video(self.settings, self.work, self.end)
            self.state.set_done("prepare_video", **info)
        except Exception as e:  # noqa: BLE001
            stage_error("prepare_video", str(e))
            self.state.set_failed("prepare_video", str(e), retryable=True)
            raise

    def step_prepare_cues(self) -> list[tuple[float, float, str]]:
        if self.state.is_done("prepare_cues"):
            data = self.state.read_json("cues.json")
            if data:
                log(f"prepare_cues already done ({len(data)} cues)")
                return [(c["start"], c["end"], c["text"]) for c in data]
        self.state.set_running("prepare_cues")
        try:
            stage("prepare_cues")
            sub = resolve_subtitle(self.work)
            log(f"subtitle source: {sub}")
            cues = load_cues(sub)
            t0, t1 = 0.0, (1e12 if self.end <= 0 else self.end)
            cues = [
                (max(s, t0), min(e, t1), t)
                for s, e, t in cues
                if e > t0 and s < t1 and min(e, t1) > max(s, t0)
            ]
            payload = [{"start": s, "end": e, "text": t} for s, e, t in cues]
            self.state.write_json("cues.json", payload)
            stage_done("prepare_cues", f"cues={len(cues)}")
            self.state.set_done("prepare_cues", cues=len(cues), subtitle=str(sub))
            return cues
        except Exception as e:  # noqa: BLE001
            stage_error("prepare_cues", str(e))
            self.state.set_failed("prepare_cues", str(e), retryable=True)
            raise

    def step_merge(self, cues: list[tuple[float, float, str]] | None = None) -> list[Segment]:
        if self.state.is_done("merge"):
            path = self.state.checkpoint_path("segments_en.json")
            if path.is_file():
                segs = load_segments(path)
                log(f"merge already done ({len(segs)} segments)")
                return segs
        self.state.set_running("merge")
        try:
            stage("merge")
            if cues is None:
                raw = self.state.read_json("cues.json") or []
                cues = [(c["start"], c["end"], c["text"]) for c in raw]
            segs = merge_cues(cues)
            self.state.write_json("segments_en.json", [s.__dict__ for s in segs])
            # also keep work/segments.json skeleton if absent
            if not (self.work / "segments.json").is_file():
                save_segments(segs, self.work / "segments.json")
            write_srt(segs, self.work / "en_merged.srt", "en")
            stage_done("merge", f"segments={len(segs)}")
            self.state.set_done("merge", segments=len(segs))
            return segs
        except Exception as e:  # noqa: BLE001
            stage_error("merge", str(e))
            self.state.set_failed("merge", str(e), retryable=True)
            raise

    def step_translate(self, segs: list[Segment] | None = None, *, force: bool = False) -> list[Segment]:
        # If segments.json already fully translated and stage done, skip
        seg_path = self.work / "segments.json"
        if self.state.is_done("translate") and seg_path.is_file() and not force:
            segs2 = load_segments(seg_path)
            zh_n = sum(1 for s in segs2 if (s.zh or "").strip())
            if zh_n >= max(1, int(len(segs2) * 0.98)):
                log(f"translate already done ({zh_n}/{len(segs2)})")
                return segs2

        if segs is None:
            en_path = self.state.checkpoint_path("segments_en.json")
            if en_path.is_file():
                segs = load_segments(en_path)
            elif seg_path.is_file():
                segs = load_segments(seg_path)
            else:
                raise RuntimeError("no segments to translate; run merge first")

        # bootstrap checkpoints from existing segments.json zh if present
        if seg_path.is_file() and not force:
            old = load_segments(seg_path)
            if len(old) == len(segs):
                for a, b in zip(segs, old):
                    if (b.zh or "").strip() and not (a.zh or "").strip():
                        a.zh = b.zh

        try:
            zhs = translate_segments(
                self.settings,
                self.state,
                [s.en for s in segs],
                force=force,
            )
            for s, zh in zip(segs, zhs):
                s.zh = zh
            save_segments(segs, seg_path)
            write_srt(segs, self.work / "zh_draft.srt", "zh")
            self.state.write_json("segments_zh.json", [s.__dict__ for s in segs])
            return segs
        except Exception:
            # partial zh still useful
            if any((s.zh or "").strip() for s in segs):
                save_segments(segs, seg_path)
            raise

    def step_tts(self, *, force: bool = False) -> list[Segment]:
        seg_path = self.work / "segments.json"
        if not seg_path.is_file():
            raise RuntimeError("missing segments.json; translate first")
        segs = load_segments(seg_path)
        t0, t1 = 0.0, (1e12 if self.end <= 0 else self.end)
        windowed = [s for s in segs if s.end > t0 and s.start < t1]
        audio_dir = self.work / "audio"
        audio_dir.mkdir(exist_ok=True)

        if self.state.is_done("tts") and not force:
            report = validate_tts(self.work, windowed)
            if report["pass"]:
                log(f"tts already done audio_ok={report['audio_ok']}")
                return segs
            log(f"tts marked done but missing={report['audio_missing'][:20]}; repairing")

        self.state.set_running("tts")
        conc = self.settings.tts_concurrency
        stage(
            "tts",
            f"segments={len(windowed)} voice={self.voice} concurrency={conc} "
            f"max_rate={self.settings.tts_max_rate}",
        )
        clear_proxy_env()
        t_all = time.time()
        try:
            def on_progress(done: int, total: int, seg: Segment, *, cached: bool) -> None:
                if cached:
                    if done == 1 or done % 50 == 0 or done == total:
                        log(f"TTS skip cached {done}/{total} idx={seg.idx:04d}")
                elif done == 1 or done == total or done % 25 == 0 or total <= 40:
                    log(
                        f"TTS {done}/{total} idx={seg.idx:04d} "
                        f"dur={seg.tts_dur:.2f}s rate={seg.rate_pct:+d}% {seg.note} "
                        f":: {seg.zh}"
                    )
                elif done % 5 == 0:
                    log(f"TTS progress {done}/{total}")
                if done % 20 == 0 or done == total:
                    self.state.data["stages"]["tts"]["detail"] = {
                        "progress": f"{done}/{total}",
                        "concurrency": conc,
                    }
                    self.state.save()
                    by_idx = {s.idx: s for s in windowed}
                    full = load_segments(seg_path)
                    mid = [by_idx.get(s.idx, s) for s in full]
                    for s in mid:
                        p = audio_dir / f"seg_{s.idx:04d}.mp3"
                        if p.is_file():
                            s.audio = str(p)
                    save_segments(mid, seg_path)

            asyncio.run(
                _fit_segments_concurrent(
                    self.settings,
                    windowed,
                    audio_dir,
                    self.voice,
                    force=force,
                    on_progress=on_progress,
                )
            )

            by_idx = {s.idx: s for s in windowed}
            full = load_segments(seg_path)
            merged = [by_idx.get(s.idx, s) for s in full]
            for s in merged:
                p = audio_dir / f"seg_{s.idx:04d}.mp3"
                if p.is_file():
                    s.audio = str(p)
            save_segments(merged, seg_path)
            write_srt(merged, self.work / "zh.srt", "zh")

            report = validate_tts(self.work, windowed)
            log(
                f"tts validate audio_ok={report['audio_ok']}/"
                f"{report['zh_nonempty']} missing={len(report['audio_missing'])} "
                f"overflow={report['overflow']}"
            )
            if not report["pass"]:
                self.state.set_failed(
                    "tts",
                    f"audio missing {len(report['audio_missing'])} segments",
                    retryable=True,
                    missing=report["audio_missing"][:50],
                    audio_ok=report["audio_ok"],
                )
                log(f"RESUME: python job_run.py --work {self.work} --resume")
                raise RuntimeError(
                    f"TTS incomplete: missing {report['audio_missing'][:20]}"
                )

            stage_done(
                "tts",
                f"ok={report['audio_ok']} overflow={report['overflow']} "
                f"elapsed={time.time()-t_all:.1f}s concurrency={conc}",
            )
            self.state.set_done(
                "tts",
                audio_ok=report["audio_ok"],
                overflow=report["overflow"],
                total=len(windowed),
                concurrency=conc,
            )
            return merged
        except Exception as e:  # noqa: BLE001
            if self.state.stage_status("tts") != "failed":
                stage_error("tts", str(e))
                self.state.set_failed("tts", str(e), retryable=True)
            raise

    def step_narration(self) -> Path:
        if self.state.is_done("narration"):
            p = self.work / "narration.wav"
            if p.is_file() and p.stat().st_size > 1000:
                log("narration already done")
                return p
        self.state.set_running("narration")
        try:
            stage("narration")
            segs = load_segments(self.work / "segments.json")
            video = self.work / "source.mp4"
            total = ffprobe_duration(self.settings, video)
            # only segs with audio
            use = []
            for s in segs:
                if s.start >= total:
                    continue
                mp3 = self.work / "audio" / f"seg_{s.idx:04d}.mp3"
                if mp3.is_file():
                    s.audio = str(mp3)
                    use.append(s)
                elif s.audio and Path(s.audio).is_file():
                    use.append(s)
            report = validate_tts(self.work, segs)
            if report["audio_missing"]:
                raise RuntimeError(
                    f"cannot build narration, missing audio: {report['audio_missing'][:20]}"
                )
            out = self.work / "narration.wav"
            build_narration(self.settings, use, self.work / "audio", out, total)
            stage_done("narration", f"file={out}")
            self.state.set_done("narration", file=str(out), clips=len(use), duration=total)
            return out
        except Exception as e:  # noqa: BLE001
            stage_error("narration", str(e))
            self.state.set_failed("narration", str(e), retryable=True)
            raise

    def step_compose(self, out_name: str | None = None) -> Path:
        out = self.work / (out_name or ("out_preview.mp4" if self.end > 0 else "out.mp4"))
        video = self.work / "source.mp4"
        if self.state.is_done("compose") and out.is_file() and out.stat().st_size > 1000:
            try:
                src_dur = ffprobe_duration(self.settings, video) if video.is_file() else 0.0
                out_dur = ffprobe_duration(self.settings, out)
            except Exception:  # noqa: BLE001
                src_dur, out_dur = 0.0, 0.0
            if src_dur <= 0 or out_dur + 2.0 >= src_dur * 0.98:
                log(f"compose already done -> {out} ({out_dur:.1f}s)")
                return out
            log(
                f"compose marked done but out truncated "
                f"({out_dur:.1f}s < source {src_dur:.1f}s); remux"
            )

        self.state.set_running("compose")
        try:
            stage("compose", f"out={out}")
            segs = load_segments(self.work / "segments.json")
            narration = self.work / "narration.wav"
            if not video.is_file():
                raise RuntimeError("missing source.mp4")
            if not narration.is_file():
                raise RuntimeError("missing narration.wav")
            write_srt(segs, self.work / "en_merged.srt", "en")
            write_srt(segs, self.work / "zh.srt", "zh")
            ass_path = self.work / "zh.ass"
            write_zh_ass(segs, ass_path)
            mux_video(self.settings, video, narration, out, ass_path, original_volume=0.0)
            size_mb = out.stat().st_size / (1024 * 1024)
            out_dur = ffprobe_duration(self.settings, out)
            stage_done("compose", f"out={out} size={size_mb:.1f}MB dur={out_dur:.1f}s")
            self.state.set_done(
                "compose",
                out=str(out),
                size_mb=round(size_mb, 1),
                duration=round(out_dur, 3),
            )
            return out
        except Exception as e:  # noqa: BLE001
            stage_error("compose", str(e))
            self.state.set_failed("compose", str(e), retryable=True)
            raise

    def _final_out_path(self) -> Path:
        if self.end > 0:
            return self.work / "out_preview.mp4"
        return self.work / "out.mp4"

    def step_clean(self, *, yes: bool = False) -> Path:
        """
        After user confirms the final video is good: delete everything in the
        work dir except the final out video (out.mp4 or out_preview.mp4).
        """
        out = self._final_out_path()
        alt = self.work / "out.mp4" if out.name != "out.mp4" else self.work / "out_preview.mp4"
        if not out.is_file() and alt.is_file():
            out = alt
        if not out.is_file() or out.stat().st_size < 1000:
            raise RuntimeError(
                f"clean refused: missing final video {out.name}. "
                "Finish compose first, or pass the correct --work."
            )

        try:
            out_dur = ffprobe_duration(self.settings, out)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"clean refused: cannot probe {out}: {e}") from e
        if out_dur < 1.0:
            raise RuntimeError(f"clean refused: {out.name} duration too short ({out_dur:.2f}s)")

        # Prefer keeping full-length out when source still exists
        src = self.work / "source.mp4"
        if not src.is_file():
            src = self.work / "source_full.mp4"
        if src.is_file():
            try:
                src_dur = ffprobe_duration(self.settings, src)
            except Exception:  # noqa: BLE001
                src_dur = 0.0
            if src_dur > 0 and out_dur + 2.0 < src_dur * 0.98:
                raise RuntimeError(
                    f"clean refused: {out.name} looks truncated "
                    f"({out_dur:.1f}s << source {src_dur:.1f}s). Remux first."
                )

        keep_name = out.name
        victims: list[Path] = []
        for child in sorted(self.work.iterdir(), key=lambda p: p.name):
            if child.name == keep_name:
                continue
            # never leave the work dir itself
            victims.append(child)

        freed = 0
        for p in victims:
            try:
                if p.is_file() or p.is_symlink():
                    freed += p.stat().st_size
                elif p.is_dir():
                    freed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            except OSError:
                pass

        stage(
            "clean",
            f"keep={keep_name} remove={len(victims)} ~{freed/1024/1024:.0f}MB "
            f"out_dur={out_dur:.1f}s",
        )
        if not victims:
            log("nothing to clean")
            stage_done("clean", "already clean")
            return out

        if not yes:
            for p in victims[:30]:
                kind = "dir" if p.is_dir() else "file"
                log(f"  will remove [{kind}] {p.name}")
            if len(victims) > 30:
                log(f"  ... and {len(victims)-30} more")
            log("dry-run only; nothing deleted")
            log(
                f"confirm out.mp4 is good, then run:\n"
                f"  python job_run.py --work {self.work} --mode clean --yes"
            )
            stage_done("clean", "dry-run")
            return out

        removed = 0
        for p in victims:
            try:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)
                removed += 1
                log(f"removed {p.name}")
            except OSError as e:
                log(f"skip {p.name}: {e}")

        stage_done(
            "clean",
            f"kept={keep_name} removed={removed} freed~{freed/1024/1024:.0f}MB",
        )
        log(f"kept: {out} ({out_dur:.1f}s, {out.stat().st_size/1024/1024:.1f}MB)")
        return out

    def run(
        self,
        *,
        url: str | None = None,
        mode: str = "all",
        resume: bool = True,
        force_from: str | None = None,
        clean_yes: bool = False,
    ) -> Path | None:
        """
        mode: all | prepare | tts-mux | translate | tts | mux | status | clean
        """
        stage("job-start", f"mode={mode} end={self.end} voice={self.voice}")
        if force_from:
            self.state.mark_pending_from(force_from)
            log(f"force from stage: {force_from}")

        if url:
            # bind work if empty id folder created by caller
            self.state.update_meta(url=url)

        # download only when url provided and needed
        if url and mode in {"all", "prepare", "download"}:
            if not resume or not self.state.is_done("download"):
                self.ensure_media_from_url(url)
            else:
                log("skip download (resume)")

        # always need local media for later steps
        need_media = mode in {"all", "prepare", "tts-mux", "tts", "mux", "translate"}
        if need_media:
            if not ((self.work / "source.mp4").is_file() or (self.work / "source_full.mp4").is_file()):
                if url:
                    self.ensure_media_from_url(url)
                else:
                    raise RuntimeError(f"no media in {self.work}")

        def do_prepare() -> list[Segment]:
            self.step_prepare_video()
            cues = self.step_prepare_cues()
            segs = self.step_merge(cues)
            segs = self.step_translate(segs)
            return segs

        def do_tts_mux() -> Path:
            self.step_prepare_video()
            self.step_tts()
            self.step_narration()
            return self.step_compose()

        out: Path | None = None
        try:
            if mode == "status":
                self.print_status()
                return None
            if mode == "clean":
                return self.step_clean(yes=clean_yes)
            if mode == "download":
                if not url:
                    raise RuntimeError("--url required for download")
                self.ensure_media_from_url(url)
                self.step_prepare_video()
            elif mode == "prepare":
                do_prepare()
            elif mode == "translate":
                self.step_prepare_video()
                if not self.state.is_done("merge"):
                    cues = self.step_prepare_cues()
                    self.step_merge(cues)
                self.step_translate()
            elif mode == "tts":
                self.step_prepare_video()
                self.step_tts()
            elif mode == "mux":
                self.step_prepare_video()
                self.step_narration()
                out = self.step_compose()
            elif mode == "tts-mux":
                out = do_tts_mux()
            elif mode == "all":
                # resume-aware full run
                nxt = self.state.next_pending() if resume else "download"
                log(f"resume pointer -> {nxt}")
                if url and (not resume or not self.state.is_done("download")):
                    self.ensure_media_from_url(url)
                self.step_prepare_video()
                if not self.state.is_done("prepare_cues"):
                    self.step_prepare_cues()
                if not self.state.is_done("merge"):
                    self.step_merge()
                if not self.state.is_done("translate"):
                    self.step_translate()
                if not self.state.is_done("tts"):
                    self.step_tts()
                if not self.state.is_done("narration"):
                    self.step_narration()
                if not self.state.is_done("compose"):
                    out = self.step_compose()
                else:
                    out = self.work / ("out_preview.mp4" if self.end > 0 else "out.mp4")
            else:
                raise SystemExit(f"unknown mode: {mode}")

            if self.state.next_pending() is None:
                self.state.data["status"] = "done"
                self.state.save()
            stage_done("job-start", f"status={self.state.data['status']}")
            self.print_status()
            return out
        except Exception as e:  # noqa: BLE001
            stage_error("job", str(e))
            log(f"STATE:  {self.state.path}")
            log(f"RESUME: python job_run.py --work {self.work} --resume")
            raise


def resolve_work_dir(settings: Settings, work: str | None, url: str | None) -> Path:
    if work:
        p = Path(work)
        if not p.is_absolute():
            p = (settings.root / p).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    if not url:
        raise SystemExit("Need --url or --work")
    stage("resolve-id", url)
    vid = resolve_video_id(settings, url)
    p = (settings.workdir / vid).resolve()
    p.mkdir(parents=True, exist_ok=True)
    stage_done("resolve-id", f"id={vid} work={p}")
    return p
