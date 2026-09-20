# Claude 项目入口

开始工作前依次阅读 [AGENTS.md](AGENTS.md)、[Claude 交接](docs/CLAUDE_HANDOFF.md)、[ADR 0009](docs/adr/0009-source-qualified-expense-ratio-bar-lookup.md) 和 [PRD v0.2](docs/PRD-v0.2.md)。

以交接文档顶部的最新状态、证据和“当前后续工作顺序”为优先。SDK 0.11.0 已正式发布；RAG 正式环境 639 tests、plain-pip 与一次真实在线搜索验收已通过，勿重走下方历史发布清单。后续优先通用索引/发布/回答链；第 20 页仍未激活。2026-09-19 已在用户授权下恢复开发；交接文档开头的恢复记录优先于下方保留的历史暂停快照。RAG 工作树中的实现尚未提交；保留全部工作树改动、运行现场、原始资产、proof、snapshot 和 current 指针，不要清理或覆盖。另见 [测试与通用入库](docs/testing-and-ingestion.md)，不要把来源审阅或 HTTP 200 当成完整通用 RAG 已完成。
