"""Copy finished video into --output using a translated title."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .config import Settings
from .logutil import detail, highlight, info, skip, warn
from .state import JobState
from .translate import translate_title

UNSAFE_FS = re.compile(r'[\\/:*?"<>|\n\r\t]')
PLACEHOLDER_TITLES = {"", "NA", "None", "null"}


def looks_like_video_id(text: str, video_id: str) -> bool:
    t = (text or "").strip()
    return not t or t == video_id or t in PLACEHOLDER_TITLES


def safe_stem(title: str, video_id: str, *, max_len: int = 80) -> str:
    t = UNSAFE_FS.sub(" ", title or video_id).strip()
    t = re.sub(r"\s+", " ", t).strip(" .") or video_id
    t = t[:max_len].rstrip(" .") or video_id
    return f"{t} [{video_id}]"


def export_filename(title: str, video_id: str, *, preview: bool = False) -> str:
    stem = safe_stem(title, video_id)
    if preview:
        return f"{stem}.preview.mp4"
    return f"{stem}.mp4"


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _copy_or_link(src: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        os.link(src, dest)
        return "link"
    except OSError:
        shutil.copy2(src, dest)
        return "copy"


def publish_output(
    settings: Settings,
    state: JobState,
    src: Path,
    *,
    output_dir: Path | None,
    preview: bool = False,
) -> Path | None:
    """Copy/link src into output_dir. Returns dest path, or None if disabled."""
    del settings
    if output_dir is None:
        return None
    if not src.is_file() or src.stat().st_size < 1000:
        warn(f"跳过导出：成片不存在或过小  {src}")
        return None

    meta = state.data.setdefault("meta", {})
    video_id = (
        str(meta.get("video_id") or "").strip()
        or state.work.name
    )
    title_zh = str(meta.get("title_zh") or "").strip()
    title_en = str(meta.get("title_en") or "").strip()
    title = title_zh or title_en or video_id
    name = export_filename(title, video_id, preview=preview)
    dest = output_dir / name
    if _same_file(src, dest):
        meta["output_file"] = str(dest)
        state.save()
        return dest

    if dest.is_file() and dest.stat().st_size == src.stat().st_size and not _same_file(src, dest):
        skip(f"导出已存在  {dest.name}")
        meta["output_file"] = str(dest)
        state.save()
        return dest

    how = _copy_or_link(src, dest)
    meta["output_file"] = str(dest)
    state.save()
    size_mb = dest.stat().st_size / (1024 * 1024)
    highlight(f"已导出 ({how})  {dest}  {size_mb:.1f}MB")
    return dest


def ensure_titles(
    settings: Settings,
    state: JobState,
    *,
    title_en: str | None = None,
    video_id: str | None = None,
) -> None:
    """Fill title_en / title_zh on job meta. Title translate failure is non-fatal."""
    meta = state.data.setdefault("meta", {})
    vid = (video_id or meta.get("video_id") or state.work.name or "").strip()
    if vid and not meta.get("video_id"):
        meta["video_id"] = vid

    en = (title_en or meta.get("title_en") or "").strip()
    if looks_like_video_id(en, vid):
        en = ""
    if en:
        meta["title_en"] = en

    zh = str(meta.get("title_zh") or "").strip()
    if zh and not looks_like_video_id(zh, vid):
        state.save()
        return

    source = str(meta.get("title_en") or "").strip()
    if looks_like_video_id(source, vid):
        warn("无可用英文标题，导出文件名将使用视频 ID")
        state.save()
        return

    try:
        zh = translate_title(settings, source)
    except Exception as e:  # noqa: BLE001
        warn(f"标题翻译失败，导出用英文标题: {e}")
        zh = source
    if looks_like_video_id(zh, vid):
        zh = source
    meta["title_zh"] = zh
    info(f"标题  EN: {source}")
    detail(f"标题  ZH: {zh}")
    state.save()
