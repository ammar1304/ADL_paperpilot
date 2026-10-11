#!/usr/bin/env bash
# Copies the workflows currently in n8n back into n8n/workflows/ (the source of truth).
#
# Run this after editing a workflow in the n8n UI and BEFORE restarting n8n:
# the n8n container re-imports n8n/workflows/ on every start and would
# overwrite your UI edits.
#
# Only workflows that already have a file in n8n/workflows/ are exported. To add
# a new workflow, export it once from the UI (or the CLI), give it a top-level
# "id", save it in n8n/workflows/, and from then on this script keeps it in sync.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

for f in n8n/workflows/*.json; do
    id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$f")
    echo "exporting ${id} -> ${f}"
    tmp="$(mktemp)"
    docker compose exec -T n8n sh -c "n8n export:workflow --id='${id}' --output=/tmp/pp-export.json >/dev/null && cat /tmp/pp-export.json" \
        | python3 scripts/normalize_workflow.py > "$tmp"
    mv "$tmp" "$f"
done

echo "Done. Review with: git diff n8n/workflows/"
