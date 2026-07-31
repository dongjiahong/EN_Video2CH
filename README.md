# MyRose — 中文配音（纯 Python 项目）

英文视频 → 中文旁白 + 黄色中文字幕（原音静音）。  
**单一入口 `job_run.py`**，全流程状态落盘，支持失败续跑、批量列表与校验。

## 目录

```text
MyRose/
  job_run.py           # 入口（单条 / 批量 -f）
  .env                 # 配置（API_KEY 等）
  .env.example
  requirements.txt
  assets/              # 可选资源（如片头封面图）
  zh_dub/              # Python 包
    config.py          # .env / 工具路径
    state.py           # job_state.json 状态机
    pipeline.py        # 阶段编排 + 最终合成
    translate.py       # 翻译（并发 batch + 多轮补译）
    captions.py        # 字幕解析 / ASS 换行
    media.py           # 下载 / ffmpeg / tts
  work/<id>/           # 每个任务工作区
    job_state.json
    checkpoints/
    segments.json
    audio/
    narration.wav
    out.mp4
  video_list.txt       # 可选：批量 URL 列表
  video_failed.txt     # 可选：批量失败记录（自动生成）
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
| `TRANSLATE_CONCURRENCY` | 主翻译 batch 并发（补译在全部主翻结束后） | `2` |
| `TRANSLATE_REFILL_MAX_ROUNDS` | 漏翻补译轮数上限（某轮零进展提前停） | `2` |
| `TTS_CONCURRENCY` | TTS 并发数（edge 易限流，可调 2～16） | `2` |
| `TTS_MAX_RATE` | TTS 最大加速百分比 | `30` |
| `COVER_IMAGE` | 最终成片片头封面图（1 秒；相对项目根或绝对路径；不配则不加） | 空 |
| `PYTHON`/`YT_DLP`/`FFMPEG`/`FFPROBE`/`EDGE_TTS` | 可选绝对路径 | 自动探测 |
| `MODELSCOPE_BASE_URL` | 翻译 API base | ModelScope 默认 |

### 封面 `COVER_IMAGE`

- 只在**最终合成**（`compose` / `mux`）时使用
- 片头静帧 **1 秒**（静音），再接正文
- 封面分辨率可与视频不同；会按视频宽高校正缩放并居中 pad
- 文件不存在：打警告并跳过封面，不中断合成

```env
COVER_IMAGE=./assets/cover_16_9.png
```

## 参数速查

```bash
python job_run.py --help
```

| 参数 | 说明 |
|---|---|
| `--url URL` | YouTube 链接；不写 `--work` 时自动解析 id 建 `work/<id>` |
| `--work DIR` | 已有任务目录（相对路径相对项目根） |
| `-f` / `--file LIST` | 批量 URL 列表（每行一个）；失败写入同目录 `video_failed.txt`，**不重试** |
| `--failed-file PATH` | 自定义失败列表路径（默认 `<list_dir>/video_failed.txt`） |
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
| `status` | 只看状态（批量 `-f` 不支持） |
| `clean` | 确认成片 OK 后清理 work 目录，只留 `out.mp4`（需 `--yes`；批量 `-f` 不支持） |

### `--from` 可选阶段

`download` → `prepare_video` → `prepare_cues` → `merge` → `translate` → `tts` → `narration` → `compose`

## 命令行案例

以下均在项目根目录、已 `conda activate python3` 的前提下。

### 1. 新任务：整片一条龙

```bash
# 默认画质（或 .env QUALITY），自动 work/<youtube_id>
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID"

# 1080p
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --quality 1080

# 最高画质
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --quality best

# 指定音色
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --voice zh-CN-YunyangNeural
```

### 2. 批量列表（`-f`）

准备 `video_list.txt`（每行一个 URL，`#` 开头为注释）：

```text
# 交易课批量
https://www.youtube.com/watch?v=aaa
https://www.youtube.com/watch?v=bbb
https://youtu.be/ccc
```

