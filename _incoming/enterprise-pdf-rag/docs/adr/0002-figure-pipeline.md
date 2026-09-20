# ADR 0002 — 单 figure 同 SVG 双路与固定快照上下文

状态：Accepted for the first vertical slice，2026-09-19。

## 决策

`FiguresService` 是当前用例入口；领域只依赖标准库，通过类型化 ports 注入解析后的 SVG、两路增强、embedding、仓库和索引。
同一个不可变 `SvgArtifact` 分别交给 `ChartExtractor.extract()` 与 `DescriptionGenerator.generate()`；描述生成器不能接收 ChartIR。
产物的 `SvgBinding` 必须完整匹配 figure、源 revision、SVG artifact ID 与内容摘要；字段 evidence 指向 SVG element，再回溯 PDF anchor。
第一纵切仅支持有明确文字标签的点值、系列、类别、单位与轴，不创建八类空模型或宣称通用图表理解。
ChartIR 与描述都不是独立真值；同源配对还必须验证字段证据、原始标签和资格状态。
仅通过验证的自然语言描述传入 `EmbeddingPort.embed_description()`；SVG、ChartIR 和结构序列化禁止编码。
`FigureBundle` 将不可变的 SVG、ChartIR、description、embedding 与 snapshot 绑定；描述命中通过这个绑定加载 ChartIR，绝不读取 latest 或回退仅摘要。
`pair()` 可以对已有 SVG、描述和新 ChartIR 重新配对；相同描述内容和 embedding fingerprint 可复用向量，旧 bundle 不变。
身份基于 schema 标签与不可变内容摘要，不读取时钟或随机数。

## 资格与边界

缺 SVG/ChartIR、source/snapshot 错配、证据不存在、未验证字段、描述数字或系列不符时 fail closed，抛出带原因的领域异常。
`estimated` 与 `unavailable` 永远不提供精确值；`derived` 在未实现可审核的计算 lineage 前同样不能冒充 explicit。
`unknown` grammar 是显式能力边界，可以保留有限且有依据的非数值描述，不据此推断精确数字。
producer 的 execution mode 显式记录；本切片未实现独立的生产来源资格，`production` 用例整体拒绝执行，即使调用方手工填写 verified 或 production 也不能放行，不做 provider fallback。
真实 SVG adapter 对 raster、复杂 clip、字体语义或导出完整性未确认时必须保留未通过资格，不能因存在合法 SVG 就自动合格。
结构化标签与 claim 校验不等于财务审计；已验证状态必须来自可信 adapter/审核流程，不能照抄模型自评。
`FigureQualificationProvider` 由组合根独立注入，资格凭据绑定完整 PDF anchor、SVG binding 和逐字段精确 element occurrence；领域检查完整字段集合，重复同名文字不能替代已确认的 occurrence。离线 adapter 仅为内部创作并独立核对位置的 fixture 登记凭据，不能按待验证的 ChartIR/描述反向生成凭据；`verified` 标记本身不授予资格。
解析命中时，bundle 自报的 figure/source 标识还必须等于实际加载的 SVG 标识，不能由相互一致的伪造 bundle 与 hit 掩盖。
当前数字描述只接受有证据支持的 `{series} for {category}: {value} {unit}.` 句式；非数字描述仅接受明确源标签转录。这是有限的可验证语法，不宣称具备任意自然语言的语义验证能力。

## 第一阶段范围

内存 adapters 仅用于离线演示与协议测试，不宣称具有 SQL CAS、跨进程发布事务、持久 job ledger 或生产权限体系。
当前服务先保存完整 bundle 依赖，再添加描述索引；缺失依赖时 resolver 仍 fail closed。完整 release 发布协议在后续阶段实现。
当前返回 `ReasoningView` 是含 ChartIR 和字段引用的已检查上下文，不是最终自由生成答案或隐藏思维链。

## 验证顺序

1. 一条公开用例测试：同 SVG 两路 → 只描述 embedding → search → 固定 snapshot hydrate ChartIR 与字段证据。
2. 逐项加入 missing ChartIR、错 SVG/source、错数字/系列、错误 snapshot 和资格不通过的反例。
3. 验证 estimated/unavailable 不伪造精确值、内容身份稳定、新 ChartIR 配对复用未变描述向量。

每次按 red → green → refactor 完成一个行为，不先写满全部测试；通过项目锁定环境执行。

已保留首个公开用例导入失败及后续错源、未验证字段、描述错配、SVG 来源映射、跨 snapshot、估计值与数字歧义的 red → green 过程。独立审查发现的伪造 bundle 来源与同名文字 occurrence 替换也各有先失败、修复后通过的公开用例测试；当前 `tests/figures/test_pipeline.py` 为 53 个离线行为测试。
通过锁定环境的 Ruff safe fixes/format 与 `mypy --strict` 检查；完整工程只读门由 `./ci.sh` 统一执行。
