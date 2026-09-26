# EN_Video2CH — 英文视频一键转中文配音

把一个英文 YouTube 视频变成**中文配音 + 黄色中文字幕**的成片（原音静音）：下载 → 本地转录 → 智能翻译 → TTS 配音 → 字幕烧录，全流程自动完成、状态落盘、断点续跑。

- 纯 Python 项目，单一入口 `job_run.py`
- 支持**单条视频 / YouTube 播放列表 / URL 批量列表**三种任务来源
- 全流程 7 个阶段，状态写入 `job_state.json`；**阶段产物 + 状态双重判定**，中断后直接重跑同一条命令即可接着跑
- 翻译与 TTS 都带指纹缓存：改中文、换音色只重做受影响的部分
- 自带失败管理：批量失败记入 `video_failed.txt`（带时间戳），可只重跑失败的
- 转录（parakeet-mlx）、翻译（ModelScope）、配音（edge-tts）均为独立阶段，互不拖累

---

## 目录

- [核心特性](#核心特性)
- [工作流程与架构](#工作流程与架构)
- [安装与配置](#安装与配置)
- [快速开始](#快速开始)
- [命令行参考](#命令行参考)
- [使用场景详解](#使用场景详解)
- [产物一览](#产物一览)
- [常见问题](#常见问题)
- [源码结构](#源码结构)

---

## 核心特性

### 1. 全流程自动化

一条命令完成「下载 → 转录 → 断句 → 翻译 → TTS → 旁白 → 合成」七个阶段：

```
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --output ./output
```

成片自动命名（中文标题 + 视频 ID + 列表序号）拷到指定目录，`work/<id>/out.mp4` 里也留一份。

### 2. 本地 ASR 转录（Apple Silicon）

- 用 **parakeet-mlx** 本地模型把视频音轨转成**带标点的英文句级字幕**（`asr.srt`），不依赖 YouTube 字幕（自动字幕缺标点、听写错误多）。
- 支持句间静音分割、长音频分块转录、greedy / beam 解码。
- parakeet 未安装**只影响 transcribe 一个阶段**，其余阶段照常可用。

### 3. 智能中文翻译（ModelScope）

- 走 OpenAI 兼容接口调 DeepSeek 等模型，Prompt 内置**金融/价格行为术语纠错**与口播化要求。
- **按 batch 并发翻译**（`TRANSLATE_CONCURRENCY`，默认 2），主翻译结束后对缺失句**多轮补译**（默认最多 2 轮，零进展提前停）。
- 译文按**英文原文**缓存到 `checkpoints/translations.json`：重新断句、调整 `TRANSLATE_BATCH_SIZE`、部分重跑都不会错位，也不会重复花钱翻译同一句。
- `API_KEY` 支持**逗号分隔多个 key**：某个 key 报 `insufficient balance` 时自动切下一个重投（切换不占重试次数），全都没余额则立即失败，补 key 后续跑。
- **译不出来的句子留空**：不写英文回填，也不配音、不出字幕，日志里给出 idx 清单；下次运行自动只补这些句子（自愈）。
- 视频标题随第一批正文顺带翻译（不单独发请求）。

### 4. 智能 TTS 配音（edge-tts）

- 每句按英文时间槽（slot）适配时长：先原始语速合成，**超时自动加速**（最高 `TTS_MAX_RATE`%），仍超则用压缩候选文本。
- 并发合成（`TTS_CONCURRENCY`，默认 2，可调 8~16）。
- 每句记录指纹 `tts_key = sha1(中文 | 音色 | max_rate)`：**改了中文或换了音色自动重配对应的句子**，不再需要手动删 mp3。
- **纯标点句自动跳过**：`zh` 只剩 `。？！` 等标点的段不发 TTS、字幕也不显示，不再因此卡住整条视频。
- 结束后校验：所有有内容的中文段必须有音频，缺句提示续跑补录。

### 5. 旁白时间轴

- 按每句 `start` 把各句 TTS 音频贴到整片时间线上（PCM 拼接），**避免上百路 ffmpeg amix 的 OOM / exit 232**。
- 解码多线程并发；重叠冲突后贴者胜；旁白时长明显短于视频会拒绝合成。

### 6. 中文字幕烧录

- 字幕按约 **32 字强制换行**、优先在标点处断开，黄色字幕略小字号烧进画面（需带 libass 的 ffmpeg）。
- 同时产出可编辑的 `zh.srt` / `en_merged.srt` / `zh.ass`。

### 7. 片头封面（可选）

- 配 `COVER_IMAGE` 后，成片开头加 **1 秒静帧封面**（静音），再进入正片。
- 封面自动缩放 + 居中 pad 到视频分辨率；文件缺失只警告、不中断合成；新配/换掉封面会自动重合成。

### 8. 批量流水线与失败管理

- **播放列表**自动展开逐条跑；**URL 列表文件**（`-f`）按行逐条跑；均支持 `--limit` 分片窗口。
- 批量中单条失败**不重试**，记入 `video_failed.txt`（带时间戳），继续下一条。
- 已成功的任务落盘在各自 `work/<id>/`，整批重跑会自动跳过已完成阶段。

### 9. 成品导出与序号

- 导出文件名：`NN_中文标题 [视频ID].mp4`，`NN` 是**列表序号**（`01`、`02`…），`limit` 的窗口位置与它一致（`--limit 20,1` → `21`）。
- 失败视频**不占位**：序号永远是它在列表里的位置；补跑时序号从 `job_state.json` 读取，不会漂移。
- 单条视频固定 `01`。

### 10. 断点续跑与状态

- 每个任务目录有 `job_state.json`，7 个阶段各自 pending/running/done/failed。
- 跳过某个阶段的条件是**「状态为 done」且「产物仍然有效」**：产物被删、被改，或上游变了（源片换了、字幕重断、中文改了、旁白/成片比输入旧），该阶段就会重做。
- 因此**默认什么都不用加**：直接重跑同一条命令即可续跑；某个阶段刚跑完，本窗口内它后面的阶段一定会跟着重做。
- `--from STAGE` / `--to STAGE` 精确控制本次要跑的阶段区间（见[命令行参考](#命令行参考)）。
- `--status` 打印各阶段状态 + 音频完整性实时检查。

### 11. 预览模式

- `--end N` 只处理前 N 秒：下载后裁剪、转录、翻译、配音、合成一条预览样片 `out_preview.mp4`，试完效果再跑整片。
- 预览 ↔ 整片互相切换时（`--end` 值变了），下载之后的阶段自动重做，不需要手动删文件。

### 12. 磁盘清理

- `--clean --yes`：确认成片无误后清理 work 目录中间件，只留最终视频；缺成片或成片偏短会**拒绝清理**。
- 全长模式下 `source.mp4` 是 `source_full.mp4` 的**硬链接**，不再白占一份源片空间。

---

## 工作流程与架构

### 阶段流水线

```mermaid
flowchart LR
    A[下载+切源<br/>download] --> B[转录<br/>transcribe] --> C[断句分段<br/>segment]
    C --> D[中文翻译<br/>translate] --> E[TTS 配音<br/>tts]
    E --> F[旁白时间线<br/>narration] --> G[合成成片<br/>compose]
```

> 目录结构：`work/<youtube_id>/` 是每个视频的工作区；`output/` 是给人看的成品目录；`zh_dub/` 是实现各阶段的 Python 包。

### 阶段说明

| 阶段 | 做什么 | 输入 → 输出 |
|---|---|---|
| `download` | yt-dlp 下载最高可用画质；再按 `--end` 切出 `source.mp4`（全长走硬链接） | URL → `source_full.mp4` → `source.mp4` |
| `transcribe` | parakeet-mlx 本地转录英文（带标点）| `source.mp4` → `asr.srt` |
| `segment` | SRT 解析 + 断句 + 合并成 TTS 大小的段落 | `asr.srt` → `segments.json` |
| `translate` | 并发翻译 + 多轮补译，写 `segments.json` 的 `zh` | 段(en) → 段(en+zh) |
| `tts` | 每句合成长度适配的 mp3，校验完整性 | 段 → `audio/seg_*.mp3` |
| `narration` | 把各句音频按时间贴成整片旁白 wav | 段+音频 → `narration.wav` |
| `compose` | 烧字幕、旁白替换原音、可选片头、生成成片 | 视频+旁白+字幕 → `out.mp4` |

### 状态与续跑机制

- `job_state.json` 里每个阶段一个对象：`{"status": "pending|running|done|failed", "detail": {...}}`；`meta` 里存 `video_id`、`title_en/zh`、`voice`、`quality`、`output_dir`、`seq`（序号）等。
- 每个阶段自己回答两个问题（`zh_dub/stages.py`）：
  1. `is_fresh()`：产物还在不在、是不是比它的输入新（例如 `asr.srt` 是否比 `source.mp4` 新、每句 mp3 的指纹是否匹配当前中文与音色、成片是否比旁白和字幕新）；
  2. `run()`：怎么产出。
- 跳过条件是 `done && is_fresh`；两者任一不成立就重做。某个阶段跑完后，**本次窗口内它后面的阶段一律重做**，避免用到过期产物。
- 典型效果：删掉某句 `audio/seg_XXXX.mp3` → 重跑只会补这一句，然后自动重建旁白与成片；改一句 `zh` → 只有这句重配，下游跟着重做。
- `--from STAGE`：把 `STAGE`（到 `--to` 为止）标为 pending 再跑，用于「强制重做」。
- `--to STAGE`：只跑前一段，后面的阶段状态原样保留。

### work 目录结构

```text
work/<id>/
  job_state.json          # 状态机
  source_full.mp4         # 原始下载
  source.mp4              # 实际使用的片源（全长=硬链接，预览=裁剪）
  asr.srt                 # parakeet 本地转录（英文，带标点）
  segments.json           # 分段中枢（en/zh/rate_pct/audio/tts_key…，可手改 zh）
  checkpoints/
    translations.json     # 英→中缓存（跨运行复用，按英文原文索引）
  audio/seg_0000.mp3      # 每句最终配音
  narration.wav           # 全片旁白时间轴
  zh.srt / en_merged.srt  # 中英字幕
  zh.ass                  # 烧录用 ASS 字幕
  out.mp4 (或 out_preview.mp4)
```

---

## 安装与配置

### 环境要求（macOS / Apple Silicon 推荐）

| 依赖 | 说明 |
|---|---|
| Python 3.10+（推荐 conda 环境 `python3`） | 运行入口 |
| `yt-dlp` | 视频下载（`brew install yt-dlp`） |
| `ffmpeg` + libass + `ffprobe` | 混音/烧字幕/时长（`brew install ffmpeg-full`） |
| `edge-tts`（pip） | TTS 配音（用的是它的 Python 库，不需要命令行程序） |
| `parakeet-mlx`（pip，可选但推荐） | Apple Silicon 本地转录；首次运行自动从 HuggingFace 下载模型 |
| `API_KEY`（ModelScope） | 对话翻译 |

```bash
cd /path/to/EN_Video2CH
conda activate python3
python -m pip install -r requirements.txt        # openai>=2, edge-tts
python -m pip install parakeet-mlx               # 可选：本地 ASR
brew install yt-dlp ffmpeg-full
cp .env.example .env
```

跑单元测试（断句、limit、导出命名、翻译缓存、TTS 指纹、Runner 调度）：

```bash
python -m pip install pytest
python -m pytest tests -q
```

### 配置文件 `.env`

必需：

| 变量 | 说明 |
|---|---|
| `API_KEY` | ModelScope API Key（翻译必填）；多个用逗号分隔，余额不足自动切换 |
| `MODEL` | 翻译模型，如 `deepseek-ai/DeepSeek-V4.1-Flash` |

常用（均有默认值）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY` | `http://127.0.0.1:7890` | 仅用于 yt-dlp 下载 |
| `VOICE` | `zh-CN-YunyangNeural` | TTS 音色 |
| `QUALITY` | `720` | `720` / `1080` / `best` |
| `WORKDIR` | `./work` | 任务根目录 |
| `OUTPUT_DIR` | 空 | 成品导出目录（`--output` 会覆盖它）；缺省不额外导出 |
| `COVER_IMAGE` | 空 | 片头封面图（1 秒），相对项目根或绝对路径 |
| `TTS_CONCURRENCY` | `2` | TTS 并发（edge-tts 易限流，可调 2~16） |
| `TTS_MAX_RATE` | `30` | TTS 最大加速百分比（0~100） |
| `TRANSLATE_BATCH_SIZE` | `100` | 翻译批大小（改它不影响已有译文缓存） |
| `TRANSLATE_MAX_RETRIES` | `5` | 单批最大重试 |
| `TRANSLATE_CONCURRENCY` | `2` | 主翻译 batch 并发（补译在主翻全部结束后串行） |
| `TRANSLATE_REFILL_MAX_ROUNDS` | `2` | 漏翻补译轮数上限（某轮零进展提前停；`0` 关闭补译） |

parakeet 本地 ASR：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PARAKEET_MODEL` | `mlx-community/parakeet-tdt-0.6b-v3` | HF 仓库名 |
| `PARAKEET_SILENCE_GAP` | `2.0` | 句间静音分割阈值（秒） |
| `PARAKEET_CHUNK_DURATION` | `120` | 长音频分块转录秒数（`0`=不分块） |
| `PARAKEET_OVERLAP_DURATION` | `15` | 分块重叠秒数 |
| `PARAKEET_DECODING` | `greedy` | `greedy` / `beam` |
| `PARAKEET_BEAM_SIZE` | `5` | beam 束宽 |

可选工具路径（配置后严格使用，不配则自动探测）：`PYTHON`、`YT_DLP`、`FFMPEG`、`FFPROBE`、`MODELSCOPE_BASE_URL`（默认 `https://api-inference.modelscope.cn/v1`）。

---

## 快速开始

```bash
# 1. 单条视频整片（自动 work/<id>/，成片同时拷到 output/）
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --output ./output

# 2. 播放列表整表（自动展开，逐条跑）
python job_run.py --url "https://www.youtube.com/playlist?list=PLAYLIST_ID" --output ./output

# 3. 先只跑前 2 条试效果
python job_run.py --url "https://www.youtube.com/playlist?list=PLAYLIST_ID" --limit 2 --output ./output

# 4. 3 分钟预览样片
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --end 180

# 5. 批量列表文件
python job_run.py -f video_list.txt --output ./output

# 6. 看某条进度
python job_run.py --work work/VIDEO_ID --status
```

---

## 命令行参考

### 参数速查

| 参数 | 说明 |
|---|---|
| `--url URL` | 单条视频或播放列表；播放列表自动展开逐条跑；`watch?v=ID&list=...` 仍按单条处理 |
| `--work DIR` | 指定已有任务目录续跑/查看（相对项目根或绝对路径） |
| `-f` / `--file LIST` | 批量 URL 列表（每行一个，`#` 开头为注释）；失败追加写入同目录 `video_failed.txt`，不重试 |
| `--failed-file PATH` | 自定义失败列表路径（默认 `<list_dir>/video_failed.txt`） |
| `--limit N` 或 `--limit OFFSET,COUNT` | SQL 风格窗口：`20`=前 20 条；`20,40`=跳过 20 再取 40（第 21~60 条）；`0` 或省略=全部 |
| `--from STAGE` | 强制重做该阶段及其之后（到 `--to` 为止）的阶段 |
| `--to STAGE` | 只跑到该阶段为止，后面的阶段不动 |
| `--end N` | `0`=整片（默认）；`>0`=只处理前 N 秒预览，出 `out_preview.mp4` |
| `--voice NAME` | 覆盖 `.env` 的 `VOICE`（指纹变化会让相关句子自动重配） |
| `--quality 720\|1080\|best` | 覆盖 `.env` 的 `QUALITY` |
| `--output DIR` | 覆盖 `.env` 的 `OUTPUT_DIR`，成片拷成 `<序号>_中文标题 [id].mp4` |
| `--status` | 打印状态后退出 |
| `--clean` | 清理 work 中间件，只留成片（不加 `--yes` 只 dry-run） |
| `--yes` | 配合 `--clean` 真正删除 |

### 阶段与 `--from` / `--to`

阶段顺序（`--from` / `--to` 的取值）：

```text
download → transcribe → segment → translate → tts → narration → compose
```

不写 `--from` / `--to` 就是「整条流水线 + 能跳的都跳」。常用的等价写法：

| 想做的事 | 命令 |
|---|---|
| 全流程（默认） | `python job_run.py --work DIR` |
| 只下载 + 切源 | `python job_run.py --url URL --to download` |
| 到翻译为止 | `python job_run.py --work DIR --to translate` |
| 只补翻译/补译漏句 | `python job_run.py --work DIR --from translate --to translate` |
| 只配音（缺句自愈） | `python job_run.py --work DIR --from tts --to tts` |
| 配音齐了只出片 | `python job_run.py --work DIR --from narration` |
| 重新配音 + 出片 | `python job_run.py --work DIR --from tts` |
| 重新转录 | `python job_run.py --work DIR --from transcribe` |
| 只重合成（新配了封面） | `python job_run.py --work DIR --from compose` |

### 导出序号规则

导出文件名：`NN_中文标题 [视频ID].mp4`。`NN` 的取值：

| 场景 | 序号 |
|---|---|
| 播放列表 | yt-dlp 展开的**列表绝对位置**（`--limit 20,1` 那条是 `21`） |
| `-f` 列表文件 | `offset + i`（`--limit 20,1` → `21`） |
| 单条 `--url` | `01` |
| 失败视频 | 不占位，序号仍是它在列表里的位置；补跑（单独 `--work`）从 `job_state.json` 读回原序号 |

### 失败文件格式

`video_failed.txt` 每行：

```text
[2026-09-24 10:00:00] https://www.youtube.com/watch?v=bbb<TAB>TTS incomplete: missing [12, 15]
```

- 行首为失败时刻（本地时间），URL 与错误用 **tab** 分隔
- 批量失败**不重试**、继续下一条；退出码：有失败 → `1`，全成功 → `0`，用户中断 → `130`
- 只重跑失败的：把每行 URL 拷进新列表（去掉 `[时间戳] ` 前缀和 tab 后的错误摘要），再 `-f` 一次

### 常用组合速查

| 场景 | 命令 |
|---|---|
| 全新视频整片 | `python job_run.py --url URL --output ./output` |
| 播放列表整表 | `python job_run.py --url PLAYLIST_URL --output ./output` |
| 播放列表分批 | `python job_run.py --url PLAYLIST_URL --limit 20,40` |
| 批量整片 | `python job_run.py -f list.txt --output ./output` |
| 3 分钟预览 | `python job_run.py --url URL --end 180` |
| 看进度 | `python job_run.py --work work/ID --status` |
| 挂了接着跑 | `python job_run.py --work work/ID` |
| 只补翻译/补译漏句 | `python job_run.py --work work/ID --from translate --to translate` |
| 只补配音（缺句自愈） | `python job_run.py --work work/ID --from tts --to tts` |
| 配音齐了只出片 | `python job_run.py --work work/ID --from narration` |
| 改中文后重配音出片 | `python job_run.py --work work/ID --from tts` |
| 重翻全文 | `python job_run.py --work work/ID --from translate` |
| 清理前预览将删内容 | `python job_run.py --work work/ID --clean` |
| 确认后清空间 | `python job_run.py --work work/ID --clean --yes` |

---

## 使用场景详解

### 1. 单条视频

```bash
# 默认画质（或 .env QUALITY）
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID"

# 1080p / 最高画质 / 指定音色
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --quality 1080
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --quality best
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --voice zh-CN-XiaoxiaoNeural

# 成品额外导出到 output/（文件名：带序号 + 中文标题）
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --output ./output
```

### 2. 播放列表 / 合集

- `--url` 指向 `/playlist?list=...` 自动展开，按列表顺序逐条跑；失败记入 `video_failed.txt` 后继续下一条。
- `watch?v=ID&list=...` 仍按**单条视频**处理。
- 已处理过的 `work/<id>/` 按状态续跑，整表跑不完可以**反复执行同一条命令**（跳过已完成）。
- 导出序号 = 列表绝对位置，`--limit 20,40` 那批里的视频编号是 21~60，而非 1~40。

```bash
python job_run.py --url "https://www.youtube.com/playlist?list=PLAYLIST_ID" --output ./output
python job_run.py --url "https://www.youtube.com/playlist?list=PLAYLIST_ID" --limit 2
python job_run.py --url "https://www.youtube.com/playlist?list=PLAYLIST_ID" --limit 20,40
```

### 3. 批量列表文件（`-f`）

```text
# video_list.txt
https://www.youtube.com/watch?v=aaa
https://www.youtube.com/watch?v=bbb   # 行尾注释也可以
```

```bash
python job_run.py -f video_list.txt
python job_run.py -f video_list.txt --output ./output --end 180
python job_run.py -f video_list.txt --limit 20      # 前 20 条
python job_run.py -f video_list.txt --limit 20,40   # 第 21~60 条
```

行为说明：

- 从上到下逐条跑，每条独立 `work/<id>/`；失败不重试，追加写 `video_failed.txt`，继续下一条。
- 批量模式下 `--url` / `--work` 被忽略；`--status` / `--clean` 不可用。
- 序号 = 行号（offset+i），失败不占位。
- `Ctrl+C` 停止整批；已写入的失败记录保留。

### 4. 预览（先跑前 N 秒试效果）

```bash
python job_run.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --end 180   # 3 分钟预览
python job_run.py --work work/VIDEO_ID --end 60                                # 已有 work 上再预览
```

- 预览任务在 `work/<id>` 里产出 `out_preview.mp4`；清理时（`--clean`）自动保留预览文件。
- 改回整片：`python job_run.py --work work/VIDEO_ID --end 0`（`--end` 变了，下载之后的阶段会自动重做）。

### 5. 指定已有 work 继续

```bash
python job_run.py --work work/VIDEO_ID          # 从当前状态接着跑（跳过已完成）
python job_run.py --work /abs/path/to/work/VIDEO_ID
python job_run.py --work work/VIDEO_ID --status # 只看状态
```

### 6. 失败后续跑

```bash
# 推荐：直接重跑同一条命令，能跳的阶段都会跳过
python job_run.py --work work/VIDEO_ID

# 卡在某个阶段（例如 TTS 缺句）时，只补那一段
python job_run.py --work work/VIDEO_ID --from tts --to tts
```

### 7. 按阶段拆开跑

```bash
python job_run.py --url URL --to download                    # 只下载 + 切源
python job_run.py --work work/ID --to translate              # 到翻译为止
python job_run.py --work work/ID --from translate --to translate
python job_run.py --work work/ID --from tts --to tts         # 只配音
python job_run.py --work work/ID --from narration            # 旁白 + 成片（TTS 已齐）
python job_run.py --work work/ID --from tts                  # TTS + 旁白 + 成片
```

### 8. 翻译：并发、缓存与漏翻自愈

```text
读 checkpoints/translations.json（英→中缓存）
  → 只挑还没译过的句子，按 batch 并发翻译（TRANSLATE_CONCURRENCY）
  → 补齐缓存后，仍缺的句子进入补译轮（默认最多 2 轮，零进展提前停）
  → 仍缺就留空：不配音、不出字幕，日志列出 idx
  → 下次运行只重发这些句子
```

- 译文按**英文原文**缓存：改 `TRANSLATE_BATCH_SIZE`、重新断句、部分重跑都不会把译文贴错句子，也不会重复花钱。
- 每轮补译**只发还没译好的句子**，不会整表重翻。
- 标题随第一批正文顺带翻译（最后一条编号），存 `meta.title_zh`，用于导出文件名。
- 调参后重跑：`python job_run.py --work work/ID --from translate --to translate`；整段重翻用 `--from translate`（会重做后续阶段）。
- 想彻底重翻：删掉 `checkpoints/translations.json` 再跑。

```env
TRANSLATE_BATCH_SIZE=100
TRANSLATE_CONCURRENCY=2
TRANSLATE_REFILL_MAX_ROUNDS=2
TRANSLATE_MAX_RETRIES=5
```

### 9. 手改中文后再出片

```bash
# 1) 编辑 work/VIDEO_ID/segments.json 里各段的 "zh"
# 2) 直接重跑：改过的句子会自动重配，旁白与成片跟着重做
python job_run.py --work work/VIDEO_ID --from tts
```

不用手动删 `audio/seg_*.mp3`：每句的音频指纹与中文、音色绑定，改了就自动失效。

### 10. TTS 调参与重跑

```bash
# .env（改完无需改命令）
# TTS_CONCURRENCY=8
# TTS_MAX_RATE=30
# VOICE=zh-CN-YunyangNeural

# 调大并发再补配音（已有音频跳过，缺句自动补）
python job_run.py --work work/VIDEO_ID --from tts --to tts

# 换音色（指纹全变，相关句子自动重配）
python job_run.py --work work/VIDEO_ID --voice zh-CN-XiaoxiaoNeural --from tts
```

TTS 机制小结：

- `rate=0` 先合一遍 → 读 mp3 时长 → 超 slot 再按需加速最多打第 2 次；仍超则用压缩候选文本。
- 每句指纹 `tts_key = sha1(zh | voice | max_rate)`，`tts_dur` / `rate_pct` / `note` 写回 `segments.json`。
- **纯标点句不发 TTS、字幕不显示、校验不要求**，不会因为一句 `。` 卡住整条。
- `TTS_MAX_RATE` 改变也会让指纹失效（加速上限不同，适配结果可能不同）。
- 结束后校验：有内容的中文段必须都有音频，否则 `tts` 标记 failed 并提示补缺。

### 11. 成片合成 / 封面 / 字幕

```bash
# 旁白时间轴坏了：从 narration 重做
python job_run.py --work work/VIDEO_ID --from narration

# 只烧字幕合成（新配了 COVER_IMAGE 后）
python job_run.py --work work/VIDEO_ID --from compose
```

- 字幕约 32 字强制换行、优先在标点处断开；黄色字体。
- 配了 `COVER_IMAGE` 会加 1 秒片头；封面路径与上次记录不一致时自动重合成。
- mux 会校验输出时长：明显短于源片（损坏/截断）会报错并拒绝出片；`--status` 也会用同样的规则判断成片是否还算完成。

### 12. 清理 work 目录

```bash
python job_run.py --work work/VIDEO_ID --clean        # dry-run：只列出将删项
python job_run.py --work work/VIDEO_ID --clean --yes  # 真正删除
```

- 保留：`out.mp4`（预览任务为 `out_preview.mp4`）。
- 删除：`audio/`、`source*.mp4`、字幕、`narration.wav`、`segments.json`、`job_state.json` 等全部中间件。
- 保护：没有合格成片（缺失/过小）或成片明显短于源片 → **拒绝清理**。
- **不可恢复**：之后要改字幕/重配音必须重新跑流水线。

---

## 产物一览

| 路径 | 说明 |
|---|---|
| `work/<id>/out.mp4` | 整片成片（可含 1s 片头） |
| `output/NN_中文标题 [id].mp4` | 导出成品（`--output` / `OUTPUT_DIR`；预览为 `.preview.mp4`） |
| `work/<id>/out_preview.mp4` | 预览样片（`--end>0`） |
| `work/<id>/asr.srt` | parakeet-mlx 本地转录的英文句级字幕 |
| `work/<id>/segments.json` | 分段中枢（`en`/`zh`/`rate_pct`/`audio`/`tts_key`…，可手改 `zh`） |
| `work/<id>/job_state.json` | 工程状态（7 阶段 + meta） |
| `work/<id>/audio/seg_XXXX.mp3` | 每句最终配音 |
| `work/<id>/narration.wav` | 全片旁白时间轴（24kHz 单声道） |
| `work/<id>/zh.srt` / `zh.ass` | 中文字幕（ASS 用于烧录） |
| `work/<id>/en_merged.srt` | 英文分段字幕 |
| `work/<id>/checkpoints/translations.json` | 英→中翻译缓存 |
| `video_failed.txt` | 批量失败记录（`[时间戳] URL<TAB>错误`，追加写） |

---

## 常见问题

| 现象 | 处理 |
|---|---|
| `transcribe` 报 parakeet-mlx 未安装 | conda python3 里 `pip install parakeet-mlx` 后重跑（首次会下载 HF 模型） |
| 转录太慢 / 想更准 | 调大 `PARAKEET_CHUNK_DURATION` 或改 `PARAKEET_DECODING=beam` |
| 换转录模型/阈值没生效 | `python job_run.py --work DIR --from transcribe` |
| 翻译漏句多 | 已有多轮补译；可调大 `TRANSLATE_REFILL_MAX_ROUNDS` 后 `--from translate --to translate`；漏句会在下次运行时继续补 |
| 日志提示「仍有 N 条未译」 | 这些句子不配音、不出字幕；再跑一次只会重发这 N 条，或删 `checkpoints/translations.json` 全量重翻 |
| 翻译太慢 | `.env` 调大 `TRANSLATE_CONCURRENCY`（如 3~4），注意 API 限流 |
| TTS 太慢 | `.env` 调大 `TTS_CONCURRENCY`（如 8），再 `--from tts --to tts` |
| 缺音频 / TTS 未完成 | `python job_run.py --work DIR --from tts --to tts`（纯标点句会自动跳过） |
| 换音色仍是旧声 | 正常路径下会自动重配；若手动改过音频文件，删掉 `audio/seg_*.mp3` 后 `--from tts` |
| 中文只剩标点导致 TTS 失败（No audio received） | 纯标点段自动跳过（不发 TTS、字幕不显示），重跑 TTS 即可 |
| yt-dlp 网络失败 | 检查 `PROXY` |
| ModelScope 401 | 检查 `API_KEY`（翻译不走代理） |
| 翻译报 `insufficient balance` | 该 key 余额不足；配了多个 key 会自动切换。全都没余额则失败，补 key 后重跑（`--from translate --to translate`） |
| `Unknown filter ass` | 安装带 libass 的 ffmpeg（如 `ffmpeg-full`） |
| narration ffmpeg exit 232 / amix 爆 | 已改 PCM 拼接；`--from narration` 重跑 |
| compose 显示 done 但片不对 / 时长被截断 | 重跑时会被判定为不新鲜并自动重做；也可 `--from compose` |
| 配了封面但成片没有片头 | 确认 `COVER_IMAGE` 路径存在，再 `--from compose` |
| 字幕太长出屏 | 已按约 32 字换行；改字幕参数后 `--from compose` 重烧 |
| 改了配置但阶段没重做 | 用 `--from STAGE` 强制；或确认产物是否真的需要重做（指纹/时间戳判定） |
| 批量一条失败 | 不重试，记入 `video_failed.txt`；可把失败 URL 重新喂列表，或对单条 `--work` 续跑 |
| 想跳过前 N 条接着跑 | `--limit N,M`（序号仍是列表绝对位置） |
| work 目录太大想腾空间 | 先确认 `out.mp4`，再 `--clean --yes` |
| clean 提示 truncated / refused | 成片不完整：先 `--from compose` 重合成，再 clean |
| clean 后想改字幕重做 | 中间件已删，需重新 `--url` 或放回源片后跑流水线 |
| 旧的 work 目录（YouTube VTT 时代的产物） | 状态文件版本不同会被当作全新任务；建议只保留 `out.mp4`，要重做就把源片放回或直接用 `--url` |

---

## 源码结构

```text
job_run.py               # 入口：参数解析、批量调度、失败记录、状态打印、清理
zh_dub/
  runner.py              # Stage 协议 + Runner：跳过判定、状态记账、失败处理
  stages.py              # 7 个阶段：download/transcribe/segment/translate/tts/narration/compose
  state.py               # JobState：job_state.json 状态机（7 阶段 + meta）
  segments.py            # Segment 数据模型、读写、srt/ass 输出、标点判定
  segmenter.py           # SRT 解析 + 断句合并（句子级打包）
  asr.py                 # parakeet-mlx 本地转录 → asr.srt
  translate.py           # ModelScope 批量翻译 + en→zh 缓存 + 多轮补译
  tts.py                 # edge-tts 并发合成 + 时长适配 + 指纹缓存 + 完整性校验
  narration.py           # 各句音频按时间贴成整片旁白 wav
  compose.py             # 烧字幕 + 旁白混音 + 可选片头
  clean.py               # 清理 work 中间件（保留成片）
  media.py               # yt-dlp 下载、ffmpeg 切源/探时长、代理环境
  sources.py             # URL 判断、播放列表展开、--limit 窗口、work/output 目录解析
  export.py              # 成品导出：中文标题 + 序号命名
  config.py              # .env 加载、工具路径探测、Settings
  logutil.py             # 日志/进度条/阶段横幅
tests/                   # pytest：断句、limit、导出命名、翻译缓存、TTS 指纹、Runner 调度
```
