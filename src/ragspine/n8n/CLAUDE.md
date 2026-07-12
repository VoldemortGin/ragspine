# CLAUDE.md — ragspine.n8n（子包宪章）

n8n workflow JSON ↔ Dify DSL 双向转换器。先读仓库根 `CLAUDE.md`；本文件是本子包的操作指南。

## 这是什么

把 n8n 导出的 workflow JSON 转成可直接喂给 `ragspine.dify` 编译器的 Dify DSL dict（反向亦然）。
公开门面（api.py）：`n8n_to_dify` / `dify_to_n8n`，返回 `(目标格式 dict, warnings)`；
str 入参按 JSON/YAML 自动识别；失败一律抛 `N8nConvertError`（code="n8n.convert"）。

## 布局

```
parse/    dict/JSON/YAML → N8nWorkflow（pydantic v2 边界，extra='allow'；本域 pydantic 仅此一段；
          另含 load_dify_document：dify 入参侧的 DifyDoc 只读校验）
convert/  mapping.py（数据驱动映射表）/ variables.py（表达式双向转换）/ to_dify.py / to_n8n.py
errors.py N8nConvertError（继承 corespine.CorespineError）
```

## 节点映射表（详表在 convert/mapping.py，改映射只改那一处）

| n8n | dify | 备注 |
|---|---|---|
| manualTrigger / webhook / executeWorkflowTrigger | start | 反向 → manualTrigger |
| if | if-else | true/false ↔ 端口 0/1 |
| switch | question-classifier | 近似 + warning |
| code | code | python↔python3；JS 原文保留 + warning |
| agent / openAi / lmChat*（独立） | llm | 反向 → agent（model 非空另产配套 lmChat + ai_languageModel 连接） |
| set / editFields | template-transform | |
| noOp | （splice 剔除） | 反向：answer/end/未知 dify type → noOp + notes |
| 其余未知 type | "n8n-passthrough" | 原始数据存 data._n8n + warning |

输出字段换算：n8n agent "output" ↔ dify llm "text"；template-transform→"output"；code→"result"。

## 铁律

- **round-trip 无损机制**：正向每个 dify 节点 `data._n8n` = 原始 n8n 节点完整 dict（attachment
  存 `_n8n.ai_attachments`），workflow 级其余键存顶层 `x_n8n`；合成 start/end 带
  `_n8n.synthetic` 标记，反向直接剔除。反向优先按 `_n8n` 还原，无 `_n8n` 才走映射表新建。
- **绝不静默丢弃**：任何无法语义映射处（未知类型 / 转不动的表达式 / 近似映射 / splice）都出
  warning；表达式转不动整值原样保留。
- 仓库 `python-project-standard`：mypy `--strict` 零警告、`filterwarnings=error` 下零 warning、
  pydantic 只在 parse 段、PyYAML 延迟 import、TDD、离线确定性（零网络、不依赖 [llm]）。

## 跑（始终从仓库根）

```bash
.venv/bin/python -c "import ragspine.n8n"     # 期望 import-clean
.venv/bin/python -m pytest tests/n8n -q       # 子包测试 GREEN
```
