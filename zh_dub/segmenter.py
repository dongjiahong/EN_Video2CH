"""English subtitle parsing + sentence-aware packing into TTS-sized segments."""

from __future__ import annotations

import re
from pathlib import Path

from .segments import Segment

TIME_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
)


def ts_to_seconds(ts: str) -> float:
    ts = ts.replace(",", ".")
    hh, mm, rest = ts.split(":")
    ss, *ms = rest.split(".")
    frac = float(f"0.{ms[0]}") if ms else 0.0
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + frac



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



def _window_units(
    units: list[tuple[float, float, str]], t0: float, t1: float
) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for s, e, t in units:
        if e <= t0 or s >= t1:
            continue
        ns, ne = max(s, t0), min(e, t1)
        if ne > ns:
            out.append((ns, ne, t))
    return out


def build_segments(
    path: Path,
    *,
    t0: float = 0.0,
    t1: float | None = None,
) -> list[Segment]:
    """Build TTS segments from a subtitle file (SRT with real punctuation)."""
    end = 1e12 if t1 is None or t1 <= 0 else t1
    cues = _window_units(parse_srt(path), t0, end)
    return merge_cues(
        cues,
        target_len=3.5,
        max_len=5.5,
        max_chars=110,
        min_seg_dur=1.2,
        min_seg_words=3,
    )


# Match a finished sentence (may appear mid-cue in auto captions).
_SENTENCE_SPLIT_RE = re.compile(
    r'(?<=[.!?…])(?:["\'”’)\]]+)?(?=\s+|$)'
)
_SENTENCE_END_RE = re.compile(r'[.!?…]["\'”’)\]]*$')
_DANGLING_END_RE = re.compile(
    r"(?i)\b("
    r"a|an|the|to|of|in|on|at|for|with|and|or|but|if|as|"
    r"is|are|was|were|be|been|being|will|would|could|should|can|may|might|must|"
    r"not|no|so|just|very|more|most|less|than|then|"
    r"that|this|these|those|there|here|"
    r"i|we|you|they|he|she|it|my|our|your|their|his|her|its|"
    r"me|us|him|them|who|whom|whose|which|what|when|where|why|how|"
    r"going|trying|looking|want|wants|need|needs|like|likes|from|into|"
    r"about|over|under|between|by|do|does|did|have|has|had|"
    r"don't|doesn't|didn't|won't|can't|couldn't|shouldn't|isn't|aren't|"
    r"i'm|we're|you're|they're|he's|she's|it's|that's|there's|who's|what's|"
    r"let's|gonna|wanna|gotta|kinda|sorta|able|still|also|even|only|"
    r"really|actually|basically|probably|maybe|something|anything|"
    r"everything|nothing|someone|anyone|everyone|because|while|after|"
    r"before|until|unless|whether|though|although|through|"
    r"every|each|any|all|such"
    r")\s*$"
)


def _normalize_cue_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,")
    text = re.sub(r"\b(um+|uh+)\b", "", text, flags=re.I)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*,+", ",", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END_RE.search(text.strip()))


def _dangling_end(text: str) -> bool:
    t = text.strip()
    if not t or _ends_sentence(t):
        return False
    bare = t.rstrip(" ,;:-")
    if bare and _DANGLING_END_RE.search(bare):
        return True
    return bool(_DANGLING_END_RE.search(t))


def _starts_continuation(text: str) -> bool:
    t = text.lstrip()
    return bool(t) and (t[0].islower() or t[0].isdigit())


def _is_mid_sentence_cut(prev: str, nxt: str) -> bool:
    """
    True only when a cut would clearly orphan a fragment.

    Intentionally narrow: content-word endings may start a new pack unit even
    if the next cue is lowercase (auto-captions often omit periods).
    """
    p = prev.strip()
    n = nxt.strip()
    if not p or not n:
        return bool(n)
    if _ends_sentence(p):
        return False
    # "... they're" / "going to" / "the" → must keep next crumb
    if _dangling_end(p):
        return True
    n_words = n.split()
    # short completion of an open clause: "lucky." / "first."
    if len(n_words) <= 3 and _ends_sentence(n):
        return True
    if len(n_words) <= 2 and _starts_continuation(n):
        return True
    return False


def _join_text(a: str, b: str) -> str:
    return re.sub(r"\s+", " ", f"{a} {b}".strip())


