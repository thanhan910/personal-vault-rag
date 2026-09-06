# Decisions

- Keep originals on source machines. Raw server uploads expire after 24 hours and are deleted immediately after successful extraction.
- Use one Qdrant text collection and one visual collection per vault. This costs some collection overhead but strengthens isolation and keeps model spaces explicit.
- Default to `voyage-4-large` at 1024 dimensions, `voyage-multimodal-3.5`, and stable `rerank-2.5`. `rerank-3` remains preview and is not the production default.
- Keep lexical search fully functional with no key. Never manufacture hash vectors or claim semantic readiness.
- Store provider keys encrypted by a host-local key and require a non-zero per-vault calendar-month allowance before each paid call. Reservations prevent concurrent calls overshooting the local allowance; it cannot cap other consumers of the same provider account.
- Use rank fusion rather than mixing text and visual similarity numbers. Text reranking cannot validate visual content.
- Sample video frames every 30 seconds, at most 120; retain previews for 14 days. The returned warning says fleeting content may be missed.
- Preserve source versions and locators. Two complete reconciliations are the deletion threshold; incomplete/offline scans never count.
- Use local MCPB packaging for Claude Desktop and remote Streamable HTTP MCP for ChatGPT. Ordinary MCP is not assumed to receive every conversation attachment.
- Keep Qdrant's container root writable because it requires a small init marker and snapshot temp path; persistent data/snapshots are explicit host binds, the process is a host-mapped non-root UID with all capabilities dropped.
