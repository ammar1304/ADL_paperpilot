#!/usr/bin/env python3
"""One P4 call to Groq with exactly the workflow's request body, in about 30 seconds.

Creates an audit run for a PDF (parsed from the cache if seen before), renders
the first batch of candidate sentences, sends it to Groq the way the 30-audit
"Extract claims" node does (model, reasoning effort, strict JSON schema taken
from the workflow file), prints Groq's answer and saves it through pp-tools so
the number guardrail runs too. Needs GROQ_API_KEY in .env.

    python scripts/groq_claims_probe.py
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_test import _dotenv, http  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def workflow_body(system: str, user: str) -> dict:
    node = next(n for n in json.loads((REPO / "n8n/workflows/30-audit.json").read_text(encoding="utf-8"))["nodes"]
                if n["name"] == "Extract claims")
    text = node["parameters"]["jsonBody"]
    schema = json.loads(text.split("strict: true, schema: ", 1)[1].rsplit(" } } }) }}", 1)[0])
    return {
        "model": re.search(r"model: '([^']+)'", text).group(1),
        "temperature": 0,
        "reasoning_effort": re.search(r"reasoning_effort: '(\w+)'", text).group(1),
        "include_reasoning": False,
        "max_completion_tokens": int(re.search(r"max_completion_tokens: (\d+)", text).group(1)),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "claims", "strict": True, "schema": schema}},
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    env = _dotenv()
    key = env.get("GROQ_API_KEY")
    if not key:
        print("GROQ_API_KEY is empty in .env")
        return 2
    pp = f"http://127.0.0.1:{env.get('PP_TOOLS_HOST_PORT', '8000')}"
    url = sys.argv[1] if len(sys.argv) > 1 else "https://arxiv.org/pdf/2401.15884v3"
    _, run = http("POST", f"{pp}/runs", {"request": {"mode": "audit", "draft_pdf_url": url}})
    run_id = run["run_id"]
    _, job = http("POST", f"{pp}/audit/{run_id}/parse")
    while True:
        _, j = http("GET", f"{pp}/jobs/{job['job_id']}")
        if j["status"] in ("done", "failed"):
            break
        time.sleep(3)
    batch = http("POST", f"{pp}/audit/{run_id}/claims/next")[1]
    body = workflow_body(batch["system"], batch["user"])
    print(f"run {run_id}, batch {batch['batch']} of {batch['batches']}, model {body['model']}, reasoning {body['reasoning_effort']}, "
          f"max {body['max_completion_tokens']}")
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "PaperPilot/0.1"}, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            answer = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"Groq HTTP {exc.code}: {exc.read().decode()[:1500]}")
        return 1
    print(f"Groq answered in {time.monotonic() - started:.1f} s, usage: {answer.get('usage')}")
    content = answer["choices"][0]["message"].get("content") or ""
    print("finish_reason:", answer["choices"][0].get("finish_reason"), "| content chars:", len(content))
    status, saved = http("POST", f"{pp}/audit/{run_id}/claims", {"batch": batch["batch"], "output_text": content, "usage": answer.get("usage")})
    print("pp-tools:", status, saved)
    _, claims = http("GET", f"{pp}/audit/{run_id}/claims")
    for c in claims.get("claims", [])[:15]:
        vals = ", ".join(f"{v['system'] or '?'}={v['value']}" for v in c["values"])
        print(f"  [{c['type']}] {c['claim'][:100]} | {vals} | refs {c['refers_to']} cites {c['cites']}")
    for d in claims.get("dropped", [])[:5]:
        print("  dropped:", d["reason"], "|", d["claim"][:80])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
