# Mini-PC deployment

This checkout is deployed as Compose project `personal-vault-rag`. The API binds only `127.0.0.1:5001`; Qdrant has no host-published port. Live data is under the ignored `PVR_DATA_DIR` declared in `.env`. The host reverse proxy supplies TLS and routes the private `/vault/` UI separately from public authenticated `/vault-mcp/` OAuth/MCP endpoints.

The API is limited to one CPU/768 MiB, the single worker to two CPUs/3 GiB, and Qdrant to one CPU/2 GiB. All have PID and rotated-log bounds. Do not raise worker concurrency until measured host memory, swap, temperature and other services remain healthy during representative imports.

Deploy with `scripts/vaultctl deploy`; update with `scripts/vaultctl update`; validate with `scripts/vaultctl verify`. Host Nginx and navigation are maintained in the infrastructure repository, not here.
