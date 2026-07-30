# MyRose — 中文配音（纯 Python 项目）

英文视频 → 中文旁白 + 黄色中文字幕（原音静音）。  
**单一入口 `job_run.py`**，全流程状态落盘，支持失败续跑与校验。

## 目录

```text
MyRose/
  job_run.py           # 入口
  .env                 # 配置（API_KEY 等）
  .env.example
  requirements.txt
  zh_dub/              # Python 包
    config.py          # .env / 工具路径
    state.py           # job_state.json 状态机
    pipeline.py        # 阶段编排
    translate.py       # 翻译（批级 checkpoint）
    captions.py        # 字幕解析
    media.py           # 下载 / ffmpeg / tts
  work/<id>/           # 每个任务工作区
    job_state.json
    checkpoints/
    segments.json
    audio/
    narration.wav
    out.mp4
```

## 安装

```bash
cd /path/to/MyRose
conda activate python3          # 本机默认环境
python -m pip install -r requirements.txt
# 系统还需: yt-dlp, ffmpeg(libass), ffprobe
# edge-tts 装在该 conda 环境里
```

`.env` 里建议固定：

```env
PYTHON=/Users/你/miniconda3/envs/python3/bin/python
EDGE_TTS=/Users/你/miniconda3/envs/python3/bin/edge-tts
```

直接 `./job_run.py` 时会按 `.env` 的 `PYTHON` 自动切到 conda 解释器。

## 配置 `.env`

```bash
cp .env.example .env
```

| 变量 | 说明 | 默认 |
|---|---|---|
| `API_KEY` | ModelScope Key | 必填 |
| `MODEL` | 翻译模型 | 必填 |
| `PROXY` | 仅 yt-dlp | `http://127.0.0.1:7890` |
| `VOICE` | TTS 音色 | `zh-CN-YunyangNeural` |
| `QUALITY` | `720` / `1080` / `best` | `720` |
| `WORKDIR` | 任务根目录 | `./work` |
| `TRANSLATE_BATCH_SIZE` | 翻译批大小 | `100` |
| `TRANSLATE_MAX_RETRIES` | 单批最大重试 | `5` |
| `TTS_CONCURRENCY` | TTS 并发数（edge 易限流，可调 2～16） | `2` |
| `TTS_MAX_RATE` | TTS 最大加速百分比 | `30` |
| `PYTHON`/`YT_DLP`/`FFMPEG`/`FFPROBE`/`EDGE_TTS` | 可选绝对路径 | 自动探测 |
| `MODELSCOPE_BASE_URL` | 翻译 API base | ModelScope 默认 |

## 参数速查

```bash
python job_run.py --help
```

| 参数 | 说明 |
|---|---|
| `--url URL` | YouTube 链接；不写 `--work` 时自动解析 id 建 `work/<id>` |
| `--work DIR` | 已有任务目录（相对路径相对项目根） |
| `--mode MODE` | 见下表，默认 `all` |
| `--end N` | `0`=整片（默认）；`>0` 只处理前 N 秒预览 |
| `--voice NAME` | 覆盖 `.env` 的 `VOICE` |
| `--quality 720\|1080\|best` | 覆盖 `.env` 的 `QUALITY`（主要影响下载） |
| `--status` | 打印状态后退出（等价 `--mode status`） |
| `--resume` | 按 `job_state.json` 续跑（默认本身也会跳过已完成阶段） |
| `--no-resume` | 尽量不依赖 done 标记（仍会复用已有媒体/音频文件） |
| `--from STAGE` | 将该阶段及之后标为 pending，再跑（强制从某步重做） |
| `--prepare-only` | 别名：`--mode prepare` |
| `--tts-mux-only` | 别名：`--mode tts-mux` |
| `--yes` | 配合 `--mode clean`：真正执行删除；不加则只 dry-run |

### `--mode` 一览

| mode | 做什么 |
|---|---|
| `all` | 全流程：下载→准备→翻译→TTS→旁白时间轴→合成 |
| `download` | 只下载视频+字幕，并 `prepare_video` |
| `prepare` | 到翻译为止（prepare_video + cues + merge + translate） |
| `translate` | 只翻译（缺 merge 时会先补 cues/merge） |
| `tts` | 只 TTS（需已有 `segments.json`） |
| `mux` | 旁白时间轴 + 成片（需已有 TTS 音频） |
| `tts-mux` | TTS + 旁白 + 成片 |
| `status` | 只看状态 |
| `clean` | 确认成片 OK 后清理 work 目录，只留 `out.mp4`（需 `--yes`） |

### `--from` 可选阶段

`download` → `prepare_video` → `prepare_cues` → `merge` → `translate` → `tts` → `narration` → `compose`

## 命令行案例

以下均在项目根目录、已 `conda activate python3` 的前提下。

### 1. 新任务：整片一条龙

```bash
# 默认 720（或 .env QUALITY），自动 work/<youtube_id>
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID"

# 1080p
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --quality 1080

# 最高画质
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --quality best

# 指定音色
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --voice zh-CN-YunyangNeural
```

