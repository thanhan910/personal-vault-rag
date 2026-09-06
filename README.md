# Personal Vault RAG

Personal Vault is a private, vault-isolated retrieval service for personal files. A lightweight Windows connector reads only selected folders, sends bounded temporary copies for extraction, and keeps originals on the source machine. The backend retains extracted text, metadata, previews where useful, and Qdrant indexes.

The service uses FastAPI, the official MCP Python SDK, SQLite, Qdrant, Docling, FFmpeg, faster-whisper, and optional Voyage embeddings/reranking. With no provider key or budget it remains usable for exact-term/BM25 search and reports semantic features as not configured.

On the mini PC it is served privately at the URL documented in `docs/MINI_PC_DEPLOYMENT.md`; the authenticated remote MCP path is separate. Start with [the human guide](docs/HOWTO-HUMAN.md), [operations](docs/OPERATIONS.md), or [restart recovery](docs/RESTART.md).

## Commands

```bash
scripts/vaultctl deploy
scripts/vaultctl status
scripts/vaultctl verify
scripts/vaultctl logs
scripts/vaultctl stop
```

See [architecture](docs/ARCHITECTURE.md), [fresh deployment](docs/DEPLOYMENT-FRESH.md), and [backup/restore](docs/BACKUP-RESTORE.md).
