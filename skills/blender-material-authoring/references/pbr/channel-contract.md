# PBR通道职责与着色契约

制作来源遵循[硬规则](framework.md#1-制作来源硬规则)。下表是语义和验证要求；需要制作的空间图均先原生生图，常量/不适用项明确记录。

| 通道 | Blender读取与连接 | 生图及验收重点 |
|---|---|---|
| Diffuse / BaseColor / Albedo | 通常sRGB→Base Color；线性源明确声明 | 无方向灯光、阴影、高光/AO烘入；金属区域BaseColor表达导体色，不把diffusion误当另一物理通道 |
| Roughness | Non-Color→Roughness | 独立粗糙分布；黑更光滑、白更粗糙依目标模型确认；不照抄彩色明暗，不把Gloss直接接入 |
| Metallic | Non-Color→Metallic，或材料明确常量 | 金属基底与非金属锈蚀/涂层分区正确；导体颜色和粗糙度配合验证，灰值说明混合/覆盖意义 |
| Normal | Non-Color→Normal Map→BSDF Normal | 直接生图；明确切线/物体空间、UV、+Y/-Y、面朝向；检测零/异常向量、负Z或越界，解码后检查方向和长度及真实渲染 |
| Height / Displacement源 | Non-Color→Bump或Displacement | 独立生成标量；记录中性点、编码、实际位深、米/毫米映射与强度；PNG8不能冒充高精度高度；剪影变化另查几何/细分 |
| Alpha / Opacity | Non-Color标量或真实RGBA alpha→透明覆盖 | 白/1不透明、黑/0透明的目标约定；真实透明不含棋盘格；检查剪切/混合、阴影、预乘与边缘光晕；不是玻璃Transmission |
| Complex Mask | Non-Color→指定图层权重 | 生图负责区域与复杂边界，检查位置/尺度/通道配准；简单渐变/数学域例外按框架记录 |
| AO | Non-Color→目标指定用途 | 独立生图艺术候选；不能宣称由几何校准，不能默认乘入BaseColor并重复压暗 |
| Emission / Transmission / Specular / Coat等 | 彩色数据按编码，标量Non-Color；明确目标插槽 | 按本材料需要分别生成；保持各自语义，不拿Opacity替代透射、不把AO当粗糙度 |

## 通道清单的必填信息

每图记录role、source_tool、prompt/input/output路径及SHA、原始尺寸、编码/位深、读取色彩空间、目标插槽、UV/物理覆盖、范围/单位、采样与边界、适用区域、生成/常量/数学遮罩类别、验收与来源用途。派生或通道打包记录源链和目标引擎约定；不默认所有项目都是ORM。贴图编码、工作空间与显示变换分别记录。

上述字段属于制作意图与资源清单，不能原样假设为CLI请求字段。执行前用material.describe的当前Schema，将图层、资源SHA、UV和目标映射到真实bindings/assignment/layer接口；未支持的通道或着色路径保留意图并明确缺口，不伪造已消费。详细工作顺序见[插件与CLI材质操作章](plugin-cli-authoring.md)。

Material Workflow 0.7.0当前Normal消费约定为OpenGL +Y、TANGENT，并绑定实际UV；源图若是-Y或物体空间不能只改清单标签冒充转换。启用Coat时共享最终Normal/Bump，尚无独立Coat Normal路径；底层与釉层独立起伏需求须明确此限制。

纹理连接会覆盖同插槽常量：已有Roughness图时，改values.roughness不能改变最终粗糙分布；需要调节用当前run/batch支持的adjustments.roughness_scale/bias。study不支持这两项，不能反复调整无效常量来寻找差异。Metallic贴图同样需检查对常量的覆盖。study限定指定assignment_id/layer_id及已支持的七项数值，不等于任意通道生成或图层重构。

## 节点与图层组织

坐标/UV域 → 生成图像采样 → 生图遮罩与合法数学域 → 各通道独立混合 → 数据解码/物性参数 → 当前版本BSDF/材质输出。

Normal按正确向量语义解码/混合/归一化；普通色彩混合不能证明方向正确。接缝或图层混合的法线要在目标切线基实测。原图保持可追溯，任何Shader保护公开参数及其必要性；赤陶Blue floor0.70仅是该异常生成图的保护，不是通用Normal标准或修好了原PNG的声明。

Height默认是否启用、幅度和实际位移路径须明确；与Normal同一结构不重复满强叠加。IOR、Metallic、Coat、Alpha等可采用有材料依据的常量，不能从赤陶照搬到釉瓷/金属。实例输入、材质槽和共享范围保存后读回。

源图与生产图、数据图与有损预览分别保存。生成布局一致不能证明逐像素几何导数一致；需要测量级物性而候选无法达到时记录缺口并解决交付要求，不能虚报校准。

语义检查是采样风险提示，可定位异常向量、通道意图和无效参数，不能证明物性校准、通道配准或审美通过。警示、实际节点读回与目标引擎画面分别保留；技术通过不覆盖未验的冰裂凹陷方向或跨引擎切线一致性。
