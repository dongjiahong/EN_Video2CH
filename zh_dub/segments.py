"""Segment data model, persistence, and subtitle writers (srt / ass)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class Segment:
    idx: int
    start: float
    end: float
    en: str
    zh: str = ""
    rate_pct: int = 0
    audio: str = ""
    tts_dur: float = 0.0
    fitted: bool = False
    note: str = ""
    tts_key: str = ""

    @property
    def slot(self) -> float:
        return max(0.05, self.end - self.start)


_READABLE_RE = re.compile(r"[A-Za-z0-9]|[\u4e00-\u9fff]|[\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]")


def is_punct_only(text: str) -> bool:
    """True when text has no readable content, only punctuation/whitespace."""
    t = (text or "").strip()
    return bool(t) and not _READABLE_RE.search(t)


def speakable(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and not is_punct_only(t)


def seconds_to_ts(sec: float, srt: bool = False) -> str:
    if sec < 0:
        sec = 0.0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    sep = "," if srt else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def dump_segments(segs: Iterable[Segment]) -> str:
    return json.dumps([asdict(s) for s in segs], ensure_ascii=False, indent=2)


def save_segments(segs: Iterable[Segment], path: Path) -> bool:
    """Write only when content changed (keeps mtime meaningful). Returns changed."""
    text = dump_segments(segs)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def load_segments(path: Path) -> list[Segment]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Segment(**row) for row in data]


def write_srt(segs: list[Segment], path: Path, field: str = "zh") -> None:
    lines: list[str] = []
    n = 1
    for seg in segs:
        text = getattr(seg, field).strip()
        if not speakable(text):
            continue
        lines.append(str(n))
        lines.append(f"{seconds_to_ts(seg.start, True)} --> {seconds_to_ts(seg.end, True)}")
        lines.append(text)
        lines.append("")
        n += 1
    path.write_text("\n".join(lines), encoding="utf-8")


def _wrap_zh_line(text: str, max_chars: int = 32) -> str:
    """Hard-wrap Chinese subtitle text for ASS (\\N). Prefer breaks after punct."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) <= max_chars:
        return text

    prefer = set("，。！？；：、,.!?;: ")
    lines: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_chars:
            lines.append(rest)
            break
        window = rest[: max_chars + 1]
        cut = -1
        for i in range(max_chars, max(max_chars // 2, 0) - 1, -1):
            if window[i - 1] in prefer:
                cut = i
                break
        if cut < 0:
            cut = max_chars
        piece = rest[:cut].strip()
        if piece:
            lines.append(piece)
        rest = rest[cut:].lstrip()
    return "\\N".join(lines)


_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ZH,PingFang SC,16,&H0000F0FF,&H000000FF,&H00101010,&H00000000,0,0,0,0,100,100,0,0,1,1.6,0,2,20,20,24,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_ts(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    cs = int(round(sec * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def write_zh_ass(segs: list[Segment], path: Path, *, max_chars: int = 32) -> None:
    events: list[str] = []
    for seg in segs:
        zh = (seg.zh or "").replace("\n", " ").replace("{", "(").replace("}", ")").strip()
        if not speakable(zh):
            continue
        zh = _wrap_zh_line(zh, max_chars=max_chars)
        events.append(f"Dialogue: 0,{_ass_ts(seg.start)},{_ass_ts(seg.end)},ZH,,0,0,0,,{zh}")
    path.write_text(_ASS_HEADER + "\n".join(events) + "\n", encoding="utf-8")
