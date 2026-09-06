#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_dir"
backup=$1
test -r "$backup"
tar -tzf "$backup" >/dev/null
if tar -tzf "$backup" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then echo "Unsafe backup paths" >&2; exit 1; fi
data_dir=$(awk -F= '$1=="PVR_DATA_DIR"{print substr($0,index($0,"=")+1)}' .env)
docker compose stop worker api qdrant
tar -C "$data_dir" -xzf "$backup"
docker compose up -d qdrant api worker
"$repo_dir/scripts/verify.sh"
