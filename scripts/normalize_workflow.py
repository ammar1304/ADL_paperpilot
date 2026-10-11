#!/usr/bin/env python3
"""Turn an `n8n export:workflow` file into a clean, diff-friendly workflow file.

Keeps only what is needed to re-import the workflow, removes pinned test data,
and sets `active` to false (the n8n entrypoint publishes workflows explicitly).

    python scripts/normalize_workflow.py < exported.json > n8n/workflows/00-router.json
"""

import json
import sys


def main() -> int:
    parsed = json.load(sys.stdin)
    wf = parsed[0] if isinstance(parsed, list) else parsed
    if not isinstance(wf, dict) or not wf.get("id"):
        print("normalize_workflow: input has no workflow with an id", file=sys.stderr)
        return 1
    clean = {
        "id": wf["id"],
        "name": wf["name"],
        "active": False,
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": wf.get("settings") or {},
        "pinData": {},
        "tags": [],
    }
    json.dump(clean, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
