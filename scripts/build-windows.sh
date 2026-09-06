#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
dist_dir="$repo_dir/dist/windows"
stage_dir="$repo_dir/build/windows-package"

mkdir -p "$dist_dir" "$stage_dir" "$repo_dir/packaging/claude-extension/server"
docker run --rm --network none \
  -e CGO_ENABLED=0 -e GOOS=windows -e GOARCH=amd64 \
  -v "$repo_dir/connector:/src:ro" -v "$stage_dir:/out" \
  -w /src golang:1.24.6-bookworm \
  go build -trimpath -ldflags='-s -w' -o /out/personal-vault-connector.exe .

cp "$stage_dir/personal-vault-connector.exe" "$repo_dir/packaging/claude-extension/server/personal-vault-connector.exe"
cp "$stage_dir/personal-vault-connector.exe" "$dist_dir/"
cp "$repo_dir/connector/install.ps1" "$repo_dir/connector/uninstall.ps1" "$dist_dir/"
cp "$repo_dir/docs/WINDOWS-CONNECTOR.md" "$dist_dir/README.txt"

(cd "$repo_dir/packaging/claude-extension" && zip -q -r "$dist_dir/personal-vault-claude-windows.mcpb" manifest.json server)
(cd "$dist_dir" && zip -q personal-vault-connector-windows-amd64.zip personal-vault-connector.exe install.ps1 uninstall.ps1 README.txt)

sha256sum "$dist_dir/personal-vault-connector.exe" "$dist_dir/personal-vault-connector-windows-amd64.zip" "$dist_dir/personal-vault-claude-windows.mcpb" > "$dist_dir/SHA256SUMS"
