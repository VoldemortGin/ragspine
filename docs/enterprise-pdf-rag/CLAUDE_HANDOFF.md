# Claude 交接：通用文档 RAG 与公开样本验收

更新时间：2026-09-21（末次追加通道选择与查询翻译一节，ADR 0016）

> 阅读顺序：先看下方“恢复开发记录”；后续旧暂停快照保留作证据，不能作为实时发布或服务状态。

## 通道选择与查询翻译（2026-09-21，分支 `feat/bm25-only-short-queries`，[ADR 0016](adr/0016-query-classification-and-translation.md)）

> 依据是本机 `data/validation/coverage-2026-09-21/`（覆盖率与三通道召回度量，`data/*` 为 git 忽略，同此前各轮）。解掉「RRF 无条件融合」这个从未被测过的假设，并把 ADR 0013 已知缺陷 (b)（中文问英文 deck）在**检索侧**补上。

**做了什么**（最小改动，TDD）

1. **通道选择**（新 `answers/query_mode.py`，纯 stdlib、零模型、零 I/O）。`classify_query(question, *, lexical_hits)`：token ≤ 5，或含数字且内容词 ≤ 1 → `bm25_only`；词面通道零命中 → `vector_only`；其余 → `rrf`。只测「含数字」不测「含期间」—— `processing/periods.py` 认得的每种期间写法都带数字，测试钉住了这条。`answers/` 不许 import SDK，所以 `tokenize_query` 复述了词面通道的分词器，并有一条测试逐字段比对两者输出（token 预算若数的不是 BM25 真正打分的 token 就没有意义）。
2. **阈值来自离线扫参**。用 `facts.jsonl` 里每条事实、每种问法、三个通道的名次，把 (N, M) 在 0…12 × 0…12 全跑一遍。这个规则族的上限**就是纯 BM25**（125 条里 93 条命中 @10），没有任何 (N, M) 能超过它；在并列到顶的若干组里取**改写查询数最少**的一组 (5, 1)（250 条探针查询改写 119 条，48%）。理由写在 ADR：探针问的全是「短标签 + 期间」，更大的 N 等于把没有证据的部分外推到叙事型问题上，而向量通道正是为后者存在的。
3. **单通道 = 与空排名做一次融合**（`adapters/hybrid_search.py`）。`search(..., mode="auto")` 返回 `SearchOutcome(mode, hits)`；`bm25_only` 完全不调 `document.search`，**每个请求少一次 embedding 调用**；分值口径不变，因为所有 mode 都走同一个 `fuse()`（单边排名得分仍是 `1/(k+rank)`），未用通道的 rank/score 为 `None`。ADR 0012 的视觉对象保底席位逻辑一行没动。
4. **查询翻译**（新 `adapters/query_translation.py`）。触发条件不是「整句词面零命中」而是「**内容词**（去虚词、去数字后）零命中」—— 这是关键：`2026 上半年 分销渠道 占比` 里的 `2026` 本身就命中，按整句判定这个功能对它要针对的用例**永远不会触发**。一次有预算、可缓存的 `complete_text_json`（task 盐 `query-translation-v1`，strict `{english_query, source_language}`，规则禁止回答 / 禁止添加信息 / 数字与专有名词逐字保留）。**译文只进两个检索通道**；prompt、period / region 前置过滤、散文数字门用的都还是原问题，claim 仍逐字比对文档英文原文。`SYSTEM_RULES` 加第 6 条：用提问语言作答，但 claim 的 `text` 永远是证据原文逐字照抄。
5. **翻译不可用不报错**，退回向量单通道；`translate_query=False` 同理。检索计划只取决于问题本身、不取决于剩余预算，所以同一个问题永远命中同一批缓存条目（**曾经**加过「给合成调用留最后一次预算」的保护，因为它让同一问题第二次改走别的计划、打不中答案缓存，已撤回）。一次被翻译的问答 = **两次** live 调用，`llm_live_calls` 如实计数；重复提问两次都命中缓存。
6. **信封**：`AnswerRequest` 增 `fusion_mode` / `translate_query`，`AnswerResult` 与 `AnswerEnvelope` 增 `fusion_mode` / `query_translation`（均为可选、有默认），`rag-chat-v1.json` 已重生成（**只增 92 行、零删除**，语义 diff 确认只多了这两个字段与一个 `$defs`）。`fusion_mode` 未暴露到 `RagChatRequest`——与 ADR 0012 的 `top_k`/`channel_limit` 同一立场。

**本轮真实基线（只读，2026-09-21，8768，旧代码）**：跑 `nl_gold_eval.py` 只是为了留一份 **BEFORE** 基线 —— 线上跑的是别的 checkout 的旧代码，本轮新行为**没有**上线复测。结果 **20 pass / 0 fail / 2 known gap**（`k01` holds、`k02` moved）。两个注意事项：

- 线上 release 已经漂了，而且**运行中途被别的进程重启过一次**（`processing` 在跑之前是 `4f6ce62fe0b7`、跑完变成 `22127d0fad13`，member 数 190 → 210，正好等于选中页数，与 `feat/page-window` 的每页页级 member 吻合）。前 20 条全部 `cache_hit=true` / `llm_live_calls=0`，而完成缓存的指纹是对整个 request payload（含 prompt 与检索到的证据）做的摘要 —— 所以这 20 条的 prompt 与历史录制逐字节相同，检索结果与钉定版一致，基线可信。
- 唯一偏离的 `k02-region-thailand-zh` 是**跨 snapshot 比较**（重启后的第三个 snapshot），且是唯一 `llm_live_calls=1`（缓存未命中 = prompt 变了 = 证据变了）的一条，因此归因为 release 漂移而非行为变化。它仍然正确拒答、`claims` 为空、没有编造，只是拒答理由从 `model_declined` 变成了 `claim_not_in_evidence`（模型给的 `1168 $m` 与页面显示的 `1,168` 不符，被守卫拦下）。抗编造不变量未被破坏。

**离线估算（前 / 后）**：125 条已索引事实、两种问法取并集、reranker 之前 —— recall@10 **70.4% → 74.4%**，recall@3 **46.4% → 55.2%**，recall@5 **51.2% → 58.4%**，MRR **0.381 → 0.476**；recall@20 持平 78.4%。@10 只差 5 条事实（薄），但越往前名次差距越大，而 prompt 席位就在最前面。

**离线验证**：`pytest tests/enterprise_pdf_rag -q` → **1106 passed**、25 skipped、1 failed。25 skipped 是整组金标离线回放：本机 `current-processing` 已变成 `4f6ce62fe0b7`，金标钉的是 `231c904c843e`，整组按设计 skip 并提示重新冻结 —— **因此中文三条用例（p06 / p11 / k02）本轮无法离线验证**。1 failed 是 `test_document_catalog_aia_smoke`（`Unsupported chart qualification scope`），**在 `main` 上同样红**，与本轮无关（已 `git stash` 复核）。

**遗留**

1. **重新冻结金标并复测**：`current-processing` 已漂移，离线回放整组 skip。重新冻结后再跑两个 runner，中文路径才算验过。
2. **离线金标 runner 固定 `fusion_mode="rrf"`**：它的向量通道是声明式的（直接返回用例脚本化 claim 引用的成员），BM25 单通道会拿掉它赖以成立的保证。通道选择改由 `test_query_mode.py` / `test_hybrid_search.py` 守，真实召回由真实 runner 负责。
3. **`k02-region-thailand-zh` 仍是 known gap，但原因变小了**：检索已翻译，region 前置过滤仍从中文原问题推导、仍匹配不上文档自己的英文 vocabulary。是否也用译文推导过滤，是一个会把「拒答」变成「作答」的行为改动，需要证据再定（ADR 已列为被拒方案 + follow-up）。
4. **query embedder 变成「用到它的请求」的依赖**：没配 embedder 时 BM25 单通道问题照常 200（答案与配置齐全时逐字相同），需要向量通道的问题仍 503。与 opt-in reranker 同一规则，但这是 ADR 0011「missing group → 503 on its routes」措辞的一次实质收窄，已写进 ADR 0016 决定 7 与 ADR 0011 的修订指针。
5. **预算**：非英文问答一次两调用，进程级 `APP_ANSWER_MAX_LIVE_CALLS`（200）能买的问答数相应减少。
6. **中文效果未量化**：探针集全英文，无法离线评估翻译收益；上线后由用户复测。

## 自然语言问答的冻结金标集与两个 runner（2026-09-20，分支 `feat/nl-gold-set`，未合并 `main`）

> 解掉 ADR 0011 follow-up 里那条“自然语言问答还没有冻结金标集”。证据在本机 `data/validation/nl-gold/2026-09-20/`（`data/*` 为 git 忽略，同此前各轮）。

**做了什么**

1. **金标集** `data/benchmarks/enterprise-pdf-rag/aia-2026-interim/nl-answers-gold-v1.json`，25 条，钉死当前 AIA 发布（processing `231c904c843e`，snapshot `2f35ca97171a`，190 个 member）。同目录 `manifest.json` 新增 `gold_sets` 登记（`aia-gold-registry-v1`），与金标文件的一致性由测试守住。分类：`positive` 15 / `abstain` 7（含 2 条 known gap）/ `adversarial` 3。
2. **schema + 判定逻辑** `src/enterprise_pdf_rag/adapters/nl_gold.py`（与两份 ChartQA 金标同层，pydantic `strict/frozen/extra=forbid`，加载即自校验）。`judge()` 是两个 runner 共用的唯一判定规则，所以它们不会分叉。`answer_prose()` 拆掉 `render_message` 的引用块，并由测试对着 `render_message` 复证。**没有**进 `check_schema.py`：那是 HTTP 公开契约的漂移门，金标不是 HTTP 契约。
3. **离线 runner** `tests/enterprise_pdf_rag/answers/test_nl_gold.py`：用生产挂载路径（`scan_catalog` + `mount_document(entry, embedder=None)`，页级元数据因此可用）只读打开钉死发布，向量通道是**声明式**的 —— 直接返回该用例脚本化 claim 引用的成员，LLM 用 `fake_llm.scripted_client` 回放金标自带的 `model_output`。不追求复现召回；守的是席位/预算、strict schema、逐字段校验、散文数字门、拒答策略，以及金标锚点是否还在证据里。缺发布或发布与 `pinned` 不符时整组 skip 并提示重新冻结。
4. **真实 runner** `scripts/enterprise_pdf_rag/nl_gold_eval.py`：逐条打 `POST /v1/chat/completions`，写 `<case_id>.json` + `report.md` + `report.json` 到 `data/validation/nl-gold/<日期>/`；known_gap 单列且不影响退出码，非 known_gap 失败即退出码 1。

**本轮真实验收（2026-09-20，8768，`gpt-5.6-luna` + Qwen3-Embedding-4B / Qwen3-Reranker-4B）**：22 条可联网用例 **20 pass / 0 fail / 2 known-gap-holds**，退出码 0。rerank 用例 ~46s，其余 6.5-8.4s。表格见 `data/validation/nl-gold/2026-09-20/report.md`。

**建金标过程中查实的两件事（都是金标写法问题，不是行为回归）**

1. `record Operating ROE of 17.5%` 这句在**第 4 页和第 8 页各印了一次**（member `599bae5018ae` 与 `2171c8c4aac1`）。所以纯关键词问法（`Operating ROE 1H 2026`）会命中 p.8，用例不可能确定性地钉死页码 —— 该用例已删除，保留全句问法的 `p01`。
2. 钉死的 1-20 页里**没有“2024 年 VONB 增长”这个事实**。2026-09-21 那轮答 p.6 的 `+11%`，本轮答 p.7 的 `VONB Up 18% to $965m`，两者都已验证、都被逐字引用，但都不是问题问的那个数。因此该用例改成 `grounded_only`：只冻结“必须落在某条已验证 claim 上 + 引用字段齐全 + period 前置过滤推导出 `Y2024`”。

**两条 known gap（行为没错，但不是想要的答案）**

- `k01-region-thailand-en`（`Thailand 1H26 VONB`）：页面把泰国的值印在已验证 region 值 `AIA Thailand` 之下，`Thailand` 前置过滤留下的是别的候选，于是拒答。
- `k02-region-thailand-zh`（`泰国 1H26 VONB`）：中文地区名匹配不上文档自己的英文 vocabulary，**根本没推导出 region 过滤**，与英文那条是两个不同的缺陷。

**离线验证**：`pytest tests/enterprise_pdf_rag -q` 由 1015 → **1063 passed**（新增 23 条 schema/judge 单测 + 25 条离线回放）；离线回放整组约 79s。mypy / ruff / 四个 check / `check_doc_drift` 见下方提交说明。

**遗留**：金标只覆盖这一份文档，且还没有表格单元格（`cell`）用例与损坏证据用例；`grounded_only` 是为“文档本身没有唯一答案”开的口子，新增用例时应优先用 `required_claims`。

## 视觉对象保底席位与散文门修正（2026-09-21，分支 `fix/visual-recall-and-gates`，未合并 `main`）

> 修 ADR 0015 真实验证暴露的三个缺陷（下节"遗留 1b"的 ①③⑤）；代码提交 `48e3547`，本文档提交在其后；复测证据在本机 `data/validation/`（`data/*` 为 git 忽略，与此前各轮相同）。

