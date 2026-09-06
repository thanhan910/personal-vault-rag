# Windows connector

The connector is a single Windows x86-64 executable. It needs no Docker, Python, Node.js, or package manager on the source PC. Setup uses the normal Windows folder picker, pairs outbound to the backend, protects the vault token with Windows DPAPI, and creates a per-user scheduled task at logon.

## Install

1. Extract `personal-vault-connector-windows-amd64.zip`.
2. In that extracted folder, right-click `install.ps1` and choose **Run with PowerShell**, or run:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
   ```

3. Enter the private setup URL and pairing code shown by the Vault admin page. Select one or more source folders, then select the destination for explicitly saved chat artifacts. If that destination is not already beneath a selected root, setup registers it as an additional indexed source.

The connector reads only selected roots. UNC paths and mapped network drives work while available. Opening a cloud placeholder may ask the sync client to download it and therefore uses laptop disk; an unavailable placeholder is reported, not marked indexed. Google-native cloud documents that exist only as link/placeholder metadata require export and are unsupported until a provider adapter is added.

Pause with `%LOCALAPPDATA%\PersonalVault\personal-vault-connector.exe pause`; resume with the same command ending in `resume`. The daemon observes changes during idle waits within five seconds and applies them before the next scan; `status` reports configured and last effective state. `uninstall.ps1` removes the scheduled task but deliberately leaves recoverable configuration. Logs are under `%LOCALAPPDATA%\PersonalVault`.

## Claude Desktop

After the connector has been paired, open Claude Desktop → Settings → Extensions → Advanced settings → Install Extension, and select `personal-vault-claude-windows.mcpb`. Restart Claude Desktop if the tools do not appear. The bundle uses the same DPAPI-protected connector configuration.

Ordinary MCP does not automatically receive every attachment added to a Claude conversation. Save files into a selected source folder, or use the configured Saved Artifacts destination, and let the connector index them. Remote attachment delivery writes binary bytes first, verifies size and SHA-256, and stores any annotation in a separate `.note.md` sidecar. Collision names include the artifact identity and use exclusive creation; retries recognize an already verified delivery. The local tool can save user-authored text notes directly; it does not treat model hypotheses as authoritative facts.

Portable connector behavior is unit-tested on Linux, and the production binary plus test suite are compiled for Windows. Windows execution, DPAPI, the folder picker, Task Scheduler, cloud hydration, and Claude Desktop UI installation require a Windows machine and are not claimed as run here.
