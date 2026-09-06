# Setup

For this installed host, use [the operator guide](HOWTO-HUMAN.md). For another installation:

1. Install Docker Engine, Compose v2, `curl`, `zip`, and a recent Git.
2. Clone privately, copy `.env.example` to `.env`, replace `USER` and the public/private HTTPS host names, and keep the file mode `600`.
3. Ensure the chosen `PVR_DATA_DIR` is a host-local SSD path, not SMB/NFS or a sync folder.
4. Run `scripts/vaultctl deploy`. Retrieve the one-time access token from the ignored data directory and move it to a password manager.
5. Put the private UI behind authenticated private ingress. Publish only the documented OAuth/MCP endpoints if a remote client is required.

The first deployment defaults to lexical-only search. Add the Voyage key and a deliberate monthly allowance in the private UI; the server never reads another application's credentials.
