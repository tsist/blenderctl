# 公共执行合同与人工接续

## 可移植运行入口

将环境变量 `BLENDERCTL_ROOT` 指向发行仓库根，`BLENDERCTL_PYTHON` 指向 Python 3.12+；Blender 二进制由实际安装位置明确给出。公共发行不提供 Blender、FFmpeg、字体或素材。仓库运行核心为 CLI 0.63.0、插件 0.1.1，发行版本 0.1.3；使用匹配版本，恢复旧作业时保留原运行身份。

```powershell
$videoRoot = $env:BLENDERCTL_ROOT
$videoPython = $env:BLENDERCTL_PYTHON
& (Join-Path $videoRoot 'blenderctl.ps1') video describe
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $videoRoot 'blenderctl.ps1') video describe --operation run --schema
exit $LASTEXITCODE
```

`BLENDERCTL_PYTHON` 未设置时根启动器可发现系统 Python；直接使用 Python 的示例须指定实际解释器。技能脚本位置以安装后的技能目录为准，不假定技能位于仓库。`describe --operation manifest|clip|output|inspect --schema` 按需要获取合同。发现不启动 Blender、不创建作业；doctor/capabilities 会创建作业，仍需独立 jobs-dir。

## 请求与身份

新工程使用 `video.run`、`manifest.action=create` 和尚不存在的新 Scene。patch 使用已匹配工作流所有权的 Scene。以下是不输出的参数形状示例；路径和 SHA 须换成真实值，空资源仅适用于已查明无外部依赖的源：

```json
{
  "schema_version": "1.0",
  "command": "video.run",
  "params": {
    "file": "ABSOLUTE_SOURCE_BLEND",
    "expected_sha256": "REAL_LOWERCASE_SHA256",
    "resources": [],
    "manifest": {
      "version": "1.0",
      "action": "create",
      "workflow_id": "cut01",
      "scene": "VideoCut",
      "timeline": {"fps": [24, 1], "width": 320, "height": 180, "frame_start": 1, "frame_end_exclusive": 9},
      "clips": [
        {"id": "shot001", "shot_id": "shot001", "kind": "COLOR", "channel": 1, "start": 1, "duration": 4, "color": [0.15, 0.3, 0.6, 1]},
        {"id": "shot002", "shot_id": "shot002", "kind": "COLOR", "channel": 1, "start": 5, "duration": 4, "color": [0.6, 0.2, 0.1, 1]}
      ]
    }
  }
}
```

例中帧 1–4 和 5–8 各贡献四帧，第九帧排除。FILE 描述符使用真实绝对 `file` 和 `expected_sha256`。SHA 现场计算，`Get-FileHash` 返回的 Hex 转为小写。源依赖需从实际场景与资源检查得出；未知不能伪装闭合。

请求保存为 UTF-8 JSON 后执行。`$videoBlender`、`$videoJobs` 和 `$videoRequest` 均为实际绝对路径；作业根保持短、独立，全局参数在 request 前：

```powershell
& (Join-Path $videoRoot 'blenderctl.ps1') --blender $videoBlender --jobs-dir $videoJobs --timeout 180 --compact request $videoRequest
exit $LASTEXITCODE
```

核退出码、JSON `ok/error`、最终 job 状态，以及报告实际列出的产物路径/SHA；不能由预设文件名推定成功。候选、源和 output 各有身份，不把旧候选路径当新输出。

## 人工往返与 patch

本工作流工程被人工修改后保存新快照，重核 SHA 与资源，用 `video.inspect` 读取状态，再限定修改范围。锁定条带与来源/目的轨道保护，未知条带保留。任意外来 VSE 不承诺接续；后台无法判断 GUI 未保存状态。

patch 保留 `version/workflow_id/scene`，`action=patch`，`edits` 顺序列有限 `add/update/remove/select_candidate`。update.changes 只放目标字段；替代候选显式 candidate_id/source。裁剪或改序影响声音/字幕时另改对应条带，不假定自动随动。

原工程、人工快照与 agent 候选各自保留。冲突指出条带/字段、当前与请求值，双方不覆盖。时序 patch 不应把内部手改吸收进可删除基线；remove、source/fit 重建需检查原生指纹、crop、custom property、混合模式与未建模动画。非目标、有效帧域、字幕与输出预算复查。

## 媒体派生、SRT 与代理 helper

技能 `scripts/prepare_media.py` 显式派生 SDR PNG+PCM。输入 JSON 恰为 `clip_id/source/fps/color_policy/audio`，fps 如 `[24,1]`，color_policy 为 `SDR_SRGB_ASSERT`，audio 为 `NONE` 或 `FIRST`。FFmpeg/FFprobe 参数指向含 FILE 描述符的 JSON 文件。先调用 helper `--help` 核当前参数。

```powershell
& $videoPython -B (Join-Path $videoSkill 'scripts/prepare_media.py') --source-manifest $mediaInput --ffmpeg $ffmpegDescriptor --ffprobe $ffprobeDescriptor --output-dir $newMediaDirectory --max-frames 100000 --timeout-seconds 60
exit $LASTEXITCODE
```

