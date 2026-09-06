# Progress

## Implemented

- Vault-scoped FastAPI/SQLite catalogue, FTS5, Qdrant text/visual collections, resumable jobs, quotas and budget reservations.
- Docling/OCR/Office extraction, structured XLSX cells, PDF/image previews, faster-whisper timestamps and bounded video sampling.
- Hybrid rank fusion/rerank path, adjacent fetch, visual inspection, precise version/locator results and signed private viewers.
- OAuth 2.0 PKCE remote MCP plus Windows local stdio MCP, queued attachment inspection and connector-confirmed explicit saves.
- Single-binary DPAPI Windows connector, folder picker, scheduled task, offline-safe reconciliation, log rotation, ZIP and MCPB packaging.
- Compose deployment/limits/health, backup/restore, unit diagnostics and disposable representative evaluation.

## Verification record

Validated on the deployment host on 2026-09-06:

- `scripts/verify.sh`: 5 automated tests passed; API, worker and Qdrant running; Windows EXE/ZIP/MCPB hashes passed.
- Real HTTP/worker evaluation: 13 generated files, 216,815 bytes; all 13 indexed; raw staging returned to zero. Exact-ID, Vietnamese, contradictory-source, multi-document, unanswerable/abstention, citation-ID and structured `Costs!B2:B4 = 60` checks passed. PDF OCR and visual preview delivery were exercised.
- Remote route: public OAuth metadata returned 200, anonymous MCP returned 401, OAuth dynamic registration + PKCE authorization-code exchange succeeded, and authenticated MCP initialize/tool discovery returned all nine focused tools. The private UI/API returned 403 through public Funnel and 200 through the tailnet.
- A project backup was created, restored, checksumed and followed by a successful full project verification.
- The synthetic source was removed after validation so the operator starts with an empty vault; two-scan confirmed deletion was re-tested to remove database rows, search entries, vectors, staging files, registered previews and orphaned render files.
- The infrastructure whole-host verification passed every existing endpoint and exposure assertion. Post-test idle use was approximately API 167 MiB, worker 144 MiB and Qdrant 17 MiB; 7.8 GiB host memory and 362 GiB disk remained available. The pre-existing `maps-valhalla` Docker health label remained unhealthy while its verified route continued to answer; it was not changed.
- Provider state was intentionally unconfigured/zero allowance. This validates truthful lexical mode, not Voyage-backed semantic, visual-vector or reranking quality.

## External checks still requiring the operator

- Run/install the connector on Windows and exercise DPAPI, the picker, Task Scheduler, network/cloud sources and pause/uninstall.
- Enter a personal Voyage key plus allowance locally, then run the small live-provider evaluation.
- Authorize the remote MCP app in the user's ChatGPT account and perform a real search/fetch.
- Install the MCPB in Claude Desktop and perform a real local search/fetch.
