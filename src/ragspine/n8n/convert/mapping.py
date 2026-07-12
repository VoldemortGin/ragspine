"""数据驱动的节点/算子映射表（集中一处，便于扩展；纯常量，零逻辑）。

n8n 节点类型 ↔ dify 节点类型、lmChat 型号 ↔ provider、比较算子、输出字段对照、
dify→n8n 新建节点的默认 typeVersion，全部收在这里。convert 段只查表不硬编码。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# n8n → dify 节点类型。
# ---------------------------------------------------------------------------

# 触发器统一映射为 start。
TRIGGER_TYPES: frozenset[str] = frozenset({
    "n8n-nodes-base.manualTrigger",
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.executeWorkflowTrigger",
})

NOOP_TYPE = "n8n-nodes-base.noOp"

# lmChat 系列（独立出现时映射为 llm；经 ai_languageModel 挂到 agent 时并入宿主）。
LMCHAT_PREFIX = "@n8n/n8n-nodes-langchain.lmChat"

# 未知 n8n 类型的落点：自定义 dify type（dify parse/lower 不拒未知 type）。
PASSTHROUGH_DIFY_TYPE = "n8n-passthrough"

N8N_TO_DIFY_TYPE: dict[str, str] = {
    "n8n-nodes-base.manualTrigger": "start",
    "n8n-nodes-base.webhook": "start",
    "n8n-nodes-base.executeWorkflowTrigger": "start",
    "n8n-nodes-base.if": "if-else",
    "n8n-nodes-base.switch": "question-classifier",  # 近似，出 warning
    "n8n-nodes-base.code": "code",
    "n8n-nodes-base.set": "template-transform",
    "n8n-nodes-base.editFields": "template-transform",
    "@n8n/n8n-nodes-langchain.agent": "llm",
    "@n8n/n8n-nodes-langchain.openAi": "llm",
}

# ---------------------------------------------------------------------------
# dify → n8n 新建节点（无 _n8n 可还原时）：type + 合理默认 typeVersion。
# ---------------------------------------------------------------------------

DIFY_TO_N8N_TYPE: dict[str, tuple[str, int | float]] = {
    "start": ("n8n-nodes-base.manualTrigger", 1),
    "if-else": ("n8n-nodes-base.if", 2.2),
    "question-classifier": ("n8n-nodes-base.switch", 3.2),  # 近似，出 warning
    "code": ("n8n-nodes-base.code", 2),
    "llm": ("@n8n/n8n-nodes-langchain.agent", 1.7),
    "template-transform": ("n8n-nodes-base.set", 3.4),
    "answer": (NOOP_TYPE, 1),
    "end": (NOOP_TYPE, 1),
}

WEBHOOK_TYPE_VERSION: int | float = 2  # 备查：webhook 节点新建时的默认版本

# ---------------------------------------------------------------------------
# lmChat 型号 ↔ provider。
# ---------------------------------------------------------------------------

LMCHAT_TO_PROVIDER: dict[str, str] = {
    "lmChatAnthropic": "anthropic",
    "lmChatOpenAi": "openai",
    "lmChatGoogleGemini": "google",
}

PROVIDER_TO_LMCHAT: dict[str, tuple[str, int | float]] = {
    "anthropic": ("@n8n/n8n-nodes-langchain.lmChatAnthropic", 1.3),
    "openai": ("@n8n/n8n-nodes-langchain.lmChatOpenAi", 1.2),
    "google": ("@n8n/n8n-nodes-langchain.lmChatGoogleGemini", 1),
}
# provider 不在表中 → 兜底 lmChatOpenAi。
DEFAULT_LMCHAT: tuple[str, int | float] = PROVIDER_TO_LMCHAT["openai"]

# ---------------------------------------------------------------------------
# 输出字段对照（变量引用换算用）：dify 节点类型 → 默认输出字段。
# n8n agent 输出字段 "output" ↔ dify llm 输出字段 "text"。
# ---------------------------------------------------------------------------

DIFY_OUTPUT_FIELD: dict[str, str] = {
    "llm": "text",
    "template-transform": "output",
    "code": "result",
    "knowledge-retrieval": "result",
}
N8N_AGENT_OUTPUT_FIELD = "output"
DIFY_LLM_OUTPUT_FIELD = "text"
DEFAULT_OUTPUT_FIELD = "output"

# ---------------------------------------------------------------------------
# 比较算子：n8n if operator.operation ↔ dify comparison_operator。
# ---------------------------------------------------------------------------

N8N_TO_DIFY_OPERATOR: dict[str, str] = {
    "equals": "==",
    "notEquals": "!=",
    "larger": ">",
    "gt": ">",
    "largerEqual": ">=",
    "gte": ">=",
    "smaller": "<",
    "lt": "<",
    "smallerEqual": "<=",
    "lte": "<=",
    "contains": "contains",
    "notContains": "not contains",
    "startsWith": "start with",
    "endsWith": "end with",
    "exists": "not empty",
    "notExists": "empty",
}

# dify comparison_operator → (n8n operator.type, operator.operation)。
DIFY_TO_N8N_OPERATOR: dict[str, tuple[str, str]] = {
    "==": ("string", "equals"),
    "!=": ("string", "notEquals"),
    ">": ("number", "gt"),
    ">=": ("number", "gte"),
    "<": ("number", "lt"),
    "<=": ("number", "lte"),
    "contains": ("string", "contains"),
    "not contains": ("string", "notContains"),
    "start with": ("string", "startsWith"),
    "end with": ("string", "endsWith"),
    "not empty": ("string", "exists"),
    "empty": ("string", "notExists"),
}
