"""Final mux: burn subtitles, replace audio with narration, optional cover intro."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .config import Settings
from .logutil import highlight, info, warn
from .media import ffprobe_duration, ffprobe_fps, ffprobe_wh

COVER_SECONDS = 1.0


def mux_video(
    settings: Settings,
    video: Path,
    narration: Path,
    out: Path,
    ass_path: Path | None,
    cover_image: Path | None = None,
) -> None:
    """ass_path must live next to out: ffmpeg runs there and gets a bare filename,
    which sidesteps filtergraph escaping of absolute paths."""
    video = video.resolve()
    narration = narration.resolve()
    out = out.resolve()
    workdir = out.parent
    # pin duration to source video so a shorter leftover out/partial encode cannot win
    video_dur = ffprobe_duration(settings, video)
    nar_dur = ffprobe_duration(settings, narration)
    info(f"合成输入  video={video_dur:.1f}s  narration={nar_dur:.1f}s")
    if nar_dur + 1.0 < video_dur * 0.95:
        raise RuntimeError(
            f"narration shorter than video ({nar_dur:.1f}s < {video_dur:.1f}s); "
            "rebuild narration first"
        )

    cover: Path | None = None
    if cover_image is not None:
        if cover_image.is_file():
            cover = cover_image
            info(f"片头封面  {cover}  +{COVER_SECONDS:.0f}s")
        else:
            warn(f"COVER_IMAGE 不存在，跳过封面: {cover_image}")

    local_ass = ass_path if ass_path is not None and ass_path.is_file() else None

    if cover is None:
        cmd: list[str] = [
            settings.ffmpeg,
            "-y",
            "-i",
            str(video),
            "-i",
            str(narration),
        ]
        cmd += ["-map", "0:v", "-map", "1:a"]
        if local_ass is not None:
            cmd += [
                "-vf",
                f"ass={local_ass.name}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
            ]
        else:
            cmd += ["-c:v", "copy"]
        expected_dur = video_dur
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            f"{expected_dur:.3f}",
            str(out),
        ]
    else:
        wh = ffprobe_wh(settings, video) or "1280x720"
        try:
            w_s, h_s = wh.lower().split("x", 1)
            width, height = int(w_s), int(h_s)
        except ValueError:
            width, height = 1280, 720
        fps = ffprobe_fps(settings, video)
        fps_s = f"{fps:.3f}".rstrip("0").rstrip(".")
        # 0=cover still, 1=source video, 2=narration
        if local_ass is not None:
            main_v = (
                f"[1:v]ass={local_ass.name},fps={fps_s},format=yuv420p,"
                f"setsar=1,setpts=PTS-STARTPTS[mainv]"
            )
        else:
            main_v = (
                f"[1:v]fps={fps_s},format=yuv420p,setsar=1,setpts=PTS-STARTPTS[mainv]"
            )
        fc = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"fps={fps_s},format=yuv420p,setpts=PTS-STARTPTS[cover];"
            f"{main_v};"
            f"[cover][mainv]concat=n=2:v=1:a=0[vout];"
            f"anullsrc=channel_layout=mono:sample_rate=24000,"
            f"atrim=0:{COVER_SECONDS:.3f},asetpts=PTS-STARTPTS[asil];"
            f"[2:a]aformat=sample_fmts=fltp:sample_rates=24000:"
            f"channel_layouts=mono,asetpts=PTS-STARTPTS[anar];"
            f"[asil][anar]concat=n=2:v=0:a=1[aout]"
        )
        expected_dur = video_dur + COVER_SECONDS
        cmd = [
            settings.ffmpeg,
            "-y",
            "-loop",
            "1",
            "-t",
            f"{COVER_SECONDS:.3f}",
            "-i",
            str(cover),
            "-i",
            str(video),
            "-i",
            str(narration),
            "-filter_complex",
            fc,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            f"{expected_dur:.3f}",
            str(out),
        ]

    info(f"ffmpeg mux -> {out.name}")
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
    highlight(f"合成完成  {time.time()-t0:.1f}s  out={out_dur:.1f}s  -> {out.name}")
    if out_dur + 2.0 < expected_dur * 0.98:
        raise RuntimeError(
            f"mux output truncated: out={out_dur:.1f}s expected={expected_dur:.1f}s"
        )
