from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .captions import drop_fillers, parse_numbered_zh
from .config import Settings
from .logutil import (
    detail,
    highlight,
    info,
    progress,
    resume_hint,
    stage,
    stage_done,
    stage_error,
    warn,
)
from .media import clear_proxy_env
from .state import JobState


class TranslateError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True, **extra: Any):
        super().__init__(message)
        self.retryable = retryable
        self.extra = extra


# --- API key pool: 余额不足的 key 本轮永久弃用，切下一个 ---
_KEY_LOCK = threading.Lock()
_KEYS: tuple[str, ...] = ()
_RETIRED: set[str] = set()


def _ensure_keys(keys: tuple[str, ...]) -> str:
    """Rebuild the pool when the key set changes; return the current alive key."""
    global _KEYS, _RETIRED
    if keys != _KEYS:
        _KEYS, _RETIRED = keys, set()
    for k in _KEYS:
        if k not in _RETIRED:
            return k
    raise TranslateError("所有 API_KEY 均余额不足", retryable=False)


def _client(settings: Settings) -> tuple[Any, str]:
    from openai import OpenAI

    clear_proxy_env()
    with _KEY_LOCK:
        key = _ensure_keys(tuple(settings.api_keys))
    return OpenAI(api_key=key, base_url=settings.modelscope_base_url), key


def _is_balance_error(low: str) -> bool:
    return "insufficient balance" in low or "insufficient_quota" in low


def _rotate_from(key: str) -> str | None:
    """Retire the failing key; return the next alive key, or None if exhausted."""
    with _KEY_LOCK:
        _RETIRED.add(key)
        for k in _KEYS:
            if k not in _RETIRED:
                return k
        return None


def _is_english_fallback(zh: str | None, en: str) -> bool:
    """True when zh is just the English fallback (drop_fillers(en))."""
    if not zh or not str(zh).strip():
        return False
    en_fb = drop_fillers(en).strip()
    if not en_fb:
        return False
    return zh.strip().lower() == en_fb.lower()


def _clean_title_zh(zh: str, fallback: str) -> str:
    t = (zh or "").strip()
    t = t.strip("\"'“” 。. ")
    t = re.sub(r"\s+", " ", t).strip()
    return t or fallback


