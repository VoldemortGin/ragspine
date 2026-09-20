# Expense Ratio bar ChartQA plan

> **Paused implementation status (2026-09-19 snapshot):** This slice has substantial uncommitted implementation and candidate validation beyond the original plan below, but it is not activated or released. Development is paused. Read [Claude handoff](CLAUDE_HANDOFF.md) before continuing; it records the exact completed boundary, immutable evidence, prerequisites, and remaining work. The gold and plan content below remains unchanged as the source contract.

This document fixes the independent source truth and evaluation plan for the second ChartQA slice. It does not grant qualification, change the active v1 donut contract or mark the broader P5 milestone complete. The architectural decision is recorded in [ADR 0009](adr/0009-source-qualified-expense-ratio-bar-lookup.md).

## Locked source candidate

- Document SHA-256: `df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e`
- Physical page: 20; source page index: 19
- Layout object: `layout-object-v1:50365bbe30728f288c8199778fbd66c13f877c6ec61ddeae612d4188c81b3388`
- Region: `[354, 324, 612, 474]` in `page-top-left-points`
- Native crop SHA-256: `268639ae163b261fa285e3d52fe83ffe3f5d742c08b71735ab52f881ab967ed9`
- Structured SVG SHA-256: `125e9f9a3962c2f4691590c689e22faa9752573ad767f83f591cb399d4eae893`
- Region source-text SHA-256: `1e5e1598e256d461209e714687cc6076fcf8f83dd703529457b22936a37d6878`
- Deterministic render SHA-256: `081bd55058c5264244b4d5d41a7547deca7752a36a178339d99c5772c4fde6bd`

These identities are source inputs for later proof. The raw ChartIR and description are review aids and are not gold authority.

## Independently transcribed observations

The gold must retain these source-sidecar numbers exactly, including their IEEE-754 JSON tails.

| Role | Exact text | Source span | Structured observation | Text range | Exact bbox |
| --- | --- | --- | --- | --- | --- |
| Series/title | `Expense Ratio` | `span-v1-d39d67c3c67a3805174ce9ffcd8cc1ce910e3254b6a2a4e6aef07e7d6cdd223a` | `obs-8e6e6085f04460d1` | `[0, 13]` | `[441.89, 331.67708, 517.8775519999998, 344.01344]` |
| 1H24 display | `8.2%` | `span-v1-1cf8ebb7629156af6b39570a37475feaeaac5aab00881f158bf9488c50214ce2` | `obs-358264ee4a922b72` | `[0, 4]` | `[390.5, 359.5132, 414.52399999999994, 371.2876]` |
| 1H24 value | `8.2` | same as display | `obs-a0386c33e67df7bf` | `[0, 3]` | same as display |
| 1H24 unit | `%` | same as display | `obs-c1f606698748add2` | `[3, 4]` | same as display |
| 1H24 category/point period | `1H24` | `span-v1-68b89127f62e1c6bbb2d331cf4e4b73797cc4e3deecc09702b1ae9eeddf9a307` | `obs-22d5f0ea5d01dffb` | `[0, 4]` | `[389.74, 453.5952, 414.9256, 465.3696]` |
| 1H25 category/point period | `1H25` | `span-v1-42f127a24b8e5c6d4f3d8d4a280dd868dec16f14ecad8372f640ad71d6d3c389` | `obs-79d1ec86e0d5327d` | `[0, 4]` | `[467.86, 453.5952, 493.0456, 465.3696]` |
| 1H26 display | `6.9%` | `span-v1-739c5313de515648c319a64c9851be810723220e303c51083a47414465aa2eec` | `obs-1744c69fe8e7531b` | `[0, 4]` | `[549.74, 375.5232, 573.764, 387.2976]` |
| 1H26 value | `6.9` | same as display | `obs-8c89fdacce9173c0` | `[0, 3]` | same as display |
| 1H26 unit | `%` | same as display | `obs-3835121580a40694` | `[3, 4]` | same as display |
| 1H26 category/point period | `1H26` | `span-v1-c5800dd7bae1bfd0ec237b19fe67ab14101791baecfd6b5c582d22367a313c5c` | `obs-12d0a5df768619e5` | `[0, 4]` | `[545.98, 453.5952, 571.1655999999999, 465.3696]` |
| Unsupported annotation | `(130) bps` | `span-v1-4f269a4b8b1632bd879fe540ff981a8c830eebd2251b579c70138038c92f75d7` | `obs-73eceed717010c5b` | `[0, 9]` | `[457.49, 355.2432, 503.7955999999999, 367.0176]` |

