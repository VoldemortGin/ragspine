# ADR 0004 — Persist and review the selected AIA source before semantic QA

Status: Accepted for this bounded ingestion milestone, 2026-09-19.

The sole business input is `aia-group-2026-interim-results-presentation.pdf`, SHA-256 `df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e`, 71 physical pages. Verify its bytes before extraction. Persist the exact original PDF, every page's native SVG and text-span observations, the selected page-25 dividend region (page index 24, bbox [731.5, 145.0, 944.2, 385.0]), and an immutable manifest in a local content-addressed store. A local current-manifest pointer makes the completed source review discoverable; this is not production multi-store atomic publication, release/CAS, ACL or rollback.

`documents/` contains immutable standard-library values, source/store Protocols and the ingestion use case. PDF access stays in pdfspine adapters. Native SVG preserves the renderer's original paths, definitions, transforms and clips; cropping changes only the outer viewport. Text spans are observations in a separate sidecar, not a proven text-to-SVG-element mapping. Asset persistence and semantic qualification are separate: chart data, descriptions, SVG fidelity/completeness and span-to-element mapping remain pending. No embedding, LLM, ChartIR promotion or summary fallback occurs.

The default local WebUI profile becomes AIA source review. It names the exact file, identifies page citations and raw extracted text, links reviewable persisted source assets, and states missing capabilities. It refuses financial conclusions or chart values without validated semantics. The synthetic 10/15 fixture is retained only for explicit offline-demo mode and tests; it must not appear as AIA content. Existing figure qualification remains unchanged.

Tests first cover byte identity, all-page persistence, immutable artifact retrieval, corruption/missing-source rejection and the real-source HTTP profile. Real AIA extraction and review supplement the offline gate; no live model call is needed for this milestone.

Project-managed outputs live under `data/output/`; this source uses `data/output/aia-2026-interim/`. The root-level `output/` directory is retired. Attempt timestamps and failure diagnostics are stored separately from immutable identity and extraction manifests.

The source review exports one lightweight HTML page per physical PDF page under `pages/page-001.html` through `page-071.html`, with a 71-page index and the page-25 focus region. Every page displays native SVG plus source-bound text observations. This is source navigation, not semantic extraction or a new frontend application.
