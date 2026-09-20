"""YouTube URL helpers: single video vs playlist, metadata fetch."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .logutil import detail, highlight, info, warn
from .media import with_proxy_env


WATCH_TMPL = "https://www.youtube.com/watch?v={id}"


def is_playlist_url(url: str) -> bool:
    """True for playlist pages. watch?v=ID&list=... stays a single video."""
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    if path.endswith("/playlist") or "/playlist/" in path:
        return True
    # youtu.be/<id> is always a single video, even with ?list=
    if "youtu.be" in host:
        return False
    qs = parse_qs(parsed.query)
    if "v" in qs:
        return False
    if "list" in qs:
        return True
    return False


def watch_url(video_id: str) -> str:
    return WATCH_TMPL.format(id=video_id)


def fetch_playlist(
    settings: Settings, url: str, *, limit: int = 0
) -> dict[str, Any]:
    """Expand a playlist into watch URLs. limit<=0 means the whole list."""
    env = with_proxy_env(settings.proxy)
    cmd = [
        settings.yt_dlp,
        "--flat-playlist",
        "--print",
        "%(playlist_title)s\t%(playlist_index)s\t%(id)s\t%(title)s\t%(duration)s",
    ]
    if limit > 0:
        cmd += ["--playlist-end", str(limit)]
    cmd.append(url)
    detail("yt-dlp --flat-playlist ...")
    cp = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    playlist_title = ""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in (cp.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            warn(f"跳过无法解析的 playlist 行: {line[:120]}")
            continue
        pl_title = parts[0].strip()
        index_raw = parts[1].strip()
        vid = parts[2].strip()
        title_en = parts[3].strip() if len(parts) > 3 else ""
        dur_raw = parts[4].strip() if len(parts) > 4 else ""
        if not vid or vid in {"NA", "None"}:
            continue
        if vid in seen:
            continue
        seen.add(vid)
        playlist_title = playlist_title or pl_title
        try:
            index = int(index_raw) if index_raw.isdigit() else len(items) + 1
        except ValueError:
            index = len(items) + 1
        duration = 0
        try:
            if dur_raw and dur_raw not in {"NA", "None"}:
                duration = int(float(dur_raw))
        except ValueError:
            duration = 0
        items.append(
            {
                "id": vid,
                "index": index,
                "title_en": title_en if title_en not in {"NA", "None", ""} else vid,
                "duration": duration,
                "url": watch_url(vid),
            }
        )
        if limit > 0 and len(items) >= limit:
            break

    if not items:
        raise RuntimeError(f"playlist is empty or could not be expanded: {url}")

    info(f"播放列表  {playlist_title or '(无标题)'}  共 {len(items)} 条")
    highlight(f"将逐条处理 {len(items)} 个视频")
    return {"title": playlist_title, "items": items, "url": url}


def resolve_output_dir(settings: Settings, cli_output: str | None) -> Path | None:
    raw = (cli_output or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (settings.root / p).resolve()
        else:
            p = p.resolve()
        return p
    return settings.output_dir