**做了什么**（最小改动，TDD）：

1. **视觉对象保底席位**（`adapters/answer_service.py::select_context`）：把 ADR 0012 的"已验证图表保底席位"推广为"每类可引用视觉对象各至多一席"——CHART 有显式值、DIAGRAM 有 ≥1 个带 label 的 node、FORMULA 有 `linear`；只看 fused 前 `2*top_k` 且不在 `top_k` 内的候选，从末位向前让座、**不让已持有可引用视觉对象的席位**，pending / 无 label 的不补、窗口外不补，只 resolve 仍缺类别的候选（`member_texts` 供 kind）。`processing/index_text.py` 的 Diagram 投影核对过：`reading_order` 含全部 node label，无需改。
2. **散文数字门放行行首 / 句首枚举标记**（`answers/verify.py::prose_grounded`，`_ENUMERATOR_RE`）：`1.` / `2)` / `3、` / `(4)` / `第 5` / `Step 6` 在行首或句首（ASCII 句末标点后需空白，避免把 `17.5.` 的 `5.` 当序号；CJK 全角标点后不需要）视为序号剔除；正文里的金额 / 百分比 / 年份规则不变。
3. **strict 响应 schema 离线守卫**（`tests/enterprise_pdf_rag/adapters/test_strict_response_schemas.py`）：参数化遍历 8 个 `complete_json` / `complete_text_json` 调用点的 9 个 `response_model`（`ModelAnswer`、`PageMetadataDTO`、`PageLayoutDTO`、`Image/Diagram/FormulaObservationsDTO`、`VisualDescriptionDTO`、`ChartObservationsDTO`、`FigureDescriptionDTO`），对 `_response_schema` 实际发出的 schema 断言：所有 properties 都在 `required`、`additionalProperties: false`、无 `prefixItems/oneOf/allOf/...` 等不支持关键字、`$ref` 只指 `#/$defs/`；另有 BUG-A 形状回归（pydantic 原始 schema 漏 `row/col/header`，发出的 schema 列全且可空）与"守卫对松散模型确实报错"的反例。9 个模型现状全部满足，无需改模型。

**离线验证**：`pytest tests/enterprise_pdf_rag -q` **1015 passed**（新增 seat 3 例 + verify 2 例 + schema 守卫 11 例）；`mypy` 493 文件零错误；`ruff check` / `ruff format` 改动文件通过；`check_conformance` / `check_architecture` / `check_schema`（9 份契约）/ `check_drift` 通过；`check_doc_drift` 23 tracked / 0 stale（`src/enterprise_pdf_rag/CLAUDE.md` bump 到 `48e3547`）。

**真实复测**（AIA legacy store `231c904c…`，8768 / 3200 按下节 stop / start 命令重启 15 s 就绪，证据 `data/validation/generic-chat-2026-09-21/visual-recall/`，8 例全 200）：

| 用例 | 期望 | 结果 |
| --- | --- | --- |
| `d1-p6-three-stages`（"What are the three stages of the agency technology investment?"） | answered，引用三个 `nodes.<id>.label` | ✅ answered，3 条 `diagram_node`（`node-foundation` / `node-growth` / `node-intelligence`），Diagram 成员融合名次 vector 10 / lexical 26，**靠保底席位进第 10 席**（此前答的是 p5 的 `quote`） |
| `d1b-p6-numbered-list`（"List the three stages … as a numbered list."） | 编号列表回答 answered | ✅ answered，正文 `1. Foundation: 100% Digitalised Agency / 2. Growth: … / 3. Intelligence: …`，3 条 `diagram_node`，`1. 2. 3.` 未被散文门拦下（`100%` 由 label claim 接地），Diagram 同样第 10 席（vector 7 / lexical 25） |
| `d4-zh-p6`（"代理人科技投入的三个阶段分别是什么？"） | 此前 abstained | ✅ answered，3 条 `diagram_node`；BM25 仍全 null（中文无词面通道，未修），Diagram 靠 vector 11 名 + 保底席位进第 10 席 |
| `d2-p6-growth-stage` | 不回归 | ✅ answered（cache 回放），`nodes.node-growth.label` |
| ISSUE-2 三问（`b-donut-p18` / `b2-donut-rephrased` / `n-no-title`） | 不回归：p18 donut 72% / 28% | ✅ 全部 answered（cache 回放），`points.point-agency.value = 72%`（b2 另含 28%） |
| ROE 控制组（`a-roe-control`） | 不回归 | ✅ answered（cache 回放），`quote` `record Operating ROE of 17.5%` |

**遗留**：中文查询无词面通道（1b ②）与 partition 把标题行判成 Diagram / Formula（1b ④）未动；`request_fingerprint` 未变（`SYSTEM_RULES` 未改），四个回放用例 `cache_hit: true`、`llm_live_calls: 0`，本轮真实调用 4 次（d1 / d1b / d4 各 1 次，d2 之前已缓存）——d2 首次在旧代码下也已进席，故回放成立；席位让座规则在 `top_k` 很小（如 2）时可能让出第 1 席，`AnswerRequest` 默认 `top_k=10`，v1 接受。

## Diagram 与 Formula 可检索（2026-09-21，分支 `feat/visual-objects`，ADR 0015）

> 本节只讲 Diagram / Formula 两类视觉对象；表格网格（ADR 0014）与更下方各节的状态不受影响。

**分支与提交**：`feat/visual-objects`，在 `origin/main` 之上 —— `4e7ad5c`（Diagram 几何 + 逐字 span 证明）、`9a1b3f0`（Diagram 检索 / 引用 / verify，policy v5）、`b621df8`（Formula token IR 与无模型证明规则）、`e0266e4`（Formula 观测 / 资格 / 检索 / 引用）、`be6bc36`（合并 `origin/main` 的 ADR 0014）、`3a9cb11`（已发布快照的视觉对象重证脚本）、`a0a0d18`（真实验证暴露的 BUG-A 修复：strict 响应 schema 的 `required` 必须列全每个属性；AIA smoke 钉到迁移前 release id），外加本节的文档提交。

**做了什么**（[ADR 0015](adr/0015-diagram-and-formula-retrievable.md)）：把 ADR 0006 里"视觉语义是模型推断、没有独立校验器"那条恒 `UNAVAILABLE` 的诊断，替换成**两条无模型、可重放的来源证明**，通过才产 `qualified_ir` / `qualified_description` / `qualification` 三个 stage 并放行检索。

- **Diagram**：`adapters/diagram_geometry.py` 从对象自己的 crop SVG 里取形状（沿树合成 transform 得 page-top-left；跳过 `<image>`/`<text>`/字形路径而不是 raise），`adapters/diagram_qualification.py` 是规则本体 —— 每个 node 的 label 必须逐字等于它引用的 span（折叠空白、**保留大小写**）、bbox 必须对上**恰一个**真实矩形类填充/描边路径（裁到对象 bbox 后每边 ≤ 2pt）、引用的 span 必须落在 node bbox 内且只被引用一次；每条 edge 必须有"从源节点出发的连线 + 未被占用的填充三角（尖端落在目标、底边中点贴住连线端点 ≤ 3pt）"，且连线不得穿过第三个节点；对象 bbox 内**每个 span 都必须被引用**（漏节点的唯一确定性护栏）。任一条失败 → 整对象 `qualification=UNAVAILABLE` 且诊断逐字（`node <id>: <reason>` / `edge <i>: <reason>` / `object: <reason>`）。通过后 `processing/diagram_description.py` 用确定性模板产描述（`Diagram with 2 nodes and 1 edge: PLAN; BUILD. PLAN -> BUILD.`），**零模型**；模型描述原样留作 lineage。
- **Formula**：`adapters/pdfspine_formula.py` 重开 pinned PDF 把证明要读的字段逐字抄成 `formula_observation`（span 的 `text_matrix`/`ctm`/`origin`/`size`/`flags` + 每字符 bbox + 翻转成 top-left 的路径），`processing/formula_rules.py` 是纯规则 —— token 只能引用 span 子串且必须满足 tiling 闭合（有序、不重叠、拼接 == 去空白的 span 全文）；上下标优先用 PDF 原文 `Ts`（`text_rise`）证明，证不了的排版式脚本按"字号比 ≤ 0.8 + 基线偏移"判为 `derived` 并落盘三项数值（pdfspine 的 superscript flag 只记录、永不判定）；分数线 / 根号必须引用真实路径，**对象 bbox 内任何解释不了的路径都是拒绝理由**。全 `text_rise` → `VERIFIED` + `proof_level="full"`；含 derived → `PENDING` + `"literal"`，两者都放行。`linear` 是 LaTeX 子集（符号保留 Unicode），`readable` 用固定连接词表，并在措辞上区分两种证明（`x 的 2 次方` vs `x 上标 2`）。
- **检索与回答链**：`eligibility` 收 DIAGRAM / FORMULA（必需 stage 与图表同形），拒绝串分别是 `Diagram structure is not proven; only geometry-qualified diagrams are retrievable` 与 `Formula tokens are not source-proven; only proven formulas are retrievable`；索引文本 policy 升 **v5**（`source-transcription-and-scoped-chart-qualification-v5`，新增唯一门 `VISUAL_PROJECTION_POLICIES`，两类共用），Diagram 投影 = `diagram figure` + 阅读序 label + 每条边 `A -> B`，Formula 投影 = `readable` + `linear` + `formula` + 每个 token；context block 新增 `nodes.<id>.label` / `edges.<index>` / `formula.linear` / `formula.readable` / `tokens.<i>` 五条可引用路径；`ClaimKind` +3、`BlockKind` +2，`_PATH_PREFIX` 的值改成 tuple（FORMULA 要两个前缀）；**`_literal` 更名 `_exact`**，逐字转写、ADR 0014 的表头、以及这三类 claim 现在用同一个比对函数（折叠空白、保留大小写），verify 侧不再比资格侧宽松。
- **迁移**：新增 `adapters/visual_requalification.py` + `scripts/enterprise_pdf_rag/requalify_visual_objects.py`，用**已发布快照自己存着的** `svg`/`ir`/`description`/`model_view` + 页 sidecar 重跑 Diagram 证明，存成新 draft（`retrieval=None`，之后 `index` → `publish`），不调模型、不切指针、`--dry-run` 一字节不写；刻意不进 `semantic_objects` 的 stage 缓存（自带 `visual-requalification-stage-v1` 指纹盐），重跑逐字节相同。**不走全量 `process-aia-semantics`** 的理由：会撞 p5 的 stage-cache（bbox 包含判定的容差自那次运行后变过）并丢掉 p18 donut 的数值资格。Formula 分支留了同样的接口位（它需要 pinned PDF 重新观测；AIA 发布里 0 个 Formula 对象）。

**真实样本观察**（AIA 前 20 页，只读）：2 个 Diagram —— p6 三阶段堆叠图 nodes-only 放行（3 节点，描述 `Diagram with 3 nodes: Foundation: 100% Digitalised Agency; …; No connecting edges.`），p5 人工补入的流程图因 5 个 label 全空在 N2 被拒（诊断 `node node-industry-leading-technology: empty_label_without_source_occurrence`，根因是 partition 没把 6 个 span 归属给对象）；**0 个 Formula**（全 71 页 text 里 `=`、希腊字母、上标数字、`×÷√≈` 命中数全为 0），公式只能靠合成夹具验证；7 个 Image 仍不可检索（非目标）。`requalify_visual_objects` 对发布快照的结果：draft `2c819fd68cf397c7150ff86316609040fb1db2232218b7a6433c84d633c38660`，`qualify` 的 eligible 189 → **190**、skipped 52 → **51**、`Diagram structure is not proven…` 从 2 条降到 1 条。

**如何验证**（全部从仓库根跑，离线、不碰 `data/`）：

```sh
.venv/bin/python -m pytest tests/enterprise_pdf_rag -q          # Diagram/Formula 全部离线用例
.venv/bin/python -m pytest tests/enterprise_pdf_rag/adapters/test_diagram_real_samples.py \
  tests/enterprise_pdf_rag/adapters/test_formula_aia_smoke.py \
  tests/enterprise_pdf_rag/adapters/test_visual_requalification.py -q   # 真实样本只读 smoke
.venv/bin/python scripts/check_doc_drift.py --quiet
.venv/bin/python scripts/enterprise_pdf_rag/check_drift.py
.venv/bin/python scripts/enterprise_pdf_rag/check_schema.py     # rag-chat-v1 / aia-processing-v1 / document-catalog-v1 已手工重生成
bash scripts/ci.sh                                              # 唯一完整门
```

迁移与只读排查命令见[测试与入库指南](testing-and-ingestion.md)的“Diagram 与 Formula 证明的测试口径（ADR 0015）”节（`requalify_visual_objects.py --dry-run`、`formula_smoke.py`）。

**真实验证（AIA legacy store + 合成 PDF，2026-09-21，证据 `data/validation/generic-chat-2026-09-21/visual-objects/`）**

