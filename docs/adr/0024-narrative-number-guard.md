---
status: accepted
date: 2026-09-24
---

# ADR 0024 — Narrative number guard (anti-fabrication extended to the narrative channel)

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Extends the anti-fabrication invariant ([0001](0001-dual-channel-determinism.md),
[0023](0023-structured-miss-narrative-fallback.md)) from the structured channel and the route
fallback to **every narrative synthesis**. Security gate order ([0010](0010-intent-parser-security-decoupling.md))
and the forced source citation are unchanged.

## Context

The narrative channel trusted model prose and only forced a citation. On the nl-gold v2 run
(`2026-09-24-translation`, A 90.9% / B 90.9%) the only stable failures were both inference, in both
routes, 3/3:

- **a02** — "how many percentage points higher is Agency than Partnerships?" The page shows 72% and
  28%, never 44. The model computed 72 − 28 = 44 and presented it as an answer with sources.
- **a03** — "which stage comes after Foundation?" The labels on the page are interleaved with
  unrelated text, so no order is provable. The model read an order off the layout ("Growth").

Whole-page context and page images make the model more willing to answer. ADR 0023 already had a
number-in-evidence check, but only as the acceptance test for the fallback (at least **one** number
must come from the snippets). A fallback answer, and every plain narrative answer, could still carry
additional computed or invented numbers.

## Decision

Switch `RAGSPINE_NARRATIVE_NUMBER_GUARD=on|off` (read by `answer_question` when its
`narrative_number_guard=` keyword is `None`; **default `on`**; any other value raises `ValueError`).
When on, it covers every `_run_narrative` synthesis: the narrative route, the attribution part of a
composite answer, and an accepted route fallback (after `_fallback_grounded`, which is unchanged).

### 1. Deterministic check (code, `agent/number_guard.py`)

Every number in the answer must be found in the retrieved snippet text (`_snippet_text`, the same
text the model saw). Matching reuses the nl-gold normalization, `normalize_answer` /
`contains_normalized`. Those two functions moved to `common/answer_text.py`, and the eval module
re-exports them with the same signatures and behavior. On top of that:

- **Exempt, not numeric claims:** the sources' `doc` / `locator` strings, `[n]` / `[n, m]` / `〔n〕` / `【n】` citation
  markers, page / slide / `page=N#paraA-B` / `第 N 页` / `pN.png` references, line-leading list
  numbers (`1.`, `2)`, `3、`), `（n）` enumerations, and numbers already in the question.
- **Years and periods** (`1H26`, `FY2024`, `Q3 25`, `2026 年`): accepted when the year matches a year or
  period in the question or snippets (`1H26` in a snippet also covers "2026 年上半年"). A period token
  whose year appears in neither is ungrounded.
- **Format variants:** thousands separators, `per cent` → `%`, NFKC and punctuation all normalize away.
  Amounts with a magnitude word are compared by value: `5.14 亿美元` = `US$514m`, and
  `11.68 亿美元` = the table figure `1,168` read as US$m (also billions and thousands).
- **Units are not interchangeable:** `44%` in a snippet does not support "44 个百分点". A percentage
  needs `n%` in the evidence, and a bare number needs a bare `n`.

### 2. Deterministic rewrite (no second LLM call)

The answer is split into sentences (by line, then `。！？!?；;` / `. `). The **lead** is the first
sentence, joined with the following ones until it reaches 12 normalized characters. That is the same
rule the nl-gold judge uses to find an answer's opening.

- **Ungrounded number in the lead** (the conclusion itself is computed or invented): the answer
  becomes `NUMBER_GUARD_NOTICE` ("资料中没有直接给出该数值：…已按防编造规则移除（不做推算）。"),
  followed by `片段中的相关原值：` and the original sentences that contain at least one grounded number
  and no ungrounded one. If there are no such sentences, the answer is the notice alone. The removed
  number is never repeated.
- **Ungrounded number only later** (an aside such as "两者合计 100%" or "≈ $2.3b"): only those sentences
  are dropped (a line left empty is dropped too). The rest is kept verbatim, and a trailing note
  "（注：已移除 N 处在检索片段中找不到原文的数字，不做推算。）" is added.

Source citation is then forced exactly as before: the "（资料来源：…）" suffix is added when the
rewritten text names no source doc, and `sources` is unchanged. The request trace gains
`narrative_number_guard={ungrounded, rewritten}` (counts only), and only when a rewrite happened.

### 3. Inference constraint (prompt, soft)

When the switch is on, the narrative system prompt gains `NUMBER_GUARD_RULE`: "只用片段里原样出现的数字，
不做计算；不推断顺序、因果或趋势；片段没有明说的，就回答资料中没有给出。" This is the only lever for
non-numeric inference such as a03's order. The code check is the guarantee for numbers.

**The prompt rule shares the switch** rather than having its own. With the switch off, the prompt and
the answer must stay byte-identical to the behavior before this ADR, and a separate prompt switch
would be one more knob with no measured need. The two halves are one policy: the prompt asks the model
not to compute, and the code enforces it when the model computes anyway. They were evaluated
together.

## Consequences

- **Byte-identical when off:** `answer_question(narrative_number_guard=False)` or
  `RAGSPINE_NARRATIVE_NUMBER_GUARD=off` produces the same system prompt, answer and trace as before
  (snapshot `test_off_is_byte_identical_snapshot`). When on and every number is grounded, the answer
  is unchanged. Only the system prompt gains the rule.
- **Structured `found` path, multi-subtask and the competitor refusal** are unaffected. They never go
  through `_run_narrative`.
- **Known limits (they err toward removal):** numbers read from a page image but absent from the text
  are treated as ungrounded. A number that appears anywhere in the retrieved snippets counts as
  grounded, even if the model attached it to the wrong claim. The check proves provenance of the
  digits, not of the sentence. Numbers written as words ("three") are not checked. `（n）` enumerations
  are exempt, so a fabricated one- or two-digit value written as `(5)` would slip through.
- **ACME / offline:** `MockProvider` echoes the question, the snippets and the source locators, and
  all of them are grounded or exempt, so the demo and the QA ratchet are unchanged (fabrication 0).
- **Default on**, because the evaluation did not get worse. nl-gold v2, full document, repeat 3,
  claude-cli + local embedding / reranker (`data/validation/ragspine-nl-gold/2026-09-24-number-guard/`):
  A 100.0% ± 0.0 and B 100.0% ± 0.0, against 90.9% ± 0 for both routes in `2026-09-24-translation`. a02
  and a03 went from 0/3 to 3/3 on both routes, and no positive case failed. In that run the prompt rule
  alone produced the refusals: the model said the gap and the order are "not given", and the guard
  rewrote nothing. In an earlier run of the same code, before `〔n〕` markers were exempt, the guard made
  one partial rewrite (B k01), which still passed. Replaying the guard offline on the 132 answers of
  `2026-09-24-translation` (evidence = the retrieved pages' text) rewrote all six a02 answers into judged
  abstentions. It removed three asides from positive answers ("≈ $2.3b" twice, "合计 100%"), and those
  answers still passed. It did not touch any other answer.
- Frozen by `tests/agent/test_narrative_number_guard.py` (computed difference blocked, raw values /
  format variants / question numbers / citation markers / periods pass, rewrite keeps sources, lead
  vs aside rule, off ≡ snapshot, env parsing, trace counts only) and by the unchanged ADR 0023 and
  anti-fabrication suites.
