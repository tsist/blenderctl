# 用基础信号制作材质，而不是叠一层噪声

程序化或混合路线先读本页；接口与导出见[程序化PBR](procedural-pbr.md)。面向Blender 5.2.1、Material Workflow 0.8.0 / CLI0.64.0公开候选的15类信号接口；公开包复验进行中。原则来自官方节点文档、Painter的区域/遮罩分层及维护者木桌、旧漆桶、厚壁玻璃有限实做；数值是起点，不是任意模型的成品保证。

## 先解释表面

先完成[参考与质量门槛](reference-quality-gate.md)，从实际看过的参考提取应有的宏观组织、中尺度和微表面贡献，再用本页组合信号。模板只是结构起点；不能以增加Noise Detail、夸张Bump或无依据满面噪声替代细致度，也不以模板名自评通过。

把目标写成“基材 → 覆盖层 → 变化发生的位置 → 中尺度结构 → 微表面”。例如漆桶是钢、漆、下缘受潮/卷边摩擦、破碎锈斑、氧化颗粒；木材是统一木轴、年轮切面、长纤维、导管、涂饰。先看结构与尺度，再调色。特殊文字、标签或可辨手绘图案交给图像来源；不以随机噪声冒充。

| 基元或操作 | 用来控制什么 | 最容易混淆的地方 |
|---|---|---|
| Noise | Scale基频；Detail细节层数；Roughness幅度谱；Lacunarity频率比 | Noise Roughness不是材质Roughness；调大Detail不能补缺失的宏观组织 |
| Wave Bands / Rings | 主方向、周期、相位、带/环切片 | 波的传播方向与可见长纹方向有区别；等距高对比棕条不等于木纹 |
| Voronoi F1 / Distance to Edge | 孔隙/颗粒；胞元边界 | 胞元边界不是模型几何棱边；本接口返回VALUE，不返回随机Color |
| Coordinate / Mapping | 尺寸、方向、原点与重复频率 | OBJECT是局部坐标，不是Generated或米制；增大采样倍率会缩小花纹 |
| Warp | 在采样前扭曲坐标，使相关结构一起弯曲 | 是f(p+h(p))，不是把颜色加噪声；Fac转Vector只是三轴复制同值 |
| Remap / Ramp | 阈值、过渡、输出区间 / 颜色范围 | 原始Noise的0..1不能直接当所有通道的物理范围 |
| Multiply / Maximum / Mix | 区域相交 / 并集 / 插值 | 不可互换；Mix产生半混合，不一定适合掉漆边界 |
| Edge / Cavity / 位置朝向场 | 有依据的磨损/遮蔽/受潮区域 | 是近似和制作假设，不是自动识别材质历史 |

当前 target-only edge 来自目标上的 Bevel 着色法线与 Geometry 法线差异，不是跨物体接触检测。相邻部件接触处不会因此自动成为磨损区；所需接触区域应另有明确、经验证的来源。

Gabor与旧Musgrave迁移仅有研究依据，未获得本版受管接口/成品资格。需要它们时做独立最小验证，不伪造signal类型。

## 坐标先统一，再加细节

检查局部bounds、matrix_world、scene.unit_settings.scale_length、轴和UV。部件之间应保持相同物理纹理尺度；每个部件各自映成-1..1会让薄侧面纹理压密。模板只保留关系，换对象后重新选择采样倍率和区域阈值。

木材`make_recipe(..., grain_axis='X', timber_scale=(1,1,1))`将显式局部采样倍率先应用，再把指定木轴旋转到公共X方向。无剪切时，可由局部单位轴经世界变换后的长度、场景单位和期望采样单位计算三轴倍率；存在父变换时不能只用obj.scale。不要静默Apply变换。示例夹具局部bounds=-1..1且无父变换，采用obj.scale/0.4只是该夹具的米制换算。

