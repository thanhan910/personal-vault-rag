# Architecture

Personal Vault has three Linux services and one Windows client. FastAPI serves the private administration/search API and the authenticated MCP resource; a single independently restartable worker leases durable SQLite jobs; Qdrant stores per-vault text and visual vectors. SQLite is the source of truth for vaults, sources, versions, locators, jobs, budgets and temporary-save state.

The Windows connector reads only user-selected roots. It uploads a bounded staging copy over HTTPS, after which the worker extracts durable text/metadata/previews and deletes the original bytes. Source originals stay on Windows. If a source is offline, indexed evidence remains available and is labelled offline; deletion requires two complete later reconciliations.

Every access token resolves to one server-side vault. Database lookups, FTS queries, Qdrant collections, preview reads, writes and downloads add that scope rather than accepting a caller's vault ID. Separate per-vault Qdrant collections also prevent vector-filter mistakes becoming a cross-vault leak. Tests exercise direct-ID denial and a second vault.

Retrieval combines SQLite FTS5 BM25/exact terms, Voyage text embeddings and Voyage multimodal candidates with reciprocal-rank fusion. Candidate scores from different spaces are never compared directly. `rerank-2.5` optionally reranks text, then results are diversified by document. Missing credentials or a zero allowance leaves truthful lexical-only operation. Spreadsheet aggregation reads persisted cell values for a bounded range instead of asking nearest-neighbour text to do arithmetic.

Docling is primary for PDF, Office and image structure/OCR; local format fallbacks preserve useful text when it fails. PDF pages, source images and bounded video frames can be inspected by a capable client. FFmpeg plus faster-whisper produces timestamped local transcripts. Markdown/TXT use line groups and headings, never invented pages.

The admin UI and viewers live behind the host's tailnet gate. Only OAuth metadata/endpoints and MCP transport are routed publicly. OAuth uses dynamic client registration, authorization code + PKCE S256, short access tokens and rotating refresh tokens. MCP tools still enforce token scopes. Chat attachments are bounded, SSRF-checked downloads and heavy extraction is queued; explicit saves remain pending until Windows confirms a source write.

Scale assumptions are tens of thousands of ordinary files, not unlimited use. One heavy worker, bounded batches, leases, retries, fair vault rotation, quotas, minimum-free-space protection and container limits protect the shared four-core host. SQLite/job and vector interfaces are deliberately separable if measured contention later warrants a shared job store.
