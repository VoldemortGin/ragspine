---
covers:
  - src/ragspine/agent/
verified-against: 6ebaaa4c3340f5dfcb69c255c94b3b6bc4724c74
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
  the command line is exactly the old one.
- `number_guard.py` — **narrative number guard (ADR 0024)**: `guard_narrative_answer` /
  `ungrounded_numbers` (deterministic, zero LLM; normalization from `common/answer_text`),
  `NUMBER_GUARD_RULE` (prompt), `NUMBER_GUARD_NOTICE`, `resolve_number_guard` /
  `RAGSPINE_NARRATIVE_NUMBER_GUARD`.
- `query_tools.py` — profile-driven `query_metric` tool schema + execution
  (`found` / `not_found` / `unrecognized_param` — never fabricates).
- `decompose.py` — **W6a query decomposition (opt-in, default-off).** `QueryDecomposer` Protocol +
  `LLMQueryDecomposer` (provider→JSON sub-question array, bounded, deterministic degrade) +
  `make_decomposer` / `RAGSPINE_QUERY_DECOMPOSE`. `answer_question(decomposer=…)` defaults `None`
  ⇒ main loop **byte-identical**; when injected and the question splits (>1 sub-q), each sub-question
  re-runs the **full** `answer_question` (`decomposer=None`, no recursion) and the guarded sub-answers
  are deterministically concatenated (route `decomposed`). Security gate + anti-fabrication rewrite are
  inherited **per sub-question** — a competitor sub-question is still out-of-scope-refused.
- `query_transform.py` — **W9 query transformation (opt-in, default-off).** Four LLM transforms on the
  `QueryRewriter` / `IntentParser` seam (ADR 0010), all byte-identical when unselected. Three are
  `NarrativeRetriever` wrappers (the W6b `CorrectiveRetriever` idiom): `HyDERetriever` (hypothetical-doc
  probe — **never a citable fact**; it replaces only the query *text* fed to `base.retrieve`), `RAGFusionRetriever`
  (LLM N variants → **RRF via `retrieval.rrf_fuse`**), `StepBackRetriever` (abstract question + original,
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
    is no found-fact rewrite here. Don't swap the rewrite for a second LLM "repair" call.
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
  (stable `code="provider.error"`); the network-only wrapping contract is unchanged.
- **provider & retriever are Protocols; `agent.py` imports no SDK and no retrieval impl**
  (`LLMProvider` in `llm_provider.py`, `NarrativeRetriever` Protocol in `agent.py`). The
  `anthropic` SDK is lazy-imported inside `AnthropicProvider` only.
- **Tool loop is capped** at `MAX_TOOL_ITERATIONS = 5` (`agent.py`); the SDK owns
  retry/backoff — don't add your own.

## Deep dives

Planned (`src/ragspine/agent/docs/`, not written yet):

- anti-fabrication — the three-path semantics + the `fabrication_guard_triggered` definition.
- clarification decision tree — the four `CLARIFY_*` states × structured/narrative/composite routing.
- security gate & IntentParser seam — deterministic refusal/masking (`security_gate.py`)
  vs the pluggable intent parser; the "deterministic where it matters" boundary (ADR 0010).
- provider abstraction & resilience boundary (Protocol + lazy import + honest degrade).
