---
status: accepted
date: 2026-09-27
---

# ADR 0028 — Per-call, per-stage LLM telemetry in the request trace

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Extends the privacy-aware trace ([invariants](../invariants.md#privacy-aware-traces)) and the provider-layer
truncation retry (`agent/truncation.py`). Onboarding budget ([0012](0012-onboarding-complexity-budget.md)) and the
anti-fabrication guards ([0023](0023-structured-miss-narrative-fallback.md), [0024](0024-narrative-number-guard.md))
are unchanged: no answer changes, `answer_question` keeps its signature.

## Context

The request trace only knew two LLM call sites. `_TraceCtx.record_provider` summed `provider_seconds` and in/out
tokens for the tool loop and narrative synthesis. Every other `provider.chat` inside a request — query decomposition
and Adaptive classification, HyDE / RAG-Fusion / step-back, query translation, listwise rerank — was invisible.
Decomposition ran **before** the request context existed, so its time, tokens and truncation counts were lost, and a
decomposed parent request emitted no trace at all. The truncation counters (`TruncationStats`, bound through a
`ContextVar` by `answer_question`) were a second, parallel mechanism for the same calls. The privacy gate only
checked top-level keys, so a nested `llm_calls[0].prompt` would have passed.

## Decision

### 1. Collector (`common/observability/llm_calls.py`, stdlib only)

- **Bucket.** `record_llm_calls()` opens a per-request bucket (a `with`, or a decorator — each call gets a fresh
  one). `answer_question` is decorated with it (`functools.wraps` keeps the signature; the ADR 0012 signature test
  still passes). A nested bucket is isolated from the outer one until it exits, and its stage starts again from
  `other` (an `ask` called inside an outer stage does not inherit it). No bucket ⇒ everything is silent.
- **Stage label.** `llm_stage(stage)` (a `with`, or a decorator) sets the stage for the calls inside it. The stage is
  a **closed enum** `STAGES`: `decompose`, `classify`, `hyde`, `rag_fusion`, `step_back`, `translation`,
  `listwise_rerank`, `tool_round`, `synthesis`, and `other` for unlabelled calls. An unknown value raises
  `ValueError` when the decorator / `with` is **built**, not when a call happens. Wired call sites:
  `LLMQueryDecomposer.decompose`, `LLMComplexityClassifier.classify`, `HyDERetriever._hypothetical_document`,
  `RAGFusionRetriever._variants`, `StepBackRetriever._step_back_question`, `LLMQueryTranslator.translate`,
  `ProviderListwiseJudge.judge` (decorators), and the two inline calls in `agent.py` (`tool_round`, `synthesis`),
  where the `with` wraps **only the `provider.chat` line** — retrieval inside synthesis keeps its own stages.
  Graph summarize / answer (outside `answer_question`) and ingest-time calls (graph extract, RAPTOR) are not wired.
- **Provider probe.** `instrument_llm_call` decorates a provider's `chat`: it times the call, opens a probe, and
  appends one frozen `LLMCall` when the call ends. Wired: `AnthropicProvider`, `MockProvider` (its `chat_stream`
  calls `chat`, so it counts once), `LiteLLMProvider`, `ClaudeCliProvider`. Inside a call the provider adds detail
  with `note_attempt(truncation=)`, `note_truncated()`, `note_reasoning_disabled()`; with no probe they are no-ops.
  When a probe is already open (a forwarding wrapper around a decorated provider, or a subclass that decorates its
  override and calls `super().chat`) the inner call passes straight through, so a call is recorded once.
  Forwarding wrappers (`CountingProvider`, corespine `RateLimitedProvider`) are not decorated; the inner provider
  records. **A provider whose `chat` is not decorated produces no entries.** `chat_stream` is not instrumented:
  streamed calls are not counted (MockProvider's stream counts only because it calls `chat`).
- **Never raises, never changes the call.** Collector failures are swallowed. The decorated `chat` returns the same
  object and re-raises the same exception (type, args, attributes, traceback origin). Without a bucket the cost is
  one `ContextVar` read (frozen by an identity test and a coarse 10k-call overhead test).

### 2. Trace schema

Only when the request made at least one LLM call — a zero-LLM request's trace is byte-identical:

- `llm_calls`: list of `{stage, ms, attempts, trunc_retries, retried, truncated, reasoning_disabled, in_tokens,
  out_tokens, error}`. `attempts` counts requests actually sent (truncation retries, CLI format retries, litellm's
  BadRequest re-send); `trunc_retries` counts the truncation retries among them; `retried = attempts > 1`;
  `truncated` = still cut after the retries; `reasoning_disabled` = the final successful request carried the
  reasoning-off parameters; tokens come from `ChatCompletion.usage` (`null` when absent); `error` is a **closed
  enum** `""` / `provider.error` (any other `provider.*` code maps here) / `provider.truncated` / `error` (any other
  exception).
- `llm_n_calls`, `llm_n_retried`, `llm_ms`. `llm_ms` is the **sum** of call durations, not wall-clock time.
- **Truncation keys are derived.** `TruncationStats` and `bind/unbind_truncation_stats` are removed;
  `retry_on_truncation` writes to the probe. `llm_truncation_retries = Σ trunc_retries` and
  `llm_truncated_final = Σ truncated`, still present only when either is non-zero. **Behavior change:** a
  truncation during decomposition is now counted too.
- **Decomposed parent trace.** With a decomposer that splits into >1 sub-questions, each sub-question's
  `answer_question` opens its own bucket and emits its own request trace. The parent then emits one more trace:
  `request_id`, `route="decomposed"`, `n_subquestions` and the `llm_*` fields of the calls left in the parent
  bucket (decompose, plus Adaptive's classify) — each call counted exactly once. It is emitted only when the parent
  bucket has calls (a zero-LLM decomposer adds no trace) and it has no `tool_status_counts`, so consumers that count
  requests by that key (`ragspine batch`) are unaffected. With one sub-question, decompose lands in the single
  request trace next to the main flow's calls.
- `provider_seconds` / `token_usage` keep their old meaning (tool loop + synthesis only) because `ragspine batch`
  reads them. `llm_*` is the full count.
- **Not recorded:** the model name (constant per request; `openai/<self-hosted>` names may expose internal
  deployments — it belongs in run settings, never in the trace) and prompt size (`in_tokens` already shows input
  scale; a `prompt_*` key sits next to the forbidden keys). If a size is ever needed it is `in_chars`, an int.

### 3. Privacy gate

- `enforce_trace_privacy` (ragspine `common/observability/sink.py`) now checks **recursively** and **fails
  closed**. Mapping keys, dataclass instances (field names as keys) and NamedTuples (`_asdict()`, field names as
  keys) are checked key by key; list / tuple elements are walked as `x[i]`, set / frozenset elements as `x{*}`.
  Leaves may only be `str` / `bytes` / `int` / `float` / `bool` / `None` (so `StrEnum` / `IntEnum` members pass as
  their base type); **any other object** — a plain `Enum`, `SimpleNamespace`, an arbitrary object, a dataclass
  class — raises `TraceError` with its path. Paths read like `llm_calls[0].prompt` or `x{*}.prompt`. Top-level checks
  and their message are unchanged. More than `MAX_TRACE_DEPTH = 8` container levels (counting the top-level payload)
  is rejected as **suspicious** — never truncated and let through; a cycle hits the same limit.
- `emit_trace` runs it through ragspine's `InProcessPrivacyTraceSink`, a same-name subclass of corespine's that
  runs the recursive gate first (the name the package has always exported, so `isinstance` against either class
  still holds). The `in_process` registry sink is that class and `OtelTraceSink` calls the gate itself; a
  third-party entry-point sink is held to the same rule by the conformance pack
  (`tests/conformance/test_trace_sink.py`), which adds nested-leak payloads (a dataclass among them) and a
  reverse-proof stub (a top-level-only gate) that must fail.
- Keys alone cannot stop body text under a harmless key (`stage="<prompt>"`), so entries are also **value
  constrained**: built only from the frozen `LLMCall` dataclass, whose `stage` / `error` must be enum members.
- corespine's own `InProcessPrivacyTraceSink` still checks only top-level keys. Making it recursive (and delegating
  ragspine's gate to it) is a follow-up in corespine.

### 4. Concurrency rule

Today nothing inside a request uses a thread pool (only `ragspine batch` does, one request per worker thread, each
opening its own bucket). New threads do not inherit `ContextVar`s. If a request ever fans out to a pool, **each task
must run under `copy_context().run(...)`** to land in the request's bucket; bucket appends are locked. Both rules
are frozen by tests (a bare pool records nothing; concurrent appends lose nothing).

### 5. Consumers

`ragspine batch` (ask mode) concatenates `llm_calls` from every trace on the question's thread (including the
decomposed parent trace) into each result's `trace` (`llm_calls`, `llm_n_calls`, `llm_n_retried`, `llm_ms`), and the
summary gains an "LLM 调用（按阶段）" table: count, per question, mean / max ms, share of Σ`llm_ms`, retry rate, plus
a total line comparing Σ`llm_ms` with the summed end-to-end latency and noting that `llm_ms` is a sum.

## Consequences

- One mechanism for per-call counts; the truncation keys can no longer drift from the call log.
- Every LLM call in an `ask` request is attributed to a stage; a test drives all wired stages through one request
  and the real `RAGSpine.ask` path and asserts no `other`.
- Internal API change: `TruncationStats`, `bind_truncation_stats`, `unbind_truncation_stats` are gone (only
  `agent.py` and tests used them).
- A third-party provider must decorate its `chat` with `instrument_llm_call` to be counted.

## Alternatives considered

- **Infer the stage in the provider** (system-prompt prefix, presence of tools) — rejected: couples every prompt to
  the provider and drifts on each prompt change.
- **`with llm_stage(...)` at every call site** — same `ContextVar`, but re-indents nine call sites; the decorator
  form changes one line per method. The two inline calls in `agent.py` use `with`.
- **Keep `TruncationStats` next to the new collector** — rejected: two counters for the same calls.
- **Record the model name / `prompt_chars`** — rejected, see §2.
- **Make corespine's sink recursive in this change** — deferred: needs a cross-repo release; ragspine's gate runs
  first on every exit it owns.
