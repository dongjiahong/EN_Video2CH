from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from .state import STAGES

# Friendly labels shown in stage banners.
STAGE_LABELS: dict[str, str] = {
    "config": "配置",
    "resolve-id": "解析视频 ID",
    "job-start": "任务开始",
    "job-done": "任务完成",
    "job": "任务",
    "status": "状态",
    "clean": "清理",
    "download": "下载与切源",
    "transcribe": "本地转录",
    "segment": "断句分段",
    "translate": "中文翻译",
    "tts": "语音合成",
    "narration": "旁白时间线",
    "compose": "合成成片",
    "playlist": "展开播放列表",
    "batch": "批量任务",
    "batch-item": "批量条目",
}

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_lock = threading.Lock()
_log_dir: Path | None = None
_job: str = ""
_fh: TextIO | None = None
_fh_date: str = ""
_progress_open = False


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") in {"1", "true", "yes"}:
        return True
    try:
        return sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def _is_tty() -> bool:
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


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _stage_index(name: str) -> tuple[int, int] | None:
    key = name.strip()
    if key not in STAGES:
        return None
    return STAGES.index(key) + 1, len(STAGES)


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


def _close_file_unlocked() -> None:
    global _fh, _fh_date
    if _fh is not None:
        try:
            _fh.close()
        except OSError:
            pass
    _fh = None
    _fh_date = ""


def _ensure_file_unlocked() -> None:
    global _fh, _fh_date
    if _log_dir is None:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if _fh is not None and _fh_date == today:
        return
    _close_file_unlocked()
    try:
        _log_dir.mkdir(parents=True, exist_ok=True)
        path = _log_dir / f"{today}.log"
        _fh = path.open("a", encoding="utf-8")
        _fh_date = today
    except OSError:
        _fh = None
        _fh_date = ""


def _write_file_unlocked(msg: str) -> None:
    if _log_dir is None:
        return
    _ensure_file_unlocked()
    if _fh is None:
        return
    prefix = datetime.now().strftime("%H:%M:%S")
    if _job:
        prefix += f" [{_job}]"
    try:
        _fh.write(f"{prefix} {_plain(msg).rstrip()}\n")
        _fh.flush()
    except OSError:
        pass


def _finish_progress_unlocked() -> None:
    global _progress_open
    if not _progress_open:
        return
    print(flush=True)
    _progress_open = False


def _emit(screen: str | None, file_msg: str | None = None) -> None:
    with _lock:
        _finish_progress_unlocked()
        if file_msg is None:
            file_msg = screen
        if file_msg:
            _write_file_unlocked(file_msg)
        if screen:
            print(screen, flush=True)


def attach(log_dir: Path | str | None) -> None:
    """Send detail logs to LOG_DIR/YYYY-MM-DD.log. None disables the file."""
    global _log_dir
    with _lock:
        _close_file_unlocked()
        if log_dir is None:
            _log_dir = None
            return
        _log_dir = Path(log_dir)
        _ensure_file_unlocked()
        _write_file_unlocked(
            f"===== session {datetime.now().isoformat(timespec='seconds')} ====="
        )


def set_job(job_id: str = "") -> None:
    global _job
    with _lock:
        _job = (job_id or "").strip()
        if _job:
            _write_file_unlocked(f"----- job {_job} -----")


def reset() -> None:
    """Test helper: drop file handle and job context."""
    global _log_dir, _job, _progress_open
    with _lock:
        _close_file_unlocked()
        _log_dir = None
        _job = ""
        _progress_open = False


def log(msg: str) -> None:
    _emit(f"{_tag_time()} {msg}", msg)


def info(msg: str) -> None:
    _emit(f"{_tag_time()} {blue('INFO')}  {msg}", f"INFO  {msg}")


def ok(msg: str) -> None:
    _emit(f"{_tag_time()} {green('OK')}    {msg}", f"OK    {msg}")


def warn(msg: str) -> None:
    _emit(f"{_tag_time()} {yellow('WARN')}  {msg}", f"WARN  {msg}")


def skip(msg: str) -> None:
    _emit(f"{_tag_time()} {dim('SKIP')}  {msg}", f"SKIP  {msg}")


def detail(msg: str) -> None:
    """File-only secondary line."""
    with _lock:
        _write_file_unlocked(f"       {msg}")


def keyval(key: str, value: Any) -> None:
    screen = f"{_tag_time()}   {dim(str(key) + ':')} {bold(str(value))}"
    _emit(screen, f"  {key}: {value}")


def progress(done: int, total: int, msg: str = "", *, label: str = "") -> None:
    """One-line step progress. TTY overwrites; details always go to the log file."""
    total = max(1, total)
    pct = min(100.0, 100.0 * done / total)
    prefix = f"{label}  " if label else ""
    extra = f"  {msg}" if msg else ""
    text = f"{prefix}{done}/{total}  {pct:.0f}%{extra}"
    with _lock:
        _write_file_unlocked(text)
        if _is_tty():
            print(f"\r{text:<80}", end="", flush=True)
            _progress_open = True
            if done >= total:
                print(flush=True)
                _progress_open = False
        elif done in {1, total} or done % max(1, total // 10) == 0:
            _finish_progress_unlocked()
            print(f"{_tag_time()} {text}", flush=True)


def rule(char: str = "─", width: int = 56) -> None:
    with _lock:
        _finish_progress_unlocked()
        line = dim(char * width)
        _write_file_unlocked(char * width)
        print(line, flush=True)


def stage(name: str, extra: str = "", *, index: int | None = None, total: int | None = None) -> None:
    badge = _stage_badge(name, index, total)
    label = _c("1;97", _label(name))
    code = dim(f"({name})")
    tail = f"  {cyan(extra)}" if extra else ""
    screen = f"{_tag_time()} {badge} ▶ STAGE  {label} {code}{tail}"
    file_msg = f"▶ STAGE  {_label(name)} ({name})" + (f"  {extra}" if extra else "")
    with _lock:
        _finish_progress_unlocked()
        _write_file_unlocked("")
        _write_file_unlocked(file_msg)
        print(flush=True)
        print(screen, flush=True)
        print(dim("─" * 56), flush=True)


def stage_done(name: str, extra: str = "", *, index: int | None = None, total: int | None = None) -> None:
    badge = _stage_badge(name, index, total)
    label = green(_label(name))
    tail = f"  {dim(extra)}" if extra else ""
    screen = f"{_tag_time()} {badge} {green('✔ DONE')}  {label}{tail}"
    file_msg = f"✔ DONE  {_label(name)}" + (f"  {extra}" if extra else "")
    _emit(screen, file_msg)


def stage_error(name: str, extra: str = "", *, index: int | None = None, total: int | None = None) -> None:
    badge = _stage_badge(name, index, total)
    label = red(_label(name))
    tail = f"  {extra}" if extra else ""
    screen = f"{_tag_time()} {badge} {red('✖ FAIL')}  {label}{tail}"
    file_msg = f"✖ FAIL  {_label(name)}" + (f"  {extra}" if extra else "")
    _emit(screen, file_msg)


def highlight(msg: str) -> None:
    """Mark an important milestone / result line."""
    _emit(f"{_tag_time()} {magenta('★')} {bold(msg)}", f"★ {msg}")


def resume_hint(work: str | Any) -> None:
    warn(f"失败后续跑: python job_run.py --work {work}")
    detail(f"状态文件: {work}/job_state.json")


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
