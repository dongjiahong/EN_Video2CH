"""Sequential stage runner.

A stage answers two questions: are its artifacts still valid (``is_fresh``),
and how do I produce them (``run``). Everything else - skip decision, state
bookkeeping, failure recording, banners - happens here once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .logutil import resume_hint, skip, stage, stage_done, stage_error, warn
from .state import STAGES, JobState


@dataclass
class Job:
    settings: Settings
    work: Path
    state: JobState
    voice: str
    end: float = 0.0
    url: str | None = None
    output_dir: Path | None = None

    def path(self, name: str) -> Path:
        return self.work / name

    @property
    def preview(self) -> bool:
        return self.end > 0

    @property
    def out(self) -> Path:
        return self.path("out_preview.mp4" if self.preview else "out.mp4")


def newer_than(path: Path, *refs: Path, min_bytes: int = 1) -> bool:
    """True when path exists with content and is at least as new as every ref."""
    if not path.is_file() or path.stat().st_size < min_bytes:
        return False
    mtime = path.stat().st_mtime
    return all(not r.is_file() or r.stat().st_mtime <= mtime for r in refs)


class Stage:
    name: str = ""

    def is_fresh(self, job: Job) -> bool:
        """True when artifacts on disk already satisfy this stage."""
        return False

    def summary(self, detail: dict[str, Any]) -> str:
        return "  ".join(f"{k}={v}" for k, v in detail.items())

    def run(self, job: Job) -> dict[str, Any]:
        raise NotImplementedError


class Runner:
    def __init__(self, job: Job, stages: dict[str, Stage] | None = None):
        self.job = job
        if stages is None:
            from .stages import STAGES_MAP  # deferred: stages import this module

            stages = STAGES_MAP
        self.stages = stages

    def run(self, from_stage: str | None = None, to_stage: str | None = None) -> None:
        job = self.job
        lo = STAGES.index(from_stage) if from_stage else 0
        hi = STAGES.index(to_stage) if to_stage else len(STAGES) - 1
        if lo > hi:
            raise SystemExit(f"--from {from_stage} 在 --to {to_stage} 之后，没有可跑的阶段")
        window = STAGES[lo : hi + 1]

        # source.mp4 was cut for a different preview window: redo everything after it
        prev_end = job.state.stage_detail("download").get("end")
        if from_stage is None and prev_end is not None and float(prev_end) != job.end:
            warn(f"预览窗口从 {float(prev_end):g}s 变为 {job.end or 'full'}，重做下载之后的阶段")
            job.state.mark_pending("transcribe", STAGES[-1])
        if from_stage is not None:
            job.state.mark_pending(window[0], window[-1])
            warn(f"强制重跑 {'..'.join([window[0], window[-1]]) if len(window) > 1 else window[0]}")

        for name in window:
            st = self.stages[name]
            if job.state.is_done(name) and st.is_fresh(job):
                skip(f"{name} 已完成")
                continue
            self._run(st)
            # a fresh stage result invalidates the rest of this window
            if name != window[-1]:
                job.state.mark_pending(STAGES[STAGES.index(name) + 1], window[-1])

    def _run(self, st: Stage) -> None:
        job = self.job
        job.state.set_running(st.name)
        stage(st.name)
        try:
            detail = st.run(job) or {}
        except Exception as e:  # noqa: BLE001
            stage_error(st.name, str(e))
            job.state.set_failed(st.name, str(e))
            resume_hint(job.work)
            raise
        job.state.set_done(st.name, **detail)
        stage_done(st.name, st.summary(detail))