```bash
# 按列表逐条跑完整流程（默认 --mode all）
python job_run.py -f video_list.txt

# 自定义失败列表路径
python job_run.py -f video_list.txt --failed-file ./video_failed.txt

# 批量预览 / 画质 / 模式
python job_run.py -f video_list.txt --end 180 --quality 720
python job_run.py -f video_list.txt --mode prepare
```

行为说明：

| 项 | 说明 |
|---|---|
| 处理顺序 | 从上到下**一条一条**跑；每条独立 `work/<id>/` |
| 失败策略 | **不重试**；记入失败文件后继续下一条 |
| 失败文件 | 默认与 list 同目录：`video_failed.txt`（**追加**写入） |
| 失败格式 | `URL<TAB>错误摘要` |
| 成功 | 正常产出 `work/<id>/out.mp4`（或预览 `out_preview.mp4`） |
| 中断 | `Ctrl+C` 停止整批；已写入的 failed 记录保留 |
| 退出码 | 有任意失败 → `1`；全部成功 → `0`；用户中断 → `130` |
| 不支持 | 批量下不可用 `--mode status` / `--mode clean` |
| 忽略 | 批量模式下若同时写了 `--url` / `--work` 会被忽略 |

失败文件示例：

```text
https://www.youtube.com/watch?v=aaa	download failed: ...
https://www.youtube.com/watch?v=bbb	TTS incomplete: missing [12, 15]
```

只重跑失败的：把 `video_failed.txt` 里的 URL 拷到新列表（去掉 `\t` 后错误摘要），再 `-f` 一次；或对单个 id：

```bash
python job_run.py --work work/VIDEO_ID --resume
```

### 3. 预览（先跑前 N 秒试效果）

```bash
# 前 3 分钟预览 → out_preview.mp4
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --end 180

# 已有 work 目录上再跑 60 秒预览
python job_run.py --work work/VIDEO_ID --end 60 --mode all
```

### 4. 已有本地素材 / 指定 work 目录

```bash
# work 里已有 source_full.mp4 / 字幕等，从当前状态接着跑
python job_run.py --work work/VIDEO_ID

# 绝对路径也可以
python job_run.py --work /path/to/MyRose/work/VIDEO_ID
```

### 5. 查看状态

```bash
python job_run.py --work work/VIDEO_ID --status
# 或
python job_run.py --work work/VIDEO_ID --mode status
```

### 6. 失败后续跑

```bash
# 推荐：从 job_state 接着跑（跳过已 done）
python job_run.py --work work/VIDEO_ID --resume

# 不写 --resume 时，mode=all 默认也会尽量跳过已完成阶段
python job_run.py --work work/VIDEO_ID

# 尽量忽略 done 标记重走逻辑（音频文件仍会缓存复用）
python job_run.py --work work/VIDEO_ID --no-resume --mode all
```

### 7. 按阶段拆开跑

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

### 8. 从某一阶段强制重做（`--from`）

会把该阶段及**之后**全部标成 pending，再执行。

```bash
# 重翻 + 其后 TTS/旁白/合成
python job_run.py --work work/VIDEO_ID --from translate

# 只重做 TTS 及之后（翻译保留）
python job_run.py --work work/VIDEO_ID --from tts

# 旁白时间轴坏了 / narration 失败 → 从 narration 重做
python job_run.py --work work/VIDEO_ID --from narration

# 只重合成成片（旁白 wav 已有；会应用 COVER_IMAGE）
python job_run.py --work work/VIDEO_ID --from compose

# 字幕合并策略要重来
python job_run.py --work work/VIDEO_ID --from merge

# 源片要重切（例如改了 --end）
python job_run.py --work work/VIDEO_ID --end 0 --from prepare_video

# 连下载一起重来
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --work work/VIDEO_ID --from download
```

### 9. 翻译：并发、补译与漏翻自愈

主翻译按 batch **并发**（默认 2）；**全部主翻结束后**再对缺失句做补译。

```text
主翻译 batch（并发 TRANSLATE_CONCURRENCY）
  → 收集仍缺失 / 英文回填的句子
  → 补译第 1 轮（只发缺失句）
  → 还有缺失？→ 补译第 2 轮（默认上限 2）
  → 仍缺才英文 FALLBACK
  → translate 标记 done
```

