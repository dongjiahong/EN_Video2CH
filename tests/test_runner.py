from pathlib import Path
from types import SimpleNamespace

import pytest

from zh_dub.runner import Job, Runner, Stage
from zh_dub.state import JobState


class Fake(Stage):
    def __init__(self, name: str, *, fresh: bool = True, fail: bool = False):
        self.name = name
        self.fresh = fresh
        self.fail = fail
        self.runs = 0

    def is_fresh(self, job: Job) -> bool:
        return self.fresh

    def run(self, job: Job) -> dict:
        self.runs += 1
        if self.fail:
            raise RuntimeError("boom")
        return {"ran": True}


STAGE_NAMES = ["download", "transcribe", "segment", "translate", "tts", "narration", "compose"]


def make(tmp_path: Path, *, end: float = 0.0, fresh: bool = True, fails: tuple[str, ...] = ()):
    work = tmp_path / "w"
    work.mkdir(parents=True, exist_ok=True)
    job = Job(
        settings=SimpleNamespace(),  # unused by the fake stages
        work=work,
        state=JobState(work),
        voice="v",
        end=end,
    )
    stages = {
        name: Fake(name, fresh=fresh, fail=name in fails) for name in STAGE_NAMES
    }
    return job, stages


def test_runs_every_stage_in_order(tmp_path):
    job, stages = make(tmp_path, fresh=False)
    Runner(job, stages).run()
    assert [s.runs for s in stages.values()] == [1] * len(STAGE_NAMES)
    assert job.state.data["status"] == "done"


def test_skips_done_and_fresh_stages(tmp_path):
    job, stages = make(tmp_path)
    runner = Runner(job, stages)
    runner.run()
    assert [s.runs for s in stages.values()] == [1] * len(STAGE_NAMES)

    runner.run()  # nothing changed on disk
    assert [s.runs for s in stages.values()] == [1] * len(STAGE_NAMES)


def test_stale_stage_reruns_with_its_downstream(tmp_path):
    job, stages = make(tmp_path)
    runner = Runner(job, stages)
    runner.run()

    stages["tts"].fresh = False
    runner.run()
    assert [s.runs for s in stages.values()] == [1, 1, 1, 1, 2, 2, 2]


def test_failure_marks_stage_and_stops(tmp_path):
    job, stages = make(tmp_path, fresh=False, fails=("translate",))
    with pytest.raises(RuntimeError):
        Runner(job, stages).run()
    assert job.state.stage_status("translate") == "failed"
    assert job.state.data["status"] == "failed"
    assert stages["tts"].runs == 0


def test_from_stage_forces_rerun_of_window(tmp_path):
    job, stages = make(tmp_path)
    runner = Runner(job, stages)
    runner.run()

    runner.run(from_stage="translate")
    assert [s.runs for s in stages.values()] == [1, 1, 1, 2, 2, 2, 2]

    runner.run(from_stage="tts", to_stage="tts")
    assert [s.runs for s in stages.values()] == [1, 1, 1, 2, 3, 2, 2]


def test_to_stage_stops_early(tmp_path):
    job, stages = make(tmp_path, fresh=False)
    Runner(job, stages).run(to_stage="translate")
    assert [s.runs for s in stages.values()] == [1, 1, 1, 1, 0, 0, 0]


def test_from_after_to_is_rejected(tmp_path):
    job, stages = make(tmp_path)
    with pytest.raises(SystemExit):
        Runner(job, stages).run(from_stage="compose", to_stage="tts")


def test_changed_preview_window_invalidates_downstream(tmp_path):
    job, stages = make(tmp_path)
    runner = Runner(job, stages)
    runner.run()
    job.state.set_done("download", preview=False, end=0.0)

    job.end = 90.0
    runner.run()
    assert [s.runs for s in stages.values()] == [1, 2, 2, 2, 2, 2, 2]