The page-context citation is separate from crop evidence:

- span `span-v1-6be7d04e59b4a148b55975083c652e458ddba6762996882168473d4ab9facb0b`;
- exact text `Expense ratio comparatives and two-year changes are shown on an actual exchange rate basis`;
- bbox `[24.96, 510.7812, 321.1157599999999, 518.5416]`.

It must accompany an answer as context, but it does not prove that two displayed rates are economically comparable.

## Versioned gold plan

Create `benchmarks/aia-2026-interim/chart-qa-bar-gold-v1.json` after the v2 DTO is frozen. Do not edit `chart-qa-gold-v1.json` or reuse its report identity.

Positive cases:

1. exact lookup of `Expense Ratio`, point/category/period `1H24`, unit `%` returns `8.2`, raw display `8.2%`;
2. exact lookup of `Expense Ratio`, point/category/period `1H26`, unit `%` returns `6.9`, raw display `6.9%`.

Each answer must be verified with unknown numerical confidence, semantic scope `source_display_only`, no calculation receipt, exact series/value/unit citations, an exact `PointPeriodInterpretation`, and the separate page-context citation. The category and period roles intentionally share one source occurrence and must declare that sharing rather than duplicate the source.

Hard-negative and unsupported cases:

- `1H25` lookup: known point, unavailable value, business refusal;
- swapped `1H24`/`1H26` point or neighboring-label association: refusal or invalid evidence;
- approximate or comparator source display such as `~8.2%` or `>8.2%`: invalid evidence;
- cross-period percentage-point difference: request rejection, with no computed `1.3`;
- `(130) bps` arrow query or derived relationship: refusal/request rejection;
- height-derived `1H25` value or mutated estimated ChartIR: invalid evidence;
- wrong point, series, unit, member, snapshot or source: refusal or pinned-source error as appropriate;
- missing raw description, normalized description, interpretation, proof or page-context branch: unavailable or invalid evidence;
- glyph, value text, category label, bar association, fill, stroke, clip, alpha, transform, occlusion or digest mutation: invalid evidence;
- unknown, degenerate or unsupported stroke geometry/style: qualification unavailable and no answer.

Fault cases must use isolated copy-on-write stores and retain the original immutable source/current digests before and after capture. Pure-domain cases may test finer refusal reasons, but a real HTTP pass must traverse the source-aware resolver.

The evaluator must report answer precision and positive coverage, business refusal and transport-fault strata, exact field citation coverage, period-interpretation exactness, page-context exactness and hard-negative escape. Required values are `1` for precision and all supported-scope coverage/exactness metrics and `0` for hard-negative escape. Both positives are mandatory, so all-refusal cannot pass. Existing v1's 19-case evaluation remains a separate required regression.

## Proposed file ownership

After the production v2 DTO is frozen, the independent evaluation owner should add only new versioned files:

- `benchmarks/aia-2026-interim/chart-qa-bar-gold-v1.json`;
- `src/enterprise_pdf_rag/adapters/chart_qa_v2_capture.py`;
- `src/enterprise_pdf_rag/adapters/chart_qa_v2_evaluation.py`;
- `tests/adapters/test_chart_qa_v2_capture.py`;
- `tests/adapters/test_chart_qa_v2_evaluation.py`.

The v2 tools must require caller-supplied gold, target and output paths and must not search a source checkout at import time. They must write a distinct content-addressed evaluation directory, preserve captured API bytes, and make no model or embedding call. Production domain, API, resolver, promotion and source-proof files remain owned by their respective implementation owners.
