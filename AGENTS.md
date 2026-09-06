# Personal Vault RAG agent brief

Read `~/AGENTS.md` and this file before changes. The project is a three-service Compose application: API on loopback port 5001, one resource-limited heavy worker, and Qdrant. SQLite, Qdrant, encrypted provider settings, staging files, and previews live under the ignored host data directory configured by `PVR_DATA_DIR`.

Invariants:

- Every database row, point, source, request, and file operation is scoped by an authenticated vault. A caller-supplied vault ID is never authorization.
- `/vault/` is tailnet-only. Only the OAuth/MCP routes under `/vault-mcp/` may be public, and MCP tools require a bearer token.
- Originals remain on the source. Backend uploads are staging objects with byte and lifetime limits and are deleted after extraction or expiry.
- A disconnected source is not deletion. Two complete reconciliations are required before removal.
- Paid calls require a per-vault encrypted key and non-zero local allowance. Never add fake production embeddings.
- `rerank-2.5` remains default while rerank-3 is preview. Text and visual rankings are fused by rank, never raw-score comparison.
- One worker only on this host. Do not increase concurrency without measuring whole-host pressure.
- Keep generated `.env`, data, Windows binaries, MCPB bundles, credentials, personal content, and host identifiers out of git.

Use `scripts/vaultctl verify`. Read `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, and `docs/HANDOFF.md`. Update docs, commit, and push intentional changes under the workspace standing authorization.