def _split_sentences(text: str) -> list[str]:
    """Split on .!? even when they sit mid-cue (common in YouTube auto-captions)."""
    text = _normalize_cue_text(text)
    if not text:
        return []
    parts: list[str] = []
    last = 0
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        chunk = text[last : m.end()].strip()
        if chunk:
            parts.append(chunk)
        last = m.end()
    tail = text[last:].strip()
    if tail:
        parts.append(tail)
    return parts or [text]


# Soft clause boundaries for long unpunctuated auto-caption rants.
_SOFT_CLAUSE_RE = re.compile(
    r"(?i)\s+(?="
    r"(?:but|so|and then|and then|then|now|okay|alright|all right|well|look|"
    r"because|however|instead|except|also|plus|still|right|"
    r"i mean|you know|the thing is|what i|what you|what we)\b"
    r")|(?<=[,;])\s+"
)


def _force_wrap(text: str, max_chars: int) -> list[str]:
    """
    Last-resort wrap. Prefer cutting after commas; never end a piece on a
    dangling function word if a later word can absorb the cut.
    """
    words = text.split()
    if not words:
        return []
    if len(text) <= max_chars:
        return [text]

    out: list[str] = []
    i = 0
    n = len(words)
    while i < n:
        # grow window up to max_chars
        j = i
        best = i + 1
        size = 0
        while j < n:
            add = len(words[j]) + (1 if j > i else 0)
            if j > i and size + add > max_chars:
                break
            size += add
            piece = " ".join(words[i : j + 1])
            # prefer comma / semicolon end; else any non-dangling end past 55%
            if words[j].endswith((",", ";", ":")):
                best = j + 1
            elif size >= max_chars * 0.55 and not _dangling_end(piece):
                best = j + 1
            j += 1
        if best <= i:
            best = min(n, i + 1)
        # if still dangling and more words remain, pull one more if room
        piece = " ".join(words[i:best])
        while best < n and _dangling_end(piece):
            nxt = " ".join(words[i : best + 1])
            if len(nxt) > int(max_chars * 1.25) and best > i + 1:
                break
            best += 1
            piece = nxt
        out.append(piece)
        i = best
    return out


