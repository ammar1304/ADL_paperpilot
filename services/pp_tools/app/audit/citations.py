"""Citation check (milestone 3, step 6): does the cited paper say what the draft attributes to it?

Standard library only (the evaluation imports it). Three stages, the first two in code:

1. marker -> reference: "Asai et al., 2024" or "[12]" is matched to one entry of the
   draft's own bibliography (first author's surname and year, or position);
2. reference -> library paper: the entry's arXiv id, or its title, found in the
   library manifest (corpus/manifest.csv);
3. evidence and verdict (in steps.py): the cited paper's chunks most relevant to the
   claim are retrieved and reranked, and P6 judges whether they support the claim.

A citation the code cannot place (no matching reference, or a paper outside the
library) is reported as such and never sent to the model (D37).
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from ..textutil import count_tokens

_ARXIV_ID = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?")
_URLISH = re.compile(r"https?\s*:\s*//\S+|doi\s*:\s*\S+|arXiv\s*:\s*\S+|abs/\S+", re.I)
_YEAR = re.compile(r"(?<![\d.])((?:19|20)\d{2})([a-z])?(?![\d])")
_NUMERIC_MARKER = re.compile(r"^\s*\[?\s*(\d{1,3})\s*\]?\s*$")
_AUTHOR_YEAR = re.compile(
    r"(?P<first>[A-Z][^\s,()]+(?:\s+(?:de|van|von|der|la|le)\s+[A-Z][^\s,()]+)?)"
    r"(?:\s+et\s+al\.?|\s+(?:and|&)\s+(?P<second>[A-Z][^\s,()]+))?"
    r"[\s,]*\(?\s*(?P<year>(?:19|20)\d{2})(?P<suffix>[a-z])?")


def compact(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^0-9a-z]", "", s)


# ---- the draft's bibliography -----------------------------------------------------------------


def merge_fragments(refs: list[str]) -> list[str]:
    """Join entries the parser split: a reference ends with a full stop, so a fragment that
    does not ("..., Jan Leike, and") continues in the next entry (Self-RAG: 4 of 60)."""
    out: list[str] = []
    for r in refs:
        r = r.strip()
        if not r:
            continue
        if out and not out[-1].rstrip().endswith((".", ".”", '."')):
            out[-1] = out[-1].rstrip() + " " + r
        else:
            out.append(r)
    return out


def first_author(ref: str) -> str:
    """Surname of the first author: "Akari Asai, Zeqiu Wu, ..." -> "asai"; "Asai, A., ..." -> "asai"."""
    # the author part ends at the first comma, "and", or the year ("A. Author. 2020. Title.");
    # not at ". ", which would cut "Tom B. Brown" after the initial
    head = re.split(r",|\band\b|(?<![\w.])(?:19|20)\d{2}", ref, maxsplit=1)[0].strip()
    words = [w for w in head.split() if w[:1].isalpha()]
    if not words:
        return ""
    if len(words) == 1:  # "Asai, A." style: the surname comes first
        return compact(words[0])
    return compact(words[-1])


def years(ref: str) -> set[str]:
    """Years written in the entry, with their letter ("2023a"), ignoring arXiv ids and URLs
    (Keskar et al.'s entry starts with arXiv:1909.05858, which is not the year)."""
    text = _URLISH.sub(" ", _ARXIV_ID.sub(" ", ref))
    return {y + (s or "") for y, s in _YEAR.findall(text)}


@dataclass
class Marker:
    kind: str  # "number" or "author_year"
    number: int = 0
    author: str = ""
    second: str = ""
    year: str = ""  # "2023" or "2023b"


def parse_marker(marker: str) -> Marker | None:
    m = _NUMERIC_MARKER.match(marker or "")
    if m:
        return Marker("number", number=int(m.group(1)))
    m = _AUTHOR_YEAR.search(marker or "")
    if m:
        return Marker("author_year", author=compact(m.group("first")), second=compact(m.group("second") or ""),
                      year=m.group("year") + (m.group("suffix") or ""))
    return None


def resolve(marker: str, refs: list[str]) -> dict[str, Any]:
    """{"status": "found", "index", "reference"} or {"status": "unparsed" | "not_found" | "ambiguous", ...}."""
    parsed = parse_marker(marker)
    if parsed is None:
        return {"status": "unparsed"}
    if parsed.kind == "number":
        if 1 <= parsed.number <= len(refs):
            return {"status": "found", "index": parsed.number - 1, "reference": refs[parsed.number - 1]}
        return {"status": "not_found"}
    year = parsed.year[:4]
    candidates = [i for i, r in enumerate(refs) if first_author(r) == parsed.author and any(y[:4] == year for y in years(r))]
    if len(candidates) > 1 and len(parsed.year) == 5:  # "2023b": the letter decides
        candidates = [i for i in candidates if parsed.year in years(refs[i])]
    if len(candidates) > 1 and parsed.second:  # "Lewis and Fan, 2019": the second author decides
        candidates = [i for i in candidates if parsed.second in compact(refs[i][:200])]
    if len(candidates) == 1:
        return {"status": "found", "index": candidates[0], "reference": refs[candidates[0]]}
    return {"status": "ambiguous" if candidates else "not_found", "candidates": len(candidates)}


# ---- the library ------------------------------------------------------------------------------


@dataclass
class LibraryPaper:
    paper_id: str  # "arxiv:2310.11511", as in the vector store
    arxiv_id: str
    title: str
    fulltext: bool


class Library:
    """The papers in the knowledge base, looked up by arXiv id or by title."""

    MIN_TITLE = 15  # compact characters: shorter titles ("Atlas") would match inside other text

    def __init__(self, papers: Iterable[LibraryPaper]):
        self.papers = list(papers)
        self.by_id = {p.arxiv_id: p for p in self.papers}
        # longest titles first, so a title that contains another one wins
        self.titles = sorted(((compact(p.title), p) for p in self.papers if len(compact(p.title)) >= self.MIN_TITLE),
                             key=lambda t: -len(t[0]))

    def match(self, reference: str) -> tuple[LibraryPaper | None, str]:
        """(the library paper the reference points to, how it was found: "arxiv_id" or "title")."""
        for arxiv_id in _ARXIV_ID.findall(reference):
            if arxiv_id in self.by_id:
                return self.by_id[arxiv_id], "arxiv_id"
        text = compact(reference)
        for title, paper in self.titles:
            if title in text:
                return paper, "title"
        return None, ""


def citation_jobs(claims: list[dict[str, Any]], references: list[str], library: Library) -> tuple[list[dict], list[dict]]:
    """(one job per claim and cited library paper, findings that need no model).

    Findings: a marker that names no entry of the bibliography (or several), and a
    cited paper that is not in the library, so nothing can be checked against it.
    """
    refs = merge_fragments(references)
    jobs, findings = [], []
    seen: set[tuple[int, str]] = set()
    for i, claim in enumerate(claims):
        for marker in claim.get("cites") or []:
            res = resolve(marker, refs)
            base = {"claim": i, "marker": marker}
            if res["status"] != "found":
                findings.append({**base, "verdict": "unresolved_reference", "detail": res["status"]})
                continue
            paper, how = library.match(res["reference"])
            if paper is None:
                findings.append({**base, "verdict": "not_in_library", "reference": res["reference"][:300]})
                continue
            if (i, paper.paper_id) in seen:  # the same paper cited twice in one sentence
                continue
            seen.add((i, paper.paper_id))
            jobs.append({**base, "paper_id": paper.paper_id, "title": paper.title, "fulltext": paper.fulltext,
                         "matched_by": how, "reference": res["reference"][:300]})
    return jobs, findings


_CITATION_IN_TEXT = re.compile(
    r"\(?\b[A-Z][^\s,()]+(?:\s+et\s+al\.?|\s+(?:and|&)\s+[A-Z][^\s,()]+)?[\s,]*\(?(?:19|20)\d{2}[a-z]?\)?|\[\d{1,3}(?:[,\s\-–]+\d{1,3})*\]")


def query_for(claim: dict[str, Any]) -> str:
    """What to search the cited paper for: the claim, without its citations in any form
    ("Yoran et al. (2024)" as well as the marker "Yoran et al., 2024", or "[12]")."""
    text = claim.get("claim") or claim.get("sentence") or ""
    for marker in claim.get("cites") or []:
        text = text.replace(f"({marker})", " ").replace(marker, " ")
    text = _CITATION_IN_TEXT.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" ,.;") + ("." if text.strip().endswith(".") else "")


