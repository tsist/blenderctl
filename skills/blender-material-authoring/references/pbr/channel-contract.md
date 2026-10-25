# PBR通道职责与着色契约

制作来源遵循[按需求路由](framework.md#1-按材质需求选择来源)。下表是语义和验证要求；constant/image/procedural/hybrid均可按适用性采用，常量/不适用项明确记录。

| 通道 | Blender读取与连接 | 制作及验收重点 |
|---|---|---|
| Diffuse / BaseColor / Albedo | 通常sRGB→Base Color；线性源明确声明 | 无方向灯光、阴影、高光/AO烘入；金属区域BaseColor表达导体色，不把diffusion误当另一物理通道 |
| Roughness | Non-Color→Roughness | 独立粗糙分布；黑更光滑、白更粗糙依目标模型确认；不照抄彩色明暗，不把Gloss直接接入 |
| Metallic | Non-Color→Metallic，或材料明确常量 | 金属基底与非金属锈蚀/涂层分区正确；导体颜色和粗糙度配合验证，灰值说明混合/覆盖意义 |
| Normal | Non-Color→Normal Map→BSDF Normal | 可靠切线数据采用真实Normal烘焙；图像候选另验，不默认校准。明确空间、UV、+Y/-Y、面朝向；解码检查方向/长度及实际渲染 |
| Height / Displacement源 | Non-Color→Bump或Displacement | 明确标量来源、中性点、符号、编码、实际位深、米/毫米映射与强度；程序化导出当前为signed float EXR；剪影变化另查几何/细分 |
| Alpha / Opacity | Non-Color标量或真实RGBA alpha→透明覆盖 | 白/1不透明、黑/0透明的目标约定；真实透明不含棋盘格；检查剪切/混合、阴影、预乘与边缘光晕；不是玻璃Transmission |
| Complex Mask | Non-Color→指定图层权重 | 图像、几何或程序信号按目标选择，记录位置/尺度与成因，不将满面噪声当合理旧化 |
| AO | Non-Color→目标指定用途 | 区分几何烘焙与图像艺术候选、局部/场景遮挡；不默认乘入BaseColor重复压暗；当前2.0 export不含ao键 |
| Emission / Transmission / Specular / Coat等 | 彩色数据按编码，标量Non-Color；明确目标插槽 | 按材料选择信号或常量；保持独立语义，不拿Opacity替代透射；IOR等超0..1数据明确编码/解码 |

## 通道清单的必填信息

每通道记录role、来源类别、目标插槽、UV/坐标/物理覆盖、范围/单位、采样/边界、适用区域与验收。图像记录工具、需要的prompt/input/output路径及SHA、尺寸/编码/位深/色彩空间；程序记录signal、参数与seed；常量记录值，需文件时确定性导出。派生或打包保留源链，不默认所有项目都是ORM。贴图编码、工作空间与显示变换分别记录。

上述字段属于制作意图与资源清单，不能原样假设为CLI请求字段。执行前用material.describe的当前Schema，将图层、资源SHA、UV和目标映射到真实bindings/assignment/layer接口；未支持的通道或着色路径保留意图并明确缺口，不伪造已消费。详细工作顺序见[插件与CLI材质操作章](plugin-cli-authoring.md)。

Material Workflow0.7.0静态图层Normal为OpenGL +Y、TANGENT，并绑定实际UV；源图若是-Y或物体空间不能只改标签冒充转换。程序化2.0由总Height生成Bump，导出真实切线Normal后重建不能再叠同Height。Coat共享最终Normal/Bump，独立Coat Normal需求另查接口。详见[程序化PBR](procedural-pbr.md)。

纹理/信号连接会覆盖同插槽常量。旧run/batch已有Roughness图时可按合同调adjustments.roughness_scale/bias，旧study不支持这两项；程序化2.0则修改实际signal/range或层权重。先读有效连接，不反复调整无效常量；旧study的七项数值不等于2.0任意信号探索。

## 节点与图层组织

坐标/UV域 → 图像采样或程序信号/常量 → 有依据的遮罩 → 各通道混合 → 数据解码/物性参数 → 当前版本BSDF输出 → 按需真实烘焙/仅贴图重建。

Normal按正确向量语义解码/混合/归一化；普通色彩混合不能证明方向正确。接缝或图层混合的法线要在目标切线基实测。原图保持可追溯，任何Shader保护公开参数及其必要性；赤陶Blue floor0.70仅是该异常生成图的保护，不是通用Normal标准或修好了原PNG的声明。

Height默认是否启用、幅度和实际位移路径须明确；与Normal同一结构不重复满强叠加。IOR、Metallic、Coat、Alpha等可采用有材料依据的常量，不能从赤陶照搬到釉瓷/金属。实例输入、材质槽和共享范围保存后读回。

源图与生产图、数据图与有损预览分别保存。生成布局一致不能证明逐像素几何导数一致；需要测量级物性而候选无法达到时记录缺口并解决交付要求，不能虚报校准。

语义检查是采样风险提示，可定位异常向量、通道意图和无效参数，不能证明物性校准、通道配准或审美通过。警示、实际节点读回与目标引擎画面分别保留；技术通过不覆盖未验的冰裂凹陷方向或跨引擎切线一致性。
