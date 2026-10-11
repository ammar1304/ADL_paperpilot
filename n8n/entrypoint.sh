#!/bin/sh
# Imports the workflows from git into n8n, publishes them, then starts n8n.
#
# The JSON files in n8n/workflows/ are the source of truth. With
# PP_IMPORT_WORKFLOWS_ON_START=true (the default) every start OVERWRITES the
# workflows in n8n with the files, so edits made in the n8n UI are lost unless
# you export them first: ./scripts/export_workflows.sh
set -eu

N8N_BIN="${N8N_BIN:-n8n}"
WORKFLOWS_DIR="${PP_WORKFLOWS_DIR:-/pp/workflows}"

workflow_ids() {
    for f in "${WORKFLOWS_DIR}"/*.json; do
        id=$(node -e 'process.stdout.write(String(JSON.parse(require("fs").readFileSync(process.argv[1], "utf8")).id || ""))' "$f")
        if [ -z "$id" ]; then
            echo "[entrypoint] ERROR: $f has no top-level \"id\"" >&2
            exit 1
        fi
        echo "$id"
    done
}

if [ "${PP_IMPORT_WORKFLOWS_ON_START:-true}" = "true" ]; then
    ids=$(workflow_ids)

    # Importing over a workflow that is already published fails in n8n 2.x
    # ("You do not have permission to deactivate this workflow"), so unpublish
    # our workflows first. On a fresh database they do not exist yet, which
    # n8n reports as an error we can ignore.
    for id in $ids; do
        $N8N_BIN unpublish:workflow --id="${id}" >/dev/null 2>&1 || true
    done

    echo "[entrypoint] importing workflows from ${WORKFLOWS_DIR}"
    $N8N_BIN import:workflow --separate --input="${WORKFLOWS_DIR}"

    for id in $ids; do
        echo "[entrypoint] publishing ${id}"
        $N8N_BIN publish:workflow --id="${id}"
    done
else
    echo "[entrypoint] PP_IMPORT_WORKFLOWS_ON_START=false, keeping workflows already in n8n"
fi

exec $N8N_BIN start
