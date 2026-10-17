"""The result page the form shows when a run ends (D39).

Built by code from the stored run, never by an LLM: every line on the page comes
from a verified step (novelty) or a check with its evidence (audit). Everything
that came from a user, a paper or a model is HTML-escaped, so a draft cannot
inject markup into the page.

The page is an HTML fragment: n8n's "Form Ending" node (Show Text) places it
inside its own <body>, so it brings its own <style>, scoped under .pp.
"""

from html import escape
from typing import Any

MAX_ITEMS = 60  # per section; a long audit stays readable, and the JSON keeps everything
EVIDENCE_CHARS = 420

STYLE = """
<style>
.pp{--ink:#14161a;--muted:#5d6470;--faint:#8a919c;--line:#e4e7ec;--card:#fff;--bg:#f6f7f9;
 --ok:#0f7b4f;--ok-bg:#e7f6ee;--bad:#b42318;--bad-bg:#fdecea;--warn:#93600a;--warn-bg:#fdf3dc;
 --info:#3446a8;--info-bg:#eceffd;--none:#5d6470;--none-bg:#eef0f3;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
 color:var(--ink);font-size:14px;line-height:1.55;width:100%;max-width:860px;margin:0 auto;padding:32px 16px 56px;text-align:left}
.pp *{box-sizing:border-box}
.pp h1{font-size:24px;font-weight:650;letter-spacing:-.01em;margin:4px 0 6px;line-height:1.25}
.pp h2{font-size:15px;font-weight:650;margin:0 0 12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pp p{margin:0 0 8px}
.pp a{color:var(--info);text-decoration:none}
.pp a:hover{text-decoration:underline}
.pp .eyebrow{font-size:11px;font-weight:650;letter-spacing:.09em;text-transform:uppercase;color:var(--faint)}
.pp .sub{color:var(--muted);margin-bottom:20px}
.pp .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:0 0 14px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.pp .banner{border-radius:12px;padding:16px 18px;margin:0 0 14px;border:1px solid transparent}
.pp .banner strong{display:block;font-size:15px;margin-bottom:2px}
.pp .banner.ok{background:var(--ok-bg);border-color:#bfe6d1;color:#0b5c3b}
.pp .banner.bad{background:var(--bad-bg);border-color:#f6c7c1;color:#8f1c13}
.pp .banner.warn{background:var(--warn-bg);border-color:#f1dca6;color:#734b06}
.pp .banner.info{background:var(--info-bg);border-color:#ccd3f7;color:#26357f}
.pp .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:0 0 14px}
.pp .stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.pp .stat b{display:block;font-size:20px;font-weight:650;line-height:1.2}
.pp .stat span{font-size:12px;color:var(--muted)}
.pp .badge{display:inline-block;font-size:11px;font-weight:650;letter-spacing:.02em;padding:2px 8px;border-radius:999px;white-space:nowrap;vertical-align:1px}
.pp .b-ok{background:var(--ok-bg);color:var(--ok)}
.pp .b-bad{background:var(--bad-bg);color:var(--bad)}
.pp .b-warn{background:var(--warn-bg);color:var(--warn)}
.pp .b-info{background:var(--info-bg);color:var(--info)}
.pp .b-none{background:var(--none-bg);color:var(--none)}
.pp .item{border-top:1px solid var(--line);padding:12px 0}
.pp .item:first-of-type{border-top:0;padding-top:2px}
.pp .item .head{display:flex;gap:10px;align-items:flex-start;justify-content:space-between}
.pp .item .text{font-weight:520}
.pp .meta{font-size:12px;color:var(--muted);margin-top:4px}
.pp .quote{font-size:12.5px;color:#3d434d;background:var(--bg);border-left:3px solid var(--line);border-radius:6px;padding:8px 10px;margin-top:8px;white-space:pre-line}
.pp .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.pp table{width:100%;border-collapse:collapse;font-size:13px}
.pp td,.pp th{text-align:left;padding:7px 8px;border-top:1px solid var(--line);vertical-align:top}
.pp th{font-weight:600;color:var(--muted);width:90px}
.pp ul{margin:4px 0 0 18px;padding:0}
.pp li{margin:3px 0}
.pp details{margin-top:4px}
.pp summary{cursor:pointer;color:var(--muted);font-size:13px}
.pp .foot{font-size:12px;color:var(--faint);margin-top:22px}
.pp .again{display:inline-block;margin-top:8px;padding:8px 14px;border-radius:8px;background:var(--ink);color:#fff;font-weight:600}
.pp .again:hover{text-decoration:none;opacity:.9}
</style>
"""