_STOP = set("the a an of to in on for and or with by from as is are was were be been that this these those its it their "
            "we our they which using use used than more most can into at not no only also such".split())


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


def best_window(text: str, query: str, chars: int) -> str:
    """The stretch of whole sentences (at most ``chars``) that shares the most words with the
    query, keeping the chunk's first line (title and section). A chunk's first 700 characters
    are often context; the statement the claim is about can come later (CRAG's citation of
    Yoran et al.: the NLI filter is past the cut)."""
    if len(text) <= chars:
        return text
    head, _, body = text.partition("\n")
    if len(head) > 200:  # no title line
        head, body = "", text
    sentences = re.split(r"(?<=[.!?])\s+", body)
    wanted = _content_words(query)
    best, best_score = "", -1
    for i in range(len(sentences)):
        window = ""
        for sent in sentences[i:]:
            if window and len(head) + len(window) + len(sent) + 2 > chars:
                break
            window = (window + " " + sent).strip()
        score = len(_content_words(window) & wanted)
        if score > best_score:
            best, best_score = window, score
    out = (head + "\n" + best) if head else best
    return out[:chars]


# ---- the judge (P6) ---------------------------------------------------------------------------

VERDICTS = ("supported", "contradicted", "not_found")
PASSAGES = 3  # reranked passages per pair
PASSAGE_CHARS = 700
RETRIEVE_K = 8  # chunks retrieved from the cited paper before reranking
# Groq counts input + output budget against 8,000 tokens a minute; P6 with its schema is
# about 750 counted tokens, a pair about 450 (three passages of 700 characters).
PAIRS_TOKENS = 1700
CITATION_MAX_OUTPUT = 1500

CITATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair": {"type": "integer"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["pair", "verdict", "evidence", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["checks"],
    "additionalProperties": False,
}


def pair_block(k: int, claim: dict[str, Any], job: dict[str, Any]) -> str:
    lines = [f"PAIR {k}", f"Claim in the draft: {(claim.get('claim') or '').strip()[:400]}"]
    sentence = (claim.get("sentence") or "").strip()
    if sentence and sentence != (claim.get("claim") or "").strip():
        lines.append(f'Sentence in the draft: "{sentence[:400]}"')
    lines.append(f"Cited as {job['marker']}: {job['title']}" + ("" if job.get("fulltext") else " (only its abstract is in the library)"))
    for p in job.get("passages") or []:
        lines.append(f"[{p['id']}] ({p.get('section') or p.get('chunk_type') or 'passage'}) {p['text'][:PASSAGE_CHARS]}")
    if not job.get("passages"):
        lines.append("(no passage was found in the cited paper)")
    return "\n".join(lines)


def batches(jobs: list[dict[str, Any]], claims: list[dict[str, Any]], budget: int = PAIRS_TOKENS) -> list[list[int]]:
    """Job indices per LLM call, under a token budget (at least one job per call)."""
    out: list[list[int]] = []
    group: list[int] = []
    used = 0
    for i, job in enumerate(jobs):
        cost = count_tokens(pair_block(1, claims[job["claim"]], job))
        if group and used + cost > budget:
            out.append(group)
            group, used = [], 0
        group.append(i)
        used += cost
    if group:
        out.append(group)
    return out


def citation_request(system: str, user: str, model: str) -> dict[str, Any]:
    """Body for Groq's chat completions, as for P4 (D34): low hidden reasoning, strict schema."""
    return {
        "model": model,
        "temperature": 0,
        "reasoning_effort": "low",
        "include_reasoning": False,
        "max_completion_tokens": CITATION_MAX_OUTPUT,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "citation_checks", "strict": True, "schema": CITATION_SCHEMA}},
    }


def validate(output: Any, batch_jobs: list[int], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One check per job of the batch. Pair numbers are 1-based positions; evidence ids the
    pair was not given are dropped (the model cannot cite a passage it did not see)."""
    if isinstance(output, str):
        output = json.loads(output)
    if not isinstance(output, dict) or not isinstance(output.get("checks"), list):
        raise ValueError("citation check output must be an object with a 'checks' list")
    by_pos: dict[int, dict[str, Any]] = {}
    for c in output["checks"]:
        if not isinstance(c, dict):
            continue
        pos = c.get("pair")
        if not isinstance(pos, int) or not 1 <= pos <= len(batch_jobs) or pos in by_pos:
            continue
        job = jobs[batch_jobs[pos - 1]]
        given = {p["id"] for p in job.get("passages") or []}
        verdict = c.get("verdict") if c.get("verdict") in VERDICTS else "not_found"
        evidence = [e for e in (c.get("evidence") or []) if e in given][:PASSAGES]
        if verdict in ("supported", "contradicted") and not evidence:
            verdict, reason = "not_found", "verdict without a passage to rest on"
        else:
            reason = str(c.get("reason") or "")[:400]
        by_pos[pos] = {"job": batch_jobs[pos - 1], "verdict": verdict, "evidence": evidence, "reason": reason}
    if not by_pos:
        raise ValueError("the answer has no verdict for any pair")
    return [by_pos.get(k) or {"job": batch_jobs[k - 1], "verdict": "not_answered", "evidence": [], "reason": ""}
            for k in range(1, len(batch_jobs) + 1)]
