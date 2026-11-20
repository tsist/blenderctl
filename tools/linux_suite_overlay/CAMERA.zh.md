# Camera Workflow · Linux 动作版

在云端 Blender 5.3 的独立配置内，打开已保存的本地工作副本。3D View 侧栏 **Camera Live** 有两个入口：

- **CHECK pose, optics and focus**：静态机位、转向、焦距、正交尺度、景深。
- **CHECK advanced camera operation**：运镜、切镜、改时、骨骼对焦，每次一个操作。在界面填写新候选目录，指定是自有工作副本。在 CHECK 通过后再选 **APPLY to new verified copy**。APPLY 产物是 `candidate.blend` 与 `receipt.json`，原件不写入。下一个操作选上一个候选并重新 CHECK。

工作副本应存于本人权限为 0700 的目录，关闭压缩后保存，不用外链资源、脚本或驱动。当前活动相机须是该次所需相机，工作帧预先停在不会被操作改掉的边界。

| 想做什么 | JSON 中的类型 | 示例条件 |
|---|---|---|
| 建立相机运镜 | `motion` | 写明位置、XYZ 弧度和帧，2–32 个关键帧，支持线性、恒定和三个缓动方式。已有动作不覆盖。 |
| 既有运镜改缓动 | `motion_ease` | 已有独占三条 XYZ 位移曲线，每条恰是两端点；保留端点与外侧手柄。 |
| 建立或移动切点 | `cut / cuts`、`cut / move_cut` | 建 Blender 原生相机标记；移动切点时写左右相邻标记名与左镜末帧，右镜从下一帧开始。 |
| 整组改时 | `cut / retime` | 点名相机和首尾帧的源/目标区间。只移动这些相机的标记和完整的变换动作；其余标记不动。用闭区间关键帧，不是 Windows 镜头表半开区间。 |
| 调整既有移焦起点 | `focus_timing` | 指定唯一指向本机位的 EMPTY 对焦物、三个固定帧和新起始帧，保留到达帧和外侧平台。 |
| 对焦跟随骨骼 | `bone` | 写明已有骨架对象、确切的骨名和光圈；通过 Blender Camera DOF 原生字段绑定，保留骨架动画。 |

高级面板填的是表中的单个操作，例如
`{"kind":"bone","focus_object":"FocusRig","focus_subtarget":"Eye.L","fstop":2.8}`。
命令行则把它放在 `sequence`，外层加 `scene` 和 `camera`：

```json
{"scene":"Scene","camera":"CamA","sequence":{"kind":"bone","focus_object":"FocusRig","focus_subtarget":"Eye.L","fstop":2.8}}
```

非渲染版的结果从实际 Blender 属性、时间线和求值坐标核对，不输出图像。Linux 的旧表可移动已存在的相邻切点，保留相机、其余镜头行和原件。Camera Live 的 SOLID 视口现在能对同场景两个本地相机做同帧 A/B 和还原；不支持跨文件候选实时切换。对旧版表使用 `blenderctl camera-shots --source <绝对路径工作副本> --frame <帧号>`，可比对旧表与 Blender 原生标记选中的相机。带旧表行的工程只按明确的 `legacy_cut` 路线另存；其他操作会拒绝带有旧表的文件。复杂绑定/合成场景按各自工具交接。

### 不安装也能跑的独立入口

`bin/camera-checked` 可从本仓库直接调用，独立于通用 scene/model base workers。需要官方 Linux x64 Blender 5.3.0（构建 02f0899d78e3）、系统 Python 3.13、bubblewrap、libseccomp，以及 Camera Workflow 提交 `89bb84cd9d422d0a8e3b3c04c4eb2294b63324a7` 的 `addons/camera_windows_static` 源码目录。无需在 Blender 启用插件。使用自己信任且调用期间不变的检出目录；入口核对 CLI 与插件清单 SHA，原事务再检查所有 Python 文件、可执行文件、源工作副本和候选，不会跳过沙箱。

以下变量均为无符号链接的绝对路径；D 是自己所有的 0700 工作目录，source.blend 是已保存的无压缩静态工作副本。以默认 Scene/Camera 为例：

```sh
printf '%s\n' '{"scene":"Scene","camera":"Camera","lens":63}' > "$D/request.json"
"$CLI" "$RUNTIME" "$ADDON" check --source "$D/source.blend" --request "$D/request.json" \
  --out "$D/candidate" --plan "$D/plan.json" --authorize-owned-work-copy
# 阅读 CHECK 输出和计划。确认后将输出的 plan_sha256 传给 APPLY：
"$CLI" "$RUNTIME" "$ADDON" apply --plan "$D/plan.json" --plan-sha256 "$REVIEWED_PLAN_SHA256"
```

本轮运行验证：静态镜头 50→63 mm，候选冷启动回读、输入未变、错误计划 SHA 被拒绝。不据此声称走通所有高级动画、镜头表、GUI 或通用 base worker。
