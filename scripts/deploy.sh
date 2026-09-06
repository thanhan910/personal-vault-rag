#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_dir"

if [[ ! -f .env ]]; then
  host_name=$(hostname)
  tailnet_suffix=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["MagicDNSSuffix"])')
  data_dir="$HOME/Personal-Vault-RAG"
  umask 077
  sed \
    -e "s|PVR_DATA_DIR=/home/USER/Personal-Vault-RAG|PVR_DATA_DIR=$data_dir|" \
    -e "s|PVR_UID=1000|PVR_UID=$(id -u)|" \
    -e "s|PVR_GID=1000|PVR_GID=$(id -g)|" \
    -e "s|HOST.TAILNET.ts.net|$host_name.$tailnet_suffix|g" \
    .env.example > .env
fi

data_dir=$(awk -F= '$1=="PVR_DATA_DIR"{print substr($0,index($0,"=")+1)}' .env)
mkdir -p "$data_dir"/{staging,previews,secrets,qdrant,qdrant-snapshots,models}
chmod 700 "$data_dir" "$data_dir"/{staging,previews,secrets,qdrant,qdrant-snapshots,models}

docker compose config >/dev/null
docker compose build
docker compose up -d qdrant api

for _ in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:5001/api/health >/dev/null; then break; fi
  sleep 2
done
curl -fsS http://127.0.0.1:5001/api/health >/dev/null

bootstrap="$data_dir/secrets/bootstrap-code"
initial_token="$data_dir/secrets/initial-access-token"
if [[ -f "$bootstrap" && ! -f "$initial_token" ]]; then
  response=$(curl -fsS -X POST -H "X-Bootstrap-Code: $(cat "$bootstrap")" -H 'Content-Type: application/json' -d '{"name":"Personal Vault"}' http://127.0.0.1:5001/api/setup)
  python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' <<<"$response" > "$initial_token"
  chmod 600 "$initial_token"
fi

docker compose up -d worker
"$repo_dir/scripts/build-windows.sh"
"$repo_dir/scripts/verify.sh"