def translate_chunk(
    settings: Settings,
    items: list[tuple[int, str]],
    *,
    max_retries: int | None = None,
    extra_title: str | None = None,
) -> dict[int, str]:
    """Translate one batch. items: local 1-based no -> en text.

    extra_title, if set, is appended as the last numbered line in the same
    request (video title, not narration). Caller should pop that extra id.
    """
    model = settings.model
    work_items = list(items)
    title_en = (extra_title or "").strip()
    if title_en:
        next_no = (max(no for no, _ in work_items) + 1) if work_items else 1
        work_items.append((next_no, title_en))
    n = len(work_items)
    retries = max_retries if max_retries is not None else settings.translate_max_retries

    cleaned_lines = [f"{no}. {drop_fillers(raw)}" for no, raw in work_items]
    payload = "\n".join(cleaned_lines)

    asr_fix_hint = (
        "输入来自 YouTube 自动生成字幕，常有听写错误。请先在脑中纠正再翻译，"
        "最终中文按纠正后的交易术语表达。常见误识别包括但不限于：\n"
        "- bare bar / bare bars → bear bar / bear bars\n"
        "- bare channel / tight bare channel → bear channel / tight bear channel\n"
        "- bull/bare 混淆时，结合上下文（上涨/下跌通道、突破方向）判断\n"
        "- 数字、百分比、K线位置描述尽量保留准确含义\n"
    )
    title_rule = ""
    if title_en:
        title_rule = (
            "6) 最后一条是视频标题，不是口播字幕：译成简洁中文标题，"
            "不要口播腔、不要解释、不要句号\n"
        )
    system = (
        "你是专业金融/价格行为学字幕翻译与校对。把英文讲解译成简洁自然的中文口播稿。\n"
        f"{asr_fix_hint}"
        "硬性要求：\n"
        "1) 严格保留编号，每行一条，格式只能是：`N. 中文`\n"
        "2) 不要时间戳、不要解释、不要合并或删除条目；输出条数必须等于输入条数\n"
        "3) 去掉 um/you know/okay/I mean 等填充词\n"
        "4) 由于需要配音，你需要考虑中文和英文的语音长度的问题，适当调整翻译内容，让翻译的配音时长尽可能一致，中文可以略微短一点点。\n"
        "5) 最终只输出编号译文，不要输出思考过程或其它内容\n"
        f"{title_rule}"
    )
    if title_en:
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

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            while True:  # 余额不足切 key 重投，不计入重试次数
                client, used_key = _client(settings)
                detail(f"API 尝试 {attempt}/{retries}  (n={n}, thinking on)")
                create_kwargs = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.2,
                    "extra_body": {"thinking": {"type": "enabled"}},
                }
                try:
                    try:
                        resp = client.chat.completions.create(
                            reasoning_effort="high", **create_kwargs
                        )
                    except TypeError:
                        resp = client.chat.completions.create(**create_kwargs)
                    except Exception as e1:
                        if (
                            "reasoning_effort" in str(e1).lower()
                            or "unexpected" in str(e1).lower()
                        ):
                            resp = client.chat.completions.create(**create_kwargs)
                        else:
                            raise
                except Exception as e:  # noqa: BLE001
                    low = str(e).lower()
                    if not _is_balance_error(low):
                        raise
                    warn(f"翻译尝试 {attempt} 失败: {e}")
                    if _rotate_from(used_key) is None:
                        raise TranslateError(
                            f"所有 API_KEY({len(settings.api_keys)} 个)"
                            f"余额不足: {e}",
                            retryable=False,
                        ) from e
                    warn("Key 余额不足，切换下一个 key 重投（不计入重试次数）")
                    continue
                break

            if resp is None or not getattr(resp, "choices", None):
                raise TranslateError(
                    "empty API response (no choices)",
                    retryable=True,
                    attempt=attempt,
                )
            msg = resp.choices[0].message
            if msg is None:
                raise TranslateError("message is null", retryable=True, attempt=attempt)
            out = (msg.content or "").strip()
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if reasoning:
                detail(f"thinking_chars={len(reasoning)}")
            usage = getattr(resp, "usage", None)
            if usage:
                detail(
                    f"tokens prompt={usage.prompt_tokens} "
                    f"completion={usage.completion_tokens}"
                )
            if not out and reasoning:
                out = reasoning
            if not out:
                raise TranslateError(
                    "empty content from model",
                    retryable=True,
                    attempt=attempt,
                )

            out = re.sub(r"^```(?:text|markdown)?\s*", "", out)
            out = re.sub(r"\s*```$", "", out).strip()
            parsed = parse_numbered_zh(out, n)
            fixed: dict[int, str] = {}
            for no, zh in parsed.items():
                zh2 = zh.replace(",", "，")
                zh2 = re.sub(r"[ ]{2,}", " ", zh2).strip("，, ")
                for a, b in [("你知道的，", ""), ("好吧，", ""), ("嗯，", ""), ("呃，", "")]:
                    zh2 = zh2.replace(a, b)
                fixed[no] = zh2

            missing = [no for no, _ in items if no not in fixed]
            miss_ratio = len(missing) / max(1, n)
            if missing and miss_ratio > 0.05 and attempt < retries:
                warn(f"缺 {len(missing)} 条，重试... 样例 {missing[:8]}")
                last_err = TranslateError(
                    f"missing {len(missing)} ids",
                    retryable=True,
                    missing=len(missing),
                )
                time.sleep(1.2 * attempt)
                continue
            if missing:
                warn(f"重试后仍缺 {len(missing)} 条，接受当前结果")
            return fixed
        except TranslateError as e:
            last_err = e
            warn(f"翻译尝试 {attempt} 失败: {e}")
            if not e.retryable:
                raise
            time.sleep(5 * attempt)
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e)
            retryable = True
            low = msg.lower()
            if any(x in low for x in ("401", "403", "invalid api", "authentication")):
                retryable = False
            warn(f"翻译尝试 {attempt} 失败: {e}")
            if not retryable:
                raise TranslateError(msg, retryable=False) from e
            time.sleep(5 * attempt)

    raise TranslateError(
        f"ModelScope translate failed after {retries} attempts: {last_err}",
        retryable=True,
    )


def _split_title_line(
    mapped: dict[int, str], chunk_len: int
) -> tuple[dict[int, str], str]:
    """Keep 1..chunk_len as captions; extra last id is the piggybacked title."""
    segs: dict[int, str] = {}
    title_zh = ""
    for no, zh in mapped.items():
        if 1 <= no <= chunk_len:
            segs[no] = zh
        elif no == chunk_len + 1 and isinstance(zh, str) and zh.strip() and not title_zh:
            title_zh = zh.strip()
    return segs, title_zh


