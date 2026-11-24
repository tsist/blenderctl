# 知识检索、沉淀与开源复用

遇到没把握的工艺先查现成经验；做完活攒下可复用的做法再收回库里。为当前交付做的研究控制在一轮之内，拿出一份复现小样、对照图或明确的路线选择就往下推进。

## 先查本地两座库

成熟技能里已经写明的步骤直接照做。遇到陌生领域、版本接口变动，或者某种做法试下来效果不对时，按顺序查两处：

1. **教程深读库（`<PROJECT_ROOT>/tutorial-library\`）**：先读顶层 `_INDEX.md` 和对应子库索引，看原作者在同版本 Blender 里怎么布线、排修改器栈、调参数。
2. **共享 Wiki（`host-memory-store`）**：调用 `wiki_query`（`mode="hybrid"`，取 3–5 条）搜领域关键词；搜不到换近义词调 `wiki_search`。准备采用某篇条目时，用 `wiki_get` 读全文，看清它的适用版本和验证范围。

两处都缺现成答案时，再去查当前版本 Blender 官方手册、RNA 定义、原作者公开案例或用 `blender-tutorial-scout` 搜中文教程，始终读一手原文。

## 借鉴开源项目

采用阶梯走到第四级、需要参考外部开源实现时：

1. 挑维护活跃、许可证清楚、适配 Windows Blender 5.3.0 的仓库，直接读核心源码、Release 说明与相关 Issue。
2. 分清是**借思路自己写**、**装成运行依赖**还是**摘录改编代码**。引入依赖或改编代码前，核对它的 LICENSE（如 GPL-3.0、MIT、Apache-2.0）与本项目兼容，保留原作者署名、许可证文本与修改说明。
3. 先在本机做个最小样例跑通，确认真能解决手头问题再往大流程里接，并在 `sources.md` 记下仓库地址、commit 或版本号。

## 把新经验存回合适的地方

各归各位，好找好维护：

- **教程拆解笔记**放进 `<PROJECT_ROOT>/tutorial-library/` 对应专题目录（仅将纯合规一手权威与原创实证总结进共享 Wiki）。
- **跨项目通用的工艺、流程与排障方法**写进共享 Wiki（分 `concepts`、`procedures`、`references`、`decisions`、`snippets`）：先搜同主题旧条目补充更新，确实是新题目再调 `wiki_new` 建档；写清适用条件、操作步骤、验证结果与来源路径，写完串行跑一次 `wiki_sync_links`、`wiki_index`、`wiki_build(force=false)`。
- **用户的长期偏好与关键架构决断**用 `memory_store` 存一两句精炼结论。
- **当次任务的执行进度**记在 Working Memory（`memory_wm_*`），原始渲染大图和 JSON 回执留在 `work/audit/<batch>//`。