def _soft_split_long(text: str, max_chars: int) -> list[str]:
    """Break long unpunctuated rants on soft clause boundaries, then wrap."""
    text = _normalize_cue_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    cuts = [0]
    for m in _SOFT_CLAUSE_RE.finditer(text):
        pos = m.start()
        if pos <= 0 or pos >= len(text):
            continue
        left = pos - cuts[-1]
        if left < max(24, max_chars // 5):
            continue
        cuts.append(pos)
    cuts.append(len(text))

    raw = [text[a:b].strip(" ,;") for a, b in zip(cuts, cuts[1:])]
    raw = [p for p in raw if p]
    if len(raw) <= 1:
        return _force_wrap(text, max_chars)

    packed: list[str] = []
    buf = raw[0]
    for piece in raw[1:]:
        cand = _normalize_cue_text(f"{buf} {piece}")
        if len(buf) < max_chars * 0.6 and len(cand) <= int(max_chars * 1.1):
            buf = cand
        else:
            packed.extend(_force_wrap(buf, max_chars) if len(buf) > max_chars else [buf])
            buf = piece
    packed.extend(_force_wrap(buf, max_chars) if len(buf) > max_chars else [buf])
    return packed


def _allocate_times(
    start: float, end: float, parts: list[str]
) -> list[tuple[float, float, str]]:
    """Spread [start,end] across text parts by character weight."""
    if not parts:
        return []
    if len(parts) == 1:
        return [(start, end, parts[0])]
    weights = [max(1, len(p)) for p in parts]
    total_w = float(sum(weights))
    dur = max(0.05, end - start)
    out: list[tuple[float, float, str]] = []
    cursor = start
    for i, (part, w) in enumerate(zip(parts, weights)):
        if i == len(parts) - 1:
            out.append((cursor, end, part))
        else:
            slice_dur = dur * (w / total_w)
            nxt = min(end, cursor + slice_dur)
            if nxt <= cursor:
                nxt = min(end, cursor + 0.05)
            out.append((cursor, nxt, part))
            cursor = nxt
    return out


def merge_cues(
    cues: list[tuple[float, float, str]],
    max_gap: float = 0.75,
    target_len: float = 3.5,
    max_len: float = 5.5,
    max_chars: int = 110,
    min_seg_dur: float = 1.2,
    min_seg_words: int = 3,
) -> list[Segment]:
    """
    Fallback packer when word timestamps are missing.

    1) Chain tight cues into runs (pause = hard cut).
    2) Split each run on real sentence punctuation (handles mid-cue periods).
    3) Re-glue only unfinished fragments.
    4) Pack into ~3.5s breath-sized segments for TTS.
    """
    if not cues:
        return []

    # --- phase 1: chain cues into pause-separated runs ---
    runs: list[tuple[float, float, str]] = []
    rs, re_, rt = cues[0]
    for st, en, tx in cues[1:]:
        gap = st - re_
        if gap > max_gap:
            text = _normalize_cue_text(rt)
            if text:
                runs.append((rs, re_, text))
            rs, re_, rt = st, en, tx
        else:
            rt = _join_text(rt, tx)
            re_ = en
    text = _normalize_cue_text(rt)
    if text:
        runs.append((rs, re_, text))

    # --- phase 2: split runs into sentence units (mid-cue aware) ---
    units: list[tuple[float, float, str]] = []
    for st, en, tx in runs:
        parts: list[str] = []
        for sent in _split_sentences(tx):
            # soft-split / wrap anything still too long for one TTS slot
            if len(sent) > max_chars:
                parts.extend(_soft_split_long(sent, max_chars))
            else:
                parts.append(sent)
        units.extend(_allocate_times(st, en, parts))

    if not units:
        return []

    # --- phase 3: glue only true mid-sentence fragments ---
    # Do NOT re-merge every unpunctuated clause (soft-split pieces stay separate
    # so phase 4 can pack them to target length).
    sentences: list[tuple[float, float, str]] = [units[0]]
    for st, en, tx in units[1:]:
        ps, pe, pt = sentences[-1]
        gap = st - pe
        if gap <= max_gap and _is_mid_sentence_cut(pt, tx):
            sentences[-1] = (ps, en, _normalize_cue_text(_join_text(pt, tx)))
        else:
            sentences.append((st, en, tx))

    # --- phase 4: pack complete sentences into TTS-sized segments ---
    segs: list[Segment] = []
    idx = 0
    bun_s, bun_e, bun_t = sentences[0]

    def flush() -> None:
        nonlocal idx, bun_s, bun_e, bun_t
        text = _normalize_cue_text(bun_t)
        if text:
            segs.append(Segment(idx=idx, start=bun_s, end=bun_e, en=text))
            idx += 1

    for st, en, tx in sentences[1:]:
        gap = st - bun_e
        cand = _normalize_cue_text(_join_text(bun_t, tx))
        cand_dur = en - bun_s
        bun_dur = bun_e - bun_s
        mid = gap <= max_gap and _is_mid_sentence_cut(bun_t, tx)
        tiny = bun_dur < min_seg_dur or len(bun_t.split()) < min_seg_words
        fits = cand_dur <= max_len and len(cand) <= max_chars
        under = bun_dur < target_len

        if gap > max_gap:
            flush()
            bun_s, bun_e, bun_t = st, en, tx
        elif mid:
            bun_t, bun_e = cand, en
        elif (tiny or under) and fits:
            bun_t, bun_e = cand, en
        else:
            flush()
            bun_s, bun_e, bun_t = st, en, tx
    flush()

    # --- phase 5: absorb micro fragments without creating long rants ---
    if len(segs) < 2:
        return segs

    fixed: list[Segment] = [segs[0]]
    for nxt in segs[1:]:
        prev = fixed[-1]
        gap = nxt.start - prev.end
        cand = _normalize_cue_text(_join_text(prev.en, nxt.en))
        cand_dur = nxt.end - prev.start
        mid = gap <= max_gap and _is_mid_sentence_cut(prev.en, nxt.en)
        prev_micro = (
            (prev.end - prev.start) < min_seg_dur or len(prev.en.split()) < min_seg_words
        )
        nxt_micro = (
            (nxt.end - nxt.start) < min_seg_dur or len(nxt.en.split()) < min_seg_words
        )
        fits = cand_dur <= max_len * 1.1 and len(cand) <= int(max_chars * 1.1)
        if mid or (gap <= max_gap and (prev_micro or nxt_micro) and fits):
            prev.end = nxt.end
            prev.en = cand
        else:
            fixed.append(nxt)

    for i, s in enumerate(fixed):
        s.idx = i
    return fixed