- **AIA 迁移**：`requalify_visual_objects.py` 对 `00d5c714…` 只重证两个 Diagram → draft `2c819fd6…`（p6 qualified 3 claims、p5 withheld，其余 239 个对象与 stage-cache 一字不动）；`index` → `231c904c…`（snapshot `2f35ca97…`，**190** 成员，2560 维，policy v5）；`publish` 把 `current-processing` 从 `00d5c714…` 切到 **`231c904c…`**（`current-manifest` 仍 `e702bf1c…`；shasum 见 `pointers-before.sha256` / `pointers-after-publish.sha256`）。8768 / 3200 用本文的 stop / start 命令重启，`/v1/documents` 显示 AIA `member_count = 190`，`api.log` 零 traceback。
- **BUG-A（阻断级，已修 `a0a0d18`）**：重启后所有 chat 请求 503 `provider_http_400`——ADR 0014 给 `ModelClaim` 加的可选 `row/col/header` 让 `$defs.ModelClaim.required` 不全，OpenAI strict 结构化输出整单拒绝；离线 stub 从不校验 schema 所以门禁看不到。修在 `json_completion._schema_arrays`（对象 schema 的 `required = list(properties)`），其余 5 个 response model 本就满足、请求指纹不变；8 条粘性的 400 失败记录已从 `data/ingestion-webui/model-cache/requests/` 移出（其指纹随 schema 变化已不可复现）。
- **HTTP 复测（AIA，8 例全 200）**：

| 用例 | 期望 | 结果 |
| --- | --- | --- |
| `d2-p6-growth-stage`（"…what does the Growth stage stand for?"） | 引用 `nodes.node-growth.label` | ✅ answered，claim kind `diagram_node`，quote `Growth: Data-Driven Lead Generation` |
| `d3-p6-order-not-inferred`（"Which stage comes after Foundation…"） | 不得产生 `diagram_edge`，nodes-only 不推断顺序 | ✅ Diagram 成员在 prompt 第 2 席，模型主动弃答（`model_declined: ambiguous`），零 `diagram_edge`、零被拒 claim |
| `d1-p6-three-stages`（"What are the three stages of the agency technology investment?"） | 三个 `nodes.<id>.label` | ⚠ answered 但 Diagram 成员未进 10 席，模型用 p5 的三条技术支柱 span（`quote`）作答——召回问题，非捏造 |
| `d4-zh-p6`（"代理人科技投入的三个阶段分别是什么？"） | 同上（中文） | ⚠ abstained：中文查询 BM25 全 null、Diagram 未进席；且答案里的 `1. 2. 3.` 编号被散文数字门当成证据外数字拦下（新发现的误拒） |
| ISSUE-2 三问（`b-donut-p18` / `b2-donut-rephrased` / `n-no-title`） | 不回归：p18 donut 72% / 28% | ✅ 全部 answered，`chart_value` 引用 `points.point-agency.value = 72%`（b2 另含 28%） |
| ROE 控制组（`a-roe-control`） | 不回归 | ✅ answered，`quote` 引用 p4 `record Operating ROE of 17.5%` |

- **合成 PDF 真实链路（`ingest --stage semantics` → `metadata` → `qualify` → `index` → `publish` → 重启 → chat，`--output-dir data/ingestion-webui`）**：

| PDF | partition 判定 | 资格 | chat 复测 |
| --- | --- | --- | --- |
| `synthetic/diagram.pdf`（`authored_pdf(diagram_page=True)`） | p3 绘制区判 Diagram ✅；p2 的标题文本行也被判成 Diagram | p3 **qualified / verified**（2 node + 1 verified edge，证明串含 `filled-arrowhead-tip`）；p2 withheld `object: no_native_shapes`（partition 误判，资格正确拒绝） | `s1`（"What comes after PLAN?"）✅ answered，`diagram_edge` 引用 `edges.0 = PLAN -> BUILD` |
| `synthetic/formula.pdf`（`authored_pdf(formula_page=True)`） | p3 判 Formula ✅（真实 partition 把分数与 x² 合成**一个**对象）；p1/p2 标题行也被判成 Formula | p3 **literal / pending**（`linear = ROE = \frac{Net\ profit}{Equity} x^{2}`，1 个 fraction 绑 0.8pt 画线，`2` 为 `derived` 上标）；p1/p2 full（纯 base token，无结构） | `s2`（"How is ROE defined?"）✅ answered，`formula` 引用 `formula.linear` |
| `synthetic/formula-rise.pdf`（reportlab 真 `Ts`） | p1 判 Formula ✅ | **full / verified**（`script_proof = text_rise`，`rise = 5.0`） | `s3` ✅ answered，`formula` 引用 `formula.linear` |

`metadata` 三次均 0 次调用（semantics 阶段已产出页元数据，幂等）；catalog 不热加载，新发布文档要重启 8768 才出现在 `/v1/models`。只读 smoke（`test_diagram_real_samples.py` 钉 run `00d5c714…`、`test_visual_requalification.py` 钉迁移前 release id、`test_formula_aia_smoke.py`）在发布后仍绿。

**遗留**
1. AIA 发布已迁移到 `231c904c…`（policy v5，190 成员）；`data/ingestion` 两个合成表快照仍是 v2，要吃到 Diagram/Formula 能力必须 `requalify`（仅 Diagram）或重跑 `semantics`，再 `index` → `publish`。
1b. 真实验证新暴露：① Diagram 召回偏弱（d1/d4 未进 10 席，只有问题带 node label 词面才召回；图表当年靠 ADR 0012 的保底席位解决，Diagram 可能需要同等待遇）——**已修（分支 `fix/visual-recall-and-gates`，见上方"视觉对象保底席位与散文门修正"节）**；② 中文查询无词面通道（BM25 全 null，RRF 退化单通道）——未修；③ 散文数字门把 `1. 2. 3.` 列表序号当证据外数字整体拒答——**已修（同上）**；④ 真实 partition 会把标题文本行判成 Diagram/Formula（资格侧正确拒绝或只产纯 base token，但会污染 kinds 统计）——未修；⑤ `ModelAnswer` 等 `response_model` 的 strict-schema 契约仍无离线守卫——**已修（同上，9 个模型参数化守卫）**。
2. `SYSTEM_RULES` 又变了（新增 diagram/formula 两句）→ `request_fingerprint` 变 → 旧回答缓存全部 miss（预期）。
3. 三份契约 JSON 仍无生成脚本，`check_schema.py` 只做全等比对、没有 `--write`；本次三份（`rag-chat-v1` / `aia-processing-v1` / `document-catalog-v1`）是手工重生成的。
4. 回答路径重证成本再叠一层：Diagram 每次 `resolve` 重算 crop 几何、Formula 每次重开 PDF 重新观测（与 ADR 0014 的表格重证并存），v1 接受。
5. 未支持：贝塞尔连线、开口箭头、一体成型曲线箭头（p5 就是这种）、多行公式、`∑`/`∫` 的上下限语义、分组 / 泳道；Image 仍不可检索；p5 的正确修法在 partition，不在资格校验器。
6. pdfspine 钉死 0.11.0：两条证明都逐值/逐字节比对它的输出，升级会让已存证明在挂载期被拒（不会静默错读），届时按 ADR 0015 末尾的升级流程走。

## 划线表格的网格来源证明（2026-09-20，分支 `feat/table-grid-proof`，ADR 0014）

> 本节只讲表格网格；上下两节的状态不受影响。

**做了什么**（[ADR 0014](adr/0014-ruled-table-grid-proof.md)）：补上 ADR 0011 Rejected alternatives 里说"缺一条来源规则"的那条规则，让**划线表**的 `TableIR.verification` 有资格成为 `VERIFIED`。`processing/geometry.py` 新增线段词汇（`Axis` / `Segment` / `rulings_at` / `covering_segments` / `segments_crossing` / `ruling_digest` / `RULING_TOLERANCE = 0.5`）；`adapters/pdfspine_tables.py` 新增 `ruling_segments(page)`（`get_drawings()` 的轴对齐实线、细填充矩形、描边矩形四边；虚线 / 曲线 / 斜线 / 线宽 > 3.0 一律不算线）与 `fill_rectangles(page)`；新纯模块 `processing/table_grid_proof.py` 是规则本体——每条行/列边界必须有 0.5pt 内的真实线，每个 cell 四边必须被线**连续覆盖**（可拼接多段），每个合并格跨过的内部边界必须**确实没有线穿过**，表头证据分 `proved`（粗内线 / 填充带）与 `heuristic`（粗体 / 首行），表头等级**不**影响 `VERIFIED`。`table_models.py` 解除 `PENDING` 硬钉，改为 `VERIFIED ⇔ 有证据`（`TableCell.border` / `TableIR.grid_evidence`，两者默认 `None`，旧 JSON 照常解析为 `PENDING`）；`cell_id` 的 `content_id` 输入未变，重建后 cell id 不变。回执 `LiteralQualification` 加 `grid_scope` / `ruling_digest`，`literal_qualification._reprove_table_grid` 在每次 `resolve` 打开 pinned 源 PDF **重新证一遍**并要求整体相等，pending 网格带这两个字段直接拒。消费侧：`ContextBlock.grid_verification` + `CellEvidence.headers`，prompt 首行 `grid=verified|pending`、只有 verified 块才打 `row=… col=… header="…"` 后缀；`ModelClaim` 加可选 `row`/`col`/`header`，`verify.py` 对 PENDING 网格、对不上的 row/col、非已证表头一律拒；引用带 `row`/`col`/`header`/`header_cell_id`。检索资格（`eligibility`）与 index-text policy **都没改**，PENDING 表照常可检索、照常可用 `cells.<id>` 纯文本引用。

**本分支的 5 个代码提交**：`7f13bfe`（纯规则：geometry + table_models + table_grid_proof）、`38fa184`（producer + `TableSpec` 夹具）、`83509e5`（回执绑定 + resolve 重证）、`0e41007`（消费侧 prompt / claim / verify / 引用字段）、`268aedb`（表头比对改用折叠空白、保留大小写的字面转写口径 `_literal`），外加本次的文档 + 真实样本 smoke 提交。

**真实样本只读 smoke（本次，未写入 `data/`）**：
- `data/ingestion/3f7233e3…`（合成 `meridian-capital-1h26.pdf`，3 页）page 2 的 4×2 表：按快照 `layout.json` 的真实区域 `[19.5, 119.8, 300.5, 224.3]` 与真实 `object_id` 重新提取 → **VERIFIED**，`grid_evidence.rows == (120, 146, 172, 198, 224)`、`cols == (20, 150, 300)`、`segment_count == 8`、`tolerance == 0.5`；8 个 cell 全部带 border；cell id 与快照里旧 `ir.json` 的 8 个**完全一致**；旧 `ir.json` 仍 `TypeAdapter(TableIR).validate_json` 成功且保持 `PENDING`、`grid_evidence is None`。表头只有 `first_row_rule/heuristic`（线全是 1pt、无填充带），所以这张表**有 row/col 引用但没有 header 引用**——符合设计。
- AIA 第 20 页敏感度矩阵区域 `(654, 125, 934, 466)`：断言不变（`result.table is None`，检测阶段 0 个区域匹配）。新增的只读诊断打印为 `p20 diagnosis: page_rulings=38 rulings_in_region=0 fills=6 found_table_bboxes=((15.692, 138.69, 325.622, 447.58387500000003),)` —— 该页确实有 38 条线段，但**敏感度矩阵区域内一条都没有**，`find_tables(lines)` 找到的唯一一张表在页面左侧、与该区域无交集。结论：这张矩阵是纯排版对齐、没有画线，属于"无线表"，按 ADR 0014 本来就不该证，与检测召回率无关。
- **CLI 重建（输入只读，输出全在 scratchpad）**：把快照的 `processing/model-cache` 拷到临时输出目录后，`enterprise-pdf-rag ingest --pdf <临时副本> --pages all --stage semantics --max-live-calls 0 --output-dir <tmp>` **0 次真实调用**跑通（客户端指纹 `8df988f7…` 与快照 producer 一致，`OPENAI_MODEL=gpt-5.6-luna` + shell 里的 `OPENAI_BASE_URL`，layout 全部走缓存回放；ADR 0013 的页级 metadata 不在旧缓存里，3 页 `deferred`，不影响表格）。新 `processing_id = e81e54b7…`，新 `ir.json` 的 `verification == "verified"`、`grid_evidence` 非空（同上数值）、`qualification.json` 带 `grid_scope = "ruled-grid-structure-v1"` 与 `ruling_digest = e493e790…`；`enterprise-pdf-rag qualify` → `eligible_member_count = 8`（Table 1 / Text 7，`skipped_reasons` 空），与旧快照一致。再用 `ProcessingRetrieval.build(..., OfflineDescriptionEmbedder())` 对新 draft 跑一遍资格化，`validate_literal_member` 的**重证路径真实执行并通过**。旧快照 `ProcessingStore(<主树>/processing).load(50d1d985…)` 仍成功、8 个对象照旧。
- **没做的**：CLI `index` 需要本地 embedding 服务（SSH 隧道），`publish` 又必须先 `index`，本次按纪律不起隧道 / 不碰服务，这两步**跳过**；主树 `data/` 全程只读，重建产物只在 scratchpad。