```bash
# .env
# TRANSLATE_CONCURRENCY=2
# TRANSLATE_REFILL_MAX_ROUNDS=2
# TRANSLATE_BATCH_SIZE=100

# 只跑翻译（含补译）
python job_run.py --work work/VIDEO_ID --mode translate

# 强制从翻译整段重来
python job_run.py --work work/VIDEO_ID --from translate
```

说明：

- 每一轮补译**只发上一轮还没译好的句子**，不会整表重翻
- 若 `segments.json` 里仍有「中文位 = 英文原文」的回填残留，即使 translate 已 done，再跑也会自动进入补译
- `TRANSLATE_REFILL_MAX_ROUNDS=0` 可关闭补译

### 10. 手改中文后再出片

```bash
# 1) 编辑 work/VIDEO_ID/segments.json 里各段的 "zh"
# 2) 删掉需要重配的 audio/seg_XXXX.mp3（可选；不删则仍用旧音频）
# 3) 重跑 TTS + 成片
python job_run.py --work work/VIDEO_ID --mode tts-mux

# 若 tts 阶段已被标 done，强制从 tts 重开：
python job_run.py --work work/VIDEO_ID --from tts
```

### 11. TTS 相关调参与重跑

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

### 12. narration / mux / 封面

```bash
# 旁白时间轴坏了：从 narration 重做
python job_run.py --work work/VIDEO_ID --from narration

# 旁白已有，只合成（会烧字幕；若配置了 COVER_IMAGE 会加 1 秒片头）
python job_run.py --work work/VIDEO_ID --mode mux

# 或
python job_run.py --work work/VIDEO_ID --from compose
```

字幕烧录（`zh.ass`）：

- 字号略小（16）
- 约 **32 字**强制换行，优先在标点处断开，减少长句出屏

### 13. 确认成片无误后清理中间文件

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
- 若仍存在 `source.mp4` / `source_full.mp4`，且成片时长明显短于源片 → **拒绝清理**
- 不加 `--yes`：只打印将删列表，不删任何文件
- 加 `--yes`：永久删除；清理后 work 目录里通常只剩最终视频
- **不可恢复**：需要改字幕/重配音时必须重新跑流水线（或重下源片）

### 14. 常见组合速查

| 场景 | 命令 |
|---|---|
| 全新视频整片 | `python job_run.py --url URL` |
| **批量整片** | `python job_run.py -f video_list.txt` |
| **批量预览 3 分钟** | `python job_run.py -f video_list.txt --end 180` |
| 先预览 3 分钟 | `python job_run.py --url URL --end 180` |
| 看进度 | `python job_run.py --work work/ID --status` |
| 挂了接着跑 | `python job_run.py --work work/ID --resume` |
| 只补翻译 / 补译漏句 | `python job_run.py --work work/ID --mode translate` |
| 只补配音 | `python job_run.py --work work/ID --mode tts` |
| 配音齐了只出片（含封面） | `python job_run.py --work work/ID --mode mux` |
| 改中文后重配音出片 | `python job_run.py --work work/ID --from tts` |
| 重翻全文 | `python job_run.py --work work/ID --from translate` |
| 1080 下载 | `python job_run.py --url URL --quality 1080` |
| 新配封面后重合成 | 配好 `COVER_IMAGE` 后 `python job_run.py --work work/ID --from compose` |
| 清理前预览将删内容 | `python job_run.py --work work/ID --mode clean` |
| 成片确认后清空间 | `python job_run.py --work work/ID --mode clean --yes` |

## 状态与续跑

每个任务目录有 `job_state.json`：

```text
stages:
  download → prepare_video → prepare_cues → merge
  → translate → tts → narration → compose
```

