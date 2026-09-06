# Personal Vault RAG notes for Claude Code

Read `AGENTS.md`. Do not expose `/vault/` through Funnel or weaken vault scoping. The remote `/vault-mcp/` surface is deliberately limited to OAuth discovery/flows and the authenticated MCP endpoint. Never store originals after extraction, treat an offline source as deletion, or label lexical-only search as semantic retrieval.

The Windows binary has two roles: background outbound source sync and a local stdio MCP adapter for Claude Desktop. Its token is protected with Windows DPAPI. Linux can cross-compile and protocol-test the artifact but cannot execute the Windows/DPAPI/folder-picker/scheduled-task path.
