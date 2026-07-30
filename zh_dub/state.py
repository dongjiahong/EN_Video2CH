from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logutil import format_status_line, log

STAGES = [
    "download",
    "prepare_video",
    "prepare_cues",
    "merge",
    "translate",
    "tts",
    "narration",
    "compose",
]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_state(work: Path, **meta: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "work": str(work),
        "created_at": _now(),
        "updated_at": _now(),
        "status": "pending",  # pending|running|failed|done
        "stage": STAGES[0],
        "error": None,
        "meta": meta,
        "stages": {
            name: {"status": "pending", "detail": {}}
            for name in STAGES
        },
        "resume_hint": f"python job_run.py --work {work} --resume",
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
                if isinstance(data, dict) and "stages" in data:
                    return data
            except Exception as e:  # noqa: BLE001
                log(f"job_state.json unreadable, recreating: {e}")
        data = default_state(self.work)
        self._write(data)
        return data

    def _write(self, data: dict[str, Any] | None = None) -> None:
        payload = data if data is not None else self.data
        payload["updated_at"] = _now()
        payload["work"] = str(self.work)
        payload["resume_hint"] = f"python job_run.py --work {self.work} --resume"
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def save(self) -> None:
        self._write()

    def update_meta(self, **kwargs: Any) -> None:
        self.data.setdefault("meta", {}).update(kwargs)
        self.save()

    def set_running(self, stage: str) -> None:
        self.data["status"] = "running"
        self.data["stage"] = stage
        self.data["error"] = None
        st = self.data["stages"].setdefault(stage, {"status": "pending", "detail": {}})
        st["status"] = "running"
        st["started_at"] = _now()
        self.save()

    def set_done(self, stage: str, **detail: Any) -> None:
        st = self.data["stages"].setdefault(stage, {"status": "pending", "detail": {}})
        st["status"] = "done"
        st["finished_at"] = _now()
        if detail:
            st.setdefault("detail", {}).update(detail)
        self.data["error"] = None
        # advance pointer to next pending
        nxt = self.next_pending()
        self.data["stage"] = nxt or stage
        if nxt is None and all(
            self.data["stages"][s]["status"] == "done" for s in STAGES
        ):
            self.data["status"] = "done"
        else:
            self.data["status"] = "running"
        self.save()

    def set_failed(self, stage: str, message: str, **extra: Any) -> None:
        st = self.data["stages"].setdefault(stage, {"status": "pending", "detail": {}})
        st["status"] = "failed"
        st["finished_at"] = _now()
        err = {"message": message, "stage": stage, **extra}
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
            st = self.stage_status(name)
            if st in {"pending", "failed", "running"}:
                return name
        return None

    def mark_pending_from(self, stage: str) -> None:
        if stage not in STAGES:
            raise SystemExit(f"unknown stage: {stage}")
        hit = False
        for name in STAGES:
            if name == stage:
                hit = True
            if hit:
                self.data["stages"][name] = {"status": "pending", "detail": {}}
        self.data["status"] = "pending"
        self.data["stage"] = stage
        self.data["error"] = None
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

    def summary_lines(self) -> list[str]:
        status = str(self.data.get("status") or "?")
        lines = [
            f"work     {self.work}",
            f"status   {status}",
            f"stage    {self.data.get('stage')}",
            f"updated  {self.data.get('updated_at')}",
            "stages:",
        ]
        for name in STAGES:
            st = self.data["stages"].get(name, {})
            detail = st.get("detail") or {}
            extra = ""
            if detail:
                bits = []
                for k in (
                    "segments",
                    "total",
                    "done_batches",
                    "audio_ok",
                    "missing",
                    "out",
                    "progress",
                    "cues",
                    "overflow",
                    "duration",
                    "size_mb",
                ):
                    if k in detail:
                        bits.append(f"{k}={detail[k]}")
                if bits:
                    extra = "  " + ", ".join(bits)
            lines.append(
                format_status_line(name, str(st.get("status", "?")), extra)
            )
        err = self.data.get("error")
        if err:
            lines.append(f"error    {err.get('message')}")
            if err.get("retryable"):
                lines.append("retryable yes")
        lines.append(f"resume   {self.data.get('resume_hint')}")
        return lines
