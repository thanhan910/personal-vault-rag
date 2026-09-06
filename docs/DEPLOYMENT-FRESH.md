# Fresh deployment

1. Restore/clone this private repository and create `.env` from `.env.example`.
2. Create the local data path with mode `700`; never place it in Git or cloud sync.
3. Run `scripts/vaultctl deploy`. It builds the pinned CPU-only image, starts Qdrant/API, performs one-time vault creation, starts one worker, builds Windows artifacts and verifies the stack.
4. Configure reverse proxy routes so the UI/viewer stays private and OAuth/MCP remains authenticated. Validate the proxy before reload.
5. Store the one-time administrator token securely. Generate a short pairing code in the UI for each Windows connector.
6. Run the synthetic evaluation before personal ingestion. Record any host-specific integration outside Git if it contains identifiers.

The deployment intentionally does not enable an unproven scheduled backup destination. Follow [backup/restore](BACKUP-RESTORE.md).
