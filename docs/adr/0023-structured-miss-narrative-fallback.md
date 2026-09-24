---
status: accepted
date: 2026-09-24
---

# ADR 0023 — Structured miss falls back to the narrative channel (anti-fabrication made precise)

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Refines [0001](0001-dual-channel-determinism.md) (dual-channel determinism) and the anti-fabrication
invariant in `CLAUDE.md` / `docs/invariants.md`. Keeps [0010](0010-intent-parser-security-decoupling.md)
intact: the deterministic security gate still runs first.

## Context

The rule-based intent parser sends any question with a numeric cue (`what was`, `how many`, `多少`…)
to the **structured** route. The fact table and metric vocabulary come from `CompanyProfile`, so a
real-world document outside the profile (an AIA interim-results deck in the nl-gold set) hits two
dead ends before the narrative channel ever sees the question:

- **Missing metric** (VONB, distribution mix… not in the profile's metric synonyms) → the
  clarification gate asks "想查询哪个指标？" (6 of 22 nl-gold cases).
- **Entity/metric not in the profile, or no fact row** → the default home entity is queried and the
  orchestrator rewrites the answer to "查不到：ROE / ACME_GROUP / …" (2 of 22).

The forced-narrative route (B) answers these from the same index, so the real ask route (A) trailed
it by 3–7 cases, all lost in routing. The old invariant — "structured channel returns no `found`
fact ⇒ rewrite to not-found" — was correct about *numbers* but too coarse about *routes*: it
refused to consult the channel that holds the evidence.

## Decision

Add a route fallback, switch `RAGSPINE_NARRATIVE_FALLBACK=on|off` (read by `answer_question` when its
`narrative_fallback=` keyword is `None`; **default `on`**; any other value raises `ValueError`).
When on **and a narrative retriever is injected**, a **structured**-route question that

1. lacks a metric (the gate's `ask_first`), reason `missing_metric`, or
2. yields no `found` fact (`not_found` / `unrecognized_param` / no tool call), reason
   `structured_no_hit`,

first runs the narrative channel on the raw question (same filters as the narrative route). The
fallback answer is accepted only if it is **grounded**:

- the narrative channel produced snippets and a model answer (empty retrieval / no retriever /
  provider error ⇒ not grounded);
- the answer does not contain the `NO_ANSWER` sentinel — the fallback's system prompt tells the
  model to output only `NO_ANSWER` when the snippets cannot answer;
- the answer contains **at least one number that appears verbatim in the retrieved snippet text**,
  not counting numbers already in the question or `[n]` snippet markers. Fallback only fires on
  numeric questions, so an answer with no number from the evidence is not an answer. This check is
  deterministic and does not trust the model.

A grounded fallback returns `route="narrative"`, `clarification.mode="none"`, `fallback=<reason>`,
the structured `tool_results` (kept for audit), and the narrative sources. Source citation is
forced exactly as on the narrative route. Otherwise:

- `structured_no_hit` → the **original structured result, unchanged** (the not-found /
  unrecognized rewrite);
- `missing_metric` → the answer becomes "查不到：资料中没有能回答该问题的依据，不提供任何推测数字。"
  followed by the original ask text. The `ask_first` clarification object (mode + metric options) is
  kept, so callers that branch on the mode still offer the metric choice.

### Anti-fabrication, stated precisely

Before: "no `found` fact ⇒ rewrite to not-found".
After: "no `found` fact (or no metric) ⇒ try the narrative channel. If it gives no grounded answer
(as defined above), answer not-found (for a missing metric, plus the original metric question)".
The model can never answer unsupported: an empty retrieval, a `NO_ANSWER`, or a number that isn't in the evidence all end in
the deterministic refusal, and a suppressed number never reaches `answer` / `answer_plain`.

Unchanged:

- **Structured `found` path** — numbers still synthesized from the fact value. The fallback never
  runs when any fact is found.
- **Competitor / out-of-scope refusal** is still the first early return, before any fallback,
  tool, retrieval or LLM call.
- **Composite and narrative routes**, multi-subtask partial hits, and every path when the switch is
  off or no retriever is injected: byte-identical.
- **Trace privacy** — the request trace gains `narrative_fallback={reason, grounded}` (codes only)
  only when a fallback was attempted; `fabrication_guard_triggered` is `False` when a grounded fallback
  replaced the rewrite.

### Intentional behavior change: "missing metric ⇒ ask first"

`agent/CLAUDE.md` used to say "missing metric → ask first; don't downgrade". That is now "missing
metric → narrative fallback first (never guess a metric). If that isn't grounded, say not-found and
repeat the metric question". A metric is still never assumed, and `clarify_scope` is unchanged. The
existing tests that assert "missing metric ⇒ ask" all run without a narrative retriever
(`tests/agent/test_agent_orchestrator.py::test_ask_first_returns_without_llm_call`,
`::test_structured_unrecognized_param`, `tests/agent/test_intent.py`, `tests/eval/test_qa_eval.py`).
Fallback needs a retriever, so no existing test had to change. The ACME QA ratchet (agent mode injects
a retriever) keeps `ask-001..004` at `ask_first` and `ref-001..007` refused, because the synthetic
narrative corpus holds no numbers and so no fallback is grounded. The ratchet stays at 1.0 with
fabrication 0. The eval metric reads the mode, not the text.

## Alternatives considered (rejected)

- **Widen the metric vocabulary / route by an LLM classifier** — profile-bound, and doesn't help
  documents outside the profile; the LLM parser seam (ADR 0010) stays available separately.
- **Accept any narrative answer with sources** — the MockProvider/echo case shows that sources
  alone don't mean an answer; a numeric question would be "answered" by an unrelated paragraph.
- **Trust the model's refusal wording only** — not deterministic; the number-in-evidence check is
  the code-level floor, and `NO_ANSWER` only adds a cheaper early exit.
- **Default off** — the user asked for this behavior, and default on leaves the QA ratchet
  unchanged.

## Consequences

- `answer_question(narrative_fallback=)` + `RAGSPINE_NARRATIVE_FALLBACK`; `AgentResult.fallback`;
  trace key `narrative_fallback`; nl-gold route label `fallback` (and `not_found` for a refused
  `ask_first`).
- A fallback costs one extra narrative retrieval + LLM call on structured misses.
- Numeric answers phrased only in words ("three stages") do not count as grounded. This is a
  known limit that errs toward refusal.
- Frozen by `tests/agent/test_narrative_fallback.py` (fallback on missing metric / out-of-profile
  entity / no hit; ungrounded ⇒ not-found; found path unchanged; competitor first; off ≡ old
  behavior; env parsing) plus the unchanged anti-fabrication / clarification / competitor suites.
