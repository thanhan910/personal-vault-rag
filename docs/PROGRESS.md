# Progress

## Implemented

- Vault-scoped FastAPI/SQLite catalogue, FTS5, Qdrant text/visual collections, resumable jobs, quotas and budget reservations.
- Docling/OCR/Office extraction, structured XLSX cells, PDF/image previews, faster-whisper timestamps and bounded video sampling.
- Hybrid rank fusion/rerank path, adjacent fetch, visual inspection, precise version/locator results and signed private viewers.
- OAuth 2.0 PKCE remote MCP plus Windows local stdio MCP, queued attachment inspection and connector-confirmed explicit saves.
- Single-binary DPAPI Windows connector, folder picker, upgrade-safe scheduled-task installer, offline-safe reconciliation, log rotation, ZIP and MCPB packaging.
- Compose deployment/limits/health, backup/restore, unit diagnostics and disposable representative evaluation.
- Version 0.2.0 repair release: separate lexical/text/visual readiness with provider backfill, per-attempt paid-call accounting from reported usage, locator-owned visual evidence, current-only default search with cross-channel filters, visual cache recovery, and checksum-safe artifact delivery.

## Verification record

Validated on the deployment host on 2026-09-06:

- `scripts/verify.sh` after the 0.2.0 deployment: 15 backend tests and four connector tests passed; the connector test suite and production executable compiled for Windows; API, worker and Qdrant were running; Windows EXE/ZIP/MCPB hashes passed.
- Strengthened real HTTP/worker evaluation: 13 generated files, 216,815 bytes; all 13 became lexically ready and raw staging returned to zero. Exact-ID, Vietnamese, both conflicting dates, both relevant programme sources, unsupported-query caution, complete citation IDs/locators, and structured `Costs!B2:B4 = 60` checks passed. PDF OCR and visual preview delivery were exercised.
- Remote route: public OAuth metadata returned 200, anonymous MCP returned 401, OAuth dynamic registration + PKCE authorization-code exchange succeeded, and authenticated MCP initialize/tool discovery returned all nine focused tools. The private UI/API returned 403 through public Funnel and 200 through the tailnet.
- The original restore drill remains valid. A separate pre-0.2.0 project backup was created before the schema migration, and the post-upgrade full verifier passed.
- Active synthetic documents were removed after validation so the operator starts with no current versions; two-scan confirmed deletion removed versions, search entries, vectors, staging files, registered previews and orphaned render files.
- The infrastructure whole-host verification passed every existing endpoint and exposure assertion. Post-test idle use was approximately API 167 MiB, worker 144 MiB and Qdrant 17 MiB; 7.8 GiB host memory and 362 GiB disk remained available. The pre-existing `maps-valhalla` Docker health label remained unhealthy while its verified route continued to answer; it was not changed.
- Provider state was intentionally unconfigured/zero allowance. Mocked regressions validate reported usage fields and budget state transitions, but live Voyage-backed semantic, visual-vector and reranking quality remain untested.

## External checks still requiring the operator

- Run/install the connector on Windows and exercise DPAPI, the picker, Task Scheduler, network/cloud sources and pause/uninstall.
- Enter a personal Voyage key plus allowance locally, then run the small live-provider evaluation.
- Authorize the remote MCP app in the user's ChatGPT account and perform a real search/fetch.
- Install the MCPB in Claude Desktop and perform a real local search/fetch.
