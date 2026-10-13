# 视频剪辑通用模板库 0.1.0

本库把可复用的剪辑样式与项目素材分开保存。模板是声明式 JSON，输入绑定本次工程、镜头、字体、字幕和声音；编译器生成普通 `video.run` 请求及回执，再交给配套发行版 0.1.3 的公共 CLI 执行。

模板不保存用户素材路径，不包含 Python、Blender 脚本或可执行表达式。`compile` 只生成请求；编译成功表示结构和绑定校验通过，作品仍须实际执行并检查。

## 首批模板

| ID | 用途 | 镜头编号 | 字幕字号 / 画幅高度 | 字幕纵向偏移 / 画幅高度 |
| --- | --- | --- | --- | --- |
| `whitebox-board` | 白膜分镜审阅，暖黄色镜头标签 | 显示 | 0.065 | -0.33 |
| `ai-final` | AI 回流成片，黑底和暖白标题 | 隐藏 | 0.060 | -0.35 |
| `cg-clean` | CG 动画简洁展示，中性背景和冷白标题 | 隐藏 | 0.055 | -0.36 |
| `subtitle-readable` | 中文字幕优先，增大字号并上移 | 隐藏 | 0.065 | -0.30 |

字号与位置按本次画幅高度换算。例如 1080 高画幅的字幕字号约为 70、65、59、70 像素，实际以编译器的整数换算为准。纵向偏移以画面中心为基准，负值向下。白膜模板的标题偏移为 0.26、镜头标签为 0.43，给同帧文字分开位置。所有模板都保留原画面比例、使用硬切，基础色彩参数为原值。`subtitle-readable` 的位置给底部留下更多余量；长行、竖版、亮背景和阅读时间仍须查看产出，样式参数不构成自动字幕排版保证。

## 查看模板与输入契约

从本库目录运行下列命令。`python` 应指向已有的 Python 运行环境。

```powershell
python -B cli.py list
python -B cli.py describe --template whitebox-board
python -B cli.py schema
python -B cli.py schema --kind input
```

模板的核心结构如下，全部样式字段必填：

```json
{
  "schema_version": "video-template/1.0",
  "id": "my-template",
  "name": "自定义中文名称",
  "description": "适用范围",
  "layout": "SEQUENCE",
  "style": {
    "background_color": [0.025, 0.025, 0.025, 1.0],
    "show_shot_labels": false,
    "label_prefix": "镜头",
    "fit": "FIT",
    "transition_frames": 0,
    "grade": {"multiply": 1.0, "saturation": 1.0},
    "title": {"font_ratio": 0.09, "y_ratio": 0.30, "color": [1.0, 1.0, 1.0, 1.0]},
    "label": {"font_ratio": 0.05, "y_ratio": 0.40, "color": [1.0, 0.8, 0.3, 1.0]},
    "subtitle": {"font_ratio": 0.06, "y_ratio": -0.35, "color": [1.0, 1.0, 1.0, 1.0]}
  }
}
```

输入由 `context` 与 `parameters` 两部分组成：

- `context` 绑定源 `.blend` 的绝对路径、SHA-256 和完整源依赖 `resources`。资源只有确认工程实际无依赖时才可留空；媒体探测工具可通过 `media_probe` 显式绑定。
- `parameters` 指定 `workflow_id`、新建 `scene`、帧率、分辨率、起始帧与顺序镜头。镜头保留稳定 `id`，可绑定候选及 `alternatives`。
- `COLOR` 镜头使用 `color`；`IMAGE` / `MOVIE` 镜头使用 `source.files` 中的 `{file, expected_sha256}`。视频为单文件；图像可声明有序序列。每段 `duration` 用帧计。
- 生成标题、镜头标签或字幕时，显式提供包含所需中文字形的 `font` 文件与 SHA-256。编译器检查绑定，文字显示还需检查成片。
- 字幕用 `{id, offset, duration, text}`，`offset` 相对于 `frame_start`；音轨用 `{id, offset, duration, source, volume, fade_in, fade_out}`，时长与淡入淡出均用帧计。
- 输出通过可选 `output` 绑定已有 SDR 输出契约。省略输出可先建立可编辑工程；导出另由公共 CLI 驱动。

首期顺序模板支持已知工作流创建的工程，并生成新场景。`transition_frames` 必须为 0；音轨保持原速。HLG、外来 VSE 工程接续、音频变速、非零声像、HDR 设备观看不在支持范围内，4K HDR 扩展未通过验证。

