#!/usr/bin/env python3
"""Prompt A/B evaluation: the same ideas through the real novelty pipeline, per prompt version.

One run per idea, configuration and repeat goes through the n8n webhook, exactly
as a user would send it. Between configurations the script switches
prompts/active.json (pp-tools reads it on every call), and restores it at the end.
Failures are counted automatically from the stored run, never judged by eye:

  - retrieval:  expected papers among the judge's candidates and in closest_work,
                and how similar the decomposer's queries are to each other
  - report:     overlap claimed for an idea where nothing should overlap
  - LLM prose:  summary states the search scope (F-002); words that claim novelty
                or coverage (F-003); discussion of facets the idea left unspecified
  - stability:  whether repeats give the same labels (gpt-oss varies at temperature 0)

Usage (stack running, knowledge base built, GROQ_API_KEY set):

    python scripts/prompt_ab.py                       # 6 ideas x 3 configs x 2 repeats, about 45 min
    python scripts/prompt_ab.py --repeats 1 --ideas adaptive-retrieval,out-of-domain
    python scripts/prompt_ab.py --config "base=P1-decomposer:v1,P3-novelty-judge:v1" --config "new=P1-decomposer:v2,P3-novelty-judge:v2"

Results: eval/results/<timestamp>-prompt-ab/ with runs.csv, runs.jsonl (every
full result and step) and summary.md (the table for the report).

Groq's free tier limits tokens per minute; --pause (default 60 s) spaces the runs.
A run refused for rate limiting is retried once after --rate-limit-wait seconds.
"""

import argparse
import csv
import itertools
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_test import _dotenv, http  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ACTIVE = REPO / "prompts" / "active.json"
DEFAULT_CONFIGS = [
    "baseline=P1-decomposer:v1,P3-novelty-judge:v1",
    "judge-v2=P1-decomposer:v1,P3-novelty-judge:v2",
    "full-v2=P1-decomposer:v2,P3-novelty-judge:v2",
]
FACETS = ("task", "method", "data", "result")

SCOPE_RE = re.compile(r"\b\d[\d,]*\s+(?:papers|candidate papers)\b", re.I)
OVERCLAIM_RE = re.compile(
    r"\b(novel|novelty|new idea|unexplored|uncovered|not (?:yet )?(?:been )?addressed|remains? open|open problem|gap in the literature)\b",
    re.I,
)
UNSPECIFIED_RE = {
    "data": re.compile(r"\b(data|datasets?|benchmarks?)\b", re.I),
    "result": re.compile(r"\b(results?|outcomes?|performance gains?)\b", re.I),
    "task": re.compile(r"\btask\b", re.I),
    "method": re.compile(r"\bmethod\b", re.I),
}
# A coverage claim, as opposed to a neutral "unknown" or "no passage says".
COVERAGE_RE = re.compile(
    r"\b(uncovered|not (?:been )?covered|less covered|least covered|not addressed|largely unspecified|gap)\b", re.I
)
RATE_LIMIT_RE = re.compile(r"too many requests|rate.?limit|429|tokens per minute", re.I)


# ---- pure scoring (unit-tested in services/pp_tools/tests/test_prompt_ab.py) -------------------


def parse_config(text: str) -> tuple[str, dict[str, str]]:
    """'name=P1-decomposer:v1,P3-novelty-judge:v2' -> ('name', {...})."""
    name, _, spec = text.partition("=")
    versions = {}
    for part in spec.split(","):
        prompt, _, version = part.strip().partition(":")
        if not prompt or not re.fullmatch(r"v\d+", version):
            raise ValueError(f"bad --config {text!r}: expected name=PROMPT:vN,PROMPT:vN")
        versions[prompt] = version
    if not name or not versions:
        raise ValueError(f"bad --config {text!r}")
    return name, versions


def query_similarity(queries: list[str]) -> float | None:
    """Mean pairwise Jaccard similarity of the queries' word sets: 1.0 means paraphrases."""
    sets = [set(re.findall(r"[a-z0-9]+", q.lower())) for q in queries]
    pairs = list(itertools.combinations(sets, 2))
    if not pairs:
        return None
    return round(sum(len(a & b) / len(a | b) for a, b in pairs if a | b) / len(pairs), 3)


