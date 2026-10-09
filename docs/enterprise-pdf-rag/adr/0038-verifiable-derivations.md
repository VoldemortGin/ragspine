# ADR 0038: Opt-in model arithmetic, re-computed by code (derivations)

Status: Accepted, 2026-10-08. Relaxes rules 2 and 3 of the answer system text
(`answers/prompt.SYSTEM_RULES`, [ADR 0011](0011-document-catalog-and-verified-answer-chain.md))
**only when a caller opts in**. Switch: `AnswerSettings.allow_derivations` /
`run_folder_pipeline(answer_derivations=True)`, default **off**; off, the system text is the same
`SYSTEM_RULES` object, `ModelAnswer`'s JSON schema and every request fingerprint are byte for byte
unchanged, and verification behaves exactly as before.

## Context

ADR 0011 forbids the answer model to calculate: every number in the prose must be a number some
verified claim prints. Financial question sets (the AIA report set in `notebooks/aia_wise.ipynb`,
aligned with SuperIndex `batch_qa`'s answer rules) routinely ask for a growth rate rounded to two
decimals or for an HK$ figure restated in US$. Under ADR 0011 the model must abstain
(`needs_calculation`) or the prose gate abstains for it, although every operand is in the
evidence. Letting the model calculate freely would break the anti-fabrication invariant: a
computed number has no span to be re-read from.

## Decision

- **A subclass schema, sent only when opted in.** `ModelAnswerWithDerivations(ModelAnswer)` appends
  `derivations: tuple[ModelDerivation, ...]` (at most `MAX_DERIVATIONS = 8`). A derivation is
  `name`, `expression`, `inputs` and `result`; an input is `name`, `value` and exactly one of
  `claim_id` (the model's own `claim_id` of a claim in the same answer) or `constant`. Every model is
  strict, frozen and `extra="forbid"`, and lists of objects keep the strict response-schema
  contract (`test_strict_response_schemas`). The default path still sends `ModelAnswer`.
- **A system-text variant.** `SYSTEM_RULES_DERIVED` replaces exactly the original text of rules 2
  and 3 (`_RULE_2` / `_RULE_3`, each pinned to occur once) and nothing else: 2′ allows + − × ÷,
  conversion and rounding, provided every computed number is written as a derivation whose inputs
  are the answer's own claims or listed constants; 3′ admits a prose number that is a claim's
  number, a derivation's result (separators, `%`, fewer decimals) or a used constant's value.
  `answer_system(extra, derivations=, constants=)` appends a sorted `name = repr(float)` constants
  block after the rules, then the caller's additional rules; the total stays within
  `MAX_SYSTEM_CHARS`.
- **A hand-written evaluator.** `answers/derivations.evaluate` is a tokenizer plus recursive
  descent over `expr / term / unary / atom` (`+ - * / ( )`, `\d+(\.\d+)?` literals, identifiers);
  no `ast`, `eval` or `exec`. Limits: 200 characters, 64 tokens, depth 16; `Decimal` at 34
  significant digits; division by zero is a typed error.
- **Verification order.** `verify_derivations` checks: unique, well-formed name → each input names
  exactly one source → a claim input cites a *verified* claim and its value is a number that
  claim's `text` prints (or its `value`) → a constant input is in the table with an equal value →
  the expression references at least one input and evaluates → `result` is one number →
  `|computed − result| ≤ ½ unit of result's last decimal + 1e-9 · |result|`.
- **The prose gate.** `prose_grounded(derived=, constants=)` admits a number that would otherwise
  escape when it equals a verified derivation's computed value or result, or lies within half a
  unit of *its own* last written decimal of one (`1,234.57`, `1,235` for `1234.5678`); no unit
  scaling (`1.23 billion` ≠ `1,234 million`). Constants enter only when a verified derivation
  used them, compared by value. `decide(derivations=)` appends
  `rejected derivations: name(reason)` to the abstain detail. `AbstainReason` is not extended.
- **Constant whitelist.** Names match `^[a-z][a-z0-9_]*$`, values are finite numbers, and
  constants without derivations are refused at construction (`answer_constants_need_derivations`).
- **Fingerprint and audit.** The opted-in system text and schema are part of the request body, so
  they enter the model-cache fingerprint; the answer journal gains a `derivations` column (an older
  journal is upgraded on open) and `rag-chat-v1` gains `derivations` / `rejected_derivations`
  (empty by default).

## Consequences

Weakened when opted in — the caller owns these:

- whether the formula answers the question (a correct growth rate of the wrong pair is still
  re-computed correctly);
- which claim numbers the model chose as inputs, and the literals (`100`, `2`) it wrote;
- the rounding tolerance (half a unit of the written last decimal);
- that the constants (e.g. a yearly HK$/US$ rate) are right.

Still guaranteed:

- rule 1 is unchanged: every claim is re-read from stored evidence;
- a rejected claim is never in the verified set, so it can never be an input;
- an expression is the four operations only, over inputs that are verified numbers or listed
  constants, and must reference at least one input;
- every number in the prose still has to fall in the admitted set, else the whole answer abstains;
- the opted-in system text and schema are in the fingerprint and the journal.

## Not done here

- Chaining: a derivation cannot take another derivation's result as an input.
- Unit scaling between million / billion or between `%` and a fraction.
- A CLI flag: only `AnswerSettings` and `run_folder_pipeline` expose the switch.
