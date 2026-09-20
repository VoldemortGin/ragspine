# ADR 0003 — Real Open WebUI as a restricted demonstration client

Status: Accepted for the requested local UI integration, 2026-09-19.

Use the official Open WebUI v0.11.3 release in a separate container, keeping its dependencies and data outside the backend uv environment. It connects only to this project's OpenAI-compatible `/v1/models` and `/v1/chat/completions`, including SSE. The initial explicit regression profile advertises `enterprise-pdf-rag-offline-demo-v1`; its fixed snapshot contains the authored PDF fixture. ADR 0004 supersedes the business default: `aia-2026-interim-source-review-v1` reads the persisted selected AIA document, exposes source observations only, and leaves chart semantics pending. Synthetic mode now requires explicit `--profile offline-demo`. Answers are deterministic renderings of the service's retrieved and hydrated ChartIR/field evidence. Unknown questions/models, attachments, source mismatches and missing evidence fail closed. No upstream LLM proxy or production qualification is introduced.

Open WebUI permission flags alone do not enforce the PDF-entry boundary: administrators can bypass some user restrictions. Wrap the unchanged upstream ASGI application with a small deployment gate that rejects document/upload/knowledge/retrieval/tool/admin-configuration routes and unsupported chat features before upstream processing. Only the guarded application is exposed on loopback. Official feature flags also hide or disable automatic title/tag/follow-up generation, web search, plugins and local model downloads. Open WebUI receives a fixed local placeholder API key, never the user's upstream key. A native preview must use an allowlisted environment and isolated DATA_DIR as well.

The official target version is pinned; an already installed older version may be used only as a separately labelled compatibility preview. No global dependency or VM installation is part of this change. If an engine is unavailable, report actual startup limits instead of claiming a running target container. Existing services and profiles remain untouched.

Validate the backend protocol and deployment gate using offline behavioral TDD, then `./ci.sh`. Exercise a real local Open WebUI page when an existing isolated runtime permits it. Do not rerun paid LLM connectivity checks or download embedding/rerank models.

Official references checked:

- [v0.11.3 release](https://github.com/open-webui/open-webui/releases/tag/v0.11.3)
- [Environment configuration](https://docs.openwebui.com/reference/env-configuration/)
- [Pinned configuration implementation](https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/config.py)
- [Pinned OpenAI adapter](https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/routers/openai.py)

The existing frontend/backend production limitations in ADRs 0001 and 0002 remain unchanged.

The environment builder constructs the full child environment instead of copying the parent. It disables python-dotenv loading and puts SQLite, static assets, caches and the session secret in the private data directory. The installed vendor's `static` directory is not used as writable storage. Source-based official containers report their version through `/app/package.json`; pip installations use distribution metadata. The two versions remain explicitly distinguished.

The OpenAI subset has a separate reviewed contract, `docs/schemas/openai-demo-v1.json`; the existing figure API contract remains unchanged. The deployment gate responds to exact read-only tool/channel discovery with empty capability lists, without calling vendor tool-server discovery. Uploads, built-in document retrieval, tools and configuration writes still fail before vendor code runs, including for an admin session.

TDD evidence: model discovery first returned 404; stream requests first returned 422; unguarded attachment/tool inputs first reached the vendor; UI discovery first returned 403. The corresponding tests now verify model/response/SSE contracts, rejection before SSE headers, rejection before vendor execution and explicit empty disabled-capability discovery. The environment test verifies that upstream credentials, unrelated database/object-storage settings and model-download settings are not inherited. Actual running version and deployment verification are recorded separately in the usage documentation.

The legacy UI sends default `title_generation` and `tags_generation` flags even when server-side generation is disabled. The guard forces only these two recognized boolean background-task flags to false before invoking vendor middleware. Unknown enabled tasks, tools, retrieval features and attachments remain rejected; this compatibility normalization does not authorize any auxiliary model call.
