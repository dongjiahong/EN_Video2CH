from pathlib import Path

import pytest

from zh_dub.export import export_filename
from zh_dub.segmenter import merge_cues, parse_srt
from zh_dub.segments import _wrap_zh_line, is_punct_only
from zh_dub.sources import apply_limit, is_playlist_url, parse_limit
from zh_dub.translate import parse_numbered_zh
from zh_dub.tts import _needed_rate_pct, compress_zh

CUES = [
    (0.0, 1.5, "So today we're going to look at"),
    (1.5, 3.0, "the bear channel on the daily chart."),
    (3.1, 4.0, "Okay."),
    (4.0, 7.5, "If you look at the bars here, they are small and the bulls are trying to"),
    (7.5, 8.2, "get in."),
    (10.0, 12.0, "Now the next thing is the breakout. It failed. Then we got a reversal"),
    (
        12.0,
        20.0,
        "and the market went down a lot but the bulls came back and so the bears "
        "were trapped and then we had a big bull bar which is a sign of strength "
        "and the next bar was also a bull bar and so on and on until the close",
    ),
]


def test_merge_cues_golden():
    got = [(s.idx, round(s.start, 3), round(s.end, 3), s.en) for s in merge_cues(CUES)]
    assert got == [
        (0, 0.0, 3.644, "So today we're going to look at the bear channel on the daily chart."),
        (1, 3.644, 8.2, "Okay. If you look at the bars here, they are small and the bulls are trying to get in."),
        (2, 10.0, 11.613, "Now the next thing is the breakout. It failed."),
        (
            3,
            11.613,
            20.0,
            "Then we got a reversal and the market went down a lot but the bulls came back "
            "and so the bears were trapped and then we had a big bull bar which is a sign "
            "of strength and the next bar was also a bull bar and so on and on until the close",
        ),
    ]


def test_merge_cues_empty():
    assert merge_cues([]) == []


def test_parse_srt(tmp_path: Path):
    p = tmp_path / "a.srt"
    p.write_text(
        "1\n00:00:01,000 --> 00:00:02,500\nHello <b>world</b>\n\n"
        "2\n00:00:03,000 --> 00:00:03,010\ntoo short\n\n"
        "3\n00:01:00.000 --> 00:01:02.000\nline one\nline two\n",
        encoding="utf-8",
    )
    assert parse_srt(p) == [(1.0, 2.5, "Hello world"), (60.0, 62.0, "line one line two")]


@pytest.mark.parametrize(
    "raw,expected",
    [(None, (0, 0)), ("", (0, 0)), ("0", (0, 0)), ("20", (0, 20)), ("20,40", (20, 40)), (" 5 , 3 ", (5, 3))],
)
def test_parse_limit(raw, expected):
    assert parse_limit(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "a", "1,0", "1,2,3"])
def test_parse_limit_invalid(raw):
    with pytest.raises(ValueError):
        parse_limit(raw)


def test_apply_limit():
    items = list(range(10))
    assert apply_limit(items, 0, 0) == items
    assert apply_limit(items, 0, 3) == [0, 1, 2]
    assert apply_limit(items, 8, 5) == [8, 9]
    assert apply_limit(items, 4, 0) == [4, 5, 6, 7, 8, 9]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/playlist?list=PL123", True),
        ("https://www.youtube.com/watch?v=abc&list=PL123", False),
        ("https://www.youtube.com/watch?v=abc", False),
        ("https://youtu.be/abc?list=PL123", False),
        ("https://www.youtube.com/?list=PL123", True),
        ("", False),
    ],
)
def test_is_playlist_url(url, expected):
    assert is_playlist_url(url) is expected


def test_export_filename():
    assert export_filename("价格/行为: 入门", "vid1") == "价格 行为 入门 [vid1].mp4"
    assert export_filename("标题", "vid1", seq=3) == "03_标题 [vid1].mp4"
    assert export_filename("", "vid1", preview=True, seq=12) == "12_vid1 [vid1].preview.mp4"


def test_parse_numbered_zh():
    text = "1. 你好\n2. “世界”\n  3.  测试 \n9. 超出\nfoo"
    assert parse_numbered_zh(text, 3) == {1: "你好", 2: "世界", 3: "测试"}


def test_is_punct_only():
    assert is_punct_only("。？！")
    assert is_punct_only(" ... ")
    assert not is_punct_only("")
    assert not is_punct_only("好。")
    assert not is_punct_only("K")


def test_compress_zh():
    assert compress_zh("我们然后看一下这个K线，其实很强。") == [
        "我们然后看一下这个K线，其实很强。",
        "然后看一下这个K线，其实很强",
        "然后看一下K线，其实很强",
        "看一下K线，其实很强",
        "看一下K线，很强",
        "看K线，很强",
        "我们然后看一下这个K线，其实很强",
    ]


def test_needed_rate_pct():
    assert [_needed_rate_pct(d, s, 30) for d, s in [(3, 3), (3.3, 3), (4, 3), (10, 3), (0, 3)]] == [
        0,
        11,
        30,
        30,
        0,
    ]


def test_wrap_zh_line():
    text = "这是一个很长的中文字幕，用来测试自动换行的效果是否符合预期，并且优先在标点处断开。最后再补一段文字。"
    assert _wrap_zh_line(text) == (
        "这是一个很长的中文字幕，用来测试自动换行的效果是否符合预期，\\N并且优先在标点处断开。最后再补一段文字。"
    )
    assert _wrap_zh_line("短句") == "短句"
