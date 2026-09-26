"""Translation cache and TTS fingerprint behaviour (no network / no edge-tts)."""

from pathlib import Path
from types import SimpleNamespace

from zh_dub import translate, tts
from zh_dub.segments import Segment
from zh_dub.state import JobState


def settings(**over):
    base = {
        "api_keys": ["k1"],
        "model": "m",
        "modelscope_base_url": "http://x",
        "translate_batch_size": 2,
        "translate_max_retries": 1,
        "translate_concurrency": 2,
        "translate_refill_max_rounds": 2,
    }
    base.update(over)
    return SimpleNamespace(**base)


def segs_of(texts: list[str]) -> list[Segment]:
    return [
        Segment(idx=i, start=float(i), end=i + 1.0, en=t) for i, t in enumerate(texts)
    ]


def install_fake(monkeypatch, calls: list[list[str]], *, drop: set[str] = frozenset()):
    def fake(_settings, texts, title_en):
        calls.append(list(texts))
        return {t: f"译{t}" for t in texts if t not in drop}, ""

    monkeypatch.setattr(translate, "_translate_batch", fake)


def test_cache_keyed_on_english_text(tmp_path: Path, monkeypatch):
    state = JobState(tmp_path)
    calls: list[list[str]] = []
    install_fake(monkeypatch, calls)

    segs = segs_of(["a", "b", "c", "d", "e"])
    report = translate.translate_segments(settings(), state, segs)
    assert [s.zh for s in segs] == ["译a", "译b", "译c", "译d", "译e"]
    assert report["filled"] == 5
    assert sorted(calls[0]) == ["a", "b"]

    # same lines, different batch size: served entirely from the cache
    calls.clear()
    regs = segs_of(["e", "a", "b", "c", "d"])
    translate.translate_segments(settings(translate_batch_size=4), state, regs)
    assert [s.zh for s in regs] == ["译e", "译a", "译b", "译c", "译d"]
    assert calls == []
    assert state.read_json("translations.json")["a"] == "译a"


def test_missing_lines_stay_empty_and_are_retried(tmp_path: Path, monkeypatch):
    state = JobState(tmp_path)
    calls: list[list[str]] = []
    install_fake(monkeypatch, calls, drop={"b"})

    segs = segs_of(["a", "b", "c"])
    report = translate.translate_segments(settings(), state, segs)
    assert [s.zh for s in segs] == ["译a", "", "译c"]
    assert report["missing"] == 1

    calls.clear()
    retry = segs_of(["a", "b", "c"])
    translate.translate_segments(settings(), state, retry)
    assert [s.zh for s in retry] == ["译a", "", "译c"]
    # the translated lines are never re-sent; only "b" is, main pass + one refill round
    assert {t for chunk in calls for t in chunk} == {"b"}


def test_english_echo_is_not_cached(tmp_path: Path, monkeypatch):
    state = JobState(tmp_path)
    monkeypatch.setattr(
        translate, "_translate_batch", lambda s, texts, t: ({x: x for x in texts}, "")
    )
    segs = segs_of(["hello world"])
    translate.translate_segments(settings(), state, segs)
    assert segs[0].zh == ""
    assert state.read_json("translations.json") == {}


def test_tts_fingerprint_invalidates_on_text_or_voice(tmp_path: Path):
    audio = tmp_path / "audio"
    audio.mkdir()
    mp3 = audio / "seg_0000.mp3"
    mp3.write_bytes(b"x" * 1000)

    seg = Segment(idx=0, start=0.0, end=3.0, en="e", zh="你好", tts_key=tts.tts_key("你好", "v1", 30))
    assert tts.is_cached(seg, audio, "v1", 30)
    assert not tts.is_cached(seg, audio, "v2", 30)  # voice changed
    assert not tts.is_cached(seg, audio, "v1", 10)  # rate ceiling changed

    seg.zh = "你好，世界"
    assert not tts.is_cached(seg, audio, "v1", 30)  # text changed

    seg.tts_key = ""
    assert not tts.is_cached(seg, audio, "v1", 30)  # never synthesized


def test_validate_tts_ignores_punctuation_only(tmp_path: Path):
    audio = tmp_path / "audio"
    audio.mkdir()
    segs = [
        Segment(idx=0, start=0.0, end=1.0, en="a", zh="好", tts_key=tts.tts_key("好", "v", 30)),
        Segment(idx=1, start=1.0, end=2.0, en="b", zh="。"),
        Segment(idx=2, start=2.0, end=3.0, en="c", zh="缺"),
    ]
    (audio / "seg_0000.mp3").write_bytes(b"x" * 1000)
    report = tts.validate_tts(segs, audio, "v", 30)
    assert report["need"] == 2
    assert report["audio_ok"] == 1
    assert report["audio_missing"] == [2]
    assert not report["pass"]
