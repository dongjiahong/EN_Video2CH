from __future__ import annotations

import re
import time
from pathlib import Path
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


def _client(settings: Settings):
    from openai import OpenAI

    clear_proxy_env()
    return OpenAI(api_key=settings.api_key, base_url=settings.modelscope_base_url)


def translate_chunk(
    settings: Settings,
    items: list[tuple[int, str]],
    *,
    max_retries: int | None = None,
) -> dict[int, str]:
    """Translate one batch. items: local 1-based no -> en text."""
    client = _client(settings)
    model = settings.model
    n = len(items)
    retries = max_retries if max_retries is not None else settings.translate_max_retries

    cleaned_lines = [f"{no}. {drop_fillers(raw)}" for no, raw in items]
    payload = "\n".join(cleaned_lines)

    asr_fix_hint = (
        "输入来自 YouTube 自动生成字幕，常有听写错误。请先在脑中纠正再翻译，"
        "最终中文按纠正后的交易术语表达。常见误识别包括但不限于：\n"
        "- bare bar / bare bars → bear bar / bear bars\n"
        "- bare channel / tight bare channel → bear channel / tight bear channel\n"
        "- bull/bare 混淆时，结合上下文（上涨/下跌通道、突破方向）判断\n"
        "- 数字、百分比、K线位置描述尽量保留准确含义\n"
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
    )
    user = (
        f"共{n}条自动字幕。请开启深度思考：先校对听写错误，再翻译成中文口播。\n"
        f"只输出 {n} 行 `N. 中文`：\n\n{payload}"
    )

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
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
                resp = client.chat.completions.create(
                    reasoning_effort="high", **create_kwargs
                )
            except TypeError:
                resp = client.chat.completions.create(**create_kwargs)
            except Exception as e1:
                if "reasoning_effort" in str(e1).lower() or "unexpected" in str(e1).lower():
                    resp = client.chat.completions.create(**create_kwargs)
                else:
                    raise

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


def translate_segments(
    settings: Settings,
    state: JobState,
    en_texts: list[str],
    *,
    force: bool = False,
) -> list[str]:
    """Batch translate with per-batch checkpoints and resume."""
    total = len(en_texts)
    batch_size = max(1, settings.translate_batch_size)
    n_batches = (total + batch_size - 1) // batch_size if total else 0
    stage(
        "translate",
        f"total={total} batches={n_batches} size={batch_size} model={settings.model}",
    )
    info(
        f"翻译启动  lines={total}  batches={n_batches}  "
        f"batch_size={batch_size}  model={settings.model}"
    )
    out: list[str | None] = [None] * total
    done_batches: list[int] = []

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
                out[abs_i] = v.strip()
                ok_n += 1
        if ok_n >= int(data.get("expected", 0) * 0.95):
            done_batches.append(bi)
            info(f"恢复 batch {bi+1}/{n_batches}: 已加载 {ok_n} 行")

    if force:
        done_batches = []
        out = [None] * total
        warn("强制重翻全部 batch")

    pending = [bi for bi in range(n_batches) if bi not in done_batches]
    highlight(
        f"待翻 batch {[b+1 for b in pending]}  "
        f"(已完成 {len(done_batches)}/{n_batches})"
    )
    state.set_running("translate")
    state.data["stages"]["translate"]["detail"] = {
        "total": total,
        "batch_size": batch_size,
        "n_batches": n_batches,
        "done_batches": [b + 1 for b in done_batches],
        "pending_batches": [b + 1 for b in pending],
    }
    state.save()

    for bi in pending:
        start = bi * batch_size
        chunk = en_texts[start : start + batch_size]
        items = [(i + 1, t) for i, t in enumerate(chunk)]
        progress(
            bi + 1,
            n_batches,
            f"lines {start+1}-{start+len(chunk)} / {total}",
        )
        t0 = time.time()
        try:
            # degrade batch size on repeated failure
            try:
                mapped = translate_chunk(settings, items)
            except TranslateError as e:
                if len(chunk) > 40 and e.retryable:
                    warn(f"batch {bi+1} 降级拆分重试")
                    mapped = {}
                    sub = 40
                    for sj in range(0, len(chunk), sub):
                        sub_items = [
                            (i + 1, t) for i, t in enumerate(chunk[sj : sj + sub])
                        ]
                        part = translate_chunk(settings, sub_items)
                        for local_no, zh in part.items():
                            mapped[sj + local_no] = zh
                else:
                    raise
        except Exception as e:  # noqa: BLE001
            stage_error("translate", f"batch {bi+1}/{n_batches}: {e}")
            state.set_failed(
                "translate",
                str(e),
                retryable=True,
                batch=bi + 1,
                done_batches=[b + 1 for b in done_batches],
                saved_lines=sum(1 for x in out if x),
            )
            resume_hint(state.work)
            raise

        for local_no, zh in mapped.items():
            abs_i = start + local_no - 1
            if 0 <= abs_i < total:
                out[abs_i] = zh
                if local_no <= 3 or abs_i % 50 == 0:
                    detail(f"ZH[{abs_i:04d}] {zh}")

        # checkpoint this batch
        mapping = {
            str(local_no): zh
            for local_no, zh in mapped.items()
            if isinstance(zh, str) and zh.strip()
        }
        state.write_json(
            f"translate/batch_{bi:03d}.json",
            {
                "batch_index": bi,
                "start": start,
                "expected": len(chunk),
                "got": len(mapping),
                "elapsed_s": round(time.time() - t0, 2),
                "mapping": mapping,
            },
        )
        done_batches.append(bi)
        state.data["stages"]["translate"]["detail"] = {
            "total": total,
            "batch_size": batch_size,
            "n_batches": n_batches,
            "done_batches": [b + 1 for b in sorted(done_batches)],
            "pending_batches": [
                b + 1 for b in range(n_batches) if b not in done_batches
            ],
            "done_lines": sum(1 for x in out if x),
        }
        state.save()
        highlight(
            f"batch {bi+1}/{n_batches} 完成  "
            f"{len(mapping)}/{len(chunk)}  {time.time()-t0:.1f}s"
        )

    filled = 0
    final: list[str] = []
    for i, zh in enumerate(out):
        if zh and zh.strip():
            final.append(zh.strip())
        else:
            fb = drop_fillers(en_texts[i])
            final.append(fb)
            filled += 1
            warn(f"ZH[{i:04d}] FALLBACK {fb}")
    if filled:
        warn(f"有 {filled} 条用英文回填（翻译缺失）")

    highlight(f"翻译完成  lines={total}  fallback={filled}")
    stage_done("translate", f"lines={total} fallback={filled}")
    state.set_done(
        "translate",
        total=total,
        fallback=filled,
        done_batches=[b + 1 for b in sorted(done_batches)],
    )
    return final
