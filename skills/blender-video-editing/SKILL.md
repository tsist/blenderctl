---
name: blender-video-editing
description: 使用 Blender 原生 VSE 与公共 CLI 制作分镜粗剪、按镜头编号原位换片、接手现场时间线手调、编排字幕音轨与导出成片。接到多镜拼装、白膜预演剪辑、AI 或正式渲染回流替换、切点微调、混音字幕及成片输出任务时使用。
metadata:
  version: "1.0.0"
---

# Blender 视频剪辑

本技能属于 [剪辑部门](../blender-work-methodology/references/departments/editing.md)，负责把白膜分镜、正式渲染画面、AI 镜头候选、对白音乐与中文字幕编排成节奏顺畅的可播放成片。公共入口与批次目录规范见 [blender-cli](../blender-cli/SKILL.md)，所有修改都在独立工作副本上执行，源文件与母本保持原样留作对照。

## 怎么做

### 1. 核对入口、镜头表与素材规格

动手前先查当前剪辑契约，拿到准确子命令与 Schema，省去猜字段名的返工。

```powershell
$ctl = './blenderctl.ps1'
& $ctl discover --product video
& $ctl video describe
& $ctl video describe --operation manifest --schema
```

接手素材后，先把每一份外部文件的绝对路径和小写 SHA256 列进资源清单（包含图像序列每张图、视频、音频、字体、代理以及所用的 `ffmpeg`/`ffprobe`）。核对视频素材是否满足与时间线一致的有理帧率（如 `[24, 1]`）、恒定帧率（CFR）、零起点和方像素。遇到异帧率或可变帧率（VFR）素材，先跑 `scripts/prepare_media.py` 派生同帧率 PNG 序列与 48kHz 对齐 PCM，再进时间线，避免切点漂移。

### 2. 搭起粗剪时间线，先定结构再追细节

按镜头表里的稳定 `shot_id` 在新场景里摆好各镜预演画面、临时对白、音乐节拍与参考字幕。区间统一用左闭右开 `[start, end_exclusive)`，`duration` 记有效贡献帧数。

- **从零建粗剪**：写好 `action: "create"` 的请求 JSON，通过 `blenderctl.ps1 --jobs-dir $jobs --compact request $req`（或 `video run`）生成可编辑 VSE 工作副本。
- **套用现成样式**：白膜审阅、AI 回流、CG 展示或中文阅读优先场景，可直接用 `scripts/video_templates.py` 编译四套内置无代码配方，快速产出标准请求。
- **过粗剪审看①**：把整段预演拼通、声音节奏对齐后先给用户看。把镜头顺序、时长和停顿在白膜阶段敲定，再让下游投入正式渲染。

### 3. 按镜头编号原位换片，改切点同步回写镜头表

正式渲染或 AI 镜头候选陆陆续续回来后，按稳定的 `shot_id` 原位替换对应条带，保住已经调准的入出点、转场、音轨和字幕。