def summary_checks(text: str | None, unspecified: list[str]) -> dict[str, bool | None]:
    """F-002 (scope), F-003 (coverage claims), and mentions of facets the idea left unspecified.

    llm_discusses_unspecified counts any mention ("no passage gives data or results" included);
    llm_coverage_claim_unspecified counts only sentences that call such a facet covered or
    uncovered, which is the actual F-003 failure.
    """
    if not text:
        return {"llm_states_scope": None, "llm_overclaims": None, "llm_discusses_unspecified": None,
                "llm_coverage_claim_unspecified": None}
    regexes = [UNSPECIFIED_RE[f] for f in unspecified if f in UNSPECIFIED_RE]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return {
        "llm_states_scope": bool(SCOPE_RE.search(text)),
        "llm_overclaims": bool(OVERCLAIM_RE.search(text)),
        "llm_discusses_unspecified": any(r.search(text) for r in regexes),
        "llm_coverage_claim_unspecified": any(
            COVERAGE_RE.search(s_) and (any(r.search(s_) for r in regexes) or re.search(r"\b(those|these) facets\b", s_, re.I))
            for s_ in sentences
        ),
    }


def score(idea: dict, result: dict, candidates: list[str], queries: list[str]) -> dict:
    """One row of measurements from a finished run."""
    expected = set(idea.get("expected", []))
    closest = [m["paper_id"] for m in result.get("closest_work", [])]
    judged = closest + [m["paper_id"] for m in result.get("checked_not_close", [])]
    row = {
        "n_queries": len(queries),
        "query_similarity": query_similarity(queries),
        "expected": len(expected),
        "expected_in_candidates": len(expected & set(candidates)),
        "expected_in_closest": len(expected & set(closest)),
        "overlap_found": result.get("overlap_found"),
        "false_overlap": bool(idea.get("expect_no_overlap") and result.get("overlap_found")),
        "n_closest": len(closest),
        "n_judged": len(judged),
        "unverified": len(result.get("unverified", [])),
        "judge_attempts": result.get("judge_attempts"),
    }
    row.update(summary_checks(result.get("summary_llm_written"), result.get("unspecified_facets", [])))
    return row


def label_agreement(runs: list[dict]) -> float | None:
    """Share of (paper, facet) labels that are identical across repeats of the same idea and config."""
    labels = [
        {(m["paper_id"], f): m["overlap"][f] for m in r.get("closest_work", []) + r.get("checked_not_close", []) for f in FACETS}
        for r in runs
    ]
    if len(labels) < 2:
        return None
    common = set.intersection(*(set(lab) for lab in labels))
    if not common:
        return None
    same = sum(1 for key in common if len({lab[key] for lab in labels}) == 1)
    return round(same / len(common), 3)


def _rate(rows: list[dict], key: str) -> str:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return f"{sum(bool(v) for v in vals)}/{len(vals)}" if vals else "n/a"


def _mean(vals: list[float]) -> str:
    vals = [v for v in vals if v is not None]
    return f"{sum(vals) / len(vals):.2f}" if vals else "n/a"