**遗留**
1. 三份契约 JSON（`aia-processing-v1` / `document-catalog-v1` / `rag-chat-v1`）没有生成脚本，`check_schema.py` 只做全等比对、没有 `--write`，只能手工重生成并自行保持缩进与键序；diagram / formula 两份方案合并后应统一重生成一次。
2. `literal_qualification` 只在 TABLE 分支检查回执的 `grid_scope` / `ruling_digest`；TEXT / LIST / GROUP 的回执即使伪造了这两个字段也不会被拒（现状无处写入，属防御性缺口）。
3. 回答路径重证成本：每个 VERIFIED 表成员在**每次** `resolve` 都要 `pdfspine.open` + `get_drawings()` 重证一遍。样本里表成员极少（≤1/页）故 v1 接受；若成瓶颈，把 digest 比对留在 `resolve`、完整重证挪到 `build`。
4. `answers/verify.py` 现在并存三种文本比对口径：cell 文本 `_norm`（折叠空白 + casefold）、表头 `_literal`（折叠空白、保留大小写）、以及散文门里的数字比较。各有理由，本次不统一（ADR 0014 Decision 8 写明）。
5. `data/ingestion` 两个 v2 快照、以及 AIA 发布的重建归属未定：要吃到网格能力必须重跑 `semantics → index → publish`，谁在什么时候触发没有归属。本次只在 scratchpad 做了验证性重建，**没有**动任何已发布指针。
6. `SYSTEM_RULES` 变了 → `request_fingerprint` 变 → 旧回答缓存全部 miss（预期）。

## 页级自动元数据与前置过滤（2026-09-21，分支 `feat/page-metadata`，ADR 0013）

> 本节是最新状态；下方 2026-09-20 的收尾状态仍有效，只是 AIA 发布指针已再次前移。

**做了什么**（[ADR 0013](adr/0013-page-metadata-and-prefilters.md)）：新 processing 阶段 `page_metadata`（每页一次文本模型调用，`title / section / page_type / language / periods / regions` 每个值逐字来自该页 span 或连续 ≤3 个 span 的拼接，否则剔除并记诊断；期间确定性规范化）；文档级 `display_title / report_period / years / regions` 零模型折叠、加载时重算校验；索引文本 policy **v4** = `display_title | page_title | section` 一行上下文头 + ADR 0012 投影（描述与引用原文不变，旧快照按 policy 门控）；`AnswerRequest.filters`（只有**期间**与**地区**两维；省略即从问题自动抽取，地区只在本文档自己的词表里逐字匹配；候选 < `top_k` 去过滤重试并标 `filters_relaxed`）；封面 / 目录页默认不进候选；多文档按封面标题独有词 + 年份路由；`/v1/models` 名称用封面标题；引用带 `page_title`。CLI：`ingest --stage metadata|semantics`、新 `metadata` 子命令。契约 `document-catalog-v1` / `rag-chat-v1` / `aia-processing-v1` 只新增可选字段。

**真实验证（AIA 前 20 页，证据 `data/validation/generic-chat-2026-09-21/page-metadata/`）**：`metadata` 对 `da1065fc…` 跑 20 页 = 20 次真实调用（预算 25；后两次 v1.1 / v1.2 重跑均从 model-cache 回放，0 次调用），20/20 `succeeded`；`display_title = "INTERIM RESULTS PRESENTATION"`（封面两行 span 拼接），`report_period = 1H2026`，`years = 2022–2026`，34 个地区词；`qualify` 189 / 52 / 9；`index` 41 s → processing `00d5c714…`、snapshot `99f47f48…`（2560 维，policy v4）；`publish` 把 `current-processing` 从 `da1065fc…` 切到 `00d5c714…`（`current-manifest` 仍 `e702bf1c…`，shasum 前后见 `pointers-*.sha256`）；8768 / 3200 用报告里的 stop / start 命令重启，16 s 就绪，`/v1/models` 显示 `INTERIM RESULTS PRESENTATION (df902346791b)`。HTTP 复测 11 例全部 200：ISSUE-2 三问与 ROE 控制组照旧答对（p.18 donut 现为融合第 1 名）；新增 `1H26 Distribution Mix`（不带 VONB）与中文 `2026 上半年 分销渠道 占比` 都从 p.18 答出 72% / 28%；显式 `filters {"periods":["1H26"]}` 应用未放宽，`{"regions":["Mars"]}` 放宽（`filters_relaxed: true`，回放无过滤答案）；`What was the VONB growth in 2024?` 收窄到 Y2024 页答 `+11%`（p.6）。**未过**：`Thailand 1H26 VONB`（地区等值只命中标 `Thailand` 的 p.4 / p.5，p.13 标的是 `AIA Thailand`，模型 `not_in_context` 拒答）；中文 `泰国 1H26 VONB`（词表是英文，抽不到地区过滤，拒答）——跨语言与地区等值是已知缺口。

**遗留**：地区匹配的同义 / 包含（`AIA Thailand` vs `Thailand`）、中文地区词、免责声明页污染地区词表、封面无公司名时路由只能靠年份、`chart_qa_bar_promotion` 追加成员时 plan policy 回落到 bar-v2（该路径的 BM25 会丢上下文头）、mypy 在本机对 `src/ragspine/common/observability/adapters/otel.py:49` 报 `opentelemetry` 无 `trace`（main 同样，环境问题，与本分支无关）。

## 会话收尾状态（2026-09-20，下一 session 从这里接手）

> **重启前接手清单（2026-09-20 深夜，Claude Code 重启前写）**
> 1. 工作树未提交（~45 个文件）：第 2 项 ①②③ + 第 3 项 1–4 + bbox 容差修复（`processing/geometry.py`）+ `webui_gate` 的 `document-catalog` profile/relay + 自带登录 opt-in（`ENTERPRISE_WEBUI_AUTH=1`）。最后一次 `pytest tests/enterprise_pdf_rag` **785 passed**、mypy 448 files 零错误、四个 check 与 `check_doc_drift` 全绿；**完整 `bash scripts/ci.sh` 尚未整体跑绿**（曾为修 bug 中断）。
> 2. 用户已授权：完整门全绿后把所有分支合并到 `main`（`merge/enterprise-pdf-rag` 已含于 main，实际只合 `feat/generic-document-service`），然后 commit + push（main 与 feature 都推）。**PyPI 发版未授权。**
> 3. 之后：以 `document-catalog` profile 起后端 8768 + Open WebUI **3200**（命令见 `open-webui.md` "document-catalog profile" 与"可选：启用 Open WebUI 自带登录"），首次开放注册建管理员（Name `linhan`，Email 需邮箱格式如 `linhan@local.test`，密码由用户口头给出、不写入文档），再以 `ENABLE_SIGNUP=False` 重启；从界面问几个 AIA 与合成 PDF 的问题并截图。
> 4. 公网暴露：frps 未配 `vhostHTTPPort`，frp 原生 basic auth 不可用；frpc（`/opt/homebrew/bin/frpc` v0.65.0，配置 `~/.config/frp/frpc.toml`，LaunchAgent `com.frpc`，无 admin API）只能重启生效，会让本机全部隧道含公网 SSH 6001 瞬断，动之前告知用户。决定：frp `type="tcp"` 把 3200 映到 `106.15.8.67:13200`（云安全组需放行），鉴权靠 Open WebUI 自带登录（frp basic auth 会与前端 `Authorization: Bearer` 冲突）。完整片段/回滚在 `data/local-models/frp-expose-3200.md`（gitignore）。
> 5. 真实模型 env（含 key）在 `data/local-models/local-models.env`（gitignore，600）；隧道 PID 78200（39002/39001）在跑；8766/8767 是旧服务别 kill；`webui_preview.py` 的 `processes.json` 可能拒绝与旧服务并存。

**仓库状态**：分支 `feat/generic-document-service`，HEAD `f0ca40d`（其前一提交 `2a7d1c1` 含第 2 项阶段 ① 与第 3 项阶段 1）。本节对应的工作树含**未 commit** 的第 2 项阶段 ②③ 与第 3 项阶段 2–4 全部代码、测试与文档（清单以 `git status --short` 为准：代码侧 17 个修改文件 + 15 个新增文件/目录，文档侧本节所列）；`main` 与本分支均未 push。提交前先核对用户授权与实际工作树。

**本 session 验证（工作树，各实现 agent 分别跑）**：`pytest tests/enterprise_pdf_rag -q` **758 passed**（BUG-1 修复前 752，文档收尾 agent 复跑一致；修复后 +6）；`mypy` 447 文件零错误；`ruff check` / `ruff format --check` 通过；`check_conformance` / `check_architecture`（含 `answers`）/ `check_schema`（8 份契约）/ `check_drift` 通过；`scripts/check_doc_drift.py` 23 tracked / 0 stale（`src/enterprise_pdf_rag/CLAUDE.md` 已 bump 到 `f0ca40d`）。完整 `bash scripts/ci.sh` 本 session **未整体跑绿**：文档 bump 前它在第 2 步（doc drift）停止，其余步骤未跑；下一 session 起手先跑一遍。

**设计方案与决定记录**：函数级设计 `plans/item2-document-catalog-and-service-mount.md`、`plans/item3-answer-chain.md`，合并调研 `plans/merge-into-ragspine-investigation.md`，路线汇报页 `rag-ingestion-brief.html`；决定、被拒方案与遗留统一记在 [ADR 0011](adr/0011-document-catalog-and-verified-answer-chain.md)；可复制的启动 env、curl、响应信封与状态码表在 [测试与入库指南](testing-and-ingestion.md) 的“document-catalog 模式”节。

**已拍板的决定**（与 ADR 0011 一致）：`MountedDocument` 含 `search/resolve/manifest/member_texts/chart_context/displayed_context`；claim 校验失败走“逐条剔除 + 散文数值门”（散文里任何数字不在已验证 claim 内 → 整体拒答 `CLAIM_NOT_IN_EVIDENCE`；零验证 claim → 拒答并取第一条 rejected 的原因）；rerank 默认关、显式开关；TABLE 成员只放行逐字转写 `VERIFIED` 的并把资格 policy 升 `-v2`；AIA 样本用 `APP_LEGACY_DOCUMENT_ROOTS` 兼容（默认空）；`execution_mode="document-catalog"` 与 `aia-source-review` 并存；`ragspine` 只能在 `enterprise_pdf_rag/adapters/` 下 import（conformance 白名单未改）。

**第 2 项（文档目录与服务挂载）— 阶段 ①②③ 完成**
- 阶段 ①（已在 `2a7d1c1`）：`core/settings.py`（`document-catalog` 模式、`ingestion_dir`/`APP_INGESTION_DIR`、`legacy_document_roots`/`APP_LEGACY_DOCUMENT_ROOTS`、`ingestion_root`）；`adapters/pdf_ingestion.py` 默认根改 `ingestion_root`；`adapters/document_catalog.py`（`CatalogEntry`、`DocumentCatalog`、`scan_catalog`、`MountedDocument`、`mount_document(entry, *, embedder)`、`MountedCatalog`、`mount_catalog`、`QueryEmbeddingUnavailable`）；测试 `test_document_catalog.py`（15）、`test_document_catalog_aia_smoke.py`（真实 store 只读）、共享 fixture `generic_publication_helpers.py`。
- 阶段 ②（本 session，未 commit）：`adapters/http/catalog_schemas.py`（契约 `document-catalog-v1`：`DocumentListItem` = `CatalogEntry` + `mounted`/`mount_error`、`DocumentListResponse`、`DocumentDetailResponse`、`DocumentSearchRequest/Response`、`DocumentContextRequest/Response`）；`adapters/http/documents.py`（`create_documents_router(mounted)` / `create_documents_app(catalog, *, embedder, llm=None, reranker=None)`；`GET /v1/documents`、`GET /v1/documents/{id}`、`/manifest`、`POST .../search`、`POST .../context`；未知 id 404、可见未挂载 409 附原因、`QueryEmbeddingUnavailable`/`ProviderRequestError` 503 固定文案、每请求 `mount.manifest()` 重校验）；`app.py::create_configured_app` 新增 `document-catalog` 分支（`scan_catalog(settings.ingestion_root, legacy_roots=...)`，`LocalEmbeddingAdapter` 显式构造一次共享，缺 `EMBEDDING_*` → embedder=None；`aia-source-review` 分支原样）；`check_schema.py` 登记；`docs/enterprise-pdf-rag/schemas/document-catalog-v1.json`；测试 `test_documents_http.py`（14）。注意 FastAPI 0.141 `app.routes` 里 `include_router` 是惰性 `_IncludedRouter`，测试用 `_route_paths` 展开。
- 阶段 ③（本 session）：文档——`src/enterprise_pdf_rag/CLAUDE.md`（目录树、Run、Invariants、`verified-against`）、ADR 0011、`testing-and-ingestion.md`、本文件，以及 `docs/enterprise-pdf-rag/README.md`、根 `CLAUDE.md`/`README.md` 各一两行。

