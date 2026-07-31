#!/usr/bin/env python3
"""MyRose 本地中文配音入口（纯 Python，带状态与断点续跑）

推荐用 conda 环境:
  conda activate python3
  python job_run.py --work work/ID --status

若直接 ./job_run.py，会读取 .env 里的 PYTHON（默认 conda python3）并自动切换。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _bootstrap_reexec() -> None:
    """Load .env early and re-exec into configured/conda PYTHON if needed."""
    from zh_dub.config import load_env, resolve_python

    load_env(ROOT)
    target = Path(resolve_python()).resolve()
    current = Path(sys.executable).resolve()
    # also accept python3.12 vs python symlink in same env
    if target == current:
        return
    if target.parent == current.parent and target.name.startswith("python"):
        return
    os.execv(str(target), [str(target), str(Path(__file__).resolve()), *sys.argv[1:]])


_bootstrap_reexec()

from zh_dub.config import Settings  # noqa: E402
from zh_dub.logutil import (  # noqa: E402
    detail,
    highlight,
    info,
    keyval,
    stage,
    stage_done,
    warn,
)
from zh_dub.pipeline import Pipeline, resolve_work_dir  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="job_run.py",
        description="EN video -> ZH narration (stateful, resumable)",
    )
    p.add_argument("--url", default=None, help="YouTube URL")
    p.add_argument("--work", default=None, help="existing work dir")
    p.add_argument(
        "-f",
        "--file",
        dest="url_file",
        default=None,
        help="batch URL list file (one URL per line); failures -> video_failed.txt",
    )
    p.add_argument(
        "--failed-file",
        default=None,
        help="where to append failed URLs (default: <list_dir>/video_failed.txt)",
    )
    p.add_argument("--end", type=float, default=0.0, help="0=full (default); >0 preview seconds")
    p.add_argument("--voice", default=None, help="override VOICE from .env")
    p.add_argument("--quality", default=None, help="720|1080|best override")
    p.add_argument(
        "--mode",
        default="all",
        choices=[
            "all",
            "prepare",
            "translate",
            "tts",
            "mux",
            "tts-mux",
            "download",
            "status",
            "clean",
        ],
        help="pipeline mode (default all)",
    )
    p.add_argument("--resume", action="store_true", help="resume from job_state.json")
    p.add_argument("--no-resume", action="store_true", help="ignore done flags where possible")
    p.add_argument(
        "--from",
        dest="force_from",
        default=None,
        choices=[
            "download",
            "prepare_video",
            "prepare_cues",
            "merge",
            "translate",
            "tts",
            "narration",
            "compose",
        ],
        help="mark this stage and after as pending, then run",
    )
    p.add_argument("--status", action="store_true", help="print job status and exit")
    p.add_argument("--prepare-only", action="store_true", help="alias: --mode prepare")
    p.add_argument("--tts-mux-only", action="store_true", help="alias: --mode tts-mux")
    p.add_argument(
        "--yes",
        action="store_true",
        help="required with --mode clean to actually delete files",
    )
    return p


def _apply_quality(settings: Settings, quality: str | None) -> None:
    if not quality:
        return
    q = quality.strip().lower()
    if q in {"720", "720p"}:
        settings.quality = "720"
    elif q in {"1080", "1080p"}:
        settings.quality = "1080"
    elif q in {"best", "max", "highest", "source"}:
        settings.quality = "best"
    else:
        raise SystemExit("quality must be 720, 1080, or best")


def _load_url_list(path: Path) -> list[str]:
    if not path.is_file():
        raise SystemExit(f"URL list not found: {path}")
    urls: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # allow "url # comment"
        if " #" in line:
            line = line.split(" #", 1)[0].strip()
        if not line or line in seen:
            continue
        seen.add(line)
        urls.append(line)
    return urls


def _append_failed(failed_path: Path, url: str, err: str) -> None:
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    # one line: url \t error (single-line)
    msg = " ".join(str(err).splitlines()).strip()
    if len(msg) > 300:
        msg = msg[:297] + "..."
    with failed_path.open("a", encoding="utf-8") as f:
        f.write(f"{url}\t{msg}\n")


def _run_one(
    settings: Settings,
    *,
    url: str | None,
    work: str | None,
    mode: str,
    end: float,
    voice: str | None,
    resume: bool,
    force_from: str | None,
    clean_yes: bool,
) -> tuple[int, Path | None, Path | None, str]:
    """
    Run a single job.
    Returns (exit_code, work_dir, output_path, error_message).
    exit_code: 0 ok, 1 failed, 130 interrupted.
    """
    work_dir: Path | None = None
    pipe: Pipeline | None = None
    try:
        work_dir = resolve_work_dir(settings, work, url)
        pipe = Pipeline(
            settings,
            work_dir,
            end=end,
            voice=voice,
        )
        out = pipe.run(
            url=url,
            mode=mode,
            resume=resume,
            force_from=force_from,
            clean_yes=clean_yes,
        )
        return 0, work_dir, out, ""
    except KeyboardInterrupt:
        warn("用户中断")
        if pipe is not None and work_dir is not None:
            info(f"STATE   {pipe.state.path}")
            info(f"RESUME  python job_run.py --work {work_dir} --resume")
        return 130, work_dir, None, "interrupted"
    except Exception as e:  # noqa: BLE001
        warn(f"FAILED: {e}")
        return 1, work_dir, None, str(e)


def _run_batch(
    settings: Settings,
    args: argparse.Namespace,
    *,
    resume: bool,
) -> int:
    list_path = Path(args.url_file).expanduser()
    if not list_path.is_absolute():
        list_path = (Path.cwd() / list_path).resolve()
    else:
        list_path = list_path.resolve()

    if args.failed_file:
        failed_path = Path(args.failed_file).expanduser()
        if not failed_path.is_absolute():
            failed_path = (Path.cwd() / failed_path).resolve()
        else:
            failed_path = failed_path.resolve()
    else:
        failed_path = list_path.parent / "video_failed.txt"

    urls = _load_url_list(list_path)
    if not urls:
        raise SystemExit(f"URL list is empty: {list_path}")

    mode = args.mode
    if mode in {"status", "clean"}:
        raise SystemExit(f"batch -f does not support --mode {mode}")

    total = len(urls)
    ok_n = 0
    fail_n = 0
    t_all = time.time()
    stage("batch", f"file={list_path}  urls={total}  failed_log={failed_path}")
    highlight(f"批量任务  {total} 条  list={list_path.name}")
    keyval("failed_log", failed_path)

    for i, url in enumerate(urls, start=1):
        stage("batch-item", f"{i}/{total}")
        highlight(f"[{i}/{total}] {url}")
        t0 = time.time()
        code, work_dir, out, err = _run_one(
            settings,
            url=url,
            work=None,
            mode=mode,
            end=args.end,
            voice=args.voice,
            resume=resume,
            force_from=args.force_from,
            clean_yes=False,
        )
        elapsed = time.time() - t0
        if code == 130:
            warn(f"批量在第 {i}/{total} 条中断")
            keyval("failed_log", failed_path)
            return 130
        if code != 0:
            fail_n += 1
            # no retry: record and continue to next URL
            _append_failed(failed_path, url, err or "failed")
            warn(f"[{i}/{total}] 失败已记录  ({elapsed:.1f}s)  -> {failed_path.name}")
            continue

        ok_n += 1
        highlight(f"[{i}/{total}] 完成  {elapsed:.1f}s  work={work_dir}")
        if out:
            keyval("output", out)

    stage_done(
        "batch",
        f"ok={ok_n} fail={fail_n} total={total} elapsed={time.time()-t_all:.1f}s",
    )
    highlight(
        f"批量结束  成功={ok_n}  失败={fail_n}  总计={total}  "
        f"耗时 {time.time()-t_all:.1f}s"
    )
    if fail_n:
        keyval("failed_log", failed_path)
        info(f"失败 URL 已写入: {failed_path}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    if args.prepare_only:
        args.mode = "prepare"
    if args.tts_mux_only:
        args.mode = "tts-mux"
    if args.status:
        args.mode = "status"

    resume = True
    if args.no_resume:
        resume = False
    if args.resume:
        resume = True

    t0 = time.time()
    stage("config")
    settings = Settings.load(ROOT)
    _apply_quality(settings, args.quality)
    for f in settings.env_files:
        detail(f"loaded {f}")
    keyval("python", settings.python)
    keyval("model", settings.model)
    keyval("voice", args.voice or settings.voice)
    keyval("quality", settings.quality)
    keyval("mode", args.mode)
    detail(
        f"tools yt-dlp={settings.yt_dlp}  ffmpeg={settings.ffmpeg}  "
        f"edge-tts={settings.edge_tts}"
    )
    stage_done("config")

    # batch mode
    if args.url_file:
        if args.url or args.work:
            warn("批量 -f 模式下忽略 --url / --work")
        return _run_batch(settings, args, resume=resume)

    if not args.url and not args.work:
        raise SystemExit("Need --url, --work, or -f url_list.txt")

    code, work, out, _err = _run_one(
        settings,
        url=args.url,
        work=args.work,
        mode=args.mode,
        end=args.end,
        voice=args.voice,
        resume=resume,
        force_from=args.force_from,
        clean_yes=bool(args.yes),
    )
    if code != 0:
        return code

    elapsed = time.time() - t0
    stage("job-done", f"elapsed={elapsed:.1f}s")
    highlight(f"任务结束  耗时 {elapsed:.1f}s")
    keyval("work", work)
    if out:
        keyval("output", out)
    if work:
        keyval("state", work / "job_state.json")
        info("常用命令:")
        detail(f"改中文后重做配音:  python job_run.py --work {work} --mode tts-mux")
        detail(f"查看进度:          python job_run.py --work {work} --status")
        detail(f"失败后续跑:        python job_run.py --work {work} --resume")
    stage_done("job-done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