def summarize(rows: list[dict], agreements: dict[tuple[str, str], float | None], configs: list[str]) -> str:
    lines = [
        "| Config | Runs ok | Expected papers in candidates | Expected in closest work | Query similarity | False overlap | LLM states scope | LLM overclaims | LLM calls unspecified facets (un)covered | LLM mentions unspecified facets | Label agreement across repeats | Judge attempts |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in configs:
        rs = [r for r in rows if r["config"] == name]
        ok = [r for r in rs if r["ok"]]
        exp = sum(r["expected"] for r in ok)
        found_c = sum(r["expected_in_candidates"] for r in ok)
        found_k = sum(r["expected_in_closest"] for r in ok)
        agree = [v for (cfg, _), v in agreements.items() if cfg == name]
        lines.append(
            f"| {name} | {len(ok)}/{len(rs)} | {found_c}/{exp} | {found_k}/{exp} | {_mean([r['query_similarity'] for r in ok])} "
            f"| {_rate(ok, 'false_overlap')} | {_rate(ok, 'llm_states_scope')} | {_rate(ok, 'llm_overclaims')} "
            f"| {_rate(ok, 'llm_coverage_claim_unspecified')} | {_rate(ok, 'llm_discusses_unspecified')} "
            f"| {_mean(agree)} | {_mean([r['judge_attempts'] for r in ok])} |"
        )
    ideas = list(dict.fromkeys(r["idea"] for r in rows))
    lines += ["", "Expected papers among the judge's candidates, per idea (found / expected, summed over repeats):", "",
              "| Idea | " + " | ".join(configs) + " |", "| --- |" + " --- |" * len(configs)]
    for idea in ideas:
        cells = []
        for name in configs:
            rs = [r for r in rows if r["config"] == name and r["idea"] == idea and r["ok"]]
            cells.append(f"{sum(r['expected_in_candidates'] for r in rs)}/{sum(r['expected'] for r in rs)}")
        lines.append(f"| {idea} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Higher is better for: expected papers found, LLM states scope, label agreement.",
        "Lower is better for: query similarity (1.0 = paraphrases), false overlap, overclaims, coverage claims and mentions of unspecified facets, judge attempts.",
        "A coverage claim (\"data and results remain uncovered\") is the F-003 failure; a mention (\"no passage gives data or results\") is a milder rule breach.",
        "Counts are over successful runs; the regular expressions are in scripts/prompt_ab.py.",
    ]
    return "\n".join(lines)


# ---- running ---------------------------------------------------------------------------------


def set_active(versions: dict[str, str], base: dict[str, str]) -> None:
    ACTIVE.write_text(json.dumps({**base, **versions}, indent=2) + "\n", encoding="utf-8")


def run_once(webhook: str, pp: str, idea: dict, wait_on_limit: float) -> tuple[dict | None, dict, str | None]:
    """Returns (result, steps detail, error)."""
    for attempt in (1, 2):
        status, body = http("POST", webhook, {"mode": "novelty", "idea_text": idea["idea"]}, timeout=300)
        if status == 200 and isinstance(body, dict) and body.get("status") == "done":
            s, steps = http("GET", f"{pp}/runs/{body['run_id']}/steps")
            return body["result"] | {"run_id": body["run_id"]}, {st["step_name"]: st["detail"] for st in steps} if s == 200 else {}, None
        _, runs = http("GET", f"{pp}/runs?limit=1")
        error = (runs[0].get("error") or {}).get("message") if isinstance(runs, list) and runs else str(body)[:300]
        if attempt == 1 and error and RATE_LIMIT_RE.search(error):
            print(f"      rate limited, waiting {wait_on_limit:.0f} s and retrying once")
            time.sleep(wait_on_limit)
            continue
        return None, {}, error or f"HTTP {status}"
    return None, {}, "unreachable"


def write_outputs(out: Path, rows: list[dict], results: dict, configs: list[str], meta: dict) -> str:
    fields = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("config", "idea", "repeat", "ok"), k))
    with (out / "runs.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    agreements = {key: label_agreement(rs) for key, rs in results.items()}
    table = summarize(rows, agreements, configs)
    (out / "summary.md").write_text(f"# Prompt A/B\n\n```json\n{json.dumps(meta, indent=2)}\n```\n\n{table}\n", encoding="utf-8")
    return table


def rescore(out: Path, ideas_file: Path) -> int:
    """Apply the current scoring rules to stored runs (no LLM calls)."""
    ideas = {i["id"]: i for i in json.loads(ideas_file.read_text(encoding="utf-8"))}
    rows, results, configs = [], {}, []
    for line in (out / "runs.jsonl").read_text(encoding="utf-8-sig").splitlines():
        rec = json.loads(line)
        row = {k: rec["row"][k] for k in ("config", "idea", "repeat", "ok", "error", "seconds", "run_id") if k in rec["row"]}
        if row["config"] not in configs:
            configs.append(row["config"])
        result, steps = rec.get("result"), rec.get("steps") or {}
        if result is not None:
            retrieved = steps.get("retrieve", {})
            row |= score(ideas[row["idea"]], result, [c["paper_id"] for c in retrieved.get("candidates", [])], retrieved.get("queries", []))
            results.setdefault((row["config"], row["idea"]), []).append(result)
        rows.append(row)
    meta_text = (out / "summary.md").read_text(encoding="utf-8") if (out / "summary.md").exists() else ""
    m = re.search(r"```json\n(.*?)\n```", meta_text, re.S)
    meta = json.loads(m.group(1)) if m else {}
    meta["rescored_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(write_outputs(out, rows, results, configs, meta))
    return 0


def main() -> int:
    env = _dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ideas-file", type=Path, default=REPO / "eval" / "prompt_ab" / "ideas.json")
    parser.add_argument("--ideas", help="comma-separated idea ids (default: all)")
    parser.add_argument("--config", action="append", help="name=PROMPT:vN,PROMPT:vN (repeatable)")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--pause", type=float, default=60, help="seconds between runs (Groq free tier)")
    parser.add_argument("--rate-limit-wait", type=float, default=90)
    parser.add_argument("--n8n-url", default=f"http://localhost:{env.get('N8N_HOST_PORT', '5678')}")
    parser.add_argument("--pp-tools-url", default=f"http://localhost:{env.get('PP_TOOLS_HOST_PORT', '8000')}")
    parser.add_argument("--label", default="prompt-ab")
    parser.add_argument("--rescore", type=Path, help="recompute summary.md and runs.csv from a results folder's runs.jsonl, without running")
    args = parser.parse_args()
    if args.rescore:
        return rescore(args.rescore, args.ideas_file)

    ideas = json.loads(args.ideas_file.read_text(encoding="utf-8"))
    if args.ideas:
        wanted = set(args.ideas.split(","))
        ideas = [i for i in ideas if i["id"] in wanted]
    configs = [parse_config(c) for c in (args.config or DEFAULT_CONFIGS)]
    original = ACTIVE.read_text(encoding="utf-8")
    base = json.loads(original)

    out = REPO / "eval" / "results" / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{args.label}"
    out.mkdir(parents=True)
    webhook = f"{args.n8n_url}/webhook/paperpilot"
    total = len(ideas) * len(configs) * args.repeats
    print(f"{total} runs: {len(ideas)} ideas x {len(configs)} configs x {args.repeats} repeats, {args.pause:.0f} s apart -> {out}")

    rows: list[dict] = []
    results: dict[tuple[str, str], list[dict]] = {}
    n = 0
    try:
        with (out / "runs.jsonl").open("w", encoding="utf-8") as raw:
            for name, versions in configs:
                set_active(versions, base)
                for idea in ideas:
                    for rep in range(1, args.repeats + 1):
                        n += 1
                        if n > 1:
                            time.sleep(args.pause)
                        started = time.monotonic()
                        result, steps, error = run_once(webhook, args.pp_tools_url, idea, args.rate_limit_wait)
                        row = {"config": name, "idea": idea["id"], "repeat": rep, "ok": result is not None, "error": error,
                               "seconds": round(time.monotonic() - started, 1)}
                        if result is not None:
                            used = result.get("prompt_versions", {})
                            if any(used.get(p) != v for p, v in versions.items()):
                                row["error"] = f"prompt versions used {used} differ from config {versions}"
                            queries = steps.get("retrieve", {}).get("queries", [])
                            candidates = [c["paper_id"] for c in steps.get("retrieve", {}).get("candidates", [])]
                            row |= {"run_id": result["run_id"]} | score(idea, result, candidates, queries)
                            results.setdefault((name, idea["id"]), []).append(result)
                        raw.write(json.dumps({"row": row, "result": result, "steps": steps}, ensure_ascii=False) + "\n")
                        raw.flush()
                        rows.append(row)
                        flag = "ok " if row["ok"] else "ERR"
                        found = f"{row.get('expected_in_candidates', '-')}/{row.get('expected', '-')}" if row["ok"] else (error or "")[:80]
                        print(f"  [{n}/{total}] {flag} {name:<10} {idea['id']:<22} #{rep}  expected found: {found}")
    finally:
        ACTIVE.write_text(original, encoding="utf-8")
        print(f"restored {ACTIVE.relative_to(REPO)}")

    meta = {"ideas": [i["id"] for i in ideas], "configs": dict(configs), "repeats": args.repeats, "pause_s": args.pause}
    table = write_outputs(out, rows, results, [name for name, _ in configs], meta)
    print("\n" + table)
    print(f"\nWrote {out.relative_to(REPO)}/ (summary.md, runs.csv, runs.jsonl)")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
