#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_dir"
docker compose ps --status running --services | grep -qx api
docker compose ps --status running --services | grep -qx worker
docker compose ps --status running --services | grep -qx qdrant
curl -fsS http://127.0.0.1:5001/api/health >/dev/null
curl -fsS http://127.0.0.1:5001/ >/dev/null
curl -fsS http://127.0.0.1:6334/healthz >/dev/null 2>&1 || docker compose exec -T qdrant bash -c 'exec 3<>/dev/tcp/127.0.0.1/6333'
docker compose run --rm --no-deps --entrypoint pytest api -q -p no:cacheprovider
test -s dist/windows/personal-vault-connector-windows-amd64.zip
test -s dist/windows/personal-vault-claude-windows.mcpb
sha256sum -c dist/windows/SHA256SUMS
python3 -m json.tool packaging/claude-extension/manifest.json >/dev/null
echo "Personal Vault verification passed"
