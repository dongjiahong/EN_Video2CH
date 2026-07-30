from __future__ import annotations

import asyncio
import os
import subprocess
import wave
from pathlib import Path

from .config import Settings
from .logutil import log, stage, stage_done


def run_cmd(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    log("+ " + " ".join(str(c) for c in cmd[:10]) + (" ..." if len(cmd) > 10 else ""))
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


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def yt_dlp_format(quality: str) -> str:
    if quality == "best":
        return "bv*+ba/b"
    return (
        f"bv*[height={quality}]+ba/b[height={quality}]/"
        f"bv*[height<={quality}]+ba/b[height<={quality}]/"
        f"bv*+ba/b"
    )


def download_video(settings: Settings, url: str, work: Path) -> dict:
    stage("download-video", f"quality={settings.quality}")
    work.mkdir(parents=True, exist_ok=True)
    fmt = yt_dlp_format(settings.quality)
    env = with_proxy_env(settings.proxy)
    full = work / "source_full.mp4"
    if full.is_file() or (work / "source.mp4").is_file():
        log("local video exists; skip download")
        stage_done("download-video", "skipped")
        return {"skipped": True, "file": str(full if full.is_file() else work / "source.mp4")}

    log(f"format={fmt}")
    run_cmd(
        [
            settings.yt_dlp,
            "-f",
            fmt,
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "--write-auto-sub",
            "--sub-lang",
            "en",
            "--sub-format",
            "vtt",
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
    log(f"resolved {wh or '?'} size={size_mb:.1f}MB")
    stage_done("download-video", f"res={wh} size={size_mb:.1f}MB")
    return {"skipped": False, "file": str(full), "res": wh, "size_mb": round(size_mb, 1)}


def download_subs(settings: Settings, url: str, work: Path) -> dict:
    stage("download-subs")
    target_vtt = work / "source.en.vtt"
    target_srt = work / "source.en.srt"
    if target_vtt.is_file() or target_srt.is_file():
        log("local subtitle exists; skip")
        stage_done("download-subs", "skipped")
        return {"skipped": True}

    full_vtt = work / "source_full.en.vtt"
    if full_vtt.is_file():
        target_vtt.write_bytes(full_vtt.read_bytes())
        stage_done("download-subs", "copied source_full.en.vtt")
        return {"skipped": False, "file": str(target_vtt)}

    env = with_proxy_env(settings.proxy)
    run_cmd(
        [
            settings.yt_dlp,
            "--skip-download",
            "--write-auto-sub",
            "--sub-lang",
            "en",
            "--sub-format",
            "vtt",
            "-o",
            str(work / "source.%(ext)s"),
            url,
        ],
        env=env,
    )
    if not target_vtt.is_file() and not target_srt.is_file():
        raise RuntimeError("subtitle download failed")
    stage_done("download-subs", "done")
    return {"skipped": False, "file": str(target_vtt if target_vtt.is_file() else target_srt)}


def resolve_video_id(settings: Settings, url: str) -> str:
    env = with_proxy_env(settings.proxy)
    cp = subprocess.run(
        [settings.yt_dlp, "--print", "%(id)s", "--skip-download", url],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    vid = (cp.stdout or "").strip().splitlines()[0].strip()
    if not vid:
        raise RuntimeError("failed to resolve youtube id")
    return vid


def prepare_source_video(settings: Settings, work: Path, end: float) -> dict:
    stage("prepare-video")
    full = work / "source_full.mp4"
    src = work / "source.mp4"
    if not full.is_file() and not src.is_file():
        raise RuntimeError(f"missing source.mp4 / source_full.mp4 in {work}")

    if end <= 0:
        if full.is_file():
            need = True
            if src.is_file():
                if src.stat().st_size >= full.stat().st_size * 0.9:
                    need = False
            if need:
                log("full copy source_full.mp4 -> source.mp4")
                run_cmd(
                    [settings.ffmpeg, "-y", "-i", str(full), "-c", "copy", str(src)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                log("source.mp4 already full-length")
        stage_done("prepare-video", "full")
        return {"mode": "full", "file": str(src)}

    if not full.is_file():
        raise RuntimeError("preview cut requires source_full.mp4")
    log(f"preview cut 0..{end}s -> source.mp4")
    run_cmd(
        [
            settings.ffmpeg,
            "-y",
            "-ss",
            "0",
            "-t",
            str(end),
            "-i",
            str(full),
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
            str(src),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stage_done("prepare-video", f"preview end={end}")
    return {"mode": "preview", "end": end, "file": str(src)}


def mp3_to_wav(settings: Settings, mp3: Path, wav: Path) -> None:
    subprocess.run(
        [settings.ffmpeg, "-y", "-i", str(mp3), "-ac", "1", "-ar", "24000", str(wav)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def synthesize_mp3_async(
    text: str,
    out_mp3: Path,
    voice: str,
    rate_pct: int,
    *,
    retries: int = 3,
) -> None:
    """Synthesize via edge-tts library (no CLI subprocess)."""
    import edge_tts

    rate = f"{rate_pct:+d}%"
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    clear_proxy_env()
    last_err: BaseException | None = None
    for attempt in range(max(1, retries)):
        try:
            if out_mp3.is_file():
                out_mp3.unlink()
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            await communicate.save(str(out_mp3))
            if out_mp3.is_file() and out_mp3.stat().st_size >= 500:
                return
            last_err = RuntimeError(f"tts empty output: {out_mp3}")
        except BaseException as e:  # noqa: BLE001
            last_err = e
        if attempt + 1 < retries:
            await asyncio.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"tts failed after {retries} tries: {last_err}")


def synthesize_mp3(
    settings: Settings, text: str, out_mp3: Path, voice: str, rate_pct: int
) -> None:
    """Sync wrapper; prefer synthesize_mp3_async inside async TTS pipeline."""
    del settings  # library path; CLI binary kept on Settings for env/tool checks
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(synthesize_mp3_async(text, out_mp3, voice, rate_pct))
        return
    raise RuntimeError("synthesize_mp3() called inside running loop; use synthesize_mp3_async")
