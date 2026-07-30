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
from zh_dub.logutil import log, stage, stage_done  # noqa: E402
from zh_dub.pipeline import Pipeline, resolve_work_dir  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="job_run.py",
        description="EN video -> ZH narration (stateful, resumable)",
    )
    p.add_argument("--url", default=None, help="YouTube URL")
    p.add_argument("--work", default=None, help="existing work dir")
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
    if args.quality:
        q = args.quality.strip().lower()
        if q in {"720", "720p"}:
            settings.quality = "720"
        elif q in {"1080", "1080p"}:
            settings.quality = "1080"
        elif q in {"best", "max", "highest", "source"}:
            settings.quality = "best"
        else:
            raise SystemExit("quality must be 720, 1080, or best")
    for f in settings.env_files:
        log(f"loaded {f}")
    log(f"python={settings.python}")
    log(f"model={settings.model}")
    log(f"voice={args.voice or settings.voice} quality={settings.quality}")
    log(
        f"tools yt-dlp={settings.yt_dlp} ffmpeg={settings.ffmpeg} "
        f"edge-tts={settings.edge_tts}"
    )
    stage_done("config")

    work = resolve_work_dir(settings, args.work, args.url)
    pipe = Pipeline(
        settings,
        work,
        end=args.end,
        voice=args.voice,
    )

    try:
        out = pipe.run(
            url=args.url,
            mode=args.mode,
            resume=resume,
            force_from=args.force_from,
            clean_yes=bool(args.yes),
        )
    except KeyboardInterrupt:
        log("interrupted by user")
        log(f"STATE:  {pipe.state.path}")
        log(f"RESUME: python job_run.py --work {work} --resume")
        return 130
    except Exception as e:  # noqa: BLE001
        log(f"FAILED: {e}")
        return 1

    elapsed = time.time() - t0
    stage("job-done", f"elapsed={elapsed:.1f}s")
    log(f"work:   {work}")
    if out:
        log(f"output: {out}")
    log(f"state:  {pipe.state.path}")
    log("改中文后重做配音成片:")
    log(f"  python job_run.py --work {work} --mode tts-mux")
    log("查看进度:")
    log(f"  python job_run.py --work {work} --status")
    log("失败后续跑:")
    log(f"  python job_run.py --work {work} --resume")
    stage_done("job-done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
