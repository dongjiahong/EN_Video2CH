from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any

# Main pipeline order for [n/N] badges (matches state.STAGES).
PIPELINE_STAGES: list[str] = [
    "download",
    "prepare_video",
    "prepare_cues",
    "merge",
    "translate",
    "tts",
    "narration",
    "compose",
]

# Friendly labels shown in stage banners.
STAGE_LABELS: dict[str, str] = {
    "config": "配置",
    "resolve-id": "解析视频 ID",
    "job-start": "任务开始",
    "job-done": "任务完成",
    "job": "任务",
    "status": "状态",
    "clean": "清理",
    "download": "下载视频/字幕",
    "download-video": "下载视频",
    "download-subs": "下载字幕",
    "prepare_video": "准备视频",
    "prepare-video": "准备视频",
    "prepare_cues": "解析字幕",
    "merge": "断句合并",
    "translate": "中文翻译",
    "tts": "语音合成",
    "narration": "旁白时间线",
    "compose": "合成成片",
}


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") in {"1", "true", "yes"}:
        return True
    try:
        return sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


_COLOR = _use_color()


def _c(code: str, text: str) -> str:
    if not _COLOR or not text:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(text: str) -> str:
    return _c("1", text)


def dim(text: str) -> str:
    return _c("2", text)


def green(text: str) -> str:
    return _c("32", text)


def yellow(text: str) -> str:
    return _c("33", text)


def red(text: str) -> str:
    return _c("31", text)


def cyan(text: str) -> str:
    return _c("36", text)


def magenta(text: str) -> str:
    return _c("35", text)


def blue(text: str) -> str:
    return _c("34", text)


def white(text: str) -> str:
    return _c("97", text)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _tag_time() -> str:
    return dim(f"[{_ts()}]")


def _stage_index(name: str) -> tuple[int, int] | None:
    key = name.strip()
    # normalize aliases
    aliases = {
        "prepare-video": "prepare_video",
        "prepare-cues": "prepare_cues",
        "download-video": "download",
        "download-subs": "download",
    }
    key = aliases.get(key, key)
    if key not in PIPELINE_STAGES:
        return None
    return PIPELINE_STAGES.index(key) + 1, len(PIPELINE_STAGES)


def _stage_badge(name: str, index: int | None = None, total: int | None = None) -> str:
    if index is None or total is None:
        found = _stage_index(name)
        if found:
            index, total = found
    if index is not None and total is not None and total > 0:
        return _c("1;36", f"[{index}/{total}]")
    return _c("1;36", "[·]")


def _label(name: str) -> str:
    return STAGE_LABELS.get(name, name)


def log(msg: str) -> None:
    print(f"{_tag_time()} {msg}", flush=True)


def info(msg: str) -> None:
    log(f"{blue('INFO')}  {msg}")


def ok(msg: str) -> None:
    log(f"{green('OK')}    {msg}")


def warn(msg: str) -> None:
    log(f"{yellow('WARN')}  {msg}")


def error(msg: str) -> None:
    log(f"{red('ERROR')} {msg}")


def skip(msg: str) -> None:
    log(f"{dim('SKIP')}  {msg}")


def detail(msg: str) -> None:
    """Secondary line, indented."""
    log(dim(f"       {msg}"))


def keyval(key: str, value: Any) -> None:
    log(f"  {dim(str(key) + ':')} {bold(str(value))}")


def progress(done: int, total: int, msg: str = "") -> None:
    """Inline step progress, e.g. TTS [12/400]."""
    total = max(1, total)
    pct = min(100.0, 100.0 * done / total)
    bar_w = 16
    filled = int(bar_w * done / total)
    bar = "█" * filled + "░" * (bar_w - filled)
    badge = cyan(f"[{done}/{total}]")
    meter = dim(f"{bar} {pct:5.1f}%")
    extra = f" {msg}" if msg else ""
    log(f"{badge} {meter}{extra}")


def rule(char: str = "─", width: int = 56) -> None:
    print(dim(char * width), flush=True)


def stage(name: str, detail: str = "", *, index: int | None = None, total: int | None = None) -> None:
    badge = _stage_badge(name, index, total)
    label = _c("1;97", _label(name))
    code = dim(f"({name})")
    extra = f"  {cyan(detail)}" if detail else ""
    print(flush=True)
    print(f"{_tag_time()} {badge} ▶ STAGE  {label} {code}{extra}", flush=True)
    rule("─")


def stage_done(name: str, detail: str = "", *, index: int | None = None, total: int | None = None) -> None:
    badge = _stage_badge(name, index, total)
    label = green(_label(name))
    extra = f"  {dim(detail)}" if detail else ""
    print(f"{_tag_time()} {badge} {green('✔ DONE')}  {label}{extra}", flush=True)


def stage_error(name: str, detail: str = "", *, index: int | None = None, total: int | None = None) -> None:
    badge = _stage_badge(name, index, total)
    label = red(_label(name))
    extra = f"  {detail}" if detail else ""
    print(f"{_tag_time()} {badge} {red('✖ FAIL')}  {label}{extra}", flush=True)


def highlight(msg: str) -> None:
    """Mark an important milestone / result line."""
    log(f"{magenta('★')} {bold(msg)}")


def resume_hint(work: str | Any) -> None:
    warn(f"失败后续跑: python job_run.py --work {work} --resume")
    detail(f"状态文件: {work}/job_state.json" if not str(work).endswith("job_state.json") else f"状态文件: {work}")


def format_status_line(name: str, status: str, extra: str = "") -> str:
    icon = {
        "done": green("✔"),
        "running": yellow("●"),
        "failed": red("✖"),
        "pending": dim("○"),
    }.get(status, dim("?"))
    st_col = {
        "done": green,
        "running": yellow,
        "failed": red,
        "pending": dim,
    }.get(status, dim)
    idx = _stage_index(name)
    badge = f"[{idx[0]}/{idx[1]}]" if idx else "[·]"
    label = f"{_label(name):12s}"
    return f"  {icon} {dim(badge)} {label} {st_col(f'{status:8s}')}{extra}"
