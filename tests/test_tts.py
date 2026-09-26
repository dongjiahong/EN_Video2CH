"""TTS timeout / retry / skip behaviour (no real edge-tts)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zh_dub.runner import Job
from zh_dub.segments import Segment, save_segments
from zh_dub.state import JobState
from zh_dub.stages import TtsStage
from zh_dub import tts


def _settings(**over) -> SimpleNamespace:
    base = dict(tts_concurrency=2, tts_max_rate=30, tts_timeout=0.2)
    base.update(over)
    return SimpleNamespace(**base)


def test_timeout_retries_then_skips(tmp_path: Path, monkeypatch):
    calls = {"n": 0}

    async def hang(text, out_mp3, voice, rate_pct):
        calls["n"] += 1
        await asyncio.sleep(30)

    monkeypatch.setattr(tts, "_edge_save", hang)
    segs = [
        Segment(idx=0, start=0.0, end=2.0, en="a", zh="你好"),
        Segment(idx=1, start=2.0, end=4.0, en="b", zh="世界"),
    ]
    t0 = time.time()
    tts.fit_segments(_settings(), segs, tmp_path / "audio", "v")
    assert time.time() - t0 < 8
    assert calls["n"] == 4  # 2 sentences × original + 1 retry
    assert segs[0].note.startswith("tts_timeout")
    assert segs[1].note.startswith("tts_timeout")
    assert segs[0].audio == ""
    assert segs[1].tts_key == ""
    assert not (tmp_path / "audio" / "seg_0000.mp3").is_file()


def test_timeout_then_success(tmp_path: Path, monkeypatch):
    calls = {"n": 0}

    async def flaky(text, out_mp3, voice, rate_pct):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(30)
        out_mp3.write_bytes(b"x" * 1000)

    monkeypatch.setattr(tts, "_edge_save", flaky)
    monkeypatch.setattr(tts, "ffprobe_duration", lambda *a, **k: 1.0)
    segs = [Segment(idx=0, start=0.0, end=2.0, en="a", zh="你好")]
    tts.fit_segments(_settings(), segs, tmp_path / "audio", "v")
    assert calls["n"] == 2
    assert segs[0].tts_key
    assert segs[0].note in {"ok", "compressed"}
    assert (tmp_path / "audio" / "seg_0000.mp3").is_file()


def test_tts_stage_raises_when_missing(tmp_path: Path, monkeypatch):
    segs = [Segment(idx=0, start=0.0, end=1.0, en="a", zh="你好")]
    save_segments(segs, tmp_path / "segments.json")
    monkeypatch.setattr("zh_dub.stages.fit_segments", lambda *a, **k: None)
    job = Job(
        settings=_settings(tts_timeout=45),
        work=tmp_path,
        state=JobState(tmp_path),
        voice="v",
    )
    with pytest.raises(RuntimeError, match="TTS 不完整"):
        TtsStage().run(job)
