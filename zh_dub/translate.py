"""ModelScope (OpenAI-compatible) batch translation with an en->zh cache."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import Settings
from .logutil import detail, highlight, info, progress, warn
from .media import clear_proxy_env
from .segments import Segment
from .state import JobState

CACHE_NAME = "translations.json"
NUM_LINE_RE = re.compile(r"(?m)^\s*(\d+)\.\s*(.+?)\s*$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class TranslateError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class KeyPool:
    """API keys; one reporting insufficient balance is retired for the process."""

    def __init__(self, keys: list[str]):
        self.keys = list(keys)
        self._retired: set[str] = set()
        self._lock = threading.Lock()

    def current(self) -> str:
        with self._lock:
            for k in self.keys:
                if k not in self._retired:
                    return k
        raise TranslateError(f"所有 API_KEY({len(self.keys)} 个)均余额不足", retryable=False)

    def retire(self, key: str) -> bool:
        """Retire key; True if another key is still alive."""
        with self._lock:
            self._retired.add(key)
            return any(k not in self._retired for k in self.keys)


_POOLS: dict[tuple[str, ...], KeyPool] = {}


def key_pool(settings: Settings) -> KeyPool:
    # shared across batch items so a drained key is not retried per video
    ident = tuple(settings.api_keys)
    if ident not in _POOLS:
        _POOLS[ident] = KeyPool(settings.api_keys)
    return _POOLS[ident]


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


def looks_untranslated(zh: str, en: str) -> bool:
    """True when the model echoed the English line back instead of translating.

    Any Chinese character means it really is a translation; otherwise compare
    the bare alphanumerics (``"75%"`` for ``"75 percent"`` is not an echo).
    """
    if _CJK_RE.search(zh):
        return False
    strip = lambda s: re.sub(r"[^0-9a-z]+", "", s.lower())  # noqa: E731
    a, b = strip(zh), strip(en)
    return bool(a) and a == b


def _clean_zh(zh: str) -> str:
    zh = zh.replace(",", "，")
    zh = re.sub(r"[ ]{2,}", " ", zh).strip("，, ")
    for filler in ("你知道的，", "好吧，", "嗯，", "呃，"):
        zh = zh.replace(filler, "")
    return zh


def _clean_title_zh(zh: str) -> str:
    t = (zh or "").strip().strip("\"'“” 。. ")
    return re.sub(r"\s+", " ", t).strip()


_ASR_FIX_HINT = (
    "输入来自 YouTube 自动生成字幕，常有听写错误。请先在脑中纠正再翻译，"
    "最终中文按纠正后的交易术语表达。常见误识别包括但不限于：\n"
    "- bare bar / bare bars → bear bar / bear bars\n"
    "- bare channel / tight bare channel → bear channel / tight bear channel\n"
    "- bull/bare 混淆时，结合上下文（上涨/下跌通道、突破方向）判断\n"
    "- 数字、百分比、K线位置描述尽量保留准确含义\n"
)


def _build_prompt(payload: str, n: int, with_title: bool) -> tuple[str, str]:
    title_rule = (
        "6) 最后一条是视频标题，不是口播字幕：译成简洁中文标题，"
        "不要口播腔、不要解释、不要句号\n"
        if with_title
        else ""
    )
    system = (
        "你是专业金融/价格行为学字幕翻译与校对。把英文讲解译成简洁自然的中文口播稿。\n"
        f"{_ASR_FIX_HINT}"
        "硬性要求：\n"
        "1) 严格保留编号，每行一条，格式只能是：`N. 中文`\n"
        "2) 不要时间戳、不要解释、不要合并或删除条目；输出条数必须等于输入条数\n"
        "3) 去掉 um/you know/okay/I mean 等填充词\n"
        "4) 由于需要配音，你需要考虑中文和英文的语音长度的问题，适当调整翻译内容，让翻译的配音时长尽可能一致，中文可以略微短一点点。\n"
        "5) 最终只输出编号译文，不要输出思考过程或其它内容\n"
        f"{title_rule}"
    )
    if with_title:
        user = (
            f"共{n}条。前 {n-1} 条是自动字幕口播，最后一条是视频标题。\n"
            f"请开启深度思考：字幕先校对听写错误再译成中文口播；标题译成简洁中文标题。\n"
            f"只输出 {n} 行 `N. 中文`：\n\n{payload}"
        )
    else:
        user = (
            f"共{n}条自动字幕。请开启深度思考：先校对听写错误，再翻译成中文口播。\n"
            f"只输出 {n} 行 `N. 中文`：\n\n{payload}"
        )
    return system, user


def _chat(settings: Settings, pool: KeyPool, system: str, user: str) -> Any:
    """One completion; drained keys rotate transparently (not counted as retry)."""
    from openai import OpenAI

    clear_proxy_env()
    kwargs = {
        "model": settings.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "extra_body": {"thinking": {"type": "enabled"}},
    }
    while True:
        key = pool.current()
        client = OpenAI(api_key=key, base_url=settings.modelscope_base_url)
        try:
            try:
                return client.chat.completions.create(reasoning_effort="high", **kwargs)
            except TypeError:
                return client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                low = str(e).lower()
                if "reasoning_effort" in low or "unexpected" in low:
                    return client.chat.completions.create(**kwargs)
                raise
        except Exception as e:  # noqa: BLE001
            low = str(e).lower()
            if "insufficient balance" not in low and "insufficient_quota" not in low:
                raise
            if not pool.retire(key):
                raise TranslateError(f"所有 API_KEY({len(pool.keys)} 个)余额不足: {e}", retryable=False) from e
            warn("Key 余额不足，切换下一个 key 重投（不计入重试次数）")


def translate_chunk(
    settings: Settings, texts: list[str], *, title_en: str = ""
) -> tuple[dict[int, str], str]:
    """Translate one batch. Returns ({1-based no: zh}, title_zh).

    title_en, if set, rides along as the last numbered line of the same request.
    """
    lines = list(texts) + ([title_en] if title_en else [])
    n = len(lines)
    payload = "\n".join(f"{i}. {drop_fillers(t)}" for i, t in enumerate(lines, start=1))
    system, user = _build_prompt(payload, n, bool(title_en))
    retries = settings.translate_max_retries
    pool = key_pool(settings)

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            detail(f"API 尝试 {attempt}/{retries}  (n={n}, thinking on)")
            resp = _chat(settings, pool, system, user)
            msg = resp.choices[0].message if resp is not None and getattr(resp, "choices", None) else None
            if msg is None:
                raise TranslateError("empty API response")
            out = (msg.content or "").strip()
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if reasoning:
                detail(f"thinking_chars={len(reasoning)}")
            usage = getattr(resp, "usage", None)
            if usage:
                detail(f"tokens prompt={usage.prompt_tokens} completion={usage.completion_tokens}")
            out = out or reasoning
            if not out:
                raise TranslateError("empty content from model")

            out = re.sub(r"^```(?:text|markdown)?\s*", "", out)
            out = re.sub(r"\s*```$", "", out).strip()
            fixed = {no: _clean_zh(zh) for no, zh in parse_numbered_zh(out, n).items()}
            title_zh = _clean_title_zh(fixed.pop(n, "")) if title_en else ""
            missing = [no for no in range(1, len(texts) + 1) if no not in fixed]
            if missing and len(missing) / max(1, n) > 0.05 and attempt < retries:
                warn(f"缺 {len(missing)} 条，重试... 样例 {missing[:8]}")
                last_err = TranslateError(f"missing {len(missing)} ids")
                time.sleep(1.2 * attempt)
                continue
            if missing:
                warn(f"重试后仍缺 {len(missing)} 条，接受当前结果")
            return fixed, title_zh
        except TranslateError as e:
            last_err = e
            warn(f"翻译尝试 {attempt} 失败: {e}")
            if not e.retryable:
                raise
            time.sleep(5 * attempt)
        except Exception as e:  # noqa: BLE001
            last_err = e
            warn(f"翻译尝试 {attempt} 失败: {e}")
            low = str(e).lower()
            if any(x in low for x in ("401", "403", "invalid api", "authentication")):
                raise TranslateError(str(e), retryable=False) from e
            time.sleep(5 * attempt)

    raise TranslateError(f"ModelScope translate failed after {retries} attempts: {last_err}")


def _translate_batch(
    settings: Settings, texts: list[str], title_en: str
) -> tuple[dict[str, str], str]:
    """Translate texts; on retryable failure of a large batch, degrade to 40-line pieces."""
    try:
        mapped, title_zh = translate_chunk(settings, texts, title_en=title_en)
        return {texts[no - 1]: zh for no, zh in mapped.items() if no <= len(texts)}, title_zh
    except TranslateError as e:
        if len(texts) <= 40 or not e.retryable:
            raise
    warn(f"batch({len(texts)}) 降级拆分重试")
    got: dict[str, str] = {}
    title_zh = ""
    for j in range(0, len(texts), 40):
        part, t = _translate_batch(settings, texts[j : j + 40], title_en if j == 0 else "")
        got.update(part)
        title_zh = title_zh or t
    return got, title_zh


def translate_segments(
    settings: Settings, state: JobState, segs: list[Segment], *, title_en: str = ""
) -> dict[str, Any]:
    """Fill empty seg.zh from the en->zh cache, translating cache misses.

    Round 0 is the concurrent main pass; later rounds re-send only what is
    still missing, serially, in small batches. Missing lines keep zh="" and
    are retried on the next run.
    """
    cache: dict[str, str] = state.read_json(CACHE_NAME) or {}
    lock = threading.Lock()
    todo = list(dict.fromkeys(s.en.strip() for s in segs if not s.zh.strip() and s.en.strip()))
    need_title = bool(title_en) and not state.meta.get("title_zh")
    title_zh = ""

    def pending() -> list[str]:
        return [t for t in todo if t not in cache]

    def commit(got: dict[str, str]) -> int:
        n = 0
        with lock:
            for en, zh in got.items():
                if zh and not looks_untranslated(zh, en):
                    cache[en] = zh
                    n += 1
            state.write_json(CACHE_NAME, cache)
        return n

    def run_round(texts: list[str], size: int, workers: int, strict: bool) -> int:
        nonlocal title_zh, need_title
        chunks = [texts[i : i + size] for i in range(0, len(texts), size)]
        total_got = 0
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(chunks)))) as ex:
            futs = {}
            for ci, chunk in enumerate(chunks):
                carry = title_en if need_title and ci == 0 else ""
                futs[ex.submit(_translate_batch, settings, chunk, carry)] = (ci, chunk)
            for fut in as_completed(futs):
                ci, chunk = futs[fut]
                t0 = time.time()
                try:
                    got, t = fut.result()
                except Exception as e:  # noqa: BLE001
                    if strict:
                        for other in futs:
                            other.cancel()
                        raise
                    warn(f"补译 batch {ci+1}/{len(chunks)} 失败: {e}")
                    continue
                if t and not title_zh:
                    title_zh = t
                n = commit(got)
                total_got += n
                progress(
                    ci + 1,
                    len(chunks),
                    f"{n}/{len(chunk)}  {time.time()-t0:.1f}s",
                    label="翻译",
                )
        if title_zh:
            need_title = False
        return total_got

    info(
        f"翻译  segments={len(segs)}  待翻={len(todo)}  已缓存={len(todo) - len(pending())}  "
        f"batch={settings.translate_batch_size}  concurrency={settings.translate_concurrency}"
    )
    rounds = 0
    if pending() or need_title:
        texts = pending()
        run_round(texts, max(1, settings.translate_batch_size), settings.translate_concurrency, True)
        for rounds in range(1, settings.translate_refill_max_rounds + 1):
            miss = pending()
            if not miss:
                break
            highlight(f"补译第 {rounds}/{settings.translate_refill_max_rounds} 轮  missing={len(miss)}")
            if run_round(miss, min(settings.translate_batch_size, 40), 1, False) == 0:
                warn(f"补译第 {rounds} 轮零进展，停止")
                break

    filled = 0
    for s in segs:
        if not s.zh.strip() and s.en.strip() in cache:
            s.zh = cache[s.en.strip()]
            filled += 1
    missing = [s.idx for s in segs if s.en.strip() and not s.zh.strip()]
    if missing:
        warn(f"仍有 {len(missing)} 条未译（不配音、不出字幕，下次运行会重试）: {missing[:20]}")
    if title_zh and title_zh != title_en:
        state.update_meta(title_zh=title_zh)
        info(f"标题  ZH: {title_zh}")
    elif need_title:
        warn("标题未随正文译出，导出用英文标题")
    highlight(f"翻译完成  filled={filled}  missing={len(missing)}  refill_rounds={rounds}")
    return {"filled": filled, "missing": len(missing), "refill_rounds": rounds}
