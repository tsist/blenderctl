# 素材预处理、代理预览、音轨字幕与成片导出

本页整理时间线里的常见媒体工艺：异帧率预处理、视觉变速、独立 PNG 代理、48kHz 音频混音、中文字幕烧录与成片导出。先核对 `video describe` 里的最新 Schema，再拼参数。

## 1. 异帧率与可变帧率（VFR）预处理

直接进时间线的视频（`MOVIE`）要求与项目有理帧率一致、恒定帧率（CFR）、零起点、方像素、分辨率恒定且主视频流在 `stream=0`，并在请求里附带带 SHA 的 `params.media_probe`（或 `output.ffprobe`）且列入 `resources`。

遇到异帧率或 VFR 视频时，用本技能自带的 `scripts/prepare_media.py` 派生同帧率 PNG 序列与 48kHz 对齐 PCM：

1. 准备输入 JSON，字段为 `clip_id`、`source`（绝对路径与 SHA 文件描述符）、`fps`（如 `[24, 1]`）、`color_policy`（`SDR_SRGB_ASSERT`）和 `audio`（`NONE` 或 `FIRST`）。
2. 为 `ffmpeg` 和 `ffprobe` 各写一份带路径与 SHA 的工具描述符 JSON。
3. 指定一个新建的绝对输出目录运行：
   ```powershell
   $py = '<PROJECT_ROOT>\runtime\blender\releases\5.3.0\blender-5.3.0-windows-x64\5.3\python\bin\python.exe'
   & $py -B './.agents/skills\blender-video-editing\scripts\prepare_media.py' `
     --source-manifest $srcJson --ffmpeg $ffmpegDesc --ffprobe $ffprobeDesc `
     --output-dir $outDir --max-frames 100000 --timeout-seconds 60
   ```
4. 核对生成的 `media-manifest.json` 里的 `status`、输入/工具 SHA、原始 PTS 映射、帧数和音频补裁；把其中的 `clip_templates` 合并进主时间线清单，把全部 `resources` 并入闭包，按需设置 `start` 和轨道号。原片与派生物同时保留，方便复查原镜头身份。

## 2. 视觉变速、停帧与画面修整

- **视觉变速与停帧**：把完整原素材放进 `META`，在内部挂 `SPEED` 条带并选用 `FRAME_NUMBER`，按源帧索引变化打 `CONSTANT` 关键帧（公式 `source_in + floor((timeline_frame - start) * speed)`，停帧时锁定在 `source_in`）。外层 `META` 保留目标左闭右开有效区间，每次输出帧刷新 `RAW/COMPOSITE` 缓存。本机保持这条路线，跳过原生 `RetimingKey.timeline_frame` 与 `SPEED MULTIPLY` 路线。
- **画面匹配与基础修整**：`fit` 使用 `FIT`、`CROP`（内部映射原生 `FILL`）或 `ORIGINAL`；`crop` 以输入像素为单位指定 `{left, right, top, bottom}`；`grade` 提供 `0..20` 范围的基础 `multiply` 与 `saturation`（默认 `1`）。复杂局部调色与特效交由合成部门处理。
- **叠化转场**：双镜头叠化时前镜保持不透明，上层后镜在交叠区间做 `LINEAR` 透明度淡入，输出后实看交叠首中末帧。

## 3. 轻量独立 PNG 代理预览

高分辨率 PNG 序列粗剪或频繁试看时，用 `scripts/prepare_proxy.py` 生成 25%、50% 或 75% 同画幅低清代理：

1. 写源清单 JSON：`{"clip_id": "<实际 IMAGE clip id>", "source_files": [<有序 FILE 描述符列表>]}`，确保全部 PNG 尺寸一致且宽高能被所选百分比整除。
2. 指向新建目录运行：
   ```powershell
   & $py -B './.agents/skills\blender-video-editing\scripts\prepare_proxy.py' `
     --source-manifest $proxySrcJson --ffmpeg $ffmpegDesc `
     --output-dir $proxyOutDir --percent 50 --timeout-seconds 60
   ```
3. 核对 `proxy-manifest.json` 的状态与全部 SHA，把其中的 `preview_entry` 填进 `output.preview.proxies`，并把原片与代理全部并入 `resources`。
4. 代理适合加速预览，最终定稿输出时省略 `preview` 字段，另起新作业用原片渲染成品。

