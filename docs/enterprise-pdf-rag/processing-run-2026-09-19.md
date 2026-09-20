# 前 20 页实际处理验收

本次仅处理 `aia-group-2026-interim-results-presentation.pdf` 的物理第 1–20 页，复用已保存的 71 页来源资产。源 SHA-256 为 `df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e`。

全部 241 个对象已有实际 typed IR 和独立描述或来源转录。数量表示真实产物已保存，不表示其中所有字段/图表关系都已验证。

| 类型 | 对象 / IR / 描述 | 资格范围 |
| --- | --- | --- |
| Text | 163 / 163 / 163 | 仅原文转录 |
| List | 11 / 11 / 11 | 仅原文转录；列表关系仍推断 |
| Group | 29 / 29 / 29 | 6 个仅转录；23 个容器含真实 children 和独立区域证据，分组 PENDING |
| Chart | 29 / 29 / 29 | 9 个仅非数字来源标签；数值关系资格 0 |
| Image | 7 / 7 / 7 | 来源绑定的模型推断，PENDING |
| Diagram | 2 / 2 / 2 | 来源绑定的节点/边推断，PENDING |

这 20 页没有确认的 Formula 对象。第 20 页敏感度区域按实际来源审阅作为 Group 保留；native typed-table 未检出可靠单元格的诊断仍保存，未用 `None` 猜出合并关系。

原始布局、规则修正布局、SVG、模型视图、原始模型响应、typed IR、独立描述和逐字段诊断可从 `data/output/aia-2026-interim/pages-001-020/review.html` 逐页进入。3 次来源摘要修正各使用显式独立请求，旧响应没有改写；修正结果与旧请求指纹有明确关联。模型置信度标签不被换算成虚构的精确概率。

检索只收具有明确资格的描述投影：180 个来源转录和 9 个图表文字标签。图表数值、期间、轴和 marks 在该检索投影中保持 unknown；完整原始 ChartIR 单独保留供审阅。金融问题 guard 拒绝仅转录和仅标签范围。

实际使用本地部署的 `Qwen/Qwen3-Embedding-4B` 保存了 189 个 2560 维向量，使用 `Qwen/Qwen3-Reranker-4B` 精排。首次运行暴露精排服务分数异常，原记录完整保留；修正服务后复用向量重新验收。固定查询 `Distribution Mix chart` 的 15 个召回候选中，第 18 页真正的 Chart 对象排第一（cosine 0.840668），精排仍为第一（0.970959，第二名 0.027243）。5 个结果均按同一 snapshot 回填，金融问答 guard 全部拒绝。该结果和独立正反例验证证明了本次链路可以运行，不是通用排序质量或数值金融问答的验收。

本次固定 processing ID 为 `89a3a92ca1c82354d034679d9a2c2deebe5016ef8b1194b4c1db044fee5f2d99`，retrieval snapshot 为 `fc2863ebe1e4bd6a7f0e7ff96c2dafab2d5806d8cf7f9784ba01c70eecd98f27`。在对应 run 的 `retrieval-evaluations/a90487e434d5ad4485304aaad69166f5fa3f0f73eed9ee9a0d3350f118f6f590/` 中，`retrieval-example.json` 保存实际回填，`retrieval-validation.json` 保存服务配置指纹、完整候选、两种分数、来源链接和 guard 结果，`evaluation.json` 绑定两份文件的内容摘要。HTML 审阅入口同时链接早期失败记录与新记录。评估仍标记 `ranking_status=not_qualified`，不把少量样例升为整体质量保证。

同 run 的 `retrieval-controls/9b4bec0100e5f4ed814a5f022b8037438cb7401fd96ccf720ebe3c1dfe99cf33/controls.json` 保存已通过的三组服务正反例/换序对照及 raw 与 adapter 一致性证据，摘要绑定文件内容，未包含服务凭证或私有主机配置。

本阶段具备本地不可变产物和原子 manifest 指针；不等于完整数值财务 QA 或生产级多存储发布已经完成。严格数值关系资格仍是后续工作，不以标签检索的成功替代。