同一块木料的顶、侧、端面共享体积场，不按UV岛换木轴。共享坐标先warp，然后各信号独立Mapping；局部原点偏移可以改变年轮切片位置。雕刻式大起伏、轮廓缺损和厚度是几何需求，Bump无法替代。

## 两个已实做的组合起点

**缎面暖木材**：Coordinate → 横向Warp → Rings与长纤维/导管 → 通道映射。

- Wave Rings X/SAW；独立warp使用Noise4D Color，scale1.6、detail2、roughness.52、sample_scale[.4,1,1]、strength[0,.18,.4]。慢沿轴变化保留长弧；过强/过快会出现虫纹、漫画线或折叠。
- Rings scale10、Mapping[.45,.9,.9]、偏移[0,0,.65]；Remap .76.. .91提取窄晚材，与Noise长纤维以.17权重组合。纹理Scale和Mapping Scale分别调节。
- 长纤维Noise scale4.5、Mapping[.45,8,8]；Voronoi F1的Mapping[.8,70,70]提取细长导管，反向Remap .08.. .20，再乘早材权重。导管颜色与高度分别控制。
- Roughness .32.. .43、少量coat；导管高度0..-.10、微纤维±.006、height_distance=.001（米制前提下导管约0.1mm）。不是把棕色亮暗直接接Bump。最终成品微孔并非所有镜头都独立可读，不为“看得见”夸张到石纹。
- 先保留纤维方向、合理色差和反射，再按需求加旧化。较宽/高对比晚材产生描线，极低warp又接近直条；按近景观察在两者之间选择，不以默认配方名认定合格。

**涂漆钢与局部锈蚀**：区域G × 分布D → 窄软阈值 → 漆/氧化层。

- 下缘height_band、边缘/凹处、已分件卷边分别提供区域；`metal_region='lower'|'edges'|'seam'`是调用者声明。局部Z不天然等于世界重力；任意模型先改区域参数。
- Noise3.2作宏观斑块，Noise19作碎边，.32混合后Remap .42.. .62；独立Noise115作微颗粒。先看G、D和最终mask，再看颜色。
- 示例掉漆和锈覆盖均将G×D重映射.25.. .28；宽过渡会泛灰，错开的两阈值可能形成蓝灰描边。确实需要露钢边时独立控制其窄度、区域和依据。
- 层顺序为裸钢metallic1 → 漆metallic0 → 氧化物metallic0 → 少量凹处灰。漆roughness .30.. .39，锈.68.. .90；锈色与高度独立取场。示例锈微起伏.04.. .40、height_distance=.001，不改变轮廓。
- 卷边暴露从连续重磨损收敛为.30；weathering主参数可关闭旧化。不能把每道凸筋整圈同样磨白当默认写实效果。

玻璃先满足真实厚壁、Alpha1、Transmission、IOR与roughness，再加克制微表面。清晰/着色/磨砂由光学属性区分；表面着色不等于体积吸收。Eevee的磨砂透射、底部和厚度近似与Cycles差异大，应分别记录。

## 从配方生成器到自己的材质

`make_recipe(family, context, target, material_id, seed=..., weathering=..., grain_axis=..., metal_region=..., timber_scale=...)`只生成新的2.0manifest，不寻找/修改模型。families见接口章。检查其signals，按稳定id修改实际需求涉及的参数，经过normalize_manifest/公共CLI验证再建图。保留主方向、宏观覆盖、边界破碎、微表面、颜色/roughness/height为可独立调整的职责，不靠不断更换seed碰运气。

每个信号必须有`id/type/parameters/inputs`；先用当前describe/Schema核对输入类型。`inputs.vector`替代源坐标，之后仍经过该信号Mapping；wood/noise/voronoi不是自动跳过Mapping。`S/id/coordinate/scale`与`S/id/scale`是独立控件。参数会规范化为float32；溢出、非零下溢、存储后坍缩的remap区间会被拒绝，不削弱校验。旧未发布候选存在控件重名时用新材质ID重建，保留旧图诊断。