**第 3 项（证据链上的自然语言回答）— 阶段 1–4 完成**
- 阶段 1（已在 `2a7d1c1`）：`answers/ports.py`（`MemberText`、`MountedDocument` Protocol）；`adapters/hybrid_search.py`（`LexicalIndex` 按快照 id 内容寻址、`build_lexical_index`、`lexical_rank`、`fuse`、`HybridSearch.search`、`RerankPort`/`LocalRerankJudge`；精确复用 `ragspine.retrieval.lexical.retrieval.{tokenize,bm25_scores,rrf_fuse}` 与 `ragspine.retrieval.rerank.listwise_rerank.{ListwiseJudge,listwise_rerank}`）；`processing/context_builder.py`（`ContextBlock.prompt_text()`、`SpanEvidence/CellEvidence/ChartFieldEvidence`、`build_context_block`、`budget_blocks` 整块丢弃）；测试 `test_hybrid_search.py`（10）、`test_context_builder.py`（8，含阶段 4 新增 1）。
- 阶段 2（本 session）：`answers/models.py`（`AnswerStatus`、`ClaimKind`、`AbstainReason`、`from_refusal`、`FusedHit`（自 `hybrid_search` 迁入并回导）、`AnswerRequest(question, document_sha256=None, top_k=6, channel_limit=20, rerank=False, history=())`、`ClaimCitation`、`VerifiedClaim`、`RejectedClaim`、`AnswerResult(status, answer, claims, rejected, abstain_reason, abstain_detail, document_sha256, processing_id, snapshot_id, member_ids, fused, request_fingerprint, llm_live_calls, cache_hit)`）；`answers/prompt.py`（`ModelClaim`/`ModelAnswer` strict pydantic、claims ≤16、`SYSTEM_RULES`、`build_prompt`）；`answers/verify.py`（`ClaimVerification`、`verify_claims`、`_verify_quote/_verify_cell/_verify_chart_value`（bar 走 `chart_context()`+`check_displayed_evidence`，donut 走 `check_context`/`check_fields`，`Decimal` 相等或来源显示串相等，永不派生）、`prose_grounded`、`decide`）；`adapters/json_completion.py::complete_text_json`（独立指纹盐 `bounded-text-json-v1`，可选 `system`）；`adapters/answer_service.py`（`AnswerSettings(rrf_k=60.0, prompt_budget_chars=18_000, max_output_tokens=1024)`、`AnswerService(documents, llm, *, settings, reranker, index_cache)`、恰好一次 LLM、异常 `UnknownDocument`/`AmbiguousDocument`/`DependencyUnavailable(.code)`）；`check_architecture.py` 把 `enterprise_pdf_rag.answers` 加进 `PACKAGES` 并用 `EXTRA_ALLOWED` 只放行 pydantic；测试 `tests/enterprise_pdf_rag/answers/`（`store_mounted_document.py` 测试桥、`fake_llm.py`、`test_verify.py` 12、`test_answer_service.py` 10）。
- 阶段 3（本 session）：`adapters/http/chat_schemas.py`（契约 `rag-chat-v1`：`RagChatRequest(model, messages 1..32, stream, stream_options, document, rerank; extra=forbid)`、`ClaimCitationOut/ClaimOut/RejectedClaimOut`、`AnswerEnvelope`、`RagCompletionResponse`、`RagCompletionChunk`）；`adapters/http/chat.py`（`create_chat_router(mounted, service|None)`：`GET /v1/models` 每个已挂载文档一个 `enterprise-pdf-rag/<sha256[:12]>`；`POST /v1/chat/completions` 选文档 `document` > `model` > 目录唯一，最后一条须 `user`，历史轮作数据、client `system` 丢弃，`content` = 回答 + `引用:` 列表 + `enterprise_pdf_rag` 信封，拒答 200，`stream=true` 先算完再切片、末帧 `choices: []` + 信封再 `[DONE]`；状态码见指南）；`app.py` 分支显式构造 `JsonCompletionClient(load_llm_config(), cache_dir=ingestion_root/"model-cache", max_live_calls=settings.answer_max_live_calls)`（缺 `OPENAI_*` → chat 503，不静默 mock）与 `RERANK_*` 齐时的 `LocalRerankJudge(LocalRerankAdapter(...))`；`core/settings.py::answer_max_live_calls`（`APP_ANSWER_MAX_LIVE_CALLS`，默认 200）；`check_schema.py` 登记；`docs/enterprise-pdf-rag/schemas/rag-chat-v1.json`；测试 `test_chat_http.py`（14）。
- 阶段 4（本 session，TABLE 放行）：`processing/table_transcription.py`（纯规则 `table_span_ids` / `check_table_transcription`，producer 与 validator 共用）；`adapters/source_objects.py::source_table_description()`；`semantic_objects.py::_table` 提取后尝试转写，失败保留 ir、description/qualification 两 stage `UNAVAILABLE` 带原因；`literal_qualification.py` TABLE 分支；`processing_retrieval.py::eligibility` 收 TABLE（非 VERIFIED 理由 `Table transcription is not verified; only verified tables are retrievable`），`_POLICY` → `source-transcription-and-scoped-chart-qualification-v2`（进 `RetrievalPlan.qualification_policy` 与 snapshot_id 哈希）；`draft_publication.py::DraftQualification.qualification_policy` → `retrieval-eligibility-kind-and-stage-completeness-v2`；`context_builder.py` TABLE 块 verification 取 description。测试：`processing/test_table_transcription.py`（3）、`test_pdf_ingestion.py::authored_pdf(table_page=)`、e2e +2、`test_draft_publication.py` +2、`test_context_builder.py` +1、`test_verify.py` +1。多 span cell 按 span 顺序拼接，顺序不同则拒绝而非误验。

**偏离计划与理由**
- **TABLE 的 “VERIFIED” 定义**：原计划放行“VERIFIED 的 TableIR”，但 `TableIR.verification`/`TableCell.verification` 在 `processing/table_models.py` 的 `__post_init__` 钉死 `PENDING`，“已验证网格”不可实现。改为与 TEXT/LIST/GROUP 同标准的**字面转写 VERIFIED**（`ObjectDescription.verification == VERIFIED`、producer `exact-source-transcription-v1`、`LiteralQualification` 回执、qualification stage SUCCEEDED），网格结构仍 PENDING；单元格引用只证明原文，不证明行列关系。 **已于 2026-09-20 部分推翻**：[ADR 0014](adr/0014-ruled-table-grid-proof.md) 补上了来源规则，**划线表**的网格可以 `VERIFIED` 并支持 `row`/`col`/`header` 引用；无线表、吸附 / 双线边界仍按本条保持 PENDING、只证明原文。
- **`mounted=True` 的“仅证据挂载”语义**：缺 `EMBEDDING_*` 时文档仍 `mounted=true`、`embedding_configured=false`，manifest/context 可用，search/chat 503；只有指纹不符、非 ready、证据损坏才 `mounted=false` 并带 `mount_error`。证据读取本身不需要 embedder，且目录必须如实区分“未配置检索”与“拒绝挂载”。
- **契约名 `rag-chat-v1`**：与 plans/item3 §5 一致，未改。
- **`FusedHit` 归属**：迁到 `answers/models.py`（`AnswerResult` 要引用它）并在 `hybrid_search` 回导，计划已预留该选项。
- **`answers/` 放行 pydantic**：用 `check_architecture.EXTRA_ALLOWED` 只放行 pydantic，而不是把模型输出 schema 挪到 adapters。
- **图表值校验口径**：claim 文本等于来源显示串 **或** 数值等于资格化 `Decimal`，二者之一即通过；verified claim 的 `text` 统一为来源显示串、`value` 为 `Decimal`。
- **旧快照兼容**：没有任何“policy 不匹配即拒绝”的逻辑，policy 只是信息字段；真实 AIA 快照 policy 为 chart-qa promotion 的 `source-transcription-and-numeric-paint-qualification-v1`，照样 ready。AIA 前 20 页无 Table 对象，`qualify` 仍 eligible=189 / skipped=52。

**遗留清单**
1. `adapters/http/webui_gate.py` 只认 `aia-2026-interim-source-review-v1` / `enterprise-pdf-rag-offline-demo-v1`，Open WebUI 网关不能前置 `document-catalog` 模式（另一 agent 正在加 `document-catalog` profile，未完成）。
2. `JsonCompletionClient` 以 `retry_failed=False` 构造：真实调用失败会被缓存回放，删 `<ingestion_root>/model-cache/requests/<fingerprint>.json` 才能重试。
3. 自然语言问答没有冻结 gold 集（只有 chart-qa v1/bar 两套）。
4. 真实 embedder 的 `index`、真实答案模型的 chat、`APP_LEGACY_DOCUMENT_ROOTS` 挂真实 AIA 发布——已于 2026-09-20 做过一轮（18 用例，反捏造守住），结论与遗留（ISSUE-2 图表召回**已解决**（[ADR 0012](adr/0012-chart-index-text-and-retrieval-seats.md)，分支 `feat/chart-index-text`，待合并 `main`）、ISSUE-3 散文门年份（已于 0.14.0 解决）、信封无 `request_fingerprint`、`visual_semantics` 无专属回归）见下方“真实模型验收”。原先把 ISSUE-2 归因为“短描述词面弱于长文本”是错的，真实原因是图表索引文本只有标题 + RRF 单通道上限 + `channel_limit`/`top_k` 切点 + reranker 输入不是证据块；真实重建与复测见下方“ISSUE-2 修复后的真实重建与复测”与 `data/validation/generic-chat-2026-09-20/aia-after-issue2/`。
5. `deploy/enterprise-pdf-rag/open-webui/backend.Dockerfile` 未复验（ADR 0021）。
6. 第 20 页 v2 独立验收（第 4 项）未动。
7. 完整 `bash scripts/ci.sh` 未在本工作树整体跑绿；全部改动未 commit / 未 push。
8. `docs/spine-family.md` §5 仍写“ragspine 的 `HybridRetriever`/rerank/agent 尚未接入它的回答链”；该文件真源在家族根目录 `~/startup/spine/docs/spine-family.md`，须改真源再 `make family-doc-sync`，本仓副本未单独改。

**环境坑**：全局 `uv` 已于 2026-09-20 升到 0.12.17（`uv self update --token $(gh auth token)`，未认证会撞 GitHub API 限流）；`.venv` 为 3.12，`uv sync --all-extras --no-extra ocr`；不用 uv 时可直接 `.venv/bin/python -m pytest|mypy`、`.venv/bin/ruff`；任何触及 `src/ragspine` 的提交后要把 23 份 tracked 文档的 `verified-against` bump 到新 HEAD，否则 `scripts/check_doc_drift.py` 红；11G 运行现场在 `data/`（gitignore），别清理；本地模型隧道变量（`LOCAL_MODELS_SSH_HOST` 等）默认不在 shell 里。

**下一 session 起手顺序**：读 `src/enterprise_pdf_rag/CLAUDE.md` → 本节 → ADR 0011 → 下方“真实模型验收”结论 → `git status --short` 核对工作树 → `bash scripts/ci.sh` 全门 → 按用户授权 commit（先 `ruff format` / `ruff check --fix` 规范化）→ 再从遗留清单挑：自然语言问答 gold 集、`webui_gate` 接 `document-catalog`、第 4 项 p20 v2。

**真实模型验收（2026-09-20）**

环境（密钥、私有主机与端点一律不写）：答案模型用 shell 里既有的 `OPENAI_BASE_URL` / `OPENAI_API_KEY`（外部 OpenAI-compatible 端点）+ `OPENAI_MODEL=gpt-5.6-luna`；embedding / rerank 为 linhan 4080 台式机（linhan-pc）上的 vLLM 0.14.0 容器 `rag-vllm-embedding`（远端 127.0.0.1:28002，`Qwen/Qwen3-Embedding-4B`，2560 维，fingerprint `local-http/Qwen/Qwen3-Embedding-4B` 与现有 AIA 索引一致）与 `rag-vllm-reranker`（远端 127.0.0.1:28001，`Qwen/Qwen3-Reranker-4B`，`/v1/rerank` Cohere 形状），经项目受管 SSH 隧道映射到本机 39002 / 39001（本包强制 loopback）。服务：`document-catalog` 模式 uvicorn `127.0.0.1:8768`，`APP_LEGACY_DOCUMENT_ROOTS` 指向 `data/output/aia-2026-interim/pages-001-020`，就绪 ≤17.8s（两文档 13.86s）。证据目录 `data/validation/generic-chat-2026-09-20/`（`aia/` 13 个、`generic/` 13 个原始响应、`generic-after-fix/`、`summary.json`）；日志与证据中三个 API key 出现 0 次，无 traceback。

AIA 前 20 页（model `enterprise-pdf-rag/df902346791b`，189 成员）：

| # | 问题 | 结果 |
| --- | --- | --- |
| a | “record Operating ROE in 1H 2026?” | 200 answered，1 claim，p.4 `fragments.<span>` 逐字引用 “record Operating ROE of 17.5%”，`llm_live_calls=1`，16.2s |
| b | “1H26 Distribution Mix chart… Agency %?” | answered 但引用的是 p.14 文本 “Premier Agency: 55% of VONB”——答非所问（正确是 p.18 donut 72%）；`rerank=true` 同样。**ISSUE-2 图表召回** |
| b2 | b 改写为含 “Partnerships” | answered，2 个 chart claim：p.18 `points.point-agency.value=72%`、`points.point-partnerships.value=28%`，回指 svg 元素，13.2s |
| c1 | “forecast VONB for 2027?” | 200 abstained `model_declined` / `not_in_context`，6.7s |
| c2 | “Agency 比 Partnerships 高多少个百分点?” | abstained `model_declined` / `needs_calculation` |
| d | 重复 a | `cache_hit=true`、`llm_live_calls=0`，4.4s |
| e | a 的 `stream=true` | 6 帧：role → content×2 → 空 delta → 尾帧含 `enterprise_pdf_rag` → `[DONE]` |
| f | a 的 `rerank=true` | 真实调用了 39001（lsof 证据），但 abstained `claim_not_in_evidence`：模型原文 “…in 1H 2026 was 17.5%.”，17.5% 已验证，“2026” 不在 claim 文本内被散文数值门整体拒答。**ISSUE-3**（已于 0.14.0 放宽，见下方验收遗留） |
| g1 / g2 | `temperature` 字段 / 不存在的 `document` | 422 `extra_forbidden` / 404 |

