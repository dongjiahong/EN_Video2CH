"""The seven pipeline stages.

Each stage only knows its own inputs, outputs and validity rule; sequencing,
state bookkeeping and error handling belong to ``runner.Runner``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .asr import transcribe_to_srt
from .compose import COVER_SECONDS, mux_video
from .export import ensure_titles
from .logutil import highlight, info, progress, warn
from .media import (
    clear_proxy_env,
    download_video,
    fetch_video_meta,
    ffprobe_duration,
    prepare_source_video,
)
from .narration import build_narration
from .runner import Job, Stage, newer_than
from .segmenter import build_segments
from .segments import Segment, load_segments, save_segments, write_srt, write_zh_ass
from .translate import translate_segments
from .tts import fit_segments, validate_tts

WATCH_TMPL = "https://www.youtube.com/watch?v={id}"


def _cover(job: Job) -> Path | None:
    """Configured intro cover, or None when unset/missing."""
    c = job.settings.cover_image
    return c if c is not None and c.is_file() else None


def _audio_mtime(job: Job) -> float:
    audio = job.path("audio")
    if not audio.is_dir():
        return 0.0
    return max((p.stat().st_mtime for p in audio.glob("seg_*.mp3")), default=0.0)


class DownloadStage(Stage):
    """Get the video from YouTube (if a URL is known) and materialize source.mp4."""

    name = "download"

    def is_fresh(self, job: Job) -> bool:
        src = job.path("source.mp4")
        if not src.is_file() or src.stat().st_size < 1:
            return False
        detail = job.state.stage_detail("download")
        if bool(detail.get("preview")) != job.preview:
            return False
        if not job.preview:
            return job.path("source_full.mp4").is_file()
        try:
            return abs(ffprobe_duration(job.settings, src) - job.end) <= 1.5
        except Exception:  # noqa: BLE001
            return False

    def run(self, job: Job) -> dict[str, Any]:
        url = job.url or str(job.state.meta.get("url") or "")
        full = job.path("source_full.mp4")
        if not full.is_file():
            if not url:
                raise RuntimeError(f"{job.work} 里没有 source_full.mp4，请用 --url 指定视频")
            self._resolve_meta(job, url)
            clear_proxy_env()  # only yt-dlp uses the explicit env proxy
            download_video(job.settings, url, job.work)
        elif not str(job.state.meta.get("title_en") or "").strip():
            self._resolve_meta(job, "")
        prepared = prepare_source_video(job.settings, job.work, job.end)
        return {"preview": job.preview, "end": job.end, **prepared}

    def _resolve_meta(self, job: Job, url: str) -> None:
        """Resolve EN title once for export naming; a failure is not fatal."""
        meta = job.state.meta
        if str(meta.get("title_en") or "").strip():
            return
        target = url or (WATCH_TMPL.format(id=job.work.name) if job.work.name else "")
        if not target:
            ensure_titles(job.settings, job.state, video_id=job.work.name)
            return
        try:
            found = fetch_video_meta(job.settings, target)
        except Exception as e:  # noqa: BLE001
            warn(f"获取标题失败，导出将用视频 ID 命名: {e}")
            ensure_titles(job.settings, job.state, video_id=job.work.name)
            return
        job.state.update_meta(video_id=found["id"], duration=found["duration"])
        ensure_titles(
            job.settings,
            job.state,
            title_en=found["title_en"],
            video_id=found["id"],
        )


class TranscribeStage(Stage):
    """Local parakeet-mlx transcription of source.mp4 -> asr.srt."""

    name = "transcribe"

    def is_fresh(self, job: Job) -> bool:
        return newer_than(job.path("asr.srt"), job.path("source.mp4"))

    def run(self, job: Job) -> dict[str, Any]:
        _out, cues = transcribe_to_srt(job.settings, job.work)
        return {"cues": cues}


class SegmentStage(Stage):
    """asr.srt -> segments.json (+ en_merged.srt): sentence-packed TTS units."""

    name = "segment"

    def is_fresh(self, job: Job) -> bool:
        path = job.path("segments.json")
        if not newer_than(path, job.path("asr.srt")):
            return False
        try:
            return bool(load_segments(path))
        except Exception:  # noqa: BLE001
            return False

    def run(self, job: Job) -> dict[str, Any]:
        segs = build_segments(job.path("asr.srt"), t1=job.end or None)
        if not segs:
            raise RuntimeError("断句结果为空，检查 asr.srt")
        save_segments(segs, job.path("segments.json"))
        write_srt(segs, job.path("en_merged.srt"), "en")
        durs = [s.end - s.start for s in segs] or [0.0]
        avg, longest = sum(durs) / len(durs), max(durs)
        highlight(f"断句完成  segments={len(segs)}  avg={avg:.1f}s  max={longest:.1f}s")
        return {"segments": len(segs), "avg_s": round(avg, 2), "max_s": round(longest, 2)}


class TranslateStage(Stage):
    """Fill seg.zh from the en->zh cache, translating whatever is missing."""

    name = "translate"

    def is_fresh(self, job: Job) -> bool:
        path = job.path("segments.json")
        if not path.is_file():
            return False
        try:
            segs = load_segments(path)
        except Exception:  # noqa: BLE001
            return False
        # lines the model could not translate keep zh="" and are retried next run
        return bool(segs) and not [s for s in segs if s.en.strip() and not s.zh.strip()]

    def run(self, job: Job) -> dict[str, Any]:
        path = job.path("segments.json")
        segs = load_segments(path)
        report = translate_segments(
            job.settings,
            job.state,
            segs,
            title_en=str(job.state.meta.get("title_en") or ""),
        )
        save_segments(segs, path)
        write_srt(segs, job.path("zh.srt"), "zh")
        return report


class TtsStage(Stage):
    """Synthesize one duration-fitted mp3 per segment, keyed for cache reuse."""

    name = "tts"

    def is_fresh(self, job: Job) -> bool:
        path = job.path("segments.json")
        if not path.is_file():
            return False
        try:
            segs = load_segments(path)
        except Exception:  # noqa: BLE001
            return False
        report = validate_tts(segs, job.path("audio"), job.voice, job.settings.tts_max_rate)
        return bool(report["pass"] and report["need"])

    def run(self, job: Job) -> dict[str, Any]:
        path = job.path("segments.json")
        segs = load_segments(path)
        audio_dir = job.path("audio")
        total = len(segs)
        info(
            f"TTS 开始  segments={total}  voice={job.voice}  "
            f"并发={job.settings.tts_concurrency}  max_rate=+{job.settings.tts_max_rate}%"
        )

        def on_progress(done: int, count: int, seg: Segment) -> None:
            if seg.note == "cached" and done % 50 and done != count:
                return
            progress(done, count, f"idx={seg.idx:04d} {seg.tts_dur:.2f}s {seg.note}")
            # persist keys as we go so an interrupted run only redoes the rest
            if done % 20 == 0 or done == count:
                save_segments(segs, path)
                job.state.set_progress("tts", progress=f"{done}/{count}")

        fit_segments(job.settings, segs, audio_dir, job.voice, on_progress)
        save_segments(segs, path)
        write_srt(segs, job.path("zh.srt"), "zh")

        report = validate_tts(segs, audio_dir, job.voice, job.settings.tts_max_rate)
        highlight(
            f"TTS 校验  audio_ok={report['audio_ok']}/{report['need']}  "
            f"missing={len(report['audio_missing'])}  overflow={report['overflow']}"
        )
        if not report["pass"]:
            raise RuntimeError(
                f"TTS 不完整，缺失 {len(report['audio_missing'])} 段音频: "
                f"{report['audio_missing'][:20]}"
            )
        return {
            "audio_ok": report["audio_ok"],
            "overflow": report["overflow"],
            "total": total,
        }


class NarrationStage(Stage):
    """Paste every clip onto a full-length timeline -> narration.wav."""

    name = "narration"

    def is_fresh(self, job: Job) -> bool:
        out = job.path("narration.wav")
        if not out.is_file() or out.stat().st_size < 1000:
            return False
        return newer_than(out, job.path("segments.json"), min_bytes=1000) and (
            _audio_mtime(job) <= out.stat().st_mtime
        )

    def run(self, job: Job) -> dict[str, Any]:
        video = job.path("source.mp4")
        if not video.is_file():
            raise RuntimeError("缺少 source.mp4")
        segs = load_segments(job.path("segments.json"))
        total = ffprobe_duration(job.settings, video)
        placed = build_narration(job.settings, segs, job.path("narration.wav"), total)
        return {"clips": placed, "duration": round(total, 3)}


class ComposeStage(Stage):
    """Burn subtitles, replace the audio track with narration, optional cover."""

    name = "compose"

    def is_fresh(self, job: Job) -> bool:
        out = job.out
        if not out.is_file() or out.stat().st_size < 1000:
            return False
        detail = job.state.stage_detail("compose")
        if str(detail.get("cover") or "") != str(_cover(job) or ""):
            return False
        refs = [job.path("narration.wav"), job.path("segments.json")]
        if any(r.is_file() and r.stat().st_mtime > out.stat().st_mtime for r in refs):
            return False
        return self._duration_ok(job, out)

    def _duration_ok(self, job: Job, out: Path) -> bool:
        """Guard against a leftover partial encode being taken for a final cut."""
        video = job.path("source.mp4")
        if not video.is_file():
            return True
        try:
            src_dur = ffprobe_duration(job.settings, video)
            out_dur = ffprobe_duration(job.settings, out)
        except Exception:  # noqa: BLE001
            return False
        expect = src_dur + (COVER_SECONDS if _cover(job) else 0.0)
        return out_dur + 2.0 >= expect * 0.98

    def run(self, job: Job) -> dict[str, Any]:
        video = job.path("source.mp4")
        narration = job.path("narration.wav")
        if not video.is_file():
            raise RuntimeError("缺少 source.mp4")
        if not narration.is_file():
            raise RuntimeError("缺少 narration.wav")
        segs = load_segments(job.path("segments.json"))
        write_srt(segs, job.path("en_merged.srt"), "en")
        write_srt(segs, job.path("zh.srt"), "zh")
        ass_path = job.path("zh.ass")
        write_zh_ass(segs, ass_path)
        cover = _cover(job)
        info(f"烧录字幕 + 旁白混音  segments={len(segs)}  cover={cover.name if cover else '无'}")
        mux_video(job.settings, video, narration, job.out, ass_path, cover_image=cover)
        size_mb = job.out.stat().st_size / (1024 * 1024)
        out_dur = ffprobe_duration(job.settings, job.out)
        highlight(f"成片输出  {job.out.name}  {size_mb:.1f}MB  {out_dur:.1f}s")
        return {
            "out": job.out.name,
            "size_mb": round(size_mb, 1),
            "duration": round(out_dur, 3),
            "cover": str(cover or ""),
        }


STAGES_MAP: dict[str, Stage] = {
    st.name: st
    for st in (
        DownloadStage(),
        TranscribeStage(),
        SegmentStage(),
        TranslateStage(),
        TtsStage(),
        NarrationStage(),
        ComposeStage(),
    )
}
