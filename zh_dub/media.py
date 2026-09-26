from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .config import Settings
from .logutil import detail, highlight, info, ok, warn


def run_cmd(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    detail("$ " + " ".join(str(c) for c in cmd[:10]) + (" ..." if len(cmd) > 10 else ""))
    return subprocess.run(cmd, check=True, **kwargs)


def clear_proxy_env() -> None:
    for k in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
    ):
        os.environ.pop(k, None)


def with_proxy_env(proxy: str) -> dict[str, str]:
    env = os.environ.copy()
    env["http_proxy"] = proxy
    env["https_proxy"] = proxy
    env["ALL_PROXY"] = proxy
    return env


def ffprobe_duration(settings: Settings, path: Path) -> float:
    cp = subprocess.run(
        [
            settings.ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(cp.stdout.strip())


def ffprobe_wh(settings: Settings, path: Path) -> str:
    cp = subprocess.run(
        [
            settings.ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return (cp.stdout or "").strip()


def ffprobe_fps(settings: Settings, path: Path) -> float:
    """Best-effort average/real fps; fallback 30."""
    cp = subprocess.run(
        [
            settings.ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    for line in (cp.stdout or "").splitlines():
        s = line.strip()
        if not s or s == "0/0":
            continue
        try:
            if "/" in s:
                a, b = s.split("/", 1)
                den = float(b)
                if den == 0:
                    continue
                fps = float(a) / den
            else:
                fps = float(s)
            if 1.0 <= fps <= 120.0:
                return fps
        except ValueError:
            continue
    return 30.0


def yt_dlp_format(quality: str) -> str:
    if quality == "best":
        return "bv*+ba/b"
    return (
        f"bv*[height={quality}]+ba/b[height={quality}]/"
        f"bv*[height<={quality}]+ba/b[height<={quality}]/"
        f"bv*+ba/b"
    )


def download_video(settings: Settings, url: str, work: Path) -> dict:
    info(f"下载视频  quality={settings.quality}")
    work.mkdir(parents=True, exist_ok=True)
    fmt = yt_dlp_format(settings.quality)
    env = with_proxy_env(settings.proxy)
    full = work / "source_full.mp4"
    info(f"yt-dlp format={fmt}")
    run_cmd(
        [
            settings.yt_dlp,
            "-f",
            fmt,
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "-o",
            str(work / "source_full.%(ext)s"),
            url,
        ],
        env=env,
    )
    if not full.is_file():
        raise RuntimeError(f"download finished but missing {full}")
    wh = ffprobe_wh(settings, full)
    size_mb = full.stat().st_size / (1024 * 1024)
    highlight(f"视频已下载  {wh or '?'}  {size_mb:.1f}MB")
    return {"res": wh, "size_mb": round(size_mb, 1)}


def fetch_video_meta(settings: Settings, url: str) -> dict:
    """Resolve id/title/duration without downloading media."""
    env = with_proxy_env(settings.proxy)
    cp = subprocess.run(
        [
            settings.yt_dlp,
            "--print",
            "%(id)s\t%(title)s\t%(duration)s",
            "--skip-download",
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    line = ""
    for raw in (cp.stdout or "").splitlines():
        if raw.strip():
            line = raw.strip()
            break
    parts = line.split("\t")
    vid = (parts[0] if parts else "").strip()
    title = (parts[1] if len(parts) > 1 else "").strip()
    dur_raw = (parts[2] if len(parts) > 2 else "").strip()
    duration = 0
    try:
        if dur_raw and dur_raw not in {"NA", "None"}:
            duration = int(float(dur_raw))
    except ValueError:
        duration = 0
    if not vid:
        raise RuntimeError("failed to resolve youtube id")
    if not title or title in {"NA", "None"}:
        title = vid
        warn("yt-dlp 未返回标题，稍后文件名将回退到视频 ID")
    return {"id": vid, "title_en": title, "duration": duration}




def prepare_source_video(settings: Settings, work: Path, end: float) -> dict:
    """source_full.mp4 -> source.mp4: hardlink for full length, re-encode cut for preview."""
    full = work / "source_full.mp4"
    src = work / "source.mp4"
    if not full.is_file():
        raise RuntimeError(f"missing source_full.mp4 in {work}")
    src.unlink(missing_ok=True)

    if end <= 0:
        try:
            os.link(full, src)
            how = "link"
        except OSError:
            shutil.copy2(full, src)
            how = "copy"
        ok(f"视频准备完成  mode=full ({how})")
        return {"mode": "full", "end": 0.0}

    info(f"预览裁剪 0..{end}s -> source.mp4")
    run_cmd(
        [
            settings.ffmpeg, "-y", "-ss", "0", "-t", str(end), "-i", str(full),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", str(src),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ok(f"视频准备完成  mode=preview end={end}")
    return {"mode": "preview", "end": float(end)}