通用合成 PDF（虚构，3 页文本 + 第 3 页原生 4×2 划线表）：修复前 sha `f41da5…`：`ingest --stage semantics --max-live-calls 3` → live=3（每页 1 次 layout，设计内）、object_count=8、failed_stage_count=5（**BUG-1**，见下）；`qualify` eligible=3 / skipped=5；`index`（首次真实 Qwen3 embedder）member_count=3、dims [2560]、1.06s；`publish` ready。重启后 `/v1/documents` 两文档 ready/mounted，`/v1/models` 两个 id。chat：

| # | 问题 | 结果 |
| --- | --- | --- |
| t1 | “cash balance?” | answered，引用 p.2 quote “Cash balance stood at 987 million” |
| t2 | 表格 Revenue | answered，`cells.table-cell-v1:…` = “1,234” 逐字一致 |
| t3 | 空单元格 Margin | abstained `not_in_context` |
| t4 | “Revenue minus Net profit” | abstained `needs_calculation` |
| t5 | 未入索引的 headcount 行 | abstained `not_in_context` |
| t6 | `model=default` 无 `document` | 422 “Document selection required” |
| t7 | `model=default` + `document=<sha12 前缀>` | answered（缓存） |

结论：18 个真实用例没有任何“证据外的数字/事实”进入 answered 回答；引用精确到 span / cell / point、页码正确；拒答理由合理。反捏造守住。ISSUE-2 是召回质量问题，不是捏造。

**BUG-1（已修复，同 session，代码未 commit）**：根因——prompt 用 `json.dumps` 全精度渲染 canonical bbox（如 `42.400000000000006`），模型回传最短小数（`42.4` / `308` / `20`），下游多处严格 `<=` 判“模型区域框 ⊇ canonical 图元”时差 6e-15 即失败；离线 stub sender 原样回传 float，所以从未暴露。修法——新增纯模块 `src/enterprise_pdf_rag/processing/geometry.py`（stdlib）：`COORDINATE_TOLERANCE = 1e-6`，`contains(outer, inner, *, tolerance)` 外框每边放宽 tol、内框保持非退化；替换 6 处“模型外框 vs canonical 内框”比较：`adapters/source_objects.py` ×2、`adapters/object_processing.py`、`processing/table_transcription.py`（`_inside` 删除；`_center_inside` 是 canonical vs canonical，不动）、`adapters/literal_qualification.py`（index / resolve 复核须与生产者一致）、`adapters/visual_semantics.py`（`_inside` 委托）、`processing/service.py::validate_partition`。不动 `table_models.py`、`pdfspine_tables.py`（本有 0.5pt 容差）、`figures/` 与 donut / bar 几何族。1e-6 的理由：float 渲染噪声 <1e-12 pt，真实版面偏移 ≫1e-3 pt，两侧各留 ≥3 个数量级，差 0.5pt 的越界 span 仍拒绝。不做边界规范化的理由：prompt 无固定精度、改 prompt 会改 request fingerprint、canonical 不能舍入（内容寻址）、snap 不覆盖表格网格与 diagram 节点。测试：新 `tests/enterprise_pdf_rag/processing/test_geometry.py`，`test_source_objects` / `test_table_transcription` / `test_partition` / `test_object_stages` 各加 “tolerates model-rendered float noise but not real overreach” 用例；离线 stub sender `_extent` 改为 `round(v, 6)` 模拟真实回传，e2e 4 个用例修前红、修后绿。全包 **758 passed**（752 + 6）、mypy 447 files 零错误、四个 check 与 doc-drift 通过。真实复验：新合成 PDF sha `3f7233e3…`（`generic-after-fix/`）`ingest` failed_stage_count=0、8 对象全部 succeeded、43.5s；`qualify` eligible=8 / skipped=0（Table 1 + Text 7）；`index` member_count=8、dims [2560]；`publish` ready。AIA 前 20 页只读 `qualify` 修前修后 JSON 逐字节一致（189/52）。

**验收遗留**（已同步进 ADR 0011 follow-ups）：
- ISSUE-2 图表召回——**已解决**（[ADR 0012](adr/0012-chart-index-text-and-retrieval-seats.md)，分支 `feat/chart-index-text`，两个代码提交 `7c57e4b` / `0efbc60`，待合并 `main`）。**归因更正**：不是“短描述词面弱于长文本”——实测是图表的索引文本只有标题（`Distribution Mix`，2 个 token），BM25 已把 p.18 donut 排第 1、向量通道却给 13–20 名，RRF(k=60) 下单通道命中上限只有 1/61，`channel_limit=20` 正卡在其向量名次上，融合名次 ≥7 被 `top_k=6` 切掉（逐字节复核 model-cache：prompt 里零图表块），而 reranker 拿到的是同一份分词拼接的索引文本而不是证据块。修法四条：图表按已资格化 IR 的投影建索引（policy v3）、默认 `top_k`/`channel_limit` 改 10/50、reranker 读证据块、图表保底席位 + `member_ranks` 可观测。真实重建与复测见下方“ISSUE-2 修复后的真实重建与复测（2026-09-20）”，证据 `data/validation/generic-chat-2026-09-20/aia-after-issue2/`。
- ISSUE-3 散文数值门把年份当数字——**已解决（rag-spine 0.14.0，用户拍板）**：`answers/verify.py::prose_grounded` 现在放行三类数字：(a) 属于某条已验证 claim 的 text / value；(b) 逐字出现在用户问题（`AnswerRequest.question`）里；(c) 出现在已验证 claim 所引用证据的原文中（span quote、表格 cell 原文、图表 claim 的 period / category 标签与 source_display，即 `ClaimCitation.quote`）。其余数字仍整体拒答，零验证 claim 的处理不变，`decide` 顺序不变；用例见 `tests/enterprise_pdf_rag/answers/test_verify.py` 与 `test_answer_service.py`。
- `AnswerEnvelope` 不含 `request_fingerprint`，排障时无法直接定位 `model-cache/requests/<fp>.json`。
- `visual_semantics.py` 的 4 个 `contains` 调用点无专属回归测试。
- `ingest` 的 layout 阶段每页 1 次真实 LLM 调用，纯文本页也一样（设计内）。
- `webui_gate.py` 的 `document-catalog` profile 另一 agent 进行中，**未完成**。

**ISSUE-2 修复后的真实重建与复测（2026-09-20）**

分支 `feat/chart-index-text`（索引侧 `7c57e4b`、查询侧 `0efbc60`，均未合并 `main`）；决定、被拒方案与代价见 [ADR 0012](adr/0012-chart-index-text-and-retrieval-seats.md)。环境与上节相同（答案模型 `gpt-5.6-luna` 用 shell 里既有的 `OPENAI_BASE_URL` / `OPENAI_API_KEY`；embedding / rerank 经项目受管隧道到本机 39002 / 39001），密钥与主机一律不写。查询侧现值：`AnswerRequest` 默认 `top_k=10`、`channel_limit=50`（上文“阶段 2”里记的 6 / 20 是当时的历史快照，不改）。

重建（AIA 前 20 页，`data/output/aia-2026-interim`）：

| 步骤 | 结果 |
| --- | --- |
| `qualify` | eligible=189 / skipped=52 / chart=9，与修前一致（投影不改资格） |
| `index`（真实 Qwen3-Embedding-4B，2560 维，fingerprint `local-http/Qwen/Qwen3-Embedding-4B`） | 40s；新 processing `da1065fc0bd6d378e1116fe6b74d0edfdc9dc06321d8ccdf526ee102b8d7763f`、新 snapshot `53e08ad418392651ffd9b79e41154c30769f9d0d99c48f1237c747d81357110a`，189 成员，policy `source-transcription-and-scoped-chart-qualification-v3` |
| `publish` | 只切 `current-processing`（`a7384f0c…` → `da1065fc…`）；`current-manifest` 不变，旧快照文件保留，旧 manifest `f59d230869d5` 仍可 load |

指针与对象数前后对照（证据 `pointers-before.txt` / `pointers-after.txt`）：

| | before（14:19:42Z） | after（14:21:32Z） |
| --- | --- | --- |
| `current-processing` 内容 | `a7384f0c…caa8d5` | `da1065fc…7763f` |
| `current-processing` 文件 shasum | `c98a31f2…6baf` | `e75de189…d55b` |
| `current-manifest` 内容 | `e702bf1c…7129f` | 不变 |
| `current-manifest` 文件 shasum | `af5eb57f…4871` | 不变 |
| processing objects / source objects | 4320 / 147 | 4543 / 147 |

预览重启（8768 + 3200，同一套 env，16s 后挂上新快照）：

```sh
# stop
ENTERPRISE_PREVIEW_STATE_DIR=$PWD/data/open-webui-catalog ENTERPRISE_API_PORT=8768 \
  ENTERPRISE_WEBUI_PORT=3200 .venv/bin/python scripts/enterprise_pdf_rag/webui_preview.py \
  stop --profile document-catalog

# start（OPENAI_API_KEY / OPENAI_BASE_URL 来自 shell，不写入文档）
set -a; source data/local-models/local-models.env; set +a
ENTERPRISE_PREVIEW_STATE_DIR=$PWD/data/open-webui-catalog ENTERPRISE_API_PORT=8768 \
  ENTERPRISE_WEBUI_PORT=3200 ENTERPRISE_WEBUI_AUTH=1 ENABLE_SIGNUP=False \
  APP_INGESTION_DIR=$PWD/data/ingestion-webui \
  APP_LEGACY_DOCUMENT_ROOTS="[\"$PWD/data/output/aia-2026-interim/pages-001-020\"]" \
  ./scripts/enterprise_pdf_rag/start.sh --profile document-catalog
```

HTTP 复测（证据 `data/validation/generic-chat-2026-09-20/aia-after-issue2/`，含五个原始响应、`qualify-before.json`、`index.json`、`publish.json`、`summary.json`）；`member_ranks` 取信封里 p.18 donut 那条（融合名次均为第 5，都在新 `top_k=10` 内，未用到保底席位）：

| # | 问题 / 参数 | 结果 | donut 的 `member_ranks` | 耗时 |
| --- | --- | --- | --- | --- |
| b | “In the 1H26 Distribution Mix chart, what percentage of VONB came from Agency?” | 200 answered，1 claim，p.18 `points.point-agency.value = 72%` | lexical 1 / vector 17 / 融合第 5 | 13.8s |
| b2 | b 改写为含 Partnerships | 200 answered，2 claims：`points.point-agency.value = 72%`、`points.point-partnerships.value = 28%` | lexical 1 / vector 17 / 融合第 5 | 14.1s |
| n | “Agency share of VONB 1H26”（不含标题，修前 BM25=0） | 200 answered，p.18 `points.point-agency.value = 72%` | lexical 1 / vector 24 / 融合第 5 | 13.4s |
| b3 | b + `rerank=true` | 200 answered，同 b | lexical 1 / vector 17 / 融合第 5 | **52.3s** |
| a | 控制组 “record Operating ROE in 1H 2026?” | 200 answered，p.4 `fragments.<span>` 逐字 “record Operating ROE of 17.5%”，与修前一致 | vector 2 / lexical 3 | 13.2s |

b3 的 52s 是已知代价、不是回归：reranker 现在读证据块，因此要 `resolve` 全部 fused 候选（≤ `2 × channel_limit` = 100 个），AIA 上每次 `resolve` ≈0.8s（`manifest()` 重载校验 ~5000 个资产摘要 + 两次 `load_retrieval` 解析 189×2560 维索引）。rerank 保持默认关、按请求 opt-in。另：`member_texts()` 在 AIA 上 ≈0.58s，所以保底席位逻辑只在需要时惰性调用一次。

## 并入 rag-spine 记录（2026-09-20）

本项目已作为**独立顶层包**并入 rag-spine 仓库（`/Users/linhan/startup/spine/ragspine`，分支 `merge/enterprise-pdf-rag`），import 名仍是 `enterprise_pdf_rag`，不在 `ragspine.*` 命名空间下；决定、被拒方案与待办见 rag-spine 的 [ADR 0021](../adr/0021-merge-enterprise-pdf-rag-as-sibling-package.md)。四个提交：

| 提交 | 含义 |
|---|---|
| `0f8499c` | `git subtree` 导入：enterprise-pdf-rag 完整历史进入 `_incoming/enterprise-pdf-rag/`，原提交可 `git log --follow` 追溯 |
| `8ce3301` | 依赖与工具链全部升到最新，`requires-python>=3.12` 与 pdfspine 对齐 |
| `95f607e` | 纯格式：ruff 0.16 全仓 format，无行为改动 |
| `407849e` | 重排合并：搬到最终目录、单一 `pyproject.toml`、ruff/mypy 按目录分级 scope、一次 pytest 合跑、`scripts/ci.sh` 第 9 步接入四个 check 脚本 |

