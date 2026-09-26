from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STAGES = [
    "download",
    "transcribe",
    "segment",
    "translate",
    "tts",
    "narration",
    "compose",
]

STATE_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _blank_stage() -> dict[str, Any]:
    return {"status": "pending", "detail": {}}


def default_state(work: Path) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "work": str(work),
        "created_at": _now(),
        "updated_at": _now(),
        "status": "pending",  # pending|running|failed|done
        "stage": STAGES[0],
        "error": None,
        "meta": {},
        "stages": {name: _blank_stage() for name in STAGES},
    }


class JobState:
    def __init__(self, work: Path):
        self.work = work.resolve()
        self.path = self.work / "job_state.json"
        self.checkpoints = self.work / "checkpoints"
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        self.data = self._load_or_init()

    def _load_or_init(self) -> dict[str, Any]:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("version") == STATE_VERSION:
                    return data
                print(f"job_state.json version mismatch, recreating: {self.path}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"job_state.json unreadable, recreating: {e}", file=sys.stderr)
        data = default_state(self.work)
        self.data = data
        self.save()
        return data

    def save(self) -> None:
        self.data["updated_at"] = _now()
        self.data["work"] = str(self.work)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def meta(self) -> dict[str, Any]:
        return self.data.setdefault("meta", {})

    def update_meta(self, **kwargs: Any) -> None:
        self.meta.update(kwargs)
        self.save()

    def _stage(self, stage: str) -> dict[str, Any]:
        return self.data["stages"].setdefault(stage, _blank_stage())

    def set_running(self, stage: str) -> None:
        self.data["status"] = "running"
        self.data["stage"] = stage
        self.data["error"] = None
        st = self._stage(stage)
        st["status"] = "running"
        st["started_at"] = _now()
        self.save()

    def set_progress(self, stage: str, **detail: Any) -> None:
        self._stage(stage).setdefault("detail", {}).update(detail)
        self.save()

    def set_done(self, stage: str, **detail: Any) -> None:
        st = self._stage(stage)
        st["status"] = "done"
        st["finished_at"] = _now()
        st["detail"] = detail
        st.pop("error", None)
        self.data["error"] = None
        nxt = self.next_pending()
        self.data["stage"] = nxt or stage
        self.data["status"] = "done" if nxt is None else "running"
        self.save()

    def set_failed(self, stage: str, message: str) -> None:
        st = self._stage(stage)
        st["status"] = "failed"
        st["finished_at"] = _now()
        err = {"message": message, "stage": stage}
        st["error"] = err
        self.data["status"] = "failed"
        self.data["stage"] = stage
        self.data["error"] = err
        self.save()

    def stage_status(self, stage: str) -> str:
        return self.data.get("stages", {}).get(stage, {}).get("status", "pending")

    def stage_detail(self, stage: str) -> dict[str, Any]:
        return deepcopy(self.data.get("stages", {}).get(stage, {}).get("detail") or {})

    def is_done(self, stage: str) -> bool:
        return self.stage_status(stage) == "done"

    def next_pending(self) -> str | None:
        for name in STAGES:
            if self.stage_status(name) != "done":
                return name
        return None

    def mark_pending(self, start: str, end: str) -> None:
        """Forget the [start..end] slice so those stages run again."""
        if start not in STAGES or end not in STAGES:
            raise SystemExit(f"unknown stage range: {start}..{end}")
        for name in STAGES[STAGES.index(start) : STAGES.index(end) + 1]:
            self.data["stages"][name] = _blank_stage()
        self.data["error"] = None
        nxt = self.next_pending()
        self.data["stage"] = nxt or STAGES[-1]
        self.data["status"] = "pending" if nxt else "done"
        self.save()

    def checkpoint_path(self, name: str) -> Path:
        return self.checkpoints / name

    def write_json(self, name: str, obj: Any) -> Path:
        path = self.checkpoint_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def read_json(self, name: str, default: Any = None) -> Any:
        path = self.checkpoint_path(name)
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
