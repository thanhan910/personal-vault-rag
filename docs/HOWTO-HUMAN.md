# Operator guide

Open the private Vault URL from a tailnet device. Paste the administrator token from the ignored data directory into the browser login; it stays in browser local storage. The readiness cards show lexical search immediately and semantic/visual/rerank readiness only after configuration.

To connect Windows, select **Generate pairing code**, download/extract the Windows package, run `install.ps1`, enter the private Vault URL and code, then choose source folders and a Saved Artifacts destination. The token is DPAPI-protected for that Windows user. Network/cloud placeholders are read only when Windows can hydrate them; unavailable items remain reported rather than deleted.

Enter a Voyage key only in **Provider settings** and set an explicit monthly USD allowance. The initial import may use text embedding at the provider's current per-token rate and visual embedding by processed pixels; actual content determines cost. The local ledger reserves before a call. Start with a small allowance and inspect usage rather than estimating the whole corpus by paid scanning.

For ChatGPT, enable Developer mode in ChatGPT web, create an MCP app using the remote MCP URL, and complete its OAuth screen with the vault token. The token goes only to the Vault authorization page—never into chat. For Claude Desktop on Windows, install the `.mcpb` from Settings → Extensions → Advanced settings after the connector is paired.

Ask clients to search before collection-specific answers, fetch adjacent evidence, cite title plus document/version/location IDs, compare conflicting revisions, and abstain when evidence is absent. These server instructions assist but cannot guarantee a subscription model's final wording.

Chat attachments are temporary unless you explicitly call save. Remote inspection returns a queued ID to poll. A save remains pending until the Windows connector confirms it wrote the original into the selected destination; if Windows is offline, the bounded staging copy expires rather than being falsely called saved.
