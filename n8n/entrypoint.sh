#!/bin/sh
# Prepares n8n, then starts it:
#   1. creates the LLM credentials (Groq, Gemini) from GROQ_API_KEY and GEMINI_API_KEY
#   2. imports the workflows from git and publishes them
#   3. removes the API keys from the environment and starts n8n
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

# ---- 1. LLM credentials ----------------------------------------------------------
# Stored encrypted in n8n's database (N8N_ENCRYPTION_KEY). The ids are fixed
# because the workflow JSON refers to them. A missing key becomes a placeholder:
# the workflows still import, and an LLM call fails with a clear auth error.
creds_file="$(mktemp /tmp/pp-creds-XXXXXX.json)"
trap 'rm -f "$creds_file"' EXIT
node -e '
const fs = require("fs");
const key = (name) => (process.env[name] || "").trim() || "not-set";
fs.writeFileSync(process.argv[1], JSON.stringify([
  { id: "ppGroqCredent001", name: "Groq (PaperPilot)", type: "groqApi",
    data: { apiKey: key("GROQ_API_KEY") } },
  { id: "ppGeminiCredent1", name: "Gemini (PaperPilot)", type: "googlePalmApi",
    data: { host: process.env.GEMINI_API_HOST || "https://generativelanguage.googleapis.com", apiKey: key("GEMINI_API_KEY") } },
]));
' "$creds_file"
[ -n "${GROQ_API_KEY:-}" ] || echo "[entrypoint] WARNING: GROQ_API_KEY is empty; LLM nodes will fail until it is set in .env"
[ -n "${GEMINI_API_KEY:-}" ] || echo "[entrypoint] note: GEMINI_API_KEY is empty; the fallback model is unavailable"
echo "[entrypoint] importing LLM credentials"
$N8N_BIN import:credentials --input="$creds_file"
rm -f "$creds_file"

# ---- 2. workflows ----------------------------------------------------------------------
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

# ---- 3. start ----------------------------------------------------------------------------
# Workflows can read environment variables ($env), so the raw keys must not
# stay in n8n's environment once they are stored as credentials (DECISIONS D6, D21).
unset GROQ_API_KEY GEMINI_API_KEY
exec $N8N_BIN start