## 观察决定下一次改动

对当前问题生成有信息量的小样：坐标/无warp对照；灰度宏观场、区域与最终mask；color-only、no-height与final。诊断可在明确的独立副本用Emission观察，不能修改受管作者节点后绕过校验。说明显示变换，PNG亮度不能直接当线性场数值。

当目标要求某磨损层参与时，核对遮罩是否实际连接到启用层，并检查区域场、混合场及最终遮罩的有效范围，识别全零、全一、未使用或被阈值压没的分支。零旧化对照的全零可属预期；不能凭信号存在或辅助图生成成功认定该分支有效。

固定机位、光照、曝光与seed，围绕一个主要假设改动；先看材料身份、结构方向、尺度、区域依据，再看接缝/薄面/端纹/玻璃内腔。正常中近景与局部图都要实际查看；没有view_image或等价观察能力时交接，不能凭日志写“已看图”。

作者效果成立后才做默认2K必要通道与磁盘重建。比对同一引擎、同一灯光的author/reconstructed，再检查换光与Eevee。微细纹理低于UV/像素采样能力时，降低无效高频或调整既有UV交付方案；不要仅以整图平均误差掩盖局部丢失。没有原图对照的“重建成功”不等于效果保持。

## 复用时的三个诊断点（2026-10-25实证）

- **有年轮节点却没有端纹**：先追踪Mix的A、B、factor和颜色映射。在本组木桌中，A是长纤维、B是晚材带，factor=.065只贡献6.5%晚材；保持坐标和UV，仅改为.17后端面弧纹可辨，顶面纵纹仍成立。这是已验案例值，不是通用推荐比例。先用端面正视和顶端同框对照，不能凭斜视整图认定缺年轮或乱改木轴。
- **钢层与锈层**：需要完整露钢时，先让掉漆mask内钢层完整替代漆，再由独立氧化覆盖场叠锈。把相同分布噪波反复相乘会不必要地削弱有效区域；连接存在和中间图非零都不证明最终层可见。以最终通道贡献检查覆盖，不把钢、锈和漆简单当三种颜色。
- **名字叫crest不等于凸脊**：`max(Normal.Z,0)`只选择朝上面。本组三道圆鼓筋的最终钢贡献集中在上肩约2.4mm，而水平向外冠部为0；这不等于整个磨损层失效。先核坐标空间、真实选区及镜头，再决定改区域还是改参数。阈值统计排除UV空白，UV面积比例与表面积比例分别说明；若需求是冠部接触磨损，换成有依据的冠部场，不能靠加gain让零方向值变非零。

2K重建会平滑极近景细孔、微颗粒和窄边界；保持正常中近景材料身份与局部结构不等于逐像素或无限放大保真。32–36微米量级的涂层Bump不能拿来承诺厚起皮；先按真实材料尺度判断，不为看得见而夸张层厚。

## 证据与来源

可选知识ID：`concepts/blender-procedural-signal-composition.md`、`procedures/blender-procedural-noise-layered-prop-workflow.md`、`references/blender-procedural-material-research-20261023.md`。这些ID仅供具备对应知识库的宿主导航，公开包不附私人Wiki、任务态或工件；没有该库时直接按本页和官方来源工作，不要求访问维护者环境。

[Noise](https://docs.blender.org/manual/en/5.2/render/shader_nodes/textures/noise.html)、[Wave](https://docs.blender.org/manual/en/5.2/render/shader_nodes/textures/wave.html)、[Voronoi](https://docs.blender.org/manual/en/5.2/render/shader_nodes/textures/voronoi.html)、[IQ域扭曲](https://iquilezles.org/articles/warp/)、[Decscroll木纹勘误](http://www.decscroll.com/?p=223)。旧教程的平方半径已勘误为径向距离再取相位；不直接移植旧节点插槽或Displacement连接。