新路径速查（均相对 rag-spine 仓库根；文中 `src/enterprise_pdf_rag/...` 与 `data/...` 写法不变）：

| 原仓库 | rag-spine |
|---|---|
| `src/enterprise_pdf_rag/` | `src/enterprise_pdf_rag/`（不变） |
| `tests/` | `tests/enterprise_pdf_rag/`（自带 `conftest.py` 的 no_network 守卫） |
| `scripts/*.py`、`scripts/start.sh` | `scripts/enterprise_pdf_rag/` |
| `configs/settings.yaml` | `config/enterprise-pdf-rag/settings.yaml` |
| `benchmarks/aia-2026-interim/` | `data/benchmarks/enterprise-pdf-rag/aia-2026-interim/` |
| `deployment/open-webui/` | `deploy/enterprise-pdf-rag/open-webui/` |
| `docs/`（ADR、PRD、schemas、samples、本文件） | `docs/enterprise-pdf-rag/` |
| `CLAUDE.md`、`AGENTS.md` | `src/enterprise_pdf_rag/CLAUDE.md`、`src/enterprise_pdf_rag/AGENTS.md`（rag-spine“每个模块一份 CLAUDE.md”约定） |
| `./ci.sh`、`.github/workflows/ci.yml` | `bash scripts/ci.sh`（rag-spine 的 GitHub Actions 为手动触发，本地门是唯一真源；旧 workflow 未并入） |
| `data/` | `data/`（运行现场以 APFS 克隆带入，仍 gitignore） |

门的跑法（在 rag-spine 仓库根）：`bash scripts/ci.sh` 全门——第 5 步一次 pytest 同时收集两套测试，第 9 步顺序运行 `scripts/enterprise_pdf_rag/check_conformance.py .`、`check_architecture.py`、`check_schema.py`、`check_drift.py`；单跑本包 `.venv/bin/python -m pytest tests/enterprise_pdf_rag -q`；文档漂移 `.venv/bin/python scripts/check_doc_drift.py --quiet`（`src/enterprise_pdf_rag/CLAUDE.md` 带 `covers` 头，改包内代码后要把它的 `verified-against` bump 到新 HEAD）。

原仓库 `/Users/linhan/startup/enterprise-pdf-rag` 的 `merge/into-ragspine` 分支是并入前快照（HEAD `03357a4`），`data/` 原件（output、validation、samples、current 指针等）保留在那里，不要清理；rag-spine 侧的 `data/` 是其克隆。本文件及 `docs/enterprise-pdf-rag/` 下其余文档中的 `./ci.sh`、`scripts/xxx.py`、`tests/adapters/...`、`benchmarks/aia-...`、`deployment/` 等路径已按上表改写；仅“历史暂停快照 / 前 5 分钟”里以 `cd /Users/linhan/startup/enterprise-pdf-rag` 开头的命令块保留原仓库布局。

并入后已知待办（详见 ADR 0021）：`httpx` 与 `httpx2` 选型待统一；`fastapi/uvicorn` 同时在 base 与 `[service]` extra；`deploy/enterprise-pdf-rag/open-webui/backend.Dockerfile` 仍 `uv sync --locked`，容器内会找不到 `[tool.uv.sources]` 指向的本地 `../corespine`，需改 `--no-sources` 或等价方案，尚未验证。下一步的通用服务挂载与自然语言回答链改在 rag-spine 上实现（见下方“当前后续工作顺序”第 2、3 项）。

## 恢复开发记录（2026-09-19）

