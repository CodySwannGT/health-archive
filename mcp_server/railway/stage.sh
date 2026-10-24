#!/bin/sh
# Stage only code, the new-patient template archive and the embedding model for a
# Railway build. No identity profile or archive data is included.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
stage=${1:?usage: stage.sh EMPTY_DIRECTORY}
mkdir -p "$stage"
[ -z "$(ls -A "$stage")" ] || { echo "stage directory must be empty" >&2; exit 2; }
cd "$root"
# database/models holds the exact public embedding model files whose hashes the indexed
# embeddings record; search refuses any other copy.
for path in template database/tools database/sql database/requirements.lock database/models symptoms/tools meds/tools mcp_server; do
  mkdir -p "$stage/$(dirname "$path")"
  rsync -a --exclude '__pycache__' --exclude '.cache' "$path" "$stage/$(dirname "$path")/"
done
cp mcp_server/railway/railway.json "$stage/railway.json"
cp mcp_server/Dockerfile "$stage/Dockerfile"
echo "$stage"