VERDICT_STYLE = {
    "supported": ("b-ok", "supported"),
    "contradicted": ("b-bad", "contradicted"),
    "unverifiable": ("b-none", "unverifiable"),
    "not_shown": ("b-none", "not shown in figure"),
    "not_found": ("b-none", "not found in cited paper"),
    "not_in_library": ("b-none", "cited paper not in library"),
    "unresolved_reference": ("b-warn", "reference not resolved"),
    "missing_figure": ("b-warn", "figure missing"),
    "not_answered": ("b-warn", "not answered"),
}
OVERLAP_STYLE = {"same": "b-bad", "partial": "b-warn", "different": "b-ok", "unknown": "b-none"}
QUIET = ("supported", "not_in_library")  # folded under <details>: what needs reading comes first
ORDER = {"contradicted": 0, "unresolved_reference": 1, "missing_figure": 1, "not_answered": 2}


def e(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _badge(verdict: str) -> str:
    cls, label = VERDICT_STYLE.get(verdict, ("b-none", verdict.replace("_", " ")))
    return f'<span class="badge {cls}">{e(label)}</span>'


def _link(url: Any, text: Any) -> str:
    u = str(url or "")
    if u.startswith(("https://", "http://")):
        return f'<a href="{e(u)}" target="_blank" rel="noopener noreferrer">{e(text)}</a>'
    return e(text)


def _stats(pairs: list[tuple[Any, str]]) -> str:
    cells = "".join(f'<div class="stat"><b>{e(v)}</b><span>{e(k)}</span></div>' for v, k in pairs)
    return f'<div class="stats">{cells}</div>'


def _page(eyebrow: str, title: str, subtitle: str, body: str, form_url: str | None) -> str:
    again = f'<a class="again" href="{e(form_url)}">Submit another</a>' if form_url else ""
    return (
        f'{STYLE}<div class="pp"><div class="eyebrow">{e(eyebrow)}</div><h1>{e(title)}</h1>'
        f'<p class="sub">{subtitle}</p>{body}{again}'
        '<p class="foot">Written by code from the stored run. Verdicts come with their evidence; '
        "the full JSON result is kept in the run record.</p></div>"
    )


# ---- novelty ---------------------------------------------------------------------------


def _paper_item(item: dict[str, Any], facets_shown: bool) -> str:
    year = f" ({e(item.get('year'))})" if item.get("year") else ""
    source = " · found on the web" if item.get("source") not in (None, "local") else ""
    chips = ""
    if facets_shown:
        labels = item.get("overlap") or {}
        chips = '<div class="chips">' + "".join(
            f'<span class="badge {OVERLAP_STYLE.get(labels.get(f), "b-none")}">{e(f)}: {e(labels.get(f, "unknown"))}</span>'
            for f in ("task", "method", "data", "result")
        ) + "</div>"
    rationale = f'<div class="quote">{e(_clip(item.get("rationale"), EVIDENCE_CHARS))}</div>' if item.get("rationale") else ""
    evidence = ", ".join(e(x) for x in item.get("evidence") or [])
    ev = f'<div class="meta">Evidence: {evidence}</div>' if evidence else ""
    return (
        f'<div class="item"><div class="text">{_link(item.get("url"), item.get("title") or item.get("paper_id"))}{year}</div>'
        f'<div class="meta">{e(item.get("paper_id"))}{source}</div>{chips}{rationale}{ev}</div>'
    )


def novelty_page(run: dict[str, Any], result: dict[str, Any], form_url: str | None = None) -> str:
    idea = (run.get("input") or {}).get("idea_text") or ""
    closest = result.get("closest_work") or []
    not_close = result.get("checked_not_close") or []
    cov = result.get("coverage") or {}
    if closest:
        banner = (f'<div class="banner warn"><strong>{len(closest)} related paper{"s" if len(closest) != 1 else ""} '
                  "overlap with this idea</strong>Read them before claiming novelty; the facets below show where they overlap.</div>")
    else:
        banner = ('<div class="banner ok"><strong>No overlapping paper found in the knowledge base</strong>'
                  "This is not proof of novelty: it only covers the papers searched below.</div>")
    body = [banner, f'<div class="card"><h2>Summary</h2><p>{e(result.get("summary"))}</p></div>']

    facets = result.get("idea_facets") or {}
    rows = "".join(f"<tr><th>{e(f)}</th><td>{e(facets.get(f))}</td></tr>" for f in ("task", "method", "data", "result") if f in facets)
    if rows:
        body.append(f'<div class="card"><h2>How the idea was read</h2><table>{rows}</table></div>')

    if closest:
        items = "".join(_paper_item(i, True) for i in closest[:MAX_ITEMS])
        body.append(f'<div class="card"><h2>Closest work <span class="badge b-warn">{len(closest)}</span></h2>{items}</div>')
    if not_close:
        items = "".join(_paper_item(i, False) for i in not_close[:MAX_ITEMS])
        body.append(f'<div class="card"><details><summary>Also checked, no overlap found ({len(not_close)})</summary>{items}</details></div>')

    rounds = result.get("search_agent") or []
    if rounds:
        lis = []
        for r in rounds:
            qs = "; ".join(e(q.get("text")) + (f' <span class="meta">({e(q.get("where"))})</span>' if q.get("where") else "")
                           for q in r.get("queries") or [])
            stop = f' Stopped: {e(r.get("stop_reason"))}.' if r.get("stop_reason") else ""
            lis.append(f'<li><b>Round {e(r.get("round"))}</b>: {e(r.get("decision"))}. {e(r.get("reason"))}'
                       f'{"<br><span class=meta>Queries: " + qs + "</span>" if qs else ""}{stop}</li>')
        body.append(f'<div class="card"><h2>What the search agent did</h2><ul>{"".join(lis)}</ul></div>')

    body.append(_stats([
        (cov.get("papers_in_knowledge_base", "?"), "papers in knowledge base"),
        (len(cov.get("queries") or []), "search queries"),
        (len(cov.get("web_queries") or []), "web queries"),
        (cov.get("candidates_considered", "?"), "candidates judged"),
    ]))
    if result.get("summary_llm_written"):
        body.append(f'<div class="card"><h2>Judge\'s own words <span class="badge b-none">LLM-written, not verified</span></h2>'
                    f'<p>{e(result.get("summary_llm_written"))}</p></div>')
    if result.get("unverified"):
        body.append(f'<div class="banner info"><strong>{len(result["unverified"])} judgement(s) failed verification</strong>'
                    "They are left out of the lists above and kept in the run record.</div>")
    subtitle = f"“{e(_clip(idea, 400))}”" if idea else ""
    return _page("PaperPilot · novelty check", "Novelty check", subtitle, "".join(body), form_url)


# ---- audit -----------------------------------------------------------------------------


def _sorted(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(checks, key=lambda c: ORDER.get(c.get("verdict"), 3 if c.get("verdict") != "supported" else 4))


def _number_items(nc: dict[str, Any], claims: list[dict[str, Any]]) -> list[tuple[str, str]]:
    out = []
    for c in _sorted(nc.get("checks") or [])[:MAX_ITEMS]:
        idx = c.get("claim")
        text = claims[idx].get("claim") if isinstance(idx, int) and 0 <= idx < len(claims) else c.get("system")
        cell = c.get("cell") or {}
        where = ""
        if cell:
            where = f'{e(cell.get("table"))}, row “{e(cell.get("row"))}”, column “{e(cell.get("column"))}”: {e(cell.get("text"))}'
            base = c.get("baseline_cell")
            if base:
                where += f' vs “{e(base.get("row"))}”: {e(base.get("text"))}'
        out.append((c.get("verdict", ""),
            f'<div class="item"><div class="head"><div class="text">{e(_clip(text, 300))}</div>{_badge(c.get("verdict", ""))}</div>'
            f'<div class="meta">Claimed: {e(c.get("claimed"))}{" · " + e(c.get("note")) if c.get("note") else ""}</div>'
            f'{f"<div class=meta>Table: {where}</div>" if where else ""}</div>'
        ))
    return out


def _figure_items(fc: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    for c in _sorted(fc.get("checks") or [])[:MAX_ITEMS]:
        values = ", ".join(e(v) for v in c.get("read_values") or [])
        quote = f'<div class="quote">{e(_clip(c.get("evidence"), EVIDENCE_CHARS))}</div>' if c.get("evidence") else ""
        out.append((c.get("verdict", ""),
            f'<div class="item"><div class="head"><div class="text">{e(_clip(c.get("text"), 300))}</div>{_badge(c.get("verdict", ""))}</div>'
            f'<div class="meta">{e(c.get("figure"))}{" · values read: " + values if values else ""}</div>'
            f'{quote}</div>'
        ))
    return out


def _citation_items(cc: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    for c in _sorted(cc.get("checks") or [])[:MAX_ITEMS]:
        cited = c.get("cited_title") or c.get("reference") or ""
        quotes = "".join(f'<div class="quote">{e(_clip(p.get("text"), EVIDENCE_CHARS))}</div>' for p in (c.get("evidence") or [])[:1])
        reason = f'<div class="meta">{e(c.get("reason"))}</div>' if c.get("reason") else ""
        out.append((c.get("verdict", ""),
            f'<div class="item"><div class="head"><div class="text">{e(_clip(c.get("text"), 300))}</div>{_badge(c.get("verdict", ""))}</div>'
            f'<div class="meta">Cites {e(c.get("marker"))}{" → " + e(_clip(cited, 140)) if cited else ""}</div>{reason}{quotes}</div>'
        ))
    return out


def _count(summary: dict[str, Any], *keys: str) -> str:
    parts = [f"{summary.get(k, 0)} {VERDICT_STYLE.get(k, ('', k.replace('_', ' ')))[1]}" for k in keys if summary.get(k)]
    return " · ".join(parts) or "nothing to check"


def audit_page(run: dict[str, Any], result: dict[str, Any], form_url: str | None = None) -> str:
    paper = result.get("paper") or {}
    claims = result.get("claims") or []
    cov = result.get("coverage") or {}
    nc = result.get("number_checks") or {}
    fc = result.get("figure_checks") or {}
    cc = result.get("citation_checks") or {}
    ns, fs, cs = nc.get("summary") or {}, fc.get("summary") or {}, cc.get("summary") or {}
    contradicted = ns.get("contradicted", 0) + fs.get("contradicted", 0) + cs.get("contradicted", 0)

    body = []
    if contradicted:
        body.append(f'<div class="banner bad"><strong>{contradicted} claim{"s" if contradicted != 1 else ""} contradicted by the evidence</strong>'
                    "Listed first in each section below, with what the table, figure or cited paper shows.</div>")
    else:
        body.append('<div class="banner ok"><strong>No claim was contradicted by the evidence found</strong>'
                    "“Unverifiable” and “not found” mean the evidence was missing, not that the claim is wrong.</div>")
    stops = [s for s in (cov.get("stopped"), fs.get("stopped"), cs.get("stopped")) if s]
    if stops:
        body.append('<div class="banner warn"><strong>Partial audit</strong>'
                    + e("; ".join(dict.fromkeys(stops))) + ". The checks below are the ones that completed.</div>")

    body.append(_stats([
        (len(claims), "claims extracted"),
        (ns.get("checks", 0), "number checks"),
        (fs.get("checks", 0), "figure checks"),
        (cs.get("citations", 0), "citations"),
    ]))
    sections = [
        ("Numbers against the paper's tables", ns, ("contradicted", "supported", "unverifiable"), _number_items(nc, claims)),
        ("Figures, read by a vision model", fs, ("contradicted", "supported", "not_shown", "missing_figure", "not_answered"), _figure_items(fc)),
        ("Citations, against the cited paper", cs, ("contradicted", "supported", "not_found", "not_in_library", "unresolved_reference", "not_answered"), _citation_items(cc)),
    ]
    for title, summary, keys, items in sections:
        if not items:
            continue
        shown = "".join(h for v, h in items if v not in QUIET)
        quiet = [(v, h) for v, h in items if v in QUIET]
        folded = ""
        for verdict, label in (("supported", "Supported"), ("not_in_library", "Cited paper not in the library")):
            group = "".join(h for v, h in quiet if v == verdict)
            if group:
                n = sum(1 for v, _ in quiet if v == verdict)
                folded += f'<details><summary>{e(label)} ({n})</summary>{group}</details>'
        body.append(f'<div class="card"><h2>{e(title)}</h2><p class="meta">{e(_count(summary, *keys))}</p>{shown}{folded}</div>')
    if not any(items for *_, items in sections):
        body.append('<div class="card"><p>No numeric, figure or citation claim could be checked in this draft.</p></div>')

    source = (run.get("input") or {}).get("draft_pdf_url") or "uploaded PDF"
    subtitle = f'{e(paper.get("pages", "?"))} pages · {e(_clip(source, 120))}'
    return _page("PaperPilot · draft audit", paper.get("title") or "Draft audit", subtitle, "".join(body), form_url)


# ---- errors ----------------------------------------------------------------------------


def error_page(title: str, message: str, details: list[str] | None = None, form_url: str | None = None) -> str:
    lis = "".join(f"<li>{e(d)}</li>" for d in details or [])
    body = f'<div class="banner bad"><strong>{e(title)}</strong>{e(message)}{f"<ul>{lis}</ul>" if lis else ""}</div>'
    return _page("PaperPilot", "The request did not go through", "", body, form_url)


FIELD_NAMES = {
    "request": "Form",
    "idea_text": "Idea",
    "draft_pdf_url": "PDF link",
    "draft_pdf_upload": "PDF file",
    "mode": "What to do",
}


def result_page(status_code: int, body: Any, run: dict[str, Any] | None, form_url: str | None = None) -> str:
    """The page for whatever the router answered: a finished run, a rejected request or a failure."""
    if status_code == 422 and isinstance(body, dict):
        details = [f'{FIELD_NAMES.get(err.get("field"), err.get("field"))}: {err.get("message", "").removeprefix("Value error, ")}'
                   for err in body.get("errors") or []]
        return error_page("Please fix the form", "", details, form_url)
    if run is not None and run.get("status") == "done" and isinstance(run.get("result"), dict):
        result = run["result"]
        if run.get("mode") == "novelty" or result.get("branch") == "novelty":
            return novelty_page(run, result, form_url)
        if run.get("mode") == "audit" or result.get("branch") == "audit":
            return audit_page(run, result, form_url)
    if run is not None and run.get("status") == "failed":
        err = run.get("error") or {}
        return error_page("The run failed", str(err.get("message") or err)[:500], None, form_url)
    message = body.get("message") if isinstance(body, dict) else None
    return error_page("The run did not finish", f"HTTP {status_code}. " + str(message or "See the n8n execution log for details."),
                      None, form_url)
