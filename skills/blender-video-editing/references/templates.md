# 通用剪辑模板库使用指南

制作常规 SDR 硬切短片（白膜审阅、AI 回流成片、CG 展示或中文阅读优先）时，用本技能自带的 `scripts/video_templates.py` 编译无代码 JSON 配方，直接生成标准 `video.run` 请求。编译阶段离线执行，随后把产物交给公共 `blenderctl.ps1` 跑工程生成与验收。

## 1. 挑选内置模板

| 模板 ID | 适用作品 | 默认视觉样式 |
| --- | --- | --- |
| `whitebox-board` | 白膜分镜审阅 | 标题栏、暖黄色镜头编号、中文字幕 |
| `ai-final` | AI 回流成片 | 暖白标题、标准中文字幕 |
| `cg-clean` | CG 动画展示 | 中性背景、简洁标题与字幕 |
| `subtitle-readable` | 中文阅读优先 | 加大字号、上移安全区字幕 |

模板管样式，项目输入管镜头、候选、字幕和音轨。长行文本、竖版画幅或复杂背景画面在导出后实看字形、换行与遮挡，手动按需微调。

先查可用模板与输入契约：

```powershell
$py  = '<PROJECT_ROOT>\runtime\blender\releases\5.3.0\blender-5.3.0-windows-x64\5.3\python\bin\python.exe'
$tpl = './.agents/skills\blender-video-editing\scripts\video_templates.py'

& $py -B $tpl list
& $py -B $tpl describe --template subtitle-readable
& $py -B $tpl schema --kind input
```

## 2. 准备输入并编译请求

模板输入 JSON 包含两大块：

- `context`：源 `.blend` 的绝对路径与小写 SHA256、完整外部 `resources` 闭包，以及按需附带的 `media_probe`。
- `parameters`：新 `scene` 名、`workflow_id`、有理 `fps`、`width`/`height`、`frame_start`、有序视觉 `shots`，以及可选的 `font`、`title`、`subtitles`、`audio_tracks` 和 `output`。

准备字段时注意四点：

1. 每个视觉镜头保留稳定 `id`，时长用帧数；支持 `COLOR`、`IMAGE`（主候选与备选序列各自放同一目录）与符合 CFR/PTS 要求的 `MOVIE`。
2. 字幕采用 `{id, offset, duration, text}` 帧数组，`offset` 相对 `frame_start` 计；已有 SRT 先用 `scripts/srt_to_clips.py` 量化后再转为模板字段。
3. 音轨的 `offset`、`duration`、`fade_in` 和 `fade_out` 同样用帧数表示，保持原速。
4. 模板导出的外挂 SRT 会包含全部生成的 `TEXT` 条带（含标题与镜头编号），对外交付纯对白 SRT 时先筛选对白条带。

指定一个新建的输出目录编译：

```powershell
$inJson = '<PROJECT_ROOT>\work\reel\template-input.json'
$inSha  = (Get-FileHash -LiteralPath $inJson -Algorithm SHA256).Hash.ToLowerInvariant()

& $py -B $tpl compile --template subtitle-readable `
  --input $inJson --expected-input-sha $inSha `
  --output-dir 'work/audit/<batch>/'
```

编译完成后，检查输出目录里同时具备 `request.json`、`recipe-used.json` 和最终 `build-receipt.json`，确认回执里的 SHA 一致且目录内无 `FAILED.json`，再把 `request.json` 交给公共 `blenderctl.ps1 --jobs-dir $jobs --compact request` 执行。

## 3. 保存自定义模板与批量编译

- **保存团队自定义样式**：复制一份内置模板 JSON 到新文件，改好 `id`、`name`、`description` 和样式参数，保存到自定义的模板目录（内置库保持原样）：
  ```powershell
  & $py -B $tpl save-template --template-file $customJson --expected-template-sha $customSha --library-dir $customDir
  ```
  后续编译时用返回结果里的 `saved.file` 和 `saved.expected_sha256` 传给 `--template-file` 与 `--expected-template-sha`。
- **批量编译多条请求**：先跑 `schema --kind batch` 查看批次格式（支持 1–16 个唯一成员，分别绑定内置 `{id}` 或自定义 `{file, expected_sha256}` 模板与各自输入），再执行：
  ```powershell
  & $py -B $tpl compile-batch --batch $batchJson --expected-batch-sha $batchSha --output-dir $batchOutDir
  ```
  核对最终 `batch-receipt.json`、每个成员回执及 SHA 全部完整且无 `FAILED.json` 后，按顺序串行提交给公共 CLI 执行导出，保持本机资源平稳。
