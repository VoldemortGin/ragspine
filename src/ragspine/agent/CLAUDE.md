---
covers:
  - src/ragspine/agent/
verified-against: 104273dda402f24b709cd7fcf68a59a18dfea38f
---

# agent — agent contract

Auto-loaded when working under `src/ragspine/agent/`. Keep terse; deep dives go in
`src/ragspine/agent/docs/`. References below name **symbols** (not line numbers) so they
survive refactors.

## What lives here

Intent parsing, the deterministic security gate, clarification gateway, tool-use
loop, LLM provider abstraction.

- `agent.py` — **orchestrator**; sole public entry `answer_question()`. Routes
  narrative / structured / composite, runs the tool loop, applies the guards.
  Takes an injectable `intent_parser` (defaults to `RuleIntentParser`). Optional
  `history: Sequence[tuple[str, str]] | None` (ADR 0017, default `None` ⇒ byte-identical):
  `(role, text)` turns are converted by `_history_messages` and **inserted between the system
  prompt and the current user turn** — pure generation context. History **never** enters intent
  parsing (parser sees only the current `question`; it stays the last user message), produces no
  new evidence (anti-fabrication + provenance unchanged), and never touches retrieval.
  **Image+text context (opt-in):** when narrative snippets carry a `page_image` ref (added by
  `retrieval/page_images`, `RAGSPINE_PAGE_IMAGES=on`), `_run_narrative` appends `图：pN.png` after that
  snippet's text and sends the user turn as parts `[text, image…]` (names `p{page}.png`, `-2`/`-3` on
  collisions, each part carries `doc_id` + `page`). A provider without `supports_image_input` gets the plain
  string (byte-identical to no images) and the request trace records `page_images={sent, dropped,
  dropped_reason="provider_no_image_input"}` — counts only, never paths. No `page_image` refs ⇒ no trace key.
- `intent.py` — rule-based (no LLM) intent + scope parse and the clarification gate.
  Exposes the `IntentParser` Protocol + default `RuleIntentParser`; `clarify_scope`
  delegates the out-of-scope decision to the `SecurityGate`.
- `security_gate.py` — **deterministic, never-pluggable** security front door
  (ADR 0010): external/competitor longest-match + masking + out-of-scope refusal.
  Zero LLM, config-driven (external list + home name from the profile).
- `llm_provider.py` — `LLMProvider` Protocol, `AnthropicProvider` (SDK lazy-imported),
  `MockProvider` (offline, deterministic). Image parts: a user message `content` may be a list of
  `{"type":"text"}` / `{"type": IMAGE_PART_TYPE ("image"), "path", "name", "doc_id", "page"}`; only a provider
  declaring `supports_image_input = True` (`provider_supports_images`) ever receives one;
  `split_message_content` → `(text, image parts)`. Wrappers must forward the flag (eval `CountingProvider` does;
  corespine `RateLimitedProvider` does not → text-only, traced). A query-translation request (system prompt starting
  with `QUERY_TRANSLATION_PROMPT_PREFIX`, `retrieval/translation`) gets the query back unchanged from `MockProvider`,
  so offline runs never add a translated query.
- `citations.py` — **narrative citation suffix merge (ADR 0029)**: `merge_citation` (pure, no retrieval import)
  groups the `（资料来源：…）` entries per document — pages / slides deduped into `page=5-6, 14`, other locators
  verbatim after them, one distinct locator printed as before; `resolve_citation_merge` /
  `RAGSPINE_CITATION_MERGE` (default `on`, off ⇒ byte-identical). Display only: `sources` stays per snippet.