def _run_one_batch(
    settings: Settings,
    bi: int,
    chunk: list[str],
    extra_title: str | None = None,
) -> tuple[int, dict[int, str], float, str]:
    """Translate one batch (with optional size degrade). Returns (bi, mapped, elapsed, title_zh)."""
    items = [(i + 1, t) for i, t in enumerate(chunk)]
    t0 = time.time()
    title_zh = ""
    try:
        mapped = translate_chunk(settings, items, extra_title=extra_title)
        mapped, title_zh = _split_title_line(mapped, len(chunk))
    except TranslateError as e:
        if len(chunk) > 40 and e.retryable:
            warn(f"batch {bi+1} 降级拆分重试")
            mapped = {}
            sub = 40
            for sj in range(0, len(chunk), sub):
                sub_items = [
                    (i + 1, t) for i, t in enumerate(chunk[sj : sj + sub])
                ]
                extra = extra_title if sj == 0 else None
                part = translate_chunk(settings, sub_items, extra_title=extra)
                part, title_part = _split_title_line(part, len(sub_items))
                if title_part and not title_zh:
                    title_zh = title_part
                for local_no, zh in part.items():
                    mapped[sj + local_no] = zh
        else:
            raise
    return bi, mapped, time.time() - t0, title_zh


def translate_segments(
    settings: Settings,
    state: JobState,
    en_texts: list[str],
    *,
    force: bool = False,
    title_en: str | None = None,
) -> list[str]:
    """Batch translate with per-batch checkpoints, concurrency, and post refill."""
    total = len(en_texts)
    batch_size = max(1, settings.translate_batch_size)
    concurrency = max(1, settings.translate_concurrency)
    n_batches = (total + batch_size - 1) // batch_size if total else 0
    stage(
        "translate",
        f"total={total} batches={n_batches} size={batch_size} "
        f"concurrency={concurrency} model={settings.model}",
    )
    info(
        f"翻译启动  lines={total}  batches={n_batches}  "
        f"batch_size={batch_size}  concurrency={concurrency}  model={settings.model}"
    )
    out: list[str | None] = [None] * total
    done_batches: list[int] = []
    lock = threading.Lock()

    # load existing batch checkpoints
    for bi in range(n_batches):
        name = f"translate/batch_{bi:03d}.json"
        data = state.read_json(name)
        if not data:
            continue
        mapping = data.get("mapping") or {}
        start = bi * batch_size
        ok_n = 0
        for k, v in mapping.items():
            local = int(k)
            abs_i = start + local - 1
            if 0 <= abs_i < total and isinstance(v, str) and v.strip():
                # skip English fallback leftovers stored as "translation"
                if _is_english_fallback(v, en_texts[abs_i]):
                    continue
                out[abs_i] = v.strip()
                ok_n += 1
        expected = int(data.get("expected", 0))
        # only treat batch as done if enough real zh (not English fallback)
        if expected > 0 and ok_n >= int(expected * 0.95):
            done_batches.append(bi)
            info(f"恢复 batch {bi+1}/{n_batches}: 已加载 {ok_n} 行")
        elif ok_n:
            info(f"恢复 batch {bi+1}/{n_batches}: 部分 {ok_n}/{expected}，将重翻缺失")

    # load previous refill results (absolute index -> zh)
    if not force:
        refill_data = state.read_json("translate/refill.json") or {}
        refill_map = refill_data.get("mapping") or {}
        loaded_refill = 0
        for k, v in refill_map.items():
            abs_i = int(k)
            if 0 <= abs_i < total and isinstance(v, str) and v.strip():
                if _is_english_fallback(v, en_texts[abs_i]):
                    continue
                if not out[abs_i]:
                    out[abs_i] = v.strip()
                    loaded_refill += 1
        if loaded_refill:
            info(f"恢复二次补译: 已加载 {loaded_refill} 行")

    if force:
        done_batches = []
        out = [None] * total
        refill_path = state.checkpoint_path("translate/refill.json")
        if refill_path.is_file():
            refill_path.unlink()
        warn("强制重翻全部 batch")

    # Treat English fallback leftovers as missing so second-pass refill can fix them
    cleared_fb = 0
    for i, zh in enumerate(out):
        if _is_english_fallback(zh, en_texts[i]):
            out[i] = None
            cleared_fb += 1
    if cleared_fb:
        warn(f"清除 {cleared_fb} 条英文回填，准备二次补译")

    pending = [bi for bi in range(n_batches) if bi not in done_batches]
    title_en = (title_en or "").strip()
    existing_zh = str((state.data.get("meta") or {}).get("title_zh") or "").strip()
    title_zh = existing_zh if existing_zh and existing_zh != title_en else ""
    need_title = bool(title_en) and not title_zh
    title_carrier = pending[0] if pending and need_title else None
    if title_carrier is not None:
        info("标题随第一批正文一起翻译（不另发请求）")
    highlight(
        f"待翻 batch {[b+1 for b in pending]}  "
        f"(已完成 {len(done_batches)}/{n_batches}, 并发={concurrency})"
    )
    state.set_running("translate")
    state.data["stages"]["translate"]["detail"] = {
        "total": total,
        "batch_size": batch_size,
        "n_batches": n_batches,
        "concurrency": concurrency,
        "done_batches": [b + 1 for b in done_batches],
        "pending_batches": [b + 1 for b in pending],
    }
    state.save()

    def _commit_batch(bi: int, mapped: dict[int, str], elapsed: float) -> None:
        start = bi * batch_size
        chunk_len = min(batch_size, total - start)
        with lock:
            mapping: dict[str, str] = {}
            for local_no, zh in mapped.items():
                if not isinstance(zh, str) or not zh.strip():
                    continue
                abs_i = start + local_no - 1
                if not (0 <= abs_i < total):
                    continue
                if _is_english_fallback(zh, en_texts[abs_i]):
                    continue
                text = zh.strip()
                out[abs_i] = text
                mapping[str(local_no)] = text
                if local_no <= 3 or abs_i % 50 == 0:
                    detail(f"ZH[{abs_i:04d}] {text}")

            state.write_json(
                f"translate/batch_{bi:03d}.json",
                {
                    "batch_index": bi,
                    "start": start,
                    "expected": chunk_len,
                    "got": len(mapping),
                    "elapsed_s": round(elapsed, 2),
                    "mapping": mapping,
                },
            )
            if bi not in done_batches:
                done_batches.append(bi)
            state.data["stages"]["translate"]["detail"] = {
                "total": total,
                "batch_size": batch_size,
                "n_batches": n_batches,
                "concurrency": concurrency,
                "done_batches": [b + 1 for b in sorted(done_batches)],
                "pending_batches": [
                    b + 1 for b in range(n_batches) if b not in done_batches
                ],
                "done_lines": sum(1 for x in out if x),
            }
            state.save()
            highlight(
                f"batch {bi+1}/{n_batches} 完成  "
                f"{len(mapping)}/{chunk_len}  {elapsed:.1f}s"
            )

    if pending:
        workers = min(concurrency, len(pending))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for bi in pending:
                start = bi * batch_size
                chunk = en_texts[start : start + batch_size]
                progress(
                    len(done_batches) + 1,
                    n_batches,
                    f"提交 batch {bi+1}  lines {start+1}-{start+len(chunk)}",
                )
                extra = title_en if bi == title_carrier else None
                fut = pool.submit(_run_one_batch, settings, bi, chunk, extra)
                futures[fut] = bi

            for fut in as_completed(futures):
                bi = futures[fut]
                try:
                    bi2, mapped, elapsed, title_part = fut.result()
                    if title_part and not title_zh:
                        title_zh = title_part
                    _commit_batch(bi2, mapped, elapsed)
                except Exception as e:  # noqa: BLE001
                    for other in futures:
                        other.cancel()
                    stage_error("translate", f"batch {bi+1}/{n_batches}: {e}")
                    state.set_failed(
                        "translate",
                        str(e),
                        retryable=getattr(e, "retryable", True),
                        batch=bi + 1,
                        done_batches=[b + 1 for b in done_batches],
                        saved_lines=sum(1 for x in out if x),
                    )
                    resume_hint(state.work)
                    raise

    # After ALL main batches finish: multi-round refill for missing lines
    def _missing_idxs() -> list[int]:
        return [
            i
            for i, zh in enumerate(out)
            if not (zh and str(zh).strip())
            or _is_english_fallback(zh, en_texts[i])
        ]

    refill_max_rounds = max(0, settings.translate_refill_max_rounds)
    refilled = 0
    rounds_ran = 0
    refill_mapping: dict[str, str] = {}
    prev_refill = state.read_json("translate/refill.json") or {}
    for k, v in (prev_refill.get("mapping") or {}).items():
        abs_i = int(k)
        if (
            0 <= abs_i < total
            and isinstance(v, str)
            and v.strip()
            and out[abs_i]
            and not _is_english_fallback(v, en_texts[abs_i])
        ):
            refill_mapping[str(abs_i)] = v.strip()

    for round_i in range(1, refill_max_rounds + 1):
        missing_idxs = _missing_idxs()
        for i in missing_idxs:
            out[i] = None
        if not missing_idxs:
            if round_i == 1:
                info("无需补译：主翻译无缺失")
            break

        refill_batch = min(batch_size, 40)
        n_refill = (len(missing_idxs) + refill_batch - 1) // refill_batch
        highlight(
            f"补译第 {round_i}/{refill_max_rounds} 轮  "
            f"missing={len(missing_idxs)}  batches={n_refill}  size={refill_batch}"
        )
        round_got = 0
        for ri in range(n_refill):
            group = missing_idxs[ri * refill_batch : (ri + 1) * refill_batch]
            items = [(j + 1, en_texts[abs_i]) for j, abs_i in enumerate(group)]
            progress(
                ri + 1,
                n_refill,
                f"R{round_i} 补译 lines {[g + 1 for g in group[:5]]}"
                + ("..." if len(group) > 5 else ""),
            )
            t0 = time.time()
            extra = (
                title_en
                if need_title and not title_zh and round_i == 1 and ri == 0
                else None
            )
            try:
                mapped = translate_chunk(settings, items, extra_title=extra)
                mapped, title_part = _split_title_line(mapped, len(group))
                if title_part and not title_zh:
                    title_zh = title_part
            except Exception as e:  # noqa: BLE001
                warn(
                    f"补译 R{round_i} batch {ri+1}/{n_refill} 失败: {e}"
                )
                continue

            got = 0
            for local_no, zh in mapped.items():
                if not (1 <= local_no <= len(group)):
                    continue
                if not isinstance(zh, str) or not zh.strip():
                    continue
                abs_i = group[local_no - 1]
                if _is_english_fallback(zh, en_texts[abs_i]):
                    continue
                out[abs_i] = zh.strip()
                refill_mapping[str(abs_i)] = zh.strip()
                refilled += 1
                got += 1
                round_got += 1
                detail(f"REFILL R{round_i} ZH[{abs_i:04d}] {zh.strip()}")

            state.write_json(
                "translate/refill.json",
                {
                    "round": round_i,
                    "max_rounds": refill_max_rounds,
                    "missing": len(missing_idxs),
                    "refilled": len(refill_mapping),
                    "mapping": refill_mapping,
                },
            )
            highlight(
                f"补译 R{round_i} batch {ri+1}/{n_refill} 完成  "
                f"{got}/{len(group)}  {time.time()-t0:.1f}s"
            )

        rounds_ran = round_i
        still = len(_missing_idxs())
        highlight(
            f"补译第 {round_i}/{refill_max_rounds} 轮结束  "
            f"本轮+{round_got}  still_missing={still}"
        )
        if still == 0:
            info(f"补译完成：第 {round_i} 轮后已无缺失")
            break
        if round_got == 0:
            warn(
                f"补译第 {round_i} 轮零进展，停止后续轮次 "
                f"(still_missing={still})"
            )
            break

    still_after = len(_missing_idxs())
    if rounds_ran:
        highlight(
            f"补译结束  rounds={rounds_ran}/{refill_max_rounds}  "
            f"refilled={refilled}  still_missing={still_after}"
        )

    filled = 0
    final: list[str] = []
    for i, zh in enumerate(out):
        if zh and zh.strip() and not _is_english_fallback(zh, en_texts[i]):
            final.append(zh.strip())
        else:
            fb = drop_fillers(en_texts[i])
            final.append(fb)
            filled += 1
            warn(f"ZH[{i:04d}] FALLBACK {fb}")
    if filled:
        warn(f"有 {filled} 条用英文回填（翻译缺失）")
    elif refilled:
        info(f"补译已补回 {refilled} 条，无英文回填")

    highlight(
        f"翻译完成  lines={total}  fallback={filled}  refilled={refilled}  "
        f"refill_rounds={rounds_ran}/{refill_max_rounds}  concurrency={concurrency}"
    )
    stage_done(
        "translate",
        f"lines={total} fallback={filled} refilled={refilled} "
        f"refill_rounds={rounds_ran}/{refill_max_rounds} concurrency={concurrency}",
    )
    if title_en:
        meta = state.data.setdefault("meta", {})
        meta["title_en"] = title_en
        zh_title = _clean_title_zh(title_zh, "")
        if zh_title and zh_title != title_en:
            meta["title_zh"] = zh_title
            info(f"标题  ZH: {zh_title}")
        elif not str(meta.get("title_zh") or "").strip():
            warn("标题未随正文译出，导出用英文标题")

    state.set_done(
        "translate",
        total=total,
        fallback=filled,
        refilled=refilled,
        refill_rounds=rounds_ran,
        refill_max_rounds=refill_max_rounds,
        concurrency=concurrency,
        done_batches=[b + 1 for b in sorted(done_batches)],
        title_zh=bool(title_en and title_zh and title_zh != title_en),
    )
    return final
