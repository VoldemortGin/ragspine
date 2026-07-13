---
status: accepted
date: 2026-07-14
---

# ADR 0017 — Conversation history as generation-only context (never intent-parsing input)

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Constrained by [0005](0005-lean-core-experimental-isolation.md) (new capability beside the
byte-identical default path) and [0010](0010-intent-parser-security-decoupling.md) (deterministic
intent / security independent of any model-facing context). Serves the multi-turn DX gap surfaced by
the product layer (spinestudio ADR 0006).

## Context

`answer_question(question, store, provider, ...)` was single-shot and stateless — no conversation
history入参. When the product layer built multi-turn chat, the only way to give the model prior
context was to **splice history into the `question` string**. That poisons the deterministic intent
parser: a prior turn's answer number (e.g. `1320`) was misread as a period `FY1320`, silently
degrading a single-metric lookup into a wrong comparison — breaking deterministic routing *and*
anti-fabrication correctness. The product layer had to ship history injection as a **default-off**
switch, waiting for the engine to expose a first-class history入参 that keeps "parse input" and
"generation context" structurally separate.

## Decision

Add an optional `history: Sequence[tuple[str, str]] | None = None` keyword to `answer_question`
(each turn `(role, text)`, `role ∈ {"user","assistant"}`), threaded to the same param on the service
`/v1/ask` + `/v1/ask/stream` endpoints (`AskRequest.history`, same semantics). Hard invariants:

- **Default `None` ⇒ byte-identical.** `_history_messages(None) == []`; the tool-loop / narrative
  message sequences are unchanged when no history is passed. Frozen by a parametrized regression
  (`tests/agent/test_history.py::test_default_none_is_byte_identical` over structured-found /
  not-found / multi-subtask / narrative).
- **History never enters deterministic intent parsing.** The parser sees **only** the current
  `question`; `intent = parser.parse(question, …)` and the security gate re-screen the raw question.
  History is converted to OpenAI-shape messages and **inserted between the system prompt and the
  current user turn** (`_history_messages` → `_run_tool_loop` / `_run_narrative`), so the current
  question stays the **last** user message (MockProvider's intent parse reads the last user turn,
  unpolluted). The public shape is a flat `(role, text)` tuple — it structurally **cannot** carry a
  `system`/`tool` role or smuggle `tool_calls`; unknown roles normalize to `user`. This is the
  structural separation of "parse input" from "generation context" that fixes the product痛点.
- **Anti-fabrication survives history.** The structured channel still synthesizes the answer
  deterministically from tool facts (found) or rewrites to not-found — history text produces **no new
  evidence**; provenance still points only at real retrieval/tool hits. Negative tests: a fabricated
  "fact" in history (`上海 FY2099 REVENUE = 999`) leaves a KB-miss answer as a refusal that never cites
  `999`, with empty `sources`; the narrative retrieval **query** stays the current question only, so
  history never enters retrieval.
- **RESTRICTED double-exit isolation is inherited unchanged.** History adds only generation-context
  messages; it never touches retrieval, so the `link/` + `rerank/` exits still strip RESTRICTED.
  Bound by parametrized conformance over both history forms
  (`tests/conformance/test_history_isolation.py`) with a non-vacuous leaky-retriever reverse-proof.

Signature stays within the "sacred signature" budget (ADR 0012): history is keyword-only with a
default, so first-answer上手成本 is unchanged.

## Alternatives considered (rejected)

- **Keep splicing history into `question`** (the status quo): the exact defect being fixed — it
  poisons deterministic parsing and anti-fabrication.
- **Accept arbitrary `list[dict]` OpenAI messages as history**: lets a caller inject a `system` role
  or `tool_calls` and smuggle fabricated tool results into the guarded path. The flat `(role, text)`
  tuple is deliberately un-smuggleable.
- **Augment the intent parser with history for coreference/ellipsis resolution**: reintroduces the
  poisoning path. Deterministic parsing must depend only on the current question (ADR 0010);
  slot carry-forward already lives separately in the opt-in `ConversationSession` skeleton.
- **Feed history into the narrative retrieval query**: history would become a new evidence source,
  breaking provenance. Retrieval query stays the current question only.

## Consequences

- The engine exposes a first-class multi-turn context入参; the product layer can flip history
  injection on by default without risking deterministic routing or anti-fabrication.
- All four code-level invariants (byte-identical default, parse/context separation, anti-fabrication,
  RESTRICTED isolation) hold with history and are bound by tests rather than trusted.
- New artifacts: `answer_question(history=)` + `_history_messages` helper, `AskRequest.history`
  passthrough on `/v1/ask[/stream]`, `tests/agent/test_history.py`,
  `tests/conformance/test_history_isolation.py`, `tests/service/api/test_api_ask_history.py`.
- `ConversationSession` (W6c slot carry-forward skeleton) is untouched — orthogonal, and could later
  build its `history` list from remembered turns to feed this seam.