## 4. 音轨组织、混音与削波检查

- **分轨与基础参数**：把对白、BGM、音效和原音放在不同轨道，使用 48kHz 立体声连续解码与整数采样窗口混合（单声道按 1:1 复制到双声道），支持线性淡入淡出、轨道静音与静态 `Scene.audio_volume`。音频保持原速与居中声像，复杂降噪或人声修复在专业音频工具完成后再导入。
- **显式声明音视频工具**：只要本次导出包含任意未静音 `SOUND` 条带（无论是 MP4 还是 PNG 序列加音轨），都在 `output` 里显式提供带 SHA 的 `ffmpeg` 与 `ffprobe`，并列入 `resources`。每个导出范围会缓存完整音源，导出整片加多段逐镜前先按源时长估算磁盘余量。
- **双维度验收声音**：主增益之后执行自动削波检查，出现超界样本立即回调音量并重跑；导出完成后核对 PCM 采样数、声画同步落点与峰值，并实际播放听检对白清晰度和音乐过渡，待听检项如实记为 `not_run`。

## 5. SRT 转可编辑中文字幕与字体容量

用 `scripts/srt_to_clips.py` 把纯文本 SRT 转成按有理帧率量化的 TEXT 条带 JSON（只产出 JSON，不直接改 `.blend`，目标文件新建）：

```powershell
& $py -B './.agents/skills\blender-video-editing\scripts\srt_to_clips.py' `
  --root '<PROJECT_ROOT>\runtime\video-editing\0.1.5' `
  --srt $srtPath --output $clipsJson `
  --font 'C:\Windows\Fonts\msyh.ttc' --fps '24/1' --timeline-start 1 --channel 10 --font-size 24
```

- **合并进时间线**：把生成的 `clips` 并入 `create` 或包进 `patch` 的 `add` 操作，把字体的文件描述符并入 `resources`，重叠字幕会自动分轨摆放。
- **控制打包字体数量**：Blender 全局字体槽位有限（64 槽），同次 `apply` 中相同字体共享一份打包字体。避免反复零散追加几十次独立字体；一旦独立读取日志报 `Too many fonts!!!`，精简或合并字体后再导出。
- **实看烧录与外挂**：重剪后同步更新字幕帧区间，分别检查烧录到画面里的每条字幕首/中/末（核对字形、缺字、换行与安全区），以及以各自输出 0 秒起算的外挂 SRT，再按正常倍速播放确认阅读速度。

## 6. 多画幅、整片/逐镜导出与色彩规格

- **同画幅缩小与多画幅重构图**：同画幅小尺寸审片保持原画布，只用 1–100 的整数 `resolution_percentage` 缩小（如 `1920×1080` 按 `50%` 输出 `960×540`），`output.width/height` 填精确缩小后的尺寸。做竖版或更大分辨率时，新建独立时间线版本，重设画布、`fit` 裁切、主体位置和字幕安全区，再实看画面。
- **整片与逐镜输出**：用 `frames` 指定主时间线的左闭右开子区间；打开 `shots: true` 可按 `shot_id` 额外导出逐镜视频（可用 `shot_ids` 精确挑选）。同一 `shot_id` 保持连续或重叠区间；镜头表默认提取有效 `IMAGE`/`MOVIE` 条带（无此两类时才取 `COLOR`），导出给用户的镜头表按 `start` 排序并排除 `TEXT`/`SOUND`。`max_pixel_frames` 按「宽 × 高 × 总帧数」覆盖整片与所有逐镜。
- **SDR 与受控 HDR10/PQ**：常规制作默认使用 SDR 输出。确需技术级 HDR10/PQ 输出时，选用 MP4 容器，显式提供带 SHA 的 `ffmpeg`、`ffprobe` 与匹配当前配置的 `ocio` 闭包，并按 `STANDARD_100`、`reference_white_nits: 100`、`exposure: 0`、`VIRTUAL_BT2020_10000NIT` 与 `REQUIRE_OPAQUE` 配置 `hdr` 策略。做大分辨率或长片导出前，先按实际帧数、临时缓存与音频缓存算清磁盘和内存余量，保持本机负载可控；技术解码指标、视觉抽检与目标设备观看分开记录。
