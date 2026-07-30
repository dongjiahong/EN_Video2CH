from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

TIME_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
)
NUM_LINE_RE = re.compile(r"(?m)^\s*(\d+)\.\s*(.+?)\s*$")


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

    @property
    def slot(self) -> float:
        return max(0.05, self.end - self.start)


def ts_to_seconds(ts: str) -> float:
    ts = ts.replace(",", ".")
    hh, mm, rest = ts.split(":")
    ss, *ms = rest.split(".")
    frac = float(f"0.{ms[0]}") if ms else 0.0
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + frac


def seconds_to_ts(sec: float, srt: bool = False) -> str:
    if sec < 0:
        sec = 0.0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    sep = "," if srt else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def clean_caption_text(raw: str) -> str:
    raw = re.sub(r"<\d{2}:\d{2}:\d{2}[.,]\d{3}>", "", raw)
    raw = re.sub(r"</?c>", "", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = (
        raw.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"\s+", " ", raw).strip()


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    text = path.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    cues: list[tuple[float, float, str]] = []
    for block in blocks:
        m = TIME_RE.search(block)
        if not m:
            continue
        start, end = ts_to_seconds(m.group(1)), ts_to_seconds(m.group(2))
        if end - start < 0.05:
            continue
        payload = block[m.end() :]
        lines: list[str] = []
        for line in payload.splitlines():
            line = line.strip()
            if not line or line.isdigit():
                continue
            cleaned = clean_caption_text(line)
            if cleaned:
                lines.append(cleaned)
        if lines:
            cues.append((start, end, " ".join(lines)))
    return cues


def parse_vtt(path: Path) -> list[tuple[float, float, str]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", text)
    raw_cues: list[tuple[float, float, str]] = []
    for block in blocks:
        m = TIME_RE.search(block)
        if not m:
            continue
        start, end = ts_to_seconds(m.group(1)), ts_to_seconds(m.group(2))
        if end - start < 0.05:
            continue
        payload = block[m.end() :]
        lines: list[str] = []
        for line in payload.splitlines():
            line = line.strip()
            if not line or line.isdigit() or line.startswith("NOTE"):
                continue
            if re.fullmatch(r"(align|position|size|line|vertical):.*", line, re.I):
                continue
            cleaned = clean_caption_text(line)
            if cleaned:
                lines.append(cleaned)
        if not lines:
            continue
        content = lines[-1]
        if content:
            raw_cues.append((start, end, content))
    if not raw_cues:
        return []

    finalized: list[tuple[float, float, str]] = []
    prev_start, prev_end, prev_text = raw_cues[0]

    def is_extension(old: str, new: str) -> bool:
        o = re.sub(r"\s+", " ", old).strip().lower()
        n = re.sub(r"\s+", " ", new).strip().lower()
        return n.startswith(o) or o.startswith(n)

    for start, end, text in raw_cues[1:]:
        if text == prev_text:
            prev_end = max(prev_end, end)
            continue
        if is_extension(prev_text, text):
            if len(text) >= len(prev_text):
                prev_text = text
            prev_end = max(prev_end, end)
            continue
        finalized.append((prev_start, max(prev_end, start), prev_text))
        prev_start, prev_end, prev_text = start, end, text
    finalized.append((prev_start, prev_end, prev_text))

    incremental: list[tuple[float, float, str]] = []
    last_full = ""
    for st, en, tx in finalized:
        tx_n = re.sub(r"\s+", " ", tx).strip()
        if last_full and tx_n.startswith(last_full):
            piece = tx_n[len(last_full) :].strip(" ,.-")
            if piece:
                incremental.append((st, en, piece))
                last_full = tx_n
        else:
            incremental.append((st, en, tx_n))
            last_full = tx_n
    return incremental


def resolve_subtitle(work: Path, explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"subtitle not found: {p}")
        return p
    for name in ("source.en.vtt", "source.en.srt", "source.vtt", "source.srt"):
        p = work / name
        if p.is_file():
            return p
    raise FileNotFoundError(f"missing subtitle in {work}")


def load_cues(path: Path) -> list[tuple[float, float, str]]:
    if path.suffix.lower() == ".srt":
        return parse_srt(path)
    return parse_vtt(path)


def merge_cues(
    cues: list[tuple[float, float, str]],
    max_gap: float = 0.55,
    target_len: float = 4.8,
    max_len: float = 8.5,
    max_chars: int = 120,
) -> list[Segment]:
    if not cues:
        return []
    segs: list[Segment] = []
    cur_s, cur_e, cur_t = cues[0]
    idx = 0

    def flush() -> None:
        nonlocal idx, cur_s, cur_e, cur_t
        text = re.sub(r"\s+", " ", cur_t).strip(" ,")
        text = re.sub(r"\b(um+|uh+)\b", "", text, flags=re.I)
        text = re.sub(r"\s+", " ", text).strip(" ,")
        if text:
            segs.append(Segment(idx=idx, start=cur_s, end=cur_e, en=text))
            idx += 1

    for st, en, tx in cues[1:]:
        gap = st - cur_e
        candidate = re.sub(r"\s+", " ", (cur_t + " " + tx).strip())
        dur = en - cur_s
        boundary = bool(re.search(r"[.!?]$", cur_t.strip()))
        can_merge = (
            gap <= max_gap
            and dur <= max_len
            and len(candidate) <= max_chars
            and (not boundary or (cur_e - cur_s) < 2.2)
            and ((cur_e - cur_s) < target_len or not boundary)
        )
        if can_merge:
            cur_t = candidate
            cur_e = en
        else:
            flush()
            cur_s, cur_e, cur_t = st, en, tx
    flush()
    return segs


def drop_fillers(text: str) -> str:
    out = re.sub(r"\b(um+|uh+|you know|i mean|okay|ok)\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", out).strip(" ,")


def parse_numbered_zh(text: str, expected: int) -> dict[int, str]:
    found: dict[int, str] = {}
    for m in NUM_LINE_RE.finditer(text.replace("\r\n", "\n")):
        n = int(m.group(1))
        zh = m.group(2).strip()
        zh = re.sub(r"^[\"'“”]+|[\"'“”]+$", "", zh).strip()
        if 1 <= n <= expected and zh:
            found[n] = zh
    return found


def save_segments(segs: Iterable[Segment], path: Path) -> None:
    path.write_text(
        __import__("json").dumps([asdict(s) for s in segs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_segments(path: Path) -> list[Segment]:
    data = __import__("json").loads(path.read_text(encoding="utf-8"))
    return [Segment(**row) for row in data]


def write_srt(segs: list[Segment], path: Path, field: str = "zh") -> None:
    lines: list[str] = []
    n = 1
    for seg in segs:
        text = getattr(seg, field).strip()
        if not text:
            continue
        lines.append(str(n))
        lines.append(
            f"{seconds_to_ts(seg.start, True)} --> {seconds_to_ts(seg.end, True)}"
        )
        lines.append(text)
        lines.append("")
        n += 1
    path.write_text("\n".join(lines), encoding="utf-8")


def write_zh_ass(segs: list[Segment], path: Path) -> None:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ZH,PingFang SC,18,&H0000F0FF,&H000000FF,&H00101010,&H00000000,0,0,0,0,100,100,0,0,1,1.6,0,2,16,16,22,1

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

    events: list[str] = []
    for seg in segs:
        zh = (seg.zh or "").replace("\n", " ").replace("{", "(").replace("}", ")").strip()
        if not zh:
            continue
        st, et = _ass_ts(seg.start), _ass_ts(seg.end)
        events.append(f"Dialogue: 0,{st},{et},ZH,,0,0,0,,{zh}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