## 绑定本次项目并编译

`examples/example-input.json` 是**不可直接运行的绑定示例**：两段色块共 72 帧，源工程和中文字体使用 `REPLACE_WITH_PROJECT` 占位路径，全零 SHA 只是示意。复制为本次输入后，应替换真实路径、计算真实 SHA，并完整列出源工程依赖。示例不包含自动输出。

```powershell
$taskInput = 'F:\YOUR_PROJECT\template-input.json'
$taskInputSha = (Get-FileHash -LiteralPath $taskInput -Algorithm SHA256).Hash.ToLowerInvariant()
python -B cli.py compile --template subtitle-readable --input $taskInput --expected-input-sha $taskInputSha --output-dir 'F:\YOUR_PROJECT\compiled-new'
```

输出目录必须是尚不存在的独立目录。输出包括 `request.json`、`recipe-used.json` 和 `build-receipt.json`。回执记录输入、模板及请求身份；后续执行依照正式公共 CLI 的作业、检查与恢复契约。不要把编译回执当成媒体验收报告。

## 自定义样式与重复使用

复制一个模板到独立 JSON，修改 `id`、`name`、`description` 和所需样式后再校验保存。背景、文字颜色均为四分量 RGBA；字号用 `font_ratio`，纵向位置用 `y_ratio`。保持镜头、字幕与音轨内容在本次输入中，可让同一样式用于不同素材和横竖画幅。

用以下命令校验并保存自定义模板，再显式绑定保存副本编译。`--library-dir` 必须是已有的绝对目录，保存的目标文件必须不存在，重复 ID 会拒绝覆盖；模板文件有意修改后重新计算 SHA。

```powershell
$taskTemplate = 'F:\YOUR_PROJECT\my-template.json'
$taskTemplateSha = (Get-FileHash -LiteralPath $taskTemplate -Algorithm SHA256).Hash.ToLowerInvariant()
python -B cli.py save-template --template-file $taskTemplate --expected-template-sha $taskTemplateSha --library-dir 'F:\YOUR_PROJECT\custom-templates'

$taskSavedTemplate = 'F:\YOUR_PROJECT\custom-templates\my-template.json'
$taskSavedTemplateSha = (Get-FileHash -LiteralPath $taskSavedTemplate -Algorithm SHA256).Hash.ToLowerInvariant()
python -B cli.py compile --template-file $taskSavedTemplate --expected-template-sha $taskSavedTemplateSha --input $taskInput --expected-input-sha $taskInputSha --output-dir 'F:\YOUR_PROJECT\custom-compiled-new'
```

`my-template.json` 对应自定义 `id` 为 `my-template` 的保存结果；实际路径以保存回执为准。这里的 `YOUR_PROJECT` 也需要替换成本次项目绝对路径。内置 `catalog.json` 保存模板路径及 SHA，并由编译器固定其身份；修改内置文件会导致校验拒绝，应使用自定义副本。

## 显式批量编译

一个批次可声明 1 至 16 个成员，每个成员都有唯一 `id`，`template` 使用内置 `{id}` 或自定义文件 `{file, expected_sha256}`，`input` 使用本次输入文件的 `{file, expected_sha256}`。完整批次契约可用 `python -B cli.py schema --kind batch` 查看。所有成员先预检，通过后才在新的输出目录生成各成员请求。

```powershell
$taskBatch = 'F:\YOUR_PROJECT\batch.json'
$taskBatchSha = (Get-FileHash -LiteralPath $taskBatch -Algorithm SHA256).Hash.ToLowerInvariant()
python -B cli.py compile-batch --batch $taskBatch --expected-batch-sha $taskBatchSha --output-dir 'F:\YOUR_PROJECT\batch-compiled-new'
```

批量编译负责请求与回执，实际作业执行、失败恢复由公共 CLI 负责。本机作品验证按单作业串行安排；批次成员上限不表示同等规模并发渲染已通过。

## 验证边界

本库首批数据限定为单段顺序剪辑布局。素材和字幕的语义、故事节奏、候选质量由制作工作流判断；模板编译器负责结构、身份、范围和契约检查。实际验收应使用低分辨率短片串行执行，并通过独立重开检查工程及输出案例。发行包验证范围见仓库 docs/VALIDATION.md。


公开版运行根默认是本源码包的仓库根；单独解压模板包时，先设置 `BLENDERCTL_ROOT` 为匹配的 CLI 源码根。编译器仍核对固定运行身份，不接受仅版本号相同的其他快照。