### 2. 预览（先跑前 N 秒试效果）

```bash
# 前 3 分钟预览 → out_preview.mp4
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --end 180

# 已有 work 目录上再跑 60 秒预览
python job_run.py --work work/VIDEO_ID --end 60 --mode all
```

### 3. 已有本地素材 / 指定 work 目录

```bash
# work 里已有 source_full.mp4 / 字幕等，从当前状态接着跑
python job_run.py --work work/VIDEO_ID

# 绝对路径也可以
python job_run.py --work /path/to/MyRose/work/VIDEO_ID
```

### 4. 查看状态

```bash
python job_run.py --work work/VIDEO_ID --status
# 或
python job_run.py --work work/VIDEO_ID --mode status
```

### 5. 失败后续跑

```bash
# 推荐：从 job_state 接着跑（跳过已 done）
python job_run.py --work work/VIDEO_ID --resume

# 不写 --resume 时，mode=all 默认也会尽量跳过已完成阶段
python job_run.py --work work/VIDEO_ID

# 尽量忽略 done 标记重走逻辑（音频文件仍会缓存复用）
python job_run.py --work work/VIDEO_ID --no-resume --mode all
```

### 6. 按阶段拆开跑

```bash
# 只下载
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --mode download

# 下载+切源+字幕 cues+merge+翻译（到 segments.json 有中文）
python job_run.py --work work/VIDEO_ID --mode prepare
# 别名：
python job_run.py --work work/VIDEO_ID --prepare-only

# 只翻译（可接着改 prompt/模型后重跑）
python job_run.py --work work/VIDEO_ID --mode translate

# 只 TTS
python job_run.py --work work/VIDEO_ID --mode tts

# 只做旁白时间轴 + 合成成片（TTS 已齐时）
python job_run.py --work work/VIDEO_ID --mode mux

# TTS + 旁白 + 成片
python job_run.py --work work/VIDEO_ID --mode tts-mux
# 别名：
python job_run.py --work work/VIDEO_ID --tts-mux-only
```

### 7. 从某一阶段强制重做（`--from`）

会把该阶段及**之后**全部标成 pending，再执行。

```bash
# 重翻 + 其后 TTS/旁白/合成
python job_run.py --work work/VIDEO_ID --from translate

# 只重做 TTS 及之后（翻译保留）
python job_run.py --work work/VIDEO_ID --from tts

# 旁白时间轴坏了 / narration 失败 → 从 narration 重做
python job_run.py --work work/VIDEO_ID --from narration

# 只重合成成片（旁白 wav 已有）
python job_run.py --work work/VIDEO_ID --from compose

# 字幕合并策略要重来
python job_run.py --work work/VIDEO_ID --from merge

# 源片要重切（例如改了 --end）
python job_run.py --work work/VIDEO_ID --end 0 --from prepare_video

# 连下载一起重来
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --work work/VIDEO_ID --from download
```

### 8. 手改中文后再出片

```bash
# 1) 编辑 work/VIDEO_ID/segments.json 里各段的 "zh"
# 2) 删掉需要重配的 audio/seg_XXXX.mp3（可选；不删则仍用旧音频）
# 3) 重跑 TTS + 成片
python job_run.py --work work/VIDEO_ID --mode tts-mux

# 若 tts 阶段已被标 done，强制从 tts 重开：
python job_run.py --work work/VIDEO_ID --from tts
```

### 9. TTS 相关调参与重跑

```bash
# .env 示例（改完无需改命令）
# TTS_CONCURRENCY=8
# TTS_MAX_RATE=30
# VOICE=zh-CN-YunyangNeural

# 提高并发后再跑 TTS
python job_run.py --work work/VIDEO_ID --mode tts

# 换音色（建议同时 --from tts，并视情况清 audio/）
python job_run.py --work work/VIDEO_ID --voice zh-CN-XiaoxiaoNeural --from tts

# TTS 中断 / 缺句：直接续跑会补 missing（已有 seg_XXXX.mp3 跳过）
python job_run.py --work work/VIDEO_ID --mode tts --resume
```

### 10. narration / mux 失败后

```bash
# 626 路之类 amix 爆掉后，代码已改为 PCM 时间轴；从 narration 重做即可
python job_run.py --work work/VIDEO_ID --from narration

# 旁白已有，只合成
python job_run.py --work work/VIDEO_ID --mode mux

# 或
python job_run.py --work work/VIDEO_ID --from compose
```

### 11. 确认成片无误后清理中间文件

成片验收通过后，用 `clean` 释放磁盘：work 目录**只保留最终视频**。

**保留**
- 整片任务：`out.mp4`
- 预览任务（`--end > 0`）：`out_preview.mp4`

**会删除（示例）**
- `audio/`（全部 TTS 中间件，通常最大）
- `source.mp4` / `source_full.mp4` 及字幕
- `narration.wav`
- `segments.json` / `job_state.json` / `checkpoints/`
- `*.srt` / `*.ass` / `validation.json` 等其余文件