- 翻译主 batch 并发：`TRANSLATE_CONCURRENCY`（默认 2），每批写 `checkpoints/translate/batch_XXX.json`
- 主翻全部结束后多轮补译：`TRANSLATE_REFILL_MAX_ROUNDS`（默认 2），结果写 `checkpoints/translate/refill.json`
- 英文回填残留会被识别为未译，续跑/再进 translate 时自动补
- TTS：`rate=0` 合成 → 读 mp3 时长 → 超 slot 再算 rate 最多打第 2 次；已有 `audio/seg_XXXX.mp3`（或旧 `_r*.mp3`）会跳过
- TTS 并发由 `TTS_CONCURRENCY` 控制（默认 2）
- TTS 结束后校验：有中文的段必须都有音频，否则失败并提示 `--resume`
- narration：按时间轴拼 PCM，避免上百路 ffmpeg `amix` OOM
- compose：烧录中文字幕；若配置 `COVER_IMAGE` 则片头加 1 秒封面
- 失败日志会打印：

```text
STATE:  work/.../job_state.json
RESUME: python job_run.py --work ... --resume
```

## 进度日志

```text
STAGE: download-video
STAGE: translate | concurrency=2 batch ...
STAGE: translate | 补译第 1/2 轮 ...
STAGE: tts | concurrency=2 ...
STAGE: tts | progress i/N
STAGE: narration
STAGE: compose          # 可含片头封面
STAGE: batch            # 仅 -f 批量
STAGE: clean            # 仅 --mode clean
STAGE: job-done
```

## 产物

| 路径 | 说明 |
|---|---|
| `out.mp4` | 整片成品（可含 1s 封面；`--mode clean --yes` 后通常只剩这个） |
| `out_preview.mp4` | 预览（`--end>0`） |
| `segments.json` | 分段中枢（可手改 `zh`） |
| `job_state.json` | 工程状态 |
| `validation.json` | 最近 TTS 校验 |
| `audio/seg_XXXX.mp3` | 每句最终配音 |
| `narration.wav` | 全片旁白时间轴 |
| `zh.ass` / `zh.srt` | 中文字幕（ASS 用于烧录，约 32 字换行） |
| `checkpoints/translate/` | 翻译 batch + refill 断点 |
| `video_failed.txt` | 批量失败 URL（在 list 同目录，追加写入） |

## 常见问题

| 现象 | 处理 |
|---|---|
| 翻译 API 空响应/漏句多 | 已有多轮补译；仍缺则看 fallback 日志，可调大 `TRANSLATE_REFILL_MAX_ROUNDS` 后 `--mode translate` |
| 翻译太慢 | `.env` 调大 `TRANSLATE_CONCURRENCY`（如 3～4），注意 API 限流 |
| 缺音频 | `python job_run.py --work DIR --mode tts --resume` |
| TTS 太慢 | `.env` 调大 `TTS_CONCURRENCY`（如 8），再 `--mode tts` |
| yt-dlp 网络失败 | 检查 `PROXY` |
| ModelScope 401 | 检查 `API_KEY`（翻译不走代理） |
| `Unknown filter ass` | 安装带 libass 的 ffmpeg |
| narration ffmpeg exit 232 / amix 爆 | 已修复为 PCM 拼接；`--from narration` 重跑 |
| compose 显示 done 但片不对 / 时长被截断 | `--from compose` 或 `--mode mux`（会校验 out 时长） |
| 配了封面但成片没有片头 | 确认 `COVER_IMAGE` 路径存在，再 `--from compose`；旧 out 时长若未 +1s 会自动重合成 |
| 封面比例/像素和视频不一致 | 正常：会 scale + pad 居中到视频分辨率 |
| 字幕太长出屏 | 已按约 32 字换行并略缩小字号；改完后需 `--mode mux` 重烧 |
| 换音色仍是旧声 | 删 `audio/seg_*.mp3` 后 `--from tts` |
| 批量一条失败 | 不会重试；见 `video_failed.txt`，其余继续 |
| 批量中断后续跑 | 已成功的 work 已落盘；失败的在 failed 文件；可对单条 `--work` `--resume` 或重喂 failed URL |
| work 目录太大想腾空间 | 先确认 `out.mp4`，再 `python job_run.py --work DIR --mode clean --yes` |
| clean 提示 truncated / refused | 成片不完整：先 `--from compose` 重合成，再 clean |
| clean 后想改字幕重做 | 中间件已删，需重新 `--url` / 放回源片后再跑流水线 |