- **同镜多候选比选**：把同镜头候选放到相同时间区间、显示变换与音量下对比，记录 `candidate_id`、可用帧段和选用理由。用 `action: "patch"` 里的 `select_candidate` 或定向 `update` 换片，动作落点变了就同步重校切点。
- **微调单镜右边界**：短 SDR 受管时间线需要拉长或缩短某镜末尾时，运行 `blenderctl.ps1 --jobs-dir $jobs --compact video ripple`（传 `--scene`、`--workflow-id`、`--clip`、`--duration` 与 `--edge END`），让后续条带顺势波纹移动。
- **切点变更回写镜头表**：只要剪辑时延长或缩短了某个镜头，先把新起止帧与手柄帧（handles）写回镜头表主档（由 VSE 左闭右开区间 `[start, end_exclusive)` 换算为镜头表和批渲染的闭区间 `[first, last]` 时，按 `last = start + duration - 1` 换算），再通知 [相机](https://github.com/tsist/blender-camera-workflow/blob/main/skills/blender-camera-workflow/SKILL.md)、动画或 [批渲染](https://github.com/tsist/blender-render-batch/blob/main/skills/blender-render-batch/SKILL.md) 部门补足对应帧段，保持全片一盘棋。

### 4. 接手用户在 Blender 窗口里的手调

后台命令读的是磁盘文件；用户在打开的 Blender 窗口里手调了条带、字幕或锁定状态时，按下面三步安全接手：

1. 在目标 Blender 窗口挂载会话目录，后台运行 `video inspect-live --session-dir $sDir --session-id $sId --scene $scene` 读取尚未保存的现场状态。
2. 请用户另存为明确的工作快照，重新算快照 SHA256，跑一次 `video inspect` 获取受管条带与锁定状态。
3. 写定向 `patch` 只改需要动的目标字段。锁定条带、手改过的非目标字段以及未知条带保持原样；出现修改冲突时保留双方版本供用户拍板。

### 5. 配齐音轨、中文字幕、变速与部门交接

- **分轨混音**：把对白、BGM、音效和原音分轨摆放，优先保住对白清晰度。声音输出走 48kHz 连续解码与整数采样混合，并在主增益之后做削波检查；含未静音音轨输出时，在 `output` 与 `resources` 中显式带上 `ffmpeg` 和 `ffprobe`。
- **可编辑中文字幕**：现有纯文本 SRT 先跑 `scripts/srt_to_clips.py` 转成按有理帧率量化的 TEXT 条带 JSON，再并入时间线。显式绑定本机中文字体（如 `msyh.ttc`）并把同次修改共享到同一份打包字体，控制总打包字体数量在 Blender 槽位上限以内。调剪后同步更新字幕时间，分别核对烧录画面与外挂 SRT。
- **视觉变速与停帧**：把完整源素材放入 META，在内部用 SPEED 的 `FRAME_NUMBER` 按源帧索引打 `CONSTANT` 关键帧，外层 META 保留目标半开区间，每帧输出前刷新缓存。
- **轻量预览与多画幅**：同画幅小尺寸审片用整数 1–100% 的 `resolution_percentage` 缩小；高分辨率 PNG 序列预览可用 `scripts/prepare_proxy.py` 生成 25%/50%/75% 独立代理并经 `output.preview.proxies` 显式绑定，定稿导出原片时去掉 `preview`。横竖版或更大画幅另建新时间线版本，重新做 `fit` 裁切与字幕安全区检查。
- **跨部门与外部软件交接**：复杂调色、辉光或扣像按显式媒体清单（含 handles、色彩空间、Alpha 与 `source_in`）交给 [合成部门](https://github.com/tsist/blender-compositing-workflow/blob/main/skills/blender-compositing/SKILL.md)。需要和外部剪辑软件交换纯剪切时间线时，用 `video otio export` 与 `video otio import` 往返。

### 6. 导出成片、管控长作业并做冷重开验收

- **长作业管控**：耗时导出加上 `--async` 提交，用 `blenderctl.ps1 --jobs-dir $jobs --compact video job wait $jobId --wait-seconds 30` 分段等待，取消或查终态走 `video job cancel/status/result`。
- **恢复旧作业**：续跑或查取历史 0.1.4 分发作业时，保留原 `--jobs-dir`，在 `video` 子命令前加上 `--video-release 0.1.4`，再执行 `video job result $jobId`；用 `resume` 指向旧完整 `video-report.json` 及其 SHA256 在新作业里重跑完整输出。
- **冷重开与真片核对**：导出完成后另起独立进程重开候选工程核对帧域与引用。整片与逐镜视频全部真实解码，亲眼抽看切点前后、快动作、停帧与每条字幕首中末，亲耳听检对白清晰度、音乐过渡与音画落点；待听检或待设备观看项如实标注 `not_run`。

## 交给下游

审看①（粗剪）、审看②（精剪）与最终交付分别提供：

- **可编辑 VSE 工程副本**：完整绝对路径、小写 SHA256 以及可重连的外部媒体资源清单。
- **镜头表**：从 `video-shots.json` 筛选出真实视觉镜头并按 `start` 排序，附各镜候选选用记录与相对白膜的切点改动说明。
- **可播放成片与外挂字幕**：核对过分辨率、帧率、音画同步与响度的整片/逐镜视频，以及以各自 0 秒起算的独立 SRT。
- **外部交换与验收说明**：需要外部剪辑往返时附纯剪切 OTIO 文件；分项列出技术核对结果、视听观察结论与待验证项。

## 例子：3 镜 15 秒短片从白膜粗剪到正式成片

接到任务：「把 `work/reel/` 下 3 个白膜镜头拼成 15 秒 1080p 24fps 粗剪，用户手调后再换入正式渲染片，配对白、BGM 和中文字幕导出成片。」

1. **建批次查契约**：在 `work/audit/<batch>/` 建 `jobs` 目录，跑 `blenderctl.ps1 video describe` 确认字段，算清 3 段白膜、BGM 与 `msyh.ttc` 的 SHA256。
2. **出粗剪过审看①**：写 `create` 请求，用 `shot001`、`shot002`、`shot003` 建 360 帧时间线，跑 `video run` 生成 `candidate-v001.blend` 并导出 50% 分辨率粗剪 MP4 给用户看。
3. **接手手调并回写镜头表**：用户在 Blender 里把 `shot002` 拉长了 12 帧并锁定了片头标题。先跑 `video inspect-live` 核对现场，保存为 `manual-snap-v001.blend` 后跑 `video inspect` 确认锁条带，把 `shot002` 新帧域回写镜头表主档，通知批渲染按新长度出正式片。
4. **原位换片配字幕混音**：收到 3 段正式渲染 PNG 序列后，用 `srt_to_clips.py` 把定稿对白 SRT 转成 TEXT 条带 JSON，写 `patch` 请求按 `shot_id` 原位替换 3 个视觉条带，加上对白/BGM 音轨与淡入淡出，保持片头锁条带原样。
5. **导出与冷重开验收**：跑 `video run` 导出 1080p 正式 MP4 与独立 SRT，另起进程冷重开最终候选工程确认零丢引用；抽看每个切点与全部字幕首中末、核对 48kHz PCM 零削波并听检对白与音乐过渡，连同工程副本、筛选后的镜头表和成片交付用户审看②。

## 参考

- [剪辑判断、候选比较与成片复盘](references/editorial.md)
- [公共 CLI、现场窗口接手、人工往返与长作业恢复](references/cli-and-roundtrip.md)
- [素材预处理、代理预览、音轨字幕与成片导出](references/media-audio-subtitles.md)
- [合成显式媒体交接与纯剪切 OTIO 往返](references/handoff-and-otio.md)
- [通用剪辑模板库使用指南](references/templates.md)
- [契约文档、本地 Wiki 与历史证据索引](references/sources.md)
- 配套脚本：`scripts/prepare_media.py`（异帧率/VFR 派生）、`scripts/prepare_proxy.py`（独立 PNG 代理）、`scripts/srt_to_clips.py`（SRT 转 TEXT JSON）、`scripts/video_templates.py`（模板编译）