```bash
# 1) dry-run：列出将删除项与大约可释放空间，不删文件
python job_run.py --work work/VIDEO_ID --mode clean

# 2) 本地播放确认 out.mp4 音画字幕都 OK 后，真正清理
python job_run.py --work work/VIDEO_ID --mode clean --yes

# 预览成片同理（work 里是 out_preview.mp4 时会自动保留它）
python job_run.py --work work/VIDEO_ID --end 180 --mode clean --yes
```

保护规则：
- 没有合格成片（缺失或过小）→ **拒绝清理**
- 若仍存在 `source.mp4` / `source_full.mp4`，且成片时长明显短于源片 → **拒绝清理**（防止误删还能重做的中间件）
- 不加 `--yes`：只打印将删列表，exit 0，不删任何文件
- 加 `--yes`：永久删除；清理后 work 目录里通常只剩最终视频
- **不可恢复**：需要改字幕/重配音时必须重新跑流水线（或重下源片）

### 12. 常见组合速查

| 场景 | 命令 |
|---|---|
| 全新视频整片 | `python job_run.py --url URL` |
| 先预览 3 分钟 | `python job_run.py --url URL --end 180` |
| 看进度 | `python job_run.py --work work/ID --status` |
| 挂了接着跑 | `python job_run.py --work work/ID --resume` |
| 只补翻译 | `python job_run.py --work work/ID --mode translate` |
| 只补配音 | `python job_run.py --work work/ID --mode tts` |
| 配音齐了只出片 | `python job_run.py --work work/ID --mode mux` |
| 改中文后重配音出片 | `python job_run.py --work work/ID --from tts` |
| 重翻全文 | `python job_run.py --work work/ID --from translate` |
| 1080 下载 | `python job_run.py --url URL --quality 1080` |
| 清理前预览将删内容 | `python job_run.py --work work/ID --mode clean` |
| 成片确认后清空间 | `python job_run.py --work work/ID --mode clean --yes` |

## 状态与续跑

每个任务目录有 `job_state.json`：

```text
stages:
  download → prepare_video → prepare_cues → merge
  → translate → tts → narration → compose
```

- 翻译按批写 `checkpoints/translate/batch_XXX.json`，中途 API 挂了只重跑失败批  
- TTS：`rate=0` 合成 → 读 mp3 时长 → 超 slot 再算 rate 最多打第 2 次；已有 `audio/seg_XXXX.mp3`（或旧 `_r*.mp3`）会跳过  
- TTS 并发由 `TTS_CONCURRENCY` 控制（默认 2）  
- TTS 结束后校验：有中文的段必须都有音频，否则失败并提示 `--resume`  
- narration：按时间轴拼 PCM，避免上百路 ffmpeg `amix` OOM  
- 失败日志会打印：

```text
STATE:  work/.../job_state.json
RESUME: python job_run.py --work ... --resume
```

## 进度日志

```text
STAGE: download-video
STAGE: translate | batch 1/N
STAGE: tts | concurrency=2 ...
STAGE: tts | progress i/N
STAGE: narration
STAGE: compose
STAGE: clean          # 仅 --mode clean
STAGE: job-done
```

## 产物

| 路径 | 说明 |
|---|---|
| `out.mp4` | 整片成品（`--mode clean --yes` 后通常只剩这个） |
| `out_preview.mp4` | 预览（`--end>0`） |
| `segments.json` | 分段中枢（可手改 `zh`） |
| `job_state.json` | 工程状态 |
| `validation.json` | 最近 TTS 校验 |
| `audio/seg_XXXX.mp3` | 每句最终配音 |
| `narration.wav` | 全片旁白时间轴 |
| `checkpoints/` | 可恢复中间件 |

## 常见问题

| 现象 | 处理 |
|---|---|
| 翻译 API 空响应/JSON 错 | 看 `job_state.json` error，直接 `--resume` |
| 缺音频 | `python job_run.py --work DIR --mode tts --resume` |
| TTS 太慢 | `.env` 调大 `TTS_CONCURRENCY`（如 8），再 `--mode tts` |
| yt-dlp 网络失败 | 检查 `PROXY` |
| ModelScope 401 | 检查 `API_KEY`（翻译不走代理） |
| `Unknown filter ass` | 安装带 libass 的 ffmpeg |
| narration ffmpeg exit 232 / amix 爆 | 已修复为 PCM 拼接；`--from narration` 重跑 |
| compose 显示 done 但片不对 / 时长被截断 | `--from compose` 或 `--mode mux`（会校验 out 不得明显短于 source） |
| 换音色仍是旧声 | 删 `audio/seg_*.mp3` 后 `--from tts`，或至少 `--from tts` 且对要重做的句删文件 |
| work 目录太大想腾空间 | 先确认 `out.mp4`，再 `python job_run.py --work DIR --mode clean --yes` |
| clean 提示 truncated / refused | 成片不完整：先 `--from compose` 重合成，再 clean |
| clean 后想改字幕重做 | 中间件已删，需重新 `--url` / 放回源片后再跑流水线 |