用户已恢复开发，要求项目面向通用 PDF；AIA 仅为公开验收样本。已在线核对 [AIA 官方业绩页](https://www.aia.com/en/investor-relations/overview/results-presentations) 与 README 中的官方 PDF，封面为 2026 年 8 月 20 日、71 页，原 PDF 不随公共仓库分发。README 已明确此边界，原批准 PRD、ADR、旧资产与旧快照仍保留。

本次服务验收中，8767 界面、8766 模型发现/原文/状态均为 200，当前仍是下文的 `a7384f…` / `f59d…`，241 objects、189 vectors、第 18 页 2 个 qualified claims。普通 OpenAI 兼容聊天只做来源审阅；财务问题 422。第 20 页 v2 尚不能因工作树有代码而宣称在线激活。

发现旧 app factory 没有注入 query embedder，因此已有索引仍不足以让在线 search 工作。本轮最小修复复用 `LocalEmbeddingAdapter` 和既有配置验证：app factory 读取独立 `EMBEDDING_BASE_URL/MODEL/API_KEY`；来源 app 将 adapter 传入 processing router。缺少/无效配置时来源读取继续，search 503；已知 provider 错误映射为不泄密的 503，不重试。只有 API 子进程继承三项 embedding 配置，Open WebUI 不继承任何模型 key。回环 URL 限制、索引 fingerprint/维度检查和财务资格 guard 保持。

离线 ASGI/transport 测试覆盖 query-only 一次调用、启动/读取零调用、缺失/部分/远端配置、服务失败、错模型/维度、context 回填、财务拒答及 launcher 凭证隔离。正式门通过后已按 owned stop/start 切换现有服务，active `.venv` 使用官方 pdfspine 0.11.0。恰好一次真实 query embedding 搜索返回 5 条命中，context、p18 typed lookup（72%、verified、字段引用）、确定性来源审阅聊天、API 状态和 UI/config 均 200；LLM、rerank、新文档 embedding 调用数为 0。运行与完整原始响应保存在 `data/validation/rollout-search-2026-09-19/`。两个 runner 退出后的独立只读检查确认 managed tunnel 和 owned API/WebUI 持续运行，marker/cwd 匹配；API 保留 embedding key，WebUI 与相关日志无该 key。原始 PDF 和两个 current 指针摘要前后相同，未推广第 20 页。完整通用自然语言回答、任意 PDF 的 UI 选择/发布仍未实现。

通用 `ingest --pdf ... --pages ... --stage ...` 入口与 `scripts/enterprise_pdf_rag/ingest.py` 薄封装已落地，首轮离线行为测试通过：完整 source 按 SHA 隔离、选页范围取真实页数、禁用 AIA 布局修正、共享调用预算、缓存可回放、始终 draft/no-index/no-activation。结果的 `retrieval_status` 明确 `not_ready`；这不是通用聊天已完成。独立只读 review 发现的共享 Table adapter 20 页上限已修复，第 21 页原生 table 的 merge/来源 span 回归由入口实现任务完成 RED→GREEN；AIA 专用入口仍保持 1–20 页范围。SDK [官方 PyPI 0.11.0](https://pypi.org/project/pdfspine/0.11.0/) 已发布；main/tag 指向 `5a1f22e`，[release run 35473565983](https://github.com/VoldemortGin/pdfspine/actions/runs/35473565983) 成功，5 wheels + sdist，并已完成 fresh Python 3.12 官方 pip 的 paint-profile API 检查。RAG 正式 `pyproject.toml` 与 `uv.lock` 已精确锁定 `pdfspine==0.11.0`，官方包独立环境完整 `bash scripts/ci.sh` 639 tests 通过（22.90s，格式 227 files、strict mypy 201 files、schema/architecture/drift 全绿）。fresh Python 3.12 plain `pip install .`、`pip check`、checkout 外 noneditable ingest/script/profile/render/app factory/HTTP 与 CI 的 installed smoke 步骤均通过；证据 `data/validation/generic-ingest-official/summary.json`。这些是本机验证，尚不代表 GitHub Linux CI 或真实 Databricks 部署。官方 macOS wheel 对真实样本第 18/20 页 native SVG 字节与候选一致，paint-profile API 完整；证据 `data/validation/official-sdk-0.11-source-check.json`，未用此代替 RAG 的 profile digest 序列化验证或 Linux 真 PDF 验收。以下“尚未发布/先修 viewBox/主动暂停”的条目是恢复前状态，不能重复执行或据此回滚。执行发布与激活前须检查对应任务的最终证据及当前 git/tag/PyPI/current 指针，不因本记录自动激活第 20 页。

通用 draft 的资格 / description-only 索引 / 发布入口已落地，按 `ingest` 返回的 store 根和 `processing_id` 工作，不再写死 AIA 文件名/页数/来源 SHA。新模块 `src/enterprise_pdf_rag/adapters/draft_publication.py`：`qualify_draft` 只读、零模型（`ProcessingStore.load` + `validate_processing_source` + 资格谓词统计）；`index_draft`（显式注入 embedder）用 description-only `ProcessingRetrieval.build` → `save_draft` 产新不可变 snapshot，`export_processing_review(update_current=False)` 不切指针，标题反映实际文档；`publish_draft(activate_source=True)` 原子切 `current-processing`，可选切 `current-manifest`，未 `index` 的 draft 抛 `ValueError`，内容寻址幂等。三 Boundary 模型 `DraftQualification/DraftIndex/DraftPublication` 把 ingest 的 `not_ready` 依次推进为 `qualified; indexing pending → indexed; publication pending → ready`。`processing_retrieval.py` 抽出模块级 `eligibility(record)` 由 `build` 与资格共用，行为不变。CLI 新增 `qualify|index|publish`（共享 `--source-store/--processing-store/--processing-id`，`index` 有 `--document-label`，`publish` 有 `--activate-source/--no-activate-source` 默认激活）；`index` 仅用生产 `LocalEmbeddingAdapter(load_local_model_config("embedding"))`，离线替身仅测试注入不暴露 flag；错误 → `{"error": ...}` + 退出码 1，fail closed。离线 E2E `tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py` 用非 AIA 程序化三页财务 PDF `meridian-semiannual.pdf` 走 ingest→qualify→index（`OfflineDescriptionEmbedder`，dims 64）→publish→`search`/`resolve`，命中带 snapshot_id/member_id、retrieval snapshot 的 `scope.source_manifest_id` 与 ingest 一致，另有 `cli.main` 三命令 JSON 状态推进 smoke；单元测试见 `tests/enterprise_pdf_rag/adapters/test_draft_publication.py`。阶段完成后 `bash scripts/ci.sh` 全绿（≥660 tests，最终数字以 `bash scripts/ci.sh` 为准）。真实冒烟（本机）：对真实 AIA store `data/output/aia-2026-interim`（processing 在 `pages-001-020`）的 `current-processing` `a7384f0c…` 只读 `qualify` 得 eligible=189、skipped=52（stage 未完 43、image 7、diagram 2）、chart=9、kinds Text163/List11/Group6/Chart9，与既有 189 vectors 一致；在 scratchpad 完整副本上 `publish` 两次幂等回到同一 `a7384f0c`（member_count 189、dims [2560]、retrieval snapshot `f59d2308…`、source_activated true、status ready）。**未覆盖项**：真实 `index` 未能在本地 embedder 上运行（`scripts/enterprise_pdf_rag/with_local_models.py` 报 `TunnelConfigurationError: Missing or invalid setting: LOCAL_MODELS_SSH_HOST`，当前 shell 无隧道配置），真实链路 index 需隧道环境；通用 `ingest` 亦从未对真实 PDF 跑过（`data/ingestion/` 不存在）。冒烟前后 `git status --short data/` 为空、两个 current 指针 shasum 不变；全部改动仍未 commit。这只是资格/索引/发布入口，**不等于通用 RAG 回答链完成**，OpenAI-compatible chat 仍只有来源审阅。

可复制 API 请求和完整能力边界见 [测试与入库指南](testing-and-ingestion.md)。

## 当前后续工作顺序

本轮 RAG 改动保持未 commit / 未 push；已发布的是 SDK 0.11.0。（2026-09-20 补记：这些改动已在原仓库以 `03357a4` 提交，并随上方四个提交并入 rag-spine；此后的工作在 rag-spine 仓库进行。）不要根据下方历史清单再次发布同一版本、重做已修复的 SVG smoke，或回退正式 lock。当前可测试范围以 [测试与入库指南](testing-and-ingestion.md) 为准。

优先按用户的通用文档目标推进：

1. （已完成 2026-09-19）为通用 `ingest_pdf` 的 draft 建立明确的资格、description-only 索引和发布入口，按返回的 store/manifest ID 工作，避免再写死 AIA 文件名、页数或来源 SHA。入口 `src/enterprise_pdf_rag/adapters/draft_publication.py` 与 CLI `qualify|index|publish`；证据见离线 E2E `tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py`、单元 `tests/enterprise_pdf_rag/adapters/test_draft_publication.py` 及上方恢复记录的真实 `qualify`/`publish` 冒烟（eligible=189、幂等回到 `a7384f0c`）。真实 embedder `index` 需隧道，属未覆盖。
2. （已完成 2026-09-20，代码未 commit）文档目录/选择与通用服务挂载：`adapters/document_catalog.py`（`scan_catalog`/`mount_document`/`mount_catalog`）、`adapters/http/documents.py` + `catalog_schemas.py`（契约 `document-catalog-v1`）、`app.py` 的 `document-catalog` 分支与 `APP_LEGACY_DOCUMENT_ROOTS`；决定见 [ADR 0011](adr/0011-document-catalog-and-verified-answer-chain.md)，证据 `tests/enterprise_pdf_rag/adapters/test_document_catalog.py`（15）、`test_documents_http.py`（14）、`test_document_catalog_aia_smoke.py`。真实 AIA 发布经 legacy root 在线挂载与真实 embedder search 的结论见顶部“真实模型验收”。
3. （已完成 2026-09-20，代码未 commit）证据链上的自然语言回答：`adapters/hybrid_search.py`（精确复用 `ragspine.retrieval.lexical.retrieval.{tokenize,bm25_scores,rrf_fuse}` 与 `ragspine.retrieval.rerank.listwise_rerank`，而非 `HybridRetriever`/agent 整体接入——理由见 ADR 0011 被拒方案）、`processing/context_builder.py`、`answers/`（models/prompt/verify）、`adapters/answer_service.py`（恰好一次 LLM + 逐字段校验 + 散文数值门）、`adapters/http/chat.py` + `chat_schemas.py`（契约 `rag-chat-v1`）、TABLE 成员逐字转写放行（`processing/table_transcription.py`，policy `-v2`）。离线已独立验收文本引用、表格单元格、图表值（donut/bar）的正例、拒答与损坏证据：`tests/enterprise_pdf_rag/answers/`（`test_verify.py` 12、`test_answer_service.py` 10）、`test_chat_http.py`（14）、`test_hybrid_search.py`（10）、`test_context_builder.py`（8）、`test_table_transcription.py`（3）。真实模型上的合成与校验结论见顶部“真实模型验收”；在此之前不称通用 RAG 聊天已验收。
4. （2026-09-21）重新冻结 NL 金标集到当前发布（`current-processing` 已漂移到 `4f6ce62fe0b7`，金标钉的是 `231c904c843e`），再跑离线与真实两个 runner —— ADR 0016 的中文路径要等这一步才算验过。顺带决定 region 前置过滤是否也用译文推导（会把 `k02` 从拒答变成作答）。
5. 第 20 页 v2 属于独立的待完成验收：仍未新增描述 embedding、未运行新 runtime 38-case 真 API 验收、未激活。若继续该切片，沿 ADR 0009 的独立评测和 atomic activation 门推进，不把两个样本事实当成通用图表能力。
6. 准备提交/推送时再次核对用户授权与实际工作树，运行既有完整门；GitHub Linux CI 和 Databricks 部署只有实际执行后才能标记完成。

## 历史暂停快照

> 以下 CI、发布、PyPI、服务与运行结果是恢复开发前的最后核查快照；保留用于追溯，不表示当前仍然如此。

## 前 5 分钟

先做只读检查，保留现有工作树和运行现场：

```bash
cd /Users/linhan/startup/enterprise-pdf-rag
git status --short
sed -n '1,220p' AGENTS.md
sed -n '1,260p' docs/CLAUDE_HANDOFF.md
sed -n '1,240p' docs/adr/0009-source-qualified-expense-ratio-bar-lookup.md
```

- 已完成边界：第 20 页 typed `chart-qa-v2` 实现、候选 proof/qualify/resolve、兼容检查与候选 SDK 隔离环境完整门；具体证据见下文。
- 当前第一项待做：仅修复 CI smoke 示例 SVG 缺失的 `viewBox="0 0 10 10"`，然后复验该 smoke。
- 发布前置条件：先确认 SDK exact-main CI 全部成功，再按既有 release 流程发布并验证官方 PyPI 0.11.0；官方包可用前，不得更新 RAG 正式依赖或继续激活流程。
- 尚未完成：官方 SDK 发布与锁文件升级、新 runtime 38-case 验收、一次新描述 embedding、atomic activation、服务切换、RAG commit/push 与 GitHub Linux CI。
- 本轮 RAG 改动仍未提交。不要清理、还原、覆盖或移动现有现场。

## 交接范围

- 项目：`/Users/linhan/startup/enterprise-pdf-rag`
- SDK：`/Users/linhan/startup/spine/pdfspine`
- RAG 仓库：https://github.com/VoldemortGin/enterprise-pdf-rag
- 本轮开发已主动停止；以下工作均未继续执行。
- 不要声称整个 PRD 或完整图表支持已经完成。

## 工程约束

- Python 3.12；本地使用 `uv`，Databricks 预期通过普通 `pip install .` 安装。
- 遵循 ADR + TDD。
- Ruff 可安全修复及格式化；`bash scripts/ci.sh` 是唯一离线、只读的完整质量门。
- 领域层仅用 stdlib、immutable 数据和 `Protocol`；外部 SDK 位于 adapters。
- `pdfspine` 是唯一 PDF parser。
- 普通迭代不得触发 LLM。
- 只处理 `data/samples/aia-group-2026-interim-results-presentation.pdf` 的物理第 1–20 页。
- 原始 PDF SHA-256：`df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e`。
- 不得覆盖原始资产、旧 proof 或旧 snapshot。
- 只允许独立的自然语言 description embedding；禁止 embedding ChartIR 或 SVG。
- 所有推断必须携带证据、置信度和验证，并 fail closed。

## Git 状态

- RAG 本轮代码全部未 commit、未 push。
- RAG `HEAD` 与 `origin/main` 均为 `4e3fd82b46af73aab8a571cdc5b6d1918dc4fb9f`。
- SDK 0.11.0 代码已 commit/push 到 `main`：`5a1f22e23e403897439ecae96e0f3a5e4073a27b`。
- SDK tree clean。

## 本轮 RAG 实现

- 新增第 20 页 Expense Ratio 的 typed `chart-qa-v2` lookup。
- 唯一允许的正向事实：`1H24 = 8.2%`、`1H26 = 6.9%`。
- `1H25` 必须返回 unavailable/refusal。
- 禁止跨期差额、箭头 `130bps` 推断和柱高估值。
- 保留原 ChartIR 全局 `period=None`。
- 新增独立 `PointPeriodInterpretation`，与 category 共享源 occurrence。
- 已保留字段引用、原页汇率 footer 引用和 description normalization receipt。
- v1 donut 行为保持不变。
- ADR：`docs/enterprise-pdf-rag/adr/0009-source-qualified-expense-ratio-bar-lookup.md`
- 阶段说明：`docs/enterprise-pdf-rag/chart-qa-bar-stage.md`
- Gold：`data/benchmarks/enterprise-pdf-rag/aia-2026-interim/chart-qa-bar-gold-v1.json`
- 已实现新 source profile、stroke/bar proof、8-lineage、存储 resolver、immutable draft/append/independent targets、v2 capture/evaluator。
- 两组交叉只读 review 均无阻塞问题。

## RAG 验证状态

- 候选 SDK 隔离环境：`data/validation/chart-qa-venv`
- 完整门已通过：616 tests、219 files Ruff/format、197 files strict mypy、架构/schema/drift。
- 运行候选完整门必须使用：

```bash
UV_PROJECT_ENVIRONMENT=/Users/linhan/startup/enterprise-pdf-rag/data/validation/chart-qa-venv UV_NO_SYNC=true bash scripts/ci.sh
```

- 原因：避免 `uv` 按旧 lock 将 SDK 降级。
- 正式 `pyproject.toml`/`uv.lock` 仍固定 `pdfspine==0.10.0`，尚未在官方锁环境验证。
- `.github/workflows/ci.yml` 已新增 Ubuntu 24.04、Python 3.12、uv 0.6.2 流程：locked 离线全门、独立 venv plain pip、checkout 外 `python -I` smoke。
- 该 workflow 在 616 测试通过后新增了实际 profile/render/app factory smoke。
- 当前明确待修：10x10 示例 SVG 缺 `viewBox`，`render_svg_png` 抛 `KeyError`。
- 修复方式：给示例 SVG 增加 `viewBox="0 0 10 10"`，然后复验。
- 尚无 GitHub RAG 新 CI 实跑，也未进行真实 Databricks 部署。

## SDK 0.11.0 状态

- 新增冻结 typed `Page.get_paint_profile()`：`strict-paint-profile-v1`。
- 覆盖 inherited Resources、native audit 和 fail closed；允许窄范围 opaque RGB page group。
- 本地验证：Rust 2065 pass / 1 ignored；Python 1574 pass / 68 skip；fmt、clippy、mypy、packaging 通过。
- 既有 vendor/deny warnings 仍存在，不可声称全 SDK 零 warning。
- 最终本地 wheel：`/Users/linhan/startup/spine/release-candidates/pdfspine-0.11.0-final-candidate/pdfspine-0.11.0-cp311-abi3-macosx_11_0_arm64.whl`
- Wheel SHA-256：`3595294a46a4c32259fd82539d40684bef49bca753e4abb0b1f04fa10bbb3788`
- 该 wheel 仅是 Mac 本地候选，不是官方公开包。
- GitHub Actions：https://github.com/VoldemortGin/pdfspine/actions/runs/35472046070
- 最后核查时仍 `in_progress`，仅剩 Windows Python 3.12/3.13，无失败。
- 本地和远端均无 `v0.11.0` tag；release 未启动；PyPI 0.11.0 尚为 404。
- 本地 watcher 已停止；远端 CI 未取消。

## 当前运行版本

- Processing：`a7384f0c2654d4a2d195e6f119e6e441ef08319af35a7655d8b7a9a099caa8d5`
- Snapshot：`f59d230869d5dac6981f0545286b4772a1a9770e63597350f1a19b1a14349703`
- 当前共有 241 objects / 189 vectors；第 18 页 donut 两个事实已 qualified。
- Review：http://127.0.0.1:8766/v1/processing/review/review.html
- UI：http://127.0.0.1:8767
- 两个地址此前均核验 HTTP 200。
- 现有服务未重启，current 未切换；未新增 LLM、embedding 或 rerank 调用。
- `current-processing` 文件 SHA-256：`c98a31f23ee1d54111352d8dfa290e0aa68e383a26aaa435ac48a3b3767f6baf`
- Review 文件：`data/output/aia-2026-interim/pages-001-020/review.html`
- 只使用 `scripts/enterprise_pdf_rag/start.sh` / `scripts/enterprise_pdf_rag/webui_preview.py` 的已有 owned-process 管理；不得 kill 外部服务。

## 第 20 页候选证据

- 本地 proof/qualify/resolve 已通过。
- 报告：`data/validation/bar-p020-candidate3/report.json`
- 来源字形、柱关系、footnote 均成立；尚未 embedding 或 activate。
- 兼容报告：`data/validation/bar-source-candidate3.json`
- 已验证第 18 页旧 v2 整套 proof 与 ID 精确相等，SVG bytes 不变。
- 尚未对真实 PDF 做跨 OS 验证。
- 原始 raw 必须保持不变。
- 仅以下两条独立原句可进入合格的描述/embedding 投影；原始及 normalization 审计资产中的其他 claims 保留，但不可进入本次索引或精确问答：
  - `Expense Ratio for 1H24: 8.2 %.`
  - `Expense Ratio for 1H26: 6.9 %.`

## 历史建议续跑顺序（已被顶部最新状态取代）

1. 阅读 `AGENTS.md`、ADR 0009、本文和 `git diff`。
2. 修复 CI smoke 的 SVG `viewBox`，复验对应 smoke。
3. 确认 SDK exact-main CI 全部成功；创建 annotated `v0.11.0` tag，必须准确指向 `5a1f22e` 并 push。
4. 等既有 `release.yml` 完成 5 wheels + sdist + PyPI；禁止 force，也禁止本地 twine 绕过检查。
5. 验证官方 PyPI 0.11.0；精确升级 RAG 的 `pyproject.toml` 和 `uv.lock`。
6. 运行 `make fmt`、完整 `bash scripts/ci.sh`、fresh Python 3.12 plain-pip smoke，以及 checkout 外 smoke。
7. 仅在上述全部通过后，用既有本地 embedding 配置调用一次新 description embedding；保持同 fingerprint、2560 维，原样复用 189 个旧 vectors，最终为 190。
8. 禁止新增 LLM 或 rerank 调用。
9. 执行 draft/save/source-aware independent targets，并跑真实 v2 gold 19 cases 与 v1 旧 19 cases 回归；旧 runtime 的 v1 此前 19 cases 已通过，新 runtime 合计 38 cases 尚未实跑。
10. v2 的 gold/targets/raw observations/report 四文件必须 content-addressed；分别验证 200 business refusal、422 unsupported、409/503 fault。
11. 不得靠全拒答获得虚假通过；fault stores 使用独立 copy-on-write，禁止 hardlink 后 truncate。
12. 全部通过后才 atomic activate，并用 owned restart；激活前 PDF 和 current 指针摘要须保持不变；激活时原子替换 current，旧不可变 snapshot/资产保持不变并验证 rollback。
13. 更新准确的阶段文档，然后 commit/push RAG，检查 GitHub Linux CI。

## 授权与边界

- 用户此前授权过 commit/push 和公开发布 SDK。
- 本轮已主动停止；待用户在 Claude 中恢复开发后，沿用已授权的范围继续；无需重复询问已授权的常规操作。
- 所有运行产物必须写入 `data/`，不要写入仓库根目录 `output/`。
- 不要在文档、提交或日志中写入任何密钥、私有 host 或凭证。