- `claude_cli_provider.py` — `ClaudeCliProvider`: eval-only provider that shells out to the local
  `claude -p` CLI (subprocess, no SDK; binary resolved lazily at call time). Each call runs in a
  fresh empty cwd with `--setting-sources ""` (the flag that keeps the user's global
  CLAUDE.md / settings `language` out — `--safe-mode` does not), `--tools ""`, no MCP / skills /
  session persistence. Tool calling is **prompt-emulated** (JSON protocol + validation + bounded
  retries); no streaming, no sampling params. Timeout / non-zero exit / `is_error` → `ProviderError`.
  **Reads images** (`supports_image_input = True`): image parts are copied into the fresh cwd under their
  validated single-segment `name` (`^[A-Za-z0-9][A-Za-z0-9._-]*\.png$`, unique), the call switches to
  `--tools Read` + `--permission-prompts none` (reads inside cwd need no approval; anything outside needs one and
  is auto-denied — deliberately **not** `--allowedTools Read`, which would pre-approve any path), and the prompt
  gets a one-line hint naming the relative files. Original storage paths never reach prompt / argv. No images ⇒
  the command line is exactly the old one. **Truncation:** the json result has no usable stop reason (it reads
  `stop_sequence` even when cut); the signal is a non-zero exit with `is_error` and `result` "…exceeded the N output
  token maximum…". The retry sets env `CLAUDE_CODE_MAX_OUTPUT_TOKENS` and adds `--settings
  '{"alwaysThinkingEnabled": false}'` (thinking counts against the output budget; `MAX_THINKING_TOKENS=0` does not
  work under `-p`). The CLI default of 32000 is above the default cap, so by default a cut raises
  `TruncatedOutputError` with no retry.
- `litellm_provider.py` — `LiteLLMProvider`: OpenAI-compatible models through `litellm.completion` (`[litellm]`
  extra; model names in litellm form — default `DEFAULT_LITELLM_MODEL = "deepseek/deepseek-chat"`, `openai/<model>` +
  `api_base`, `azure/…`, `ollama/…`; keys come from each vendor's env var). litellm is imported on the **first call**
  (`_load_litellm`; missing ⇒ `ImportError` naming the extra), which also turns off telemetry, clears every callback
  list, sets `turn_off_message_logging` / `suppress_debug_info` and forces the bundled cost map
  (`LITELLM_LOCAL_MODEL_COST_MAP=True`, no import-time fetch). Native OpenAI tool calling (tools passed through,
  `tool_calls` mapped to corespine `ToolCall`, empty arguments ⇒ `"{}"`); `chat_stream` yields text deltas.
  `LITELLM_EXCEPTION_TYPES` ⇒ `ProviderError`, program errors propagate; retries are litellm's `num_retries`, plus a
  `max_concurrency` semaphore (default 8). **Images are declared, not detected**: `image_input=` (default `False`) sets
  `supports_image_input`; `litellm.supports_vision` is deliberately not used (misses `openai/<self-hosted>` models and
  would force the import). When on, image parts become a filename text part + a base64 `image_url`; when off, a part
  list is flattened to its text. `from_env` reads `RAGSPINE_LITELLM_MODEL` / `_API_BASE` / `_IMAGE_INPUT` (same keys as
  the `ServiceConfig` fields). **Truncation:** `finish_reason="length"` ⇒ retry with `max_tokens` doubled (base: the
  `max_tokens` argument, else the call's `usage.completion_tokens`) plus `reasoning_effort="none"` + `drop_params=True`;
  a `BadRequestError` on that request ⇒ the same request is sent again without the two params, and this instance stops
  sending them. `chat_stream` does not retry.
- `openai_compat_provider.py` — `OpenAICompatProvider(config: LLMConfig, *, timeout, max_tokens, image_input, sender)`:
  stdlib HTTP straight to an OpenAI-style `/v1/chat/completions` (no openai SDK), `provider_type="openai"`. Reuses the
  evidence-chain transport/config (`common/evidence/providers/providers.py`: `LLMConfig`, `load_llm_config` reading
  `APP_LLM_API_KEY` / `_BASE_URL` (https) / `_MODEL`, `_send_once`, injectable `SmokeSender`) and
  `litellm_provider._openai_content` (messages / tools are already OpenAI-shaped and go out as is). **One HTTP request
  per `chat`, no retry**; `finish_reason="length"` ⇒ `TruncatedOutputError` (no truncation retry). `ProviderRequestError`
  and malformed responses ⇒ `ProviderError` (key never in the message); program errors propagate. Images are declared
  (`image_input`, default off; the factory leaves it off). `chat` carries `instrument_llm_call`; no logging, no trace.
- `truncation.py` — **provider-layer truncation retry** shared by `LiteLLMProvider` / `ClaudeCliProvider` /
  `AnthropicProvider` (`stop_reason="max_tokens"`; it never enables thinking, so there is nothing to turn off).
  `TruncationPolicy` (`RAGSPINE_LLM_TRUNCATION_RETRY=on|off`, default `on`; `RAGSPINE_LLM_TRUNCATION_MAX_TOKENS`,
  default `DEFAULT_TRUNCATION_MAX_TOKENS = 16384`; `DEFAULT_TRUNCATION_MAX_RETRIES = 2`; each provider takes a
  `truncation=` override) and `retry_on_truncation`. On a cut, the budget is doubled up to the cap; retries stop when
  the budget can't grow any more. A cut that survives the retries ⇒ `TruncatedOutputError(ProviderError)`, which takes
  the existing honest degrade and is never returned as an answer (see Invariants). No cut ⇒ one call with a
  byte-identical request. Off ⇒ a cut result comes back unchanged, as before. Counts go to the current LLM call probe
  (`note_attempt(truncation=True)` / `note_truncated()`, `common/observability/llm_calls`, ADR 0028); the request
  trace's `llm_truncation_retries` / `llm_truncated_final` are derived from `llm_calls`, only when non-zero.
- `number_guard.py` — **narrative number guard (ADR 0024)**: `guard_narrative_answer` /
  `ungrounded_numbers` (deterministic, zero LLM; normalization from `common/answer_text`),
  `NUMBER_GUARD_RULE` (prompt), `NUMBER_GUARD_NOTICE`, `resolve_number_guard` /
  `RAGSPINE_NARRATIVE_NUMBER_GUARD`.
- `query_tools.py` — profile-driven `query_metric` tool schema + execution
  (`found` / `not_found` / `unrecognized_param` — never fabricates).
- `decompose.py` — **W6a query decomposition (opt-in, default-off).** `QueryDecomposer` Protocol +
  `LLMQueryDecomposer` (provider→JSON sub-question array parsed by `common/llm_json.parse_llm_json`, bounded,
  deterministic degrade) +
  `make_decomposer` / `RAGSPINE_QUERY_DECOMPOSE`. `answer_question(decomposer=…)` defaults `None`
  ⇒ main loop **byte-identical**; when injected and the question splits (>1 sub-q), each sub-question
  re-runs the **full** `answer_question` (`decomposer=None`, no recursion) and the guarded sub-answers
  are deterministically concatenated (route `decomposed`). Security gate + anti-fabrication rewrite are
  inherited **per sub-question** — a competitor sub-question is still out-of-scope-refused.
- `query_transform.py` — **W9 query transformation (opt-in, default-off).** Four LLM transforms on the
  `QueryRewriter` / `IntentParser` seam (ADR 0010), all byte-identical when unselected. Three are
  `NarrativeRetriever` wrappers (the W6b `CorrectiveRetriever` idiom): `HyDERetriever` (hypothetical-doc
  probe — **never a citable fact**; it replaces only the query *text* fed to `base.retrieve`), `RAGFusionRetriever`
  (LLM N variants, parsed by `common/llm_json.parse_llm_json` → **RRF via `retrieval.rrf_fuse`**), `StepBackRetriever` (abstract question + original,
  RRF-merged); selected by `make_query_transform(base, spec, *, provider)` / `RAGSPINE_QUERY_TRANSFORM` (`none`
  → **base unchanged**; degrades to base when no provider injected). The fourth is **Adaptive-RAG**:
  `HeuristicComplexityClassifier` (deterministic default — routes by listed-axis count / comparison cues) /
  `LLMComplexityClassifier` (opt-in) + `AdaptiveDecomposer` (implements `QueryDecomposer`, **reuses
  `answer_question(decomposer=)`** — `multi` → W6a fan-out, `simple`/`single` → the byte-identical single-shot
  route); `make_adaptive_decomposer(spec, *, provider)` / `RAGSPINE_ADAPTIVE`. **Security inherited**: every
  LLM-generated variant / step-back question passes the deterministic `SecurityGate` **before retrieval** (a
  competitor variant is dropped, never retrieved); isolation inherited from `base` (RESTRICTED stripped upstream).
  **Degrade honest** (provider failure / no provider → original query).

## Invariants

- **Anti-fabrication is per-path — do not unify the three:**
  - *structured* — `_structured_answer`: the answer is **deterministically
    synthesized on every path**. found facts are rendered from the fact value
    (`实体 期间 指标（渠道）：值 单位（来源…）`, same format as `_multi_subtask_answer`);
    no-found is rewritten to "not found" / "unrecognized" — **after** the route fallback
    below has had its chance. The model's prose is
    **never** trusted for the number — a live LLM cannot smuggle an extra fabricated
    figure on the found path (audit HIGH closed; regression:
    `test_found_path_discards_fabricated_extra_number`). Don't reintroduce
    `model_text` into the found branch.
  - *multi-subtask* — `_multi_subtask_answer` never calls the LLM at all.
  - *narrative* — `_run_narrative` **forces source citation**, and (ADR 0024,
    `RAGSPINE_NARRATIVE_NUMBER_GUARD=on|off`, default `on`; `answer_question(narrative_number_guard=)`
    overrides) every answer number must appear in the snippet text. Question numbers, citation / page /
    list markers, and years or periods that match the evidence are exempt. Otherwise the answer is
    rewritten deterministically: an ungrounded number in the lead ⇒ `NUMBER_GUARD_NOTICE` + the grounded
    raw-value sentences; elsewhere ⇒ only those sentences are dropped, plus a note. On also appends
    `NUMBER_GUARD_RULE` to the system prompt. Off ⇒ byte-identical. The prose is otherwise trusted; there
    is no found-fact rewrite here. Don't swap the rewrite for a second LLM "repair" call. The lineage suffix
    is appended **after** the guard (merged per document, ADR 0029); keep that order.
  - *route fallback (ADR 0023)* — `RAGSPINE_NARRATIVE_FALLBACK=on|off` (default `on`;
    `answer_question(narrative_fallback=)` overrides). With a retriever injected, a structured-route
    missing metric (`missing_metric`) or zero `found` (`structured_no_hit`) first runs
    `_run_narrative(fallback=True)`. Accepted only if `_fallback_grounded` holds: no `NO_ANSWER`
    sentinel, and some answer number that isn't in the question appears in the snippet text. Else a
    no-hit returns the original result byte-identical, and a missing metric answers "查不到：…" + the
    original ask (the `ask_first` clarification is kept). Result: `route=narrative`, `fallback=<reason>`,
    `clarification.mode=none`. Never on `found` / composite / narrative, never before the
    competitor refusal. Don't relax the grounding check to "has sources". The number guard runs after
    `_fallback_grounded` on an accepted fallback answer too.
- **Security is deterministic and never-pluggable.** Intent extraction is a swappable
  `IntentParser` Protocol; the `SecurityGate` is not. The gate re-derives external /
  competitor scope from the raw question and decides refusal independently of whatever
  the parser produced — swapping in an LLM parser cannot defeat it (ADR 0010).
- **Conversation history is generation-only, never a parse input (ADR 0017).** The `history=` seam
  feeds provider context messages only; the current question stays the last user turn so the
  deterministic parser/security gate read it unpolluted. Don't splice history into the question or
  the intent parser — that reintroduces the poisoning defect this seam fixes.
- **No hardcoded company** — home identity / entities / tool schema all derive from
  `load_company_profile()` (`agent.py` `_PROFILE`, `query_tools.py` builders).
  Never backfill "ACME".
- **Privacy-aware traces** — `_TraceCtx` records metadata only (tokens, timings,
  chunk_id, scores), never answer / fact value / chunk text.
- **Per-call LLM telemetry (ADR 0028)** — `answer_question` is wrapped in `@record_llm_calls()` (one bucket per
  request, signature unchanged); every provider `chat` here is decorated with `instrument_llm_call`, and each LLM
  call site carries an `llm_stage` label from the closed enum (`decompose` / `classify` / `hyde` / `rag_fusion` /
  `step_back` / `tool_round` / `synthesis` here; `translation` / `listwise_rerank` in `retrieval/`). In `agent.py`
  the `with llm_stage(...)` wraps **only the `provider.chat` line** (retrieval inside synthesis keeps its own
  stages). A new LLM call site needs a stage, or it shows up as `other`; a new provider needs the decorator, or it
  is not recorded. The trace gains `llm_calls` + `llm_n_calls` / `llm_n_retried` / `llm_ms` only when there were
  calls; a split decomposition also emits a parent trace (`route="decomposed"`, `n_subquestions`, decompose /
  classify calls only). `provider_seconds` / `token_usage` keep their old tool-loop + synthesis scope. No model
  name, no prompt size. Any in-request thread pool must run each task under `copy_context().run`.

## Read before editing

- **Out-of-scope entity must reject first.** In `answer_question`,
  `CLARIFY_OUT_OF_SCOPE_ENTITY` returns before any tool / retrieval / LLM call; a
  competitor/external entity must never reach a channel. Don't reorder the early-returns.
- **External-entity masking is an invariant, not a cleanup.** It lives in
  `SecurityGate.detect`: matched aliases are replaced with **equal-length spaces**, and
  home-entity matching runs on the masked text (used by `parse_intent`). "Simplifying"
  this leaks competitor data via substring collisions (e.g. a masked competitor leaving
  `中国` → `ACME_CN`). Security. The refusal decision is made by `SecurityGate.screen`
  on the **raw question** (via `clarify_scope`), not by trusting `intent.external_entity`.
- **Clarification asymmetry is deliberate.** In `clarify_scope`: missing *metric* → ask
  first; missing *entity/period* → answer with surfaced assumptions. Don't downgrade
  metric-missing to "assume and answer". The orchestrator may try the narrative fallback before
  it asks (ADR 0023), but it never guesses a metric. `clarify_scope` itself is unchanged.
- **`ProviderError` wraps only network / API / timeout errors** (`llm_provider.py`);
  program errors (KeyError/TypeError) must propagate. Never `except Exception` into a
  degrade path — it buries real bugs. It now inherits the family base `corespine.CorespineError`
  (stable `code="provider.error"`); the network-only wrapping contract is unchanged. One addition: an output
  still cut after the truncation retries is `TruncatedOutputError` (`code="provider.truncated"`), a subclass,
  so it takes the same degrade.
- **provider & retriever are Protocols; `agent.py` imports no SDK and no retrieval impl**
  (`LLMProvider` in `llm_provider.py`, `NarrativeRetriever` Protocol in `agent.py`). The
  `anthropic` SDK is lazy-imported inside `AnthropicProvider` only; `litellm` only inside
  `litellm_provider._load_litellm` (never at module import or construction).
- **Tool loop is capped** at `MAX_TOOL_ITERATIONS = 5` (`agent.py`); the SDK owns
  retry/backoff — don't add your own. The one provider-level retry is the length-cut retry in
  `truncation.py` (a larger output budget, not a network retry).

## Deep dives

Planned (`src/ragspine/agent/docs/`, not written yet):

- anti-fabrication — the three-path semantics + the `fabrication_guard_triggered` definition.
- clarification decision tree — the four `CLARIFY_*` states × structured/narrative/composite routing.
- security gate & IntentParser seam — deterministic refusal/masking (`security_gate.py`)
  vs the pluggable intent parser; the "deterministic where it matters" boundary (ADR 0010).
- provider abstraction & resilience boundary (Protocol + lazy import + honest degrade).
