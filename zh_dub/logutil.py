from __future__ import annotations

from datetime import datetime


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def stage(name: str, detail: str = "") -> None:
    extra = f" | {detail}" if detail else ""
    print(f"\n[{_ts()}] ============ STAGE: {name}{extra} ============", flush=True)


def stage_done(name: str, detail: str = "") -> None:
    extra = f" | {detail}" if detail else ""
    print(f"[{_ts()}] OK STAGE: {name}{extra}", flush=True)


def stage_error(name: str, detail: str = "") -> None:
    extra = f" | {detail}" if detail else ""
    print(f"[{_ts()}] FAIL STAGE: {name}{extra}", flush=True)
