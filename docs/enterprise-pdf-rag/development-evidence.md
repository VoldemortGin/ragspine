# Initial slice — observed development evidence

Date: 2026-09-19. Execution: Python 3.12.11, locked uv environment, offline tests. This records observed failures and subsequent passing behaviors; it is not a model quality qualification.

covers: src/enterprise_pdf_rag/adapters/pdfspine_figure.py, src/enterprise_pdf_rag/adapters/runtime.py, src/enterprise_pdf_rag/adapters/http/app.py, src/enterprise_pdf_rag/figures/service.py, tests/enterprise_pdf_rag/adapters/test_entries.py

| Behavior | Observed red | Observed green |
| --- | --- | --- |
| Real PDF → readable SVG/source anchors | `pytest tests/enterprise_pdf_rag/adapters/test_pdfspine_figure.py` failed importing the absent parser | Same test passed after the pdfspine-only adapter |
| SVG two branches → retrieval → ChartIR context | `pytest tests/enterprise_pdf_rag/adapters/test_demo.py` failed importing absent runtime | The source PDF, explicit 10/15 values, same binding and populated field evidence passed |
| Review HTML contains a real inline SVG | CLI entry test failed because XML serialization emitted `ns0:svg` | Default-namespace serialization rendered `<svg>` and the entry test passed |
| Invalid HTTP query fails at boundary | Whitespace query reached embedder and raised ValueError | Strict Pydantic validation returns 422; string-to-number coercion is rejected |
| Domain eligibility | See ADR 0002 and `tests/enterprise_pdf_rag/figures/test_pipeline.py` | Qualified same-source pairs pass; missing/contradictory/unqualified evidence fails closed |

The HTTP harness uses `httpx2.ASGITransport`, avoiding a deprecated Starlette TestClient/AnyIO alias; warnings are not suppressed. Beartype remains enabled, including tests. The environment uses the real released pdfspine 0.10.0 wheel. No alternate PDF parser or model was invoked by the offline figure pipeline or test gate.

Executed manual smoke commands from README:

- `demo`: produced source PDF, structured SVG, review HTML and JSON; offline mode, values 10/15, 10 field-evidence entries.
- AIA physical page 10, bounds `(30,120,310,330)`: produced 7 anchored source elements, `pending`; clipPath and two compound-path limitations were reported. This is a diagnostic, not a production completeness verdict.
- In-process HTTP: ingest 201, search/context 200, cross-snapshot 409, malformed requests 422.

The final verification command is `bash scripts/ci.sh`. Its outcome should be reported from the actual latest run rather than from this static document.

Independent review found two qualification gaps: equal-text wrong occurrences and mismatched bundle figure/source metadata. Both received failing regression tests before correction. A separate trusted fixture adapter now records exact occurrences independently of the two producers; source receipt tampering and absent receipts fail. The adapter test first failed on its absent implementation, then passed with explicit source registration.

The user separately authorized one LLM connectivity probe. `llm-smoke` was introduced with a failing command/configuration test before implementation; missing environment model, key redaction, one bounded request and independent local-model settings are covered offline. On 2026-09-19 the single live probe requested `gpt-5.6-luna` through the configured OpenAI-compatible endpoint (private address omitted) and validated the short OK response in 3309 ms. It used a temporary subprocess model override, sent no PDF, changed no shell profile, and performed no retry. This is connectivity evidence only, not evidence that real chart extraction or embedding/rerank is connected.

## Selected AIA source ingestion

Source identity, immutable persistence/reopen, corrupted objects, non-finite geometry, source-only SSE/profile and wrong-page region binding were tested before fixes. The wrong-page sidecar and non-finite geometry tests each observed `DID NOT RAISE` before correction. Original source SHA, complete 71-page coverage and region/native-page equality are checked; semantic fields remain pending. A separate AIA HTTP schema is reviewed, while existing explicit demo/figure contracts remain unchanged. Runtime attempts and presentation exports are outside content-addressed identity. The final acceptance uses the real selected PDF locally and the offline completion gate; no additional live model probe is used.

The page-navigation integration test first failed because the review index had no `pages/page-001.html` link. It then passed with per-page HTML export, HTTP index/page aliases, same-source PDF/text export and rejection of out-of-range pages. The pure HTML renderer separately covers all-page links, first/last navigation, pending qualification, source identity and escaped source text. These views reuse stored assets without another PDF parse or model call.
