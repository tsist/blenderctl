# 当前有限资格与已知失败

发行0.1.3，CLI0.63.0，插件0.1.1，模板0.1.0；制作验证基线Blender5.2.1 LTS/build9e2066aef7ef。以下总结发行前有限证据，不提供跨版本、所有素材或所有参数组合保证。公共发行的可移植适配和包验证以该发行报告为准。运行核心身份为`4a6e44b18e1187baf1569e0f93f3eda4ae2ffa170a3d73b8c7083c0ca8f38bcb`。

## 视觉时间与素材

IMAGE/MOVIE源帧定义为source_in+floor((timeline_frame-start)*speed)，source_in零基；freeze保持source_in。完整源放入META，内部SPEED FRAME_NUMBER按源索引以CONSTANT映射，外层保留目标半开范围。受控IMAGE半速/非零入点1.5倍、MOVIE2倍/非零入点半速/停帧有39个源身份样本及乱序重开。源范围校验和每实际输出帧缓存失效不可省略。它们不证明VFR、音频变速、全部倍率或长片性能。

RetimingKey.timeline_frame setter两次访问冲突已停用；早期SPEED MULTIPLY半速/非零入点错帧，不回退该路线。声音速度保持原速，属性动画不随源帧映射自动重定时。

原生异fps/VFR Movie有错时反例。当前MOVIE guard检查完整PTS/duration/末端、同有理fps的CFR、零起点、方像素、稳定尺寸和内容计数。显式prepare_media的六组CFR24/CFR30/VFR到24/30派生映射与Blender集成通过，PCM最大1LSB；有限正/负起点音频补裁有样本。源视频缺PTS仍拒绝。该helper是SDR_SRGB_ASSERT/8-bit RGBA，不保证位深/色度保真或所有容器时钟。

## 修整、版本与人工接续

FIT/CROP/ORIGINAL可用，CROP对应原生FILL。crop以输入像素计，grade multiply/saturation默认1、范围0..20。有限原生/公共短样验证crop/grade保存重开、RGBA对照、手改crop/custom note保护和互不重叠更新；不推导任意动态控制或艺术色彩质量。

META时序patch后内部手改仍应被remove/source/fit冲突检查识别，普通时序后正常remove可行。非目标Action和关键帧保护有负例。允许继续编辑本工作流创建后手改工程，未知内容保留；任意外来工程不支持接续。

输出缩小保留canvas，用整数百分比。不同画幅/更大尺寸建立新timeline并重构图。受控横竖码流尺寸检查不等于每个真实作品构图验收。有限线性EXR/BOX二倍缩小对照有面积均值证据；AUTO不等于面积平均，不推广其它比例/变换。

## 声音与字幕

0.1.3保留0.1.2的连续原点FFmpeg解码/整数样本窗口修复。48k mono/stereo，mono unity复制；有限源入点、LINEAR淡变、轨静音和Scene.audio_volume进入QA。削波在master gain后拒绝；use_audio只控制播放。非零pan、音频变速、嵌套SOUND、驱动/NLA/未资格化音量曲线拒绝。旧aud浮点分块和sound.mixdown崩溃/空WAV路线停用。

0.1.3同次apply共享新私有packedfont，独立reader字体容量错误明确失败，修复了后段字幕静默缺显。累积旧多font/多次patch仍受Blender容量约束。五分钟修订版57条字幕及完整音频/PTS复验通过，修订交付获批准，但没有新增完整听感评价。任意字体、长文本和背景仍需实际检查。

SRT helper纯文本→TEXT JSON，floor-start/ceil-end、重叠分轨与描述符；只编译不改工程。输出SRT按每份输出从0秒计，与烧录分别验。模板SRT还包含标题与镜头标签。

## 候选、逐镜、代理与恢复

受控两镜成组候选、反序、回退有RGBA对照和源身份保护，未知候选拒绝。逐镜导出来自当前组合时间线，shot_id连续或重叠范围；不连续拒绝，TEXT/SOUND不当镜头。生成媒体SHA会受元数据影响，不能替代像素比较。

PNG代理实际读取有有限40帧/五案例身份、构图、Alpha、原片前后和原音保护；公共run、取消后新job恢复有样本。代理低分辨率RGB允许采样差，不承诺原片品质。范围仅同画幅更小PNG IMAGE、有限变换、静态crop和所测动画；CONSTANT crop动画代码路径未单独验收。MOVIE/VFR/HDR直接代理、任意动态控制、跨版本和GUI读取不支持或未验。

公共取消恢复保持未变候选、原保存前期望，完整重做输出；不承诺中间帧续算、任意损坏或断电恢复。硬取消后的遗留报告running不代表权威job仍运行。可见GUI观察与后台自动原生编辑分别记录，人工不承担GUI验证。

## HDR10/PQ与高负载限制

有限技术路径仅STANDARD_100/EV0，显式SHA-bound当前OCIO、REQUIRE_OPAQUE、虚拟BT2020/D65/10000nit mastering；虚拟mastering不是显示器测量。Main10/PQ、元数据、完整解码与有限帧身份有受控样本。HDR设备观看不在支持范围。

早期默认空间16帧样本原raw EXR绝对1e-4门槛失败，最大约6.73294e-4；找到颜色根因或其它技术项passed不抹掉失败。明确线性工作空间的新样本是独立路线，不能改签旧证据或宣称普遍零误差。媒体交接中的理想sRGB公式1e-6失败另见[media-handoff.md](media-handoff.md)。

短4K SDR与1080 HDR代表实片有技术证据，SDR预览获认可。高负载4K HDR扩展造成系统失去响应并重启，中断未通过，当前不继续或恢复。研究候选没有替换发行核心；禁止以研究模式或空闲磁盘增加推导可靠性。长时HDR需要计算全部spool、参考、尝试和QA；磁盘预算不能保证内存/桌面响应。

## 规模与作品分开

300镜结构/成组替换不证明300镜真实片性能。既有30分钟1080p SDR技术回放为12秒动态配方循环：300逻辑镜、43200帧完整PNG/fresh MP4身份与PTS、86400000双声道PCM差0LSB、602个AAC声道cue偏移/首末漂移0。它只能证明该受控组合，不证明复杂剧情、严格全图颜色、人的完整听感或4K HDR长片。

12秒基础案例、15.5秒真实白膜→AI回流剪辑和8秒原创CG的SDR案例获得用户认可；五分钟修订交付获批准。复杂演出及真实长片作品仅为未来目标，各作品分别验收。模板有限资格见[templates.md](templates.md)，正式交付判断见[acceptance.md](acceptance.md)。
