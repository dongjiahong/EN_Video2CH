"""Delete every intermediate in a work dir, keeping only the final video."""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import Settings
from .logutil import detail, highlight, info, skip, stage, stage_done, warn
from .media import ffprobe_duration


def _size(p: Path) -> int:
    try:
        if p.is_dir() and not p.is_symlink():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size
    except OSError:
        return 0


def clean_work(settings: Settings, work: Path, out: Path, *, yes: bool = False) -> Path:
    alt = work / ("out.mp4" if out.name != "out.mp4" else "out_preview.mp4")
    if not out.is_file() and alt.is_file():
        out = alt
    if not out.is_file() or out.stat().st_size < 1000:
        raise RuntimeError(
            f"clean refused: missing final video {out.name}. "
            "Finish compose first, or pass the correct --work."
        )
    try:
        out_dur = ffprobe_duration(settings, out)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"clean refused: cannot probe {out}: {e}") from e
    if out_dur < 1.0:
        raise RuntimeError(f"clean refused: {out.name} duration too short ({out_dur:.2f}s)")

    src = work / "source.mp4"
    if src.is_file():
        try:
            src_dur = ffprobe_duration(settings, src)
        except Exception:  # noqa: BLE001
            src_dur = 0.0
        if src_dur > 0 and out_dur + 2.0 < src_dur * 0.98:
            raise RuntimeError(
                f"clean refused: {out.name} looks truncated "
                f"({out_dur:.1f}s << source {src_dur:.1f}s). Remux first."
            )

    victims = sorted((p for p in work.iterdir() if p.name != out.name), key=lambda p: p.name)
    freed = sum(_size(p) for p in victims)
    stage(
        "clean",
        f"keep={out.name} remove={len(victims)} ~{freed/1024/1024:.0f}MB out_dur={out_dur:.1f}s",
    )
    if not victims:
        skip("没有可清理内容")
        stage_done("clean", "already clean")
        return out

    if not yes:
        for p in victims[:30]:
            detail(f"will remove [{'dir' if p.is_dir() else 'file'}] {p.name}")
        if len(victims) > 30:
            detail(f"... and {len(victims)-30} more")
        warn("dry-run：未删除任何文件")
        info(f"确认 {out.name} 无误后执行:\n  python job_run.py --work {work} --clean --yes")
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
            detail(f"removed {p.name}")
        except OSError as e:
            warn(f"skip {p.name}: {e}")

    stage_done("clean", f"kept={out.name} removed={removed} freed~{freed/1024/1024:.0f}MB")
    highlight(f"保留 {out.name}  ({out_dur:.1f}s, {out.stat().st_size/1024/1024:.1f}MB)")
    return out
