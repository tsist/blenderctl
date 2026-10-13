# 通用剪辑模板库 0.1.0

模板是无代码 JSON 配方，将复用样式与本次工程、镜头、候选、字体、字幕和声音分开。编译器产生普通 video.run 请求与回执，不启动 Blender，不自动提交或并发渲染。执行沿用发行0.1.3公共 CLI，核心为0.1.1/CLI0.63.0。

仓库入口由 `BLENDERCTL_ROOT` 定位，不依赖技能安装位置：

```powershell
$videoRoot = $env:BLENDERCTL_ROOT
$videoPython = $env:BLENDERCTL_PYTHON
$templateCli = Join-Path $videoRoot 'templates/video/cli.py'
& $videoPython -B $templateCli list
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $videoPython -B $templateCli describe --template subtitle-readable
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $videoPython -B $templateCli schema --kind input
exit $LASTEXITCODE
```

解释器需Python3.12+；未设环境变量时指定实际可用解释器。完整输入合同以 cli.py schema 为准。

## 选择样式

| 模板 ID | 用途 | 样式 |
| --- | --- | --- |
| whitebox-board | 白膜分镜审阅 | 标题、暖黄色镜头编号、中文字幕 |
| ai-final | AI 回流成片 | 暖白标题、标准字幕 |
| cg-clean | CG 动画展示 | 中性背景、简洁标题字幕 |
| subtitle-readable | 中文字幕阅读优先 | 更大字号、上移字幕 |

style 字段包括背景RGBA、镜头标签开关/前缀、fit、transition_frames、multiply/saturation与文字font_ratio/y_ratio/color。字号按本次画幅高度整数换算；纵向偏移以画面中心为基准，负值向下。首版只支持 SEQUENCE、SDR、硬切（transition_frames=0）、原速音轨。非零转场和HDR明确拒绝，不改变核心已有有限叠化/PQ能力。

所有 TEXT 显式绑定同一字体。长行、亮背景、竖版和阅读时长实际检查；样式不自动断行、不自动决定节奏。源镜头保留 FIT 原比例也不保证构图符合新画幅意图。

## 输入绑定与编译

context 包含源 `.blend` 的绝对 file/SHA、完整 resources及可选media_probe；parameters 指定新 scene、workflow_id、fps、width/height、frame_start、顺序shots，以及可选font/title/subtitles/audio_tracks/output。

视觉类型为 COLOR/IMAGE/MOVIE，每镜稳定id及duration帧数。COLOR用color；IMAGE有序files，主候选与各备选序列分别同目录；MOVIE单文件并满足公共CFR/PTS、fps及解码合同。candidate/alternatives按真实Schema绑定，资源须完整。

字幕为 `{id,offset,duration,text}`，offset 相对frame_start；音轨offset/duration/fade_in/fade_out也以帧计。现成SRT先用技能helper转换，再显式变为模板字段；模板不直接读取SRT。输出SRT包含全部生成TEXT，包括标题与镜头标签，不能称作只含对白。

仓库 examples/example-input.json 只是形状示例，占位路径/全零SHA必须替换，不能直接执行。真实输入保存在项目的新文件中，输出目录尚不存在且父目录存在：

```powershell
$templateInputSha = (Get-FileHash -LiteralPath $templateInput -Algorithm SHA256).Hash.ToLowerInvariant()
& $videoPython -B $templateCli compile --template subtitle-readable --input $templateInput --expected-input-sha $templateInputSha --output-dir $newCompiledDirectory
exit $LASTEXITCODE
```

输出 request.json、recipe-used.json和最后写出的build-receipt.json。消费前核完整最终回执、请求/配方SHA，并确认本包及所属批次无 FAILED.json。文件存在不判成功；execution_status=not_run 表示未执行。编译检查声明结构、SHA和正式normalize；实际工程资源闭包、新Scene是否可用、解码和中文字形仍由公共Host/输出验收。

使用 [公共执行](execution.md) 中的独立jobs-dir、明确Blender、timeout和UTF-8请求。核JSON结果、权威终态、保存重开和实际媒体。

## 自定义保存与批量

复制内置模板到新JSON，修改id/name/description/style。`save-template --template-file <绝对路径> --expected-template-sha <SHA> --library-dir <已有绝对目录>` 校验后保存；目标存在拒绝覆盖。内置catalog身份固定，不直接改内置文件。后续按保存回执file/SHA，用compile的--template-file/--expected-template-sha，不将自定义id当内置成员。

`schema --kind batch` 描述1–16个唯一成员，每个明确template（内置{id}或自定义{file,expected_sha256}）和input描述符。`compile-batch --batch <绝对路径> --expected-batch-sha <SHA> --output-dir <新目录>` 先预检全部成员，再生成成员包及最终batch-receipt.json。全部成员回执、最终批回执和SHA完整且无FAILED才可消费。批量只编译，实际输出串行；上限16不证明16任务并发性能。

## 有限验证

模板原交付有35项纯正负检查，以及四内置、自定义、MOVIE、异画幅IMAGE FIT共7个320×180 SDR公共create/output/coldinspect案例。152主PNG和24MP4帧完整解码，受控移动块误差0、PCM0LSB、AAC cue lag0。字形检查限该批短文字，合成音调没有人工听检。发行可移植适配需另验实际发行包；旧记录不自动证明适配后可执行。

HLG、任意外来VSE、音频变速/非零声像、HDR设备观看不在支持范围；长时4K HDR不继续。复杂演出和真实长片是未来作品目标，各新作品按 [验收](acceptance.md) 判断。

所有输入描述符与输出目录使用绝对路径；编译输出和自定义模板目录须在 `BLENDERCTL_ROOT` 之外，运行根为受保护只读源码，不能把本次产出写回其中。
