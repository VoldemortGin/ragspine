---
covers:
  - ragspine/agent/
verified-against: 3c6bf0b
---

# agent — agent contract

Auto-loaded when working under `ragspine/agent/`. Keep terse; deep dives go in
`ragspine/agent/docs/`.

## What lives here

Intent parsing, clarification gateway, tool-use loop, LLM provider abstraction.

- `agent.py` — **orchestrator**; sole public entry `answer_question()`. Routes
  narrative / structured / composite, runs the tool loop, applies the guards.
- `intent.py` — rule-based (no LLM) intent + scope parse and the clarification gate.
- `llm_provider.py` — `LLMProvider` Protocol, `AnthropicProvider` (SDK lazy-imported),
  `MockProvider` (offline, deterministic).
- `query_tools.py` — profile-driven `query_metric` tool schema + execution
  (`found` / `not_found` / `unrecognized_param` — never fabricates).

## Invariants

- **Anti-fabrication is per-path — do not unify the three:**
  - *structured* — on no `found` fact the model's text is **discarded** and the
    answer is deterministically rewritten to "not found" / "unrecognized"
    (`agent.py:191-218`). A model-invented number can never survive.
  - *multi-subtask* — never calls the LLM at all (`agent.py:248-297`).
  - *narrative* — trusts model prose but **forces source citation**
    (`agent.py:363-368`); no found-fact rewrite here. That asymmetry is deliberate.
- **No hardcoded company** — home identity / entities / tool schema all derive from
  `load_company_profile()` (`agent.py:50`, `query_tools.py:73-88`). Never backfill "ACME".
- **Privacy-aware traces** — `_TraceCtx` records metadata only (tokens, timings,
  chunk_id, scores), never answer / fact value / chunk text (`agent.py:84-93`).

## Read before editing

- **Out-of-scope entity must reject first.** `CLARIFY_OUT_OF_SCOPE_ENTITY` returns
  before any tool / retrieval / LLM call (`agent.py:444`); a competitor/external
  entity must never reach a channel. Don't reorder the early-returns.
- **External-entity masking is an invariant, not a cleanup.** Matched aliases are
  replaced with **equal-length spaces**, and home-entity matching runs on the masked
  text (`intent.py:131-147,261`). "Simplifying" this leaks competitor data via
  substring collisions (e.g. a masked competitor leaving `中国` → `ACME_CN`). Security.
- **Clarification asymmetry is deliberate.** Missing *metric* → ask first
  (`intent.py:309-315`); missing *entity/period* → answer with surfaced assumptions
  (`intent.py:321-348`). Don't downgrade metric-missing to "assume and answer".
- **`ProviderError` wraps only network / API / timeout errors** (`llm_provider.py:25-30`);
  program errors (KeyError/TypeError) must propagate. Never `except Exception` into a
  degrade path — it buries real bugs.
- **provider & retriever are Protocols; `agent.py` imports no SDK and no retrieval impl**
  (`llm_provider.py:57-78`, `agent.py:65-70`). The `anthropic` SDK is lazy-imported
  inside `AnthropicProvider` only.
- **Tool loop is capped** at `MAX_TOOL_ITERATIONS = 5` (`agent.py:43`); the SDK owns
  retry/backoff — don't add your own.

## Deep dives

Planned (`ragspine/agent/docs/`, not written yet):

- anti-fabrication — the three-path semantics + the `fabrication_guard_triggered` definition.
- clarification decision tree — the four `CLARIFY_*` states × structured/narrative/composite routing.
- external-entity masking & out-of-scope refusal (security-sensitive).
- provider abstraction & resilience boundary (Protocol + lazy import + honest degrade).