输出目录必须新建。核 media-manifest.json 的完成状态、输入/工具 SHA、完整 PTS/末端、逐帧映射、fps、样本与补裁。clip_templates 合入目标 manifest，另设目标 start/channel，resources 并入完整闭包。sample-and-hold 按 PTS 采样、ceil duration 到项目帧，记录复制/跳帧和尾差；不做光流或音频变速。音频要求连续 48k mono/stereo，mono unity 复制到双声道，起点按样本补零/裁剪。HDR/BT2020、旋转、非方像素、变化尺寸、未知末帧 duration、非48k或断裂音频拒绝。SDR_SRGB_ASSERT 是调用方的字节解释，不证明色度资格。

普通 MOVIE 不论是否输出，都需 SHA-bound `params.media_probe`，或 `output.ffprobe` 作为替代，并列入 resources。完整解码 PTS、duration、末端、Blender 内容计数须一致：同项目有理fps的 CFR、零起点、方像素、稳定尺寸与 stream=0。异fps/VFR 不用 source.speed 隐式转换。

`scripts/srt_to_clips.py` 只生成 TEXT JSON，不自动修改时间线。显式 `--root $videoRoot`、字体路径、fps、timeline-start、channel 和 font-size；调用 `--help` 后填实际参数。floor-start/ceil-end 量化保留原毫秒、映射、字体/SRT descriptor，重叠字幕分轨。只支持纯文本 SRT，不隐含 ASR、翻译或富文本。clips 合入 create 或逐条 patch add，resources 合入闭包；改剪后重算时轴并分别核烧录与 SRT。

`scripts/prepare_proxy.py` 输入恰为 `{"clip_id":"id","source_files":[FILE...]}`，FFmpeg 使用描述符 JSON。percent 仅25/50/75，同尺寸 PNG 宽高须可精确整除，输出目录新建。核 proxy-manifest.json 身份与成员，将 preview_entry 放到 `output.preview.proxies`，resources 合入闭包。

已验证代理只支持同画幅更小 PNG IMAGE。source_files 须精确匹配保存候选完整原序列及顺序/SHA，代理数一一对应。静态 crop 缩小后须为整数；有限变换/动画以当前 guard 为准，精确 CONSTANT crop 动画仍未单独验收。驱动、活动 NLA、未资格化曲线拒绝。原片定稿省略 preview。原生 StripProxy 设置保存不证明 GUI 读取；generated cache 无法枚举时拒绝。helper 在图像之间取消检查，单次 FFmpeg 有界超时，不承诺硬实时取消。

## 输出与存储

output 可省略以先交付可编辑工程。按当前 Schema 配置整片、逐镜、声音、SRT和指定版本。frames 是非空半开子区间；shots=true 可另导实际组合时间线的镜头，shot_ids 需同时显式启用 shots。默认使用 active IMAGE/MOVIE，仅无此类媒体才取 COLOR，TEXT/SOUND 不当镜头。同 shot_id 范围须连续/重叠，不连续则拒绝；逐镜保留该区间其它画面、字幕和声音，SRT 各自从0秒开始。

output 宽高只能为原 canvas 精确整数1–100%同画幅缩小，如1920×1080→960×540。1280×720不是该 canvas 的整数百分比；更大尺寸/另画幅做新 timeline 并重构图。max_pixel_frames 预算覆盖整片与所有逐镜输出，不静默截断。

含 SOUND 的 PNG/MP4 输出均需 SHA-bound ffmpeg/ffprobe 及资源声明。声音连续解码成48k float缓存，再用整数窗口；每个 range 可独立缓存整源，预算计入源时长×range数、图像、编码尝试和 QA，不按 MP4 大小估算。削波在 master gain 后检查，不静默夹限。Scene.audio_volume 生效，use_audio 只影响播放，未资格化主音量动画拒绝。

有限 HDR10_PQ 技术路径需要 MP4、明确 ffmpeg/ffprobe 和 hdr policy：SHA-bound OCIO、STANDARD_100、reference_white_nits=100、exposure=0、VIRTUAL_BT2020_10000NIT、REQUIRE_OPAQUE。实际 OCIO 配置及依赖完整绑定；policy/SHA/Alpha错误拒绝，不自动降为 SDR。该路径有限资格见 [qualification.md](qualification.md)；模板编译不接受 HDR，高负载 4K HDR 扩展不继续。

输出完成后实际完整解码，核帧/采样/PTS和视听。编码标签不代替解码。线性 EXR 输入的工作空间须显式决定；IMAGE AUTO 缩小不等于面积平均，有限 BOX 路径不能泛化全部比例/变换。

## 取消与恢复

可用 --async 提交后，一次有界 `--compact job wait <job_id> --wait-seconds 30`，允许0–55秒；不要循环 status 保持 agent 等待。仅取消本任务拥有的 job，并核权威终态和资源释放。

`resume={file,expected_sha256}` 指向旧完整 video-report.json，保持原请求、源/资源/实现/运行时和保存前期望身份。新 job 重新验证旧 checkpoint；改过源或候选则另建新请求，不改旧 SHA/回执。硬取消后旧 checkpoint/progress 可仍写 running，权威 job cancelled 优先；不能由遗留字段把取消任务当运行。既有恢复证据是新 job 完整重做输出，不承诺 partial 帧复用、断电或任意损坏恢复。

保留被依赖的媒体、候选、报告与上游 checkpoint。状态 JSON 被原子替换时可能发生短暂读取冲突，使用 CLI 已有共享 DELETE 读取与有界重试；持续拒绝、坏 JSON 和未知终态不能洗成通过。
