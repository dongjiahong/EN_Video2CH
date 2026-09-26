#!/usr/bin/env python3
"""EN_Video2CH 入口：英文视频 → 中文配音 + 中文字幕成片（状态落盘、断点续跑）

推荐用 conda 环境:
  conda activate python3
  python job_run.py --work work/ID --status

若直接 ./job_run.py，会读取 .env 里的 PYTHON（默认 conda python3）并自动切换。

常用命令:
  python job_run.py --url URL                    # 整条流水线
  python job_run.py --url URL --end 60           # 预览前 60 秒
  python job_run.py --work work/ID --from tts    # 从某个阶段重做
  python job_run.py --work work/ID --to translate  # 只跑到翻译
  python job_run.py --work work/ID --clean --yes # 清理中间件，只留成片
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

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

from zh_dub.clean import clean_work  # noqa: E402
from zh_dub.config import Settings, normalize_quality  # noqa: E402
from zh_dub.export import publish_output  # noqa: E402
from zh_dub.logutil import (  # noqa: E402
    detail,
    format_status_line,
    highlight,
    info,
    keyval,
    log,
    stage,
    stage_done,
    warn,
)
from zh_dub.runner import Job, Runner  # noqa: E402
from zh_dub.segments import load_segments  # noqa: E402
from zh_dub.sources import (  # noqa: E402
    apply_limit,
    fetch_playlist,
    is_playlist_url,
    parse_limit,
    resolve_output_dir,
    resolve_work_dir,
)
from zh_dub.state import STAGES, JobState  # noqa: E402
from zh_dub.tts import validate_tts  # noqa: E402

STATUS_KEYS = (
    "segments",
    "cues",
    "audio_ok",
    "missing",
    "overflow",
    "progress",
    "clips",
    "duration",
    "size_mb",
    "out",
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="job_run.py",
        description="EN video -> ZH narration (stateful, resumable)",
    )
    p.add_argument("--url", default=None, help="YouTube video or playlist URL (playlist expands to batch)")
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
    p.add_argument(
        "--from",
        dest="from_stage",
        default=None,
        choices=STAGES,
        help="redo this stage and everything after it",
    )
    p.add_argument(
        "--to",
        dest="to_stage",
        default=None,
        choices=STAGES,
        help="stop after this stage",
    )
    p.add_argument("--end", type=float, default=0.0, help="0=full (default); >0 preview seconds")
    p.add_argument(
        "--output",
        default=None,
        help="copy finished mp4 here as '<序号_中文标题> [id].mp4' (overrides OUTPUT_DIR)",
    )
    p.add_argument(
        "--limit",
        default="0",
        metavar="N | OFFSET,COUNT",
        help="SQL-style window: N = first N; OFFSET,COUNT = skip OFFSET then take COUNT; 0 = all",
    )
    p.add_argument("--voice", default=None, help="override VOICE from .env")
    p.add_argument("--quality", default=None, help="720|1080|best override")
    p.add_argument("--status", action="store_true", help="print job status and exit")
    p.add_argument("--clean", action="store_true", help="delete intermediates, keep the final video")
    p.add_argument(
        "--yes",
        action="store_true",
        help="required with --clean to actually delete files",
    )
    return p


def _print_status(job: Job) -> None:
    stage("status", str(job.work))
    state = job.state
    keyval("status", state.data.get("status"))
    keyval("stage", state.data.get("stage"))
    keyval("updated", state.data.get("updated_at"))
    log("stages:")
    for name in STAGES:
        bits = [
            f"{k}={state.stage_detail(name)[k]}"
            for k in STATUS_KEYS
            if k in state.stage_detail(name)
        ]
        log(format_status_line(name, state.stage_status(name), ("  " + ", ".join(bits)) if bits else ""))
    err = state.data.get("error")
    if err:
        warn(f"{err.get('stage')}: {err.get('message')}")
        detail(f"续跑: python job_run.py --work {job.work}")

    seg_path = job.path("segments.json")
    if not seg_path.is_file():
        return
    segs = load_segments(seg_path)
    report = validate_tts(segs, job.path("audio"), job.voice, job.settings.tts_max_rate)
    highlight(
        f"实时检查  segments={len(segs)}  zh={sum(1 for s in segs if s.zh.strip())}  "
        f"audio_ok={report['audio_ok']}  missing={len(report['audio_missing'])}"
    )
    if report["audio_missing"][:10]:
        warn(f"缺失音频 idx 样例: {report['audio_missing'][:10]}")


def _load_url_list(path: Path) -> list[str]:
    if not path.is_file():
        raise SystemExit(f"URL list not found: {path}")
    urls: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " #" in line:  # allow "url # comment"
            line = line.split(" #", 1)[0].strip()
        if not line or line in seen:
            continue
        seen.add(line)
        urls.append(line)
    return urls


def _append_failed(failed_path: Path, url: str, err: str) -> None:
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    msg = " ".join(str(err).splitlines()).strip()
    if len(msg) > 300:
        msg = msg[:297] + "..."
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with failed_path.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {url}\t{msg}\n")


def _make_job(
    settings: Settings,
    work: Path,
    *,
    url: str | None,
    end: float,
    voice: str,
    output_dir: Path | None,
    title_en: str | None,
    video_id: str | None,
    seq: int | None,
) -> Job:
    state = JobState(work)
    meta: dict[str, Any] = {"voice": voice, "quality": settings.quality}
    if url:
        meta["url"] = url
    if title_en:
        meta["title_en"] = title_en
    if video_id:
        meta["video_id"] = video_id
    if seq is not None:
        meta["seq"] = seq
    if output_dir is not None:
        meta["output_dir"] = str(output_dir)
    state.update_meta(**meta)
    return Job(
        settings=settings,
        work=work,
        state=state,
        voice=voice,
        end=end,
        url=url,
        output_dir=output_dir,
    )


def _publish(job: Job) -> Path | None:
    """Copy/link the finished video into the export dir, when one is configured."""
    if job.output_dir is None:
        return None
    src = job.out
    if not src.is_file():
        alt = job.path("out.mp4" if src.name != "out.mp4" else "out_preview.mp4")
        if not alt.is_file():
            return None
        src = alt
    return publish_output(
        job.settings,
        job.state,
        src,
        output_dir=job.output_dir,
        preview=src.name.startswith("out_preview"),
    )


def _run_one(
    settings: Settings,
    args: argparse.Namespace,
    *,
    url: str | None,
    work: str | None,
    output_dir: Path | None,
    title_en: str | None = None,
    video_id: str | None = None,
    seq: int | None = None,
) -> tuple[int, Path | None, Path | None, str]:
    """Run one job. Returns (exit_code, work_dir, output_path, error)."""
    work_dir: Path | None = None
    job: Job | None = None
    try:
        work_dir = resolve_work_dir(settings, work, url)
        job = _make_job(
            settings,
            work_dir,
            url=url,
            end=args.end,
            voice=args.voice or settings.voice,
            output_dir=output_dir,
            title_en=title_en,
            video_id=video_id,
            seq=seq,
        )
        if args.clean:
            return 0, work_dir, clean_work(settings, work_dir, job.out, yes=args.yes), ""
        if args.status:
            _print_status(job)
            return 0, work_dir, None, ""
        Runner(job).run(args.from_stage, args.to_stage)
        return 0, work_dir, _publish(job), ""
    except KeyboardInterrupt:
        warn("用户中断")
        if job is not None and work_dir is not None:
            info(f"STATE   {job.state.path}")
            info(f"RESUME  python job_run.py --work {work_dir}")
        return 130, work_dir, None, "interrupted"
    except Exception as e:  # noqa: BLE001
        warn(f"FAILED: {e}")
        return 1, work_dir, None, str(e)


def _limit_from_args(args: argparse.Namespace) -> tuple[int, int]:
    try:
        return parse_limit(args.limit)
    except ValueError as e:
        raise SystemExit(str(e)) from e


def _failed_path_for(args: argparse.Namespace, default_parent: Path) -> Path:
    if args.failed_file:
        p = Path(args.failed_file).expanduser()
        return p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
    return (default_parent / "video_failed.txt").resolve()


def _run_url_batch(
    settings: Settings,
    args: argparse.Namespace,
    items: list[dict],
    *,
    failed_path: Path,
    label: str,
    output_dir: Path | None,
    offset: int = 0,
) -> int:
    total = len(items)
    ok_n = 0
    fail_n = 0
    t_all = time.time()
    stage("batch", f"{label}  urls={total}  failed_log={failed_path}")
    highlight(f"批量任务  {total} 条  {label}")
    if output_dir is not None:
        keyval("output_dir", output_dir)
    keyval("failed_log", failed_path)

    for i, item in enumerate(items, start=1):
        url = item["url"]
        title = item.get("title_en") or ""
        stage("batch-item", f"{i}/{total}")
        highlight(f"[{i}/{total}] {url}")
        if title:
            info(f"EN  {title}")
        t0 = time.time()
        work = str(settings.workdir / str(item["id"])) if item.get("id") else None
        # playlist items carry their absolute index; file-list items use offset+i
        seq = item.get("index") or (offset + i)
        code, work_dir, out, err = _run_one(
            settings,
            args,
            url=url,
            work=work,
            output_dir=output_dir,
            title_en=title or None,
            video_id=item.get("id"),
            seq=seq,
        )
        elapsed = time.time() - t0
        if code == 130:
            warn(f"批量在第 {i}/{total} 条中断")
            keyval("failed_log", failed_path)
            return 130
        if code != 0:
            fail_n += 1
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
    highlight(f"批量结束  成功={ok_n}  失败={fail_n}  总计={total}  耗时 {time.time()-t_all:.1f}s")
    if fail_n:
        keyval("failed_log", failed_path)
        info(f"失败 URL 已写入: {failed_path}")
        return 1
    return 0


def _run_file_batch(
    settings: Settings, args: argparse.Namespace, *, output_dir: Path | None
) -> int:
    list_path = Path(args.url_file).expanduser()
    list_path = list_path.resolve() if list_path.is_absolute() else (Path.cwd() / list_path).resolve()

    urls = _load_url_list(list_path)
    if not urls:
        raise SystemExit(f"URL list is empty: {list_path}")
    items = [{"url": u, "id": None, "title_en": ""} for u in urls]
    offset, count = _limit_from_args(args)
    sliced = apply_limit(items, offset, count)
    if not sliced:
        raise SystemExit(
            f"URL list has {len(items)} items; --limit offset={offset} count={count} selected none"
        )
    if offset or count:
        info(f"列表窗口  全表 {len(items)} 条  本批 {len(sliced)} 条  offset={offset} count={count or 'all'}")
    return _run_url_batch(
        settings,
        args,
        sliced,
        failed_path=_failed_path_for(args, list_path.parent),
        label=f"file={list_path}",
        output_dir=output_dir,
        offset=offset,
    )


def _run_playlist_batch(
    settings: Settings, args: argparse.Namespace, *, output_dir: Path | None
) -> int:
    stage("playlist", args.url)
    offset, count = _limit_from_args(args)
    data = fetch_playlist(settings, args.url, offset=offset, count=count)
    items = data["items"]
    highlight(f"播放列表  {data.get('title') or ''}  {len(items)} 条")
    stage_done("playlist", f"items={len(items)}")
    return _run_url_batch(
        settings,
        args,
        items,
        failed_path=_failed_path_for(args, Path.cwd()),
        label=f"playlist={data.get('title') or args.url}",
        output_dir=output_dir,
        offset=offset,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    if args.yes and not args.clean:
        warn("--yes 只在 --clean 时有效")

    t0 = time.time()
    stage("config")
    settings = Settings.load(ROOT)
    if args.quality:
        settings.quality = normalize_quality(args.quality)
    for f in settings.env_files:
        detail(f"loaded {f}")
    keyval("python", settings.python)
    keyval("model", settings.model)
    keyval("voice", args.voice or settings.voice)
    keyval("quality", settings.quality)
    if args.from_stage or args.to_stage:
        keyval("stages", f"{args.from_stage or STAGES[0]}..{args.to_stage or STAGES[-1]}")
    output_dir = resolve_output_dir(settings, args.output)
    if output_dir is not None:
        keyval("output_dir", output_dir)
    detail(f"tools yt-dlp={settings.yt_dlp}  ffmpeg={settings.ffmpeg}  ffprobe={settings.ffprobe}")
    stage_done("config")

    if args.url_file:
        if args.url or args.work:
            warn("批量 -f 模式下忽略 --url / --work")
        if args.status or args.clean:
            raise SystemExit("批量模式不支持 --status / --clean")
        return _run_file_batch(settings, args, output_dir=output_dir)

    if args.url and is_playlist_url(args.url):
        if args.work:
            warn("播放列表模式下忽略 --work")
        if args.status or args.clean:
            raise SystemExit("播放列表不支持 --status / --clean")
        return _run_playlist_batch(settings, args, output_dir=output_dir)

    if not args.url and not args.work:
        raise SystemExit("Need --url, --work, or -f url_list.txt")

    code, work, out, _err = _run_one(
        settings,
        args,
        url=args.url,
        work=args.work,
        output_dir=output_dir,
        # single non-playlist URL exports as 01_; --work reruns keep stored meta.seq
        seq=1 if args.url and not args.work else None,
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
        detail(f"查看进度:          python job_run.py --work {work} --status")
        detail(f"改中文后重做配音:  python job_run.py --work {work} --from tts")
        detail(f"失败后续跑:        python job_run.py --work {work}")
    stage_done("job-done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
