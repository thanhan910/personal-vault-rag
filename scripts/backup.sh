#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_dir"
data_dir=$(awk -F= '$1=="PVR_DATA_DIR"{print substr($0,index($0,"=")+1)}' .env)
destination=${1:-$data_dir/backups}
mkdir -p "$destination"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$destination/personal-vault-rag-$stamp.tar.gz"

docker compose stop worker api qdrant
trap 'docker compose up -d qdrant api worker' EXIT
tar -C "$data_dir" -czf "$target" catalog.sqlite3 qdrant qdrant-snapshots secrets previews
tar -tzf "$target" >/dev/null
chmod 600 "$target"
echo "$target"
