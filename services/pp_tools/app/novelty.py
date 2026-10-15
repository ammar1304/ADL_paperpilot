"""Novelty branch logic that must be exact: output contracts, retrieval, the verifier.

The LLM calls themselves run in n8n (nodes "Decompose idea" and "Judge novelty").
pp-tools does everything around them that must be deterministic and testable:

1. save_facets      validate the decomposer's JSON and store it
2. retrieve         search the knowledge base with every query, fuse the rankings
3. judge_prompt     render P3 with the facets, the candidates and any feedback
4. verify           check the judge's output against what retrieval really returned
5. assemble         build the branch result from the stored steps

Every step is stored in run_steps, so a run can be replayed and audited, and the
verifier reads the retrieved set from the database rather than trusting what
n8n passes back.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .db import Database
from .prompts import PromptLibrary, RenderedPrompt
from .external.semantic_scholar import ExternalPaper, SemanticScholar, SemanticScholarError
from .retrieval.reranker import Reranker, RerankerError
from .retrieval.search import Retriever

FacetName = Literal["task", "method", "data", "result"]
Label = Literal["same", "partial", "different", "unknown"]
FACETS: tuple[str, ...] = ("task", "method", "data", "result")
NOT_SPECIFIED = "not specified"  # what P1 writes for a facet the idea does not mention
# How strongly a label counts as overlap, for ranking the report (not a novelty score).
LABEL_WEIGHT = {"same": 2, "partial": 1, "different": 0, "unknown": 0}

DECOMPOSER = "P1-decomposer"
PLANNER = "P2-search-planner"
JUDGE = "P3-novelty-judge"
MAX_VERIFY_ATTEMPTS = 2  # first answer + one retry with the verifier's feedback


class NoveltyError(Exception):
    """A step was called out of order or its inputs are missing."""


# ---- contracts for LLM output -----------------------------------------------------


class Facets(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task: str = Field(min_length=2, max_length=400)
    method: str = Field(min_length=2, max_length=600)
    data: str = Field(min_length=2, max_length=400)
    result: str = Field(min_length=2, max_length=400)


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=3, max_length=300)
    facets: list[FacetName] = Field(min_length=1, max_length=4)


class DecomposerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facets: Facets
    queries: list[SearchQuery] = Field(min_length=1, max_length=8)

    @field_validator("queries")
    @classmethod
    def distinct_queries(cls, queries: list[SearchQuery]) -> list[SearchQuery]:
        seen: set[str] = set()
        out = []
        for q in queries:
            key = q.text.lower()
            if key not in seen:
                seen.add(key)
                out.append(q)
        return out


class Overlap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Label
    method: Label
    data: Label
    result: Label


class JudgeMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    paper_id: str
    overlap: Overlap
    evidence: list[str] = Field(default_factory=list, max_length=10)
    rationale: str = Field(min_length=3, max_length=800)


class JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    matches: list[JudgeMatch] = Field(max_length=12)
    summary: str = Field(min_length=3, max_length=1500)


class PlanQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=3, max_length=300)
    where: Literal["local", "web"]
    facets: list[FacetName] = Field(min_length=1, max_length=4)


class PlanOutput(BaseModel):
    """The search planner's decision for one round (P2)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["search", "stop"]
    reason: str = Field(min_length=3, max_length=600)
    gaps: list[FacetName] = Field(default_factory=list, max_length=4)
    queries: list[PlanQuery] = Field(default_factory=list, max_length=3)

    @field_validator("queries")
    @classmethod
    def distinct(cls, queries: list[PlanQuery]) -> list[PlanQuery]:
        seen: set[str] = set()
        return [q for q in queries if not (q.text.lower() in seen or seen.add(q.text.lower()))]


# ---- steps ---------------------------------------------------------------------------------


def _field_errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc']) or 'output'}: {e['msg']}" for e in exc.errors()]


def save_facets(db: Database, run_id: UUID, output: dict[str, Any], prompt_version: str) -> DecomposerOutput:
    _require_run(db, run_id)
    parsed = DecomposerOutput.model_validate(output)  # ValidationError -> 422 in the API
    db.add_step(run_id, "decompose", "done", {"prompt": DECOMPOSER, "prompt_version": prompt_version, "output": parsed.model_dump()})
    return parsed


@dataclass
class RetrievalConfig:
    strategy: str = "structured"
    per_query_k: int = 50  # chunks retrieved per query
    pool_papers: int = 30  # fused candidates the reranker re-orders
    top_papers: int = 6  # candidates sent to the judge
    evidence_per_paper: int = 2
    evidence_chars: int = 900  # per passage, keeps the judge prompt within Groq's per-minute token budget
    # Abstracts are kept whole up to this length: cutting them at 900 characters hid
    # the method sentence of Adaptive-RAG and the end of 2604.07985's abstract (F-004).
    abstract_chars: int = 2000
    rrf_k: int = 60  # standard reciprocal rank fusion constant
    rerank: bool = True  # re-order the pool with the cross-encoder when one is configured
    rerank_chunks_per_paper: int = 2  # a paper's best chunks scored by the reranker; its score is the max
    rerank_chars: int = 1200  # text per scored chunk (speed on CPU)
    idea_as_query: bool = False  # also search with the idea text itself, next to the decomposer's queries
    # Score each pool paper's abstract as well as its best-matching chunk. A paper's best
    # vector matches can be results or related-work passages that do not say what the
    # paper does (IRCoT dropped from rank 2-6 to 7-10 when reranked on those, E8 run 1).
    rerank_abstract: bool = False
    # Final order: "rerank" (cross-encoder score) or "hybrid" (reciprocal rank fusion of
    # the fused rank and the rerank rank, so a paper both stages like stays high).
    order: str = "hybrid"  # best in E8 run 2 (D28)


def rank_candidates(
    retriever: Retriever,
    reranker: "Reranker | None",
    idea_text: str,
    queries: list[str],
    cfg: RetrievalConfig,
    external: list[list[ExternalPaper]] | None = None,
) -> dict[str, Any]:
    """Search with every query, fuse the rankings, then optionally rerank the pool.

    1. Each query retrieves cfg.per_query_k chunks. Reciprocal rank fusion gives a
       paper credit for its best chunk in every query (D24).
    2. The cfg.pool_papers best fused papers are re-ordered by a cross-encoder that
       reads the idea and each paper's best chunks together (D28). If the reranker
       is missing or fails, the fused order is kept and the reason recorded.
    3. The cfg.top_papers best become the judge's candidates; their evidence is the
       chunks the final ranking scored highest.

    `external` holds the search agent's Semantic Scholar results, one ranked list
    per web query. Each list joins the fusion like a local query, with the paper's
    abstract as its only chunk, so local and external papers compete in one pool.
    """
    searches = list(queries) + ([idea_text] if cfg.idea_as_query else [])
    paper_score: dict[str, float] = defaultdict(float)
    paper_queries: dict[str, set[int]] = defaultdict(set)
    chunk_score: dict[str, float] = defaultdict(float)
    chunks: dict[str, Any] = {}
    sources: dict[str, set[str]] = defaultdict(set)
    for qi, query in enumerate(searches):
        hits = retriever.chunks(query, cfg.strategy, cfg.per_query_k)
        seen_papers: set[str] = set()
        for rank, hit in enumerate(hits, start=1):
            chunk_score[hit.chunk_id] += 1.0 / (cfg.rrf_k + rank)
            chunks[hit.chunk_id] = hit
            if hit.paper_id not in seen_papers:  # a paper's rank in this query = its best chunk's rank
                seen_papers.add(hit.paper_id)
                paper_score[hit.paper_id] += 1.0 / (cfg.rrf_k + len(seen_papers))
                paper_queries[hit.paper_id].add(qi)
                sources[hit.paper_id].add("local")

    external_meta: dict[str, ExternalPaper] = {}
    for ei, papers in enumerate(external or []):
        qi = len(searches) + ei
        for rank, paper in enumerate(papers, start=1):
            hit = paper.as_hit()
            chunk_score[hit.chunk_id] += 1.0 / (cfg.rrf_k + rank)
            chunks.setdefault(hit.chunk_id, hit)  # a local abstract with the same id wins
            paper_score[paper.paper_id] += 1.0 / (cfg.rrf_k + rank)
            paper_queries[paper.paper_id].add(qi)
            sources[paper.paper_id].add("semantic_scholar")
            external_meta.setdefault(paper.paper_id, paper)

    fused = sorted(paper_score, key=lambda p: paper_score[p], reverse=True)
    pool = fused[: max(cfg.pool_papers, cfg.top_papers)]
    own = {
        pid: sorted((c for c in chunks.values() if c.paper_id == pid), key=lambda c: chunk_score[c.chunk_id], reverse=True)
        for pid in pool
    }

    method = "fused"
    note = None
    rerank_chunk: dict[str, float] = {}
    rerank_paper: dict[str, float] = {}
    rerank_rank: dict[str, int] = {}
    if cfg.rerank and reranker is not None and pool:
        scored = []
        abstracts = _abstracts(retriever, pool, cfg.strategy) if cfg.rerank_abstract else {}
        for pid in pool:
            picked = []
            if pid in abstracts:
                picked.append(abstracts[pid])
                if all(c.chunk_id != abstracts[pid].chunk_id for c in own[pid]):
                    own[pid].append(abstracts[pid])  # also available as evidence
            for c in own[pid]:
                if len(picked) >= cfg.rerank_chunks_per_paper:
                    break
                if all(c.chunk_id != x.chunk_id for x in picked):
                    picked.append(c)
            scored.extend(picked)
        try:
            values = reranker.scores(idea_text, [c.text[: cfg.rerank_chars] for c in scored])
            rerank_chunk = {c.chunk_id: v for c, v in zip(scored, values, strict=True)}
            for c in scored:
                rerank_paper[c.paper_id] = max(rerank_paper.get(c.paper_id, 0.0), rerank_chunk[c.chunk_id])
            by_rerank = sorted(pool, key=lambda p: (-rerank_paper.get(p, 0.0), fused.index(p)))
            rerank_rank = {p: i + 1 for i, p in enumerate(by_rerank)}
            if cfg.order == "hybrid":
                pool = sorted(
                    pool,
                    key=lambda p: (-(1 / (cfg.rrf_k + fused.index(p) + 1) + 1 / (cfg.rrf_k + rerank_rank[p])), rerank_rank[p]),
                )
                method = "hybrid"
            else:
                pool = by_rerank
                method = "reranked"
        except RerankerError as exc:
            note = f"reranker failed, fused order kept: {str(exc)[:200]}"
    elif cfg.rerank and reranker is None:
        note = "no reranker configured, fused order kept"

    ranking = [
        {
            "paper_id": pid,
            "title": own[pid][0].title,
            "year": own[pid][0].year,
            "fused_rank": fused.index(pid) + 1,
            "fused_score": round(paper_score[pid], 6),
            "rerank_score": round(rerank_paper[pid], 6) if pid in rerank_paper else None,
            "rerank_rank": rerank_rank.get(pid),
            "matched_queries": sorted(paper_queries[pid]),
            "source": "+".join(sorted(sources[pid])),
            "url": _url(pid, external_meta.get(pid)),
        }
        for pid in pool
    ]

    candidates = []
    for entry in ranking[: cfg.top_papers]:
        pid = entry["paper_id"]
        best = sorted(own[pid], key=lambda c: (-rerank_chunk.get(c.chunk_id, -1.0), -chunk_score[c.chunk_id]))
        evidence = [
            {
                "chunk_id": c.chunk_id,
                "chunk_type": c.chunk_type,
                "section": c.section,
                "text": c.text[: cfg.abstract_chars if c.chunk_type == "abstract" else cfg.evidence_chars],
            }
            for c in best[: cfg.evidence_per_paper]
        ]
        candidates.append({**entry, "evidence": evidence})

    return {
        "queries": list(queries),
        "idea_as_query": cfg.idea_as_query,
        "rerank_abstract": cfg.rerank_abstract,
        "ranking_method": method,
        "ranking_note": note,
        "pool": ranking,
        "candidates": candidates,
    }


def _url(paper_id: str, external: "ExternalPaper | None") -> str | None:
    if paper_id.startswith("arxiv:"):
        return f"https://arxiv.org/abs/{paper_id.removeprefix('arxiv:')}"
    return external.url if external else None


def _abstracts(retriever: Retriever, papers: list[str], strategy: str) -> dict[str, Any]:
    """Each paper's abstract chunk, looked up by id (structured collection only)."""
    if strategy != "structured":
        return {}
    local = [p for p in papers if p.startswith("arxiv:")]
    hits = retriever.by_ids([f"{p}#abs" for p in local] + [f"{p}#abs/p1" for p in local], strategy)
    out: dict[str, Any] = {}
    for h in hits:
        out.setdefault(h.paper_id, h)
    return out


def retrieve(
    db: Database,
    retriever: Retriever,
    run_id: UUID,
    cfg: RetrievalConfig,
    coverage: dict[str, Any],
    reranker: "Reranker | None" = None,
) -> dict[str, Any]:
    """Rank candidates for the run's idea with the decomposer's queries, and store the step."""
    decomposed = DecomposerOutput.model_validate(_last_step(db, run_id, "decompose")["output"])
    run = db.get_run(run_id)
    idea_text = (run.get("input") or {}).get("idea_text") or decomposed.facets.method
    ranked = rank_candidates(retriever, reranker, idea_text, [q.text for q in decomposed.queries], cfg)
    detail = {"strategy": cfg.strategy, **ranked, "coverage": coverage}
    db.add_step(run_id, "retrieve", "done", detail)
    return detail


# ---- the search agent (D29) -------------------------------------------------------------------
#
# After the first retrieval, an LLM (P2, node "Plan search" in n8n) looks at the idea
# and the current candidates and decides: stop, or search again with up to 3 new
# queries, each either in the local library or on Semantic Scholar. Code executes
# the plan, re-ranks everything found so far, and records what each round added.
# Guards that the LLM cannot override: a round limit, at most 3 queries per round,
# no repeated query, and a stop when a round finds no new paper.

PLAN_CANDIDATES_SHOWN = 8


def _plan_steps(db: Database, run_id: UUID) -> list[dict[str, Any]]:
    return [s_["detail"] for s_ in db.get_steps(run_id, "plan")]


def plan_prompt(db: Database, library: PromptLibrary, run_id: UUID, max_rounds: int) -> dict[str, Any]:
    """Render P2 for the next round, or report that the round budget is used up."""
    decomposed = DecomposerOutput.model_validate(_last_step(db, run_id, "decompose")["output"])
    retrieved = _last_step(db, run_id, "retrieve")
    plans = _plan_steps(db, run_id)
    rnd = len(plans) + 1
    stopped = any(p_.get("continue") is False for p_ in plans)
    if rnd > max_rounds or stopped:
        return {"run": False, "round": rnd, "max_rounds": max_rounds}

    run = db.get_run(run_id) or {}
    idea = (run.get("input") or {}).get("idea_text") or decomposed.facets.method
    lines = []
    for i, c in enumerate(retrieved.get("pool", retrieved["candidates"])[:PLAN_CANDIDATES_SHOWN], start=1):
        ev = next((c2["evidence"] for c2 in retrieved["candidates"] if c2["paper_id"] == c["paper_id"]), [])
        snippet = " ".join(ev[0]["text"].split())[:300] if ev else ""
        score = f"{c['rerank_score']:.2f}" if c.get("rerank_score") is not None else "n/a"
        lines.append(f"{i}. {c['title']} ({c.get('year') or 'n.d.'}; source: {c.get('source', 'local')}; relevance {score})")
        if snippet:
            lines.append(f"   {snippet}")
    history = []
    all_queries = list(retrieved.get("queries", []))
    history.append("Decomposer queries (local library): " + "; ".join(f'"{q}"' for q in all_queries[: len(decomposed.queries)]))
    for p_ in plans:
        for q in p_.get("queries", []):
            history.append(f'Round {p_["round"]}, {q["where"]}: "{q["text"]}" -> {q.get("new_papers", 0)} new papers'
                           + (f' (failed: {q["error"]})' if q.get("error") else ""))
    variables = {
        "idea_block": idea,
        "facets_block": _facets_block(decomposed.facets),
        "round_block": f"ROUND {rnd} of {max_rounds}. Searches left after this one: {max_rounds - rnd}.",
        "candidates_block": "\n".join(lines) or "(no candidates yet)",
        "history_block": "\n".join(history),
    }
    prompt = library.load(PLANNER)
    rendered = prompt.render({k: v for k, v in variables.items() if k in prompt.variables})
    db.add_step(run_id, "plan_prompt", "done", {"prompt": PLANNER, "prompt_version": rendered.version, "round": rnd})
    return {"run": True, "round": rnd, "max_rounds": max_rounds, **rendered.__dict__}


def apply_plan(
    db: Database,
    retriever: Retriever,
    reranker: "Reranker | None",
    s2: "SemanticScholar | None",
    run_id: UUID,
    output: Any,
    cfg: RetrievalConfig,
    coverage: dict[str, Any],
    max_rounds: int,
    results_per_query: int = 10,
) -> dict[str, Any]:
    """Execute one planner decision. Returns {"continue": bool, ...} for the n8n loop."""
    decomposed = DecomposerOutput.model_validate(_last_step(db, run_id, "decompose")["output"])
    before = _last_step(db, run_id, "retrieve")
    plans = _plan_steps(db, run_id)
    rnd = len(plans) + 1
    detail: dict[str, Any] = {"round": rnd, "max_rounds": max_rounds}

    try:
        plan = PlanOutput.model_validate(output)
    except ValidationError as exc:
        # An unusable plan ends the search; the judge still runs on what was found.
        detail |= {"decision": "invalid", "errors": _field_errors(exc)[:10], "continue": False, "stop_reason": "invalid plan"}
        db.add_step(run_id, "plan", "failed", detail)
        return detail
    detail |= {"decision": plan.decision, "reason": plan.reason, "gaps": plan.gaps}

    if rnd > max_rounds:
        detail |= {"continue": False, "stop_reason": "round limit reached", "queries": []}
    elif plan.decision == "stop":
        detail |= {"continue": False, "stop_reason": "planner decided to stop", "queries": []}
    elif not plan.queries:
        detail |= {"continue": False, "stop_reason": "search decided without queries", "queries": []}
    else:
        used = {q.lower() for q in before.get("queries", [])} | {
            q["text"].lower() for p_ in plans for q in p_.get("queries", [])
        }
        executed = []
        for q in plan.queries:
            entry: dict[str, Any] = {"text": q.text, "where": q.where, "facets": q.facets}
            if q.text.lower() in used:
                entry["skipped"] = "already searched"
            elif q.where == "web":
                if s2 is None:
                    entry["error"] = "Semantic Scholar not configured"
                else:
                    try:
                        entry["results"] = [p_.as_dict() for p_ in s2.search(q.text, results_per_query)]
                    except SemanticScholarError as exc:
                        entry["error"] = str(exc)[:200]
            used.add(q.text.lower())
            executed.append(entry)

        local_queries = [q.text for q in decomposed.queries] + [
            e["text"] for p_ in plans + [{"queries": executed}] for e in p_.get("queries", [])
            if e["where"] == "local" and not e.get("skipped")
        ]
        web = [e for p_ in plans + [{"queries": executed}] for e in p_.get("queries", []) if e.get("results")]
        external = [[ExternalPaper.from_dict(d) for d in e["results"]] for e in web]
        run = db.get_run(run_id) or {}
        idea = (run.get("input") or {}).get("idea_text") or decomposed.facets.method
        ranked = rank_candidates(retriever, reranker, idea, local_queries, cfg, external)

        old_pool = {c["paper_id"] for c in before.get("pool", before["candidates"])}
        old_top = {c["paper_id"] for c in before["candidates"]}
        new_pool = [c["paper_id"] for c in ranked["pool"] if c["paper_id"] not in old_pool]
        for e in executed:
            found = {r["paper_id"] for r in e.get("results", [])}
            e["new_papers"] = len(found - old_pool) if e["where"] == "web" else None
            if e.get("results"):
                e["results_count"] = len(e["results"])
        cov = {**coverage, "web_queries": len(web), "search_rounds": rnd}
        db.add_step(
            run_id, "retrieve", "done",
            {"strategy": cfg.strategy, **ranked, "web_queries": [e["text"] for e in web], "round": rnd, "coverage": cov},
        )
        added_top = [c["paper_id"] for c in ranked["candidates"] if c["paper_id"] not in old_top]
        detail |= {"queries": executed, "new_in_pool": new_pool, "new_candidates": added_top}
        if not new_pool:
            detail |= {"continue": False, "stop_reason": "the round found no new paper"}
        else:
            detail |= {"continue": rnd < max_rounds, "stop_reason": None if rnd < max_rounds else "round limit reached"}

    db.add_step(run_id, "plan", "done", detail)
    return {k: v for k, v in detail.items() if k != "queries"} | {
        "queries": [{k: v for k, v in e.items() if k != "results"} for e in detail.get("queries", [])]
    }


def _facets_block(facets: Facets) -> str:
    return "\n".join(f"- {name}: {getattr(facets, name)}" for name in FACETS)


def unspecified_facets(facets: dict[str, str]) -> list[str]:
    """Facets the idea itself leaves open: nothing can be covered or uncovered there."""
    return [name for name in FACETS if facets.get(name, "").strip().lower().startswith(NOT_SPECIFIED)]


def _passage_label(e: dict[str, Any]) -> str:
    kind = e.get("chunk_type") or "passage"
    if kind == "abstract":
        return "abstract"
    if kind == "section" and e.get("section"):
        return f"section: {e['section']}"
    return kind  # window, table, figure


def _library_block(retrieved: dict[str, Any]) -> str:
    """What was searched, so the judge can qualify its summary (F-002)."""
    cov = retrieved.get("coverage", {})
    papers = cov.get("papers_in_knowledge_base")
    full = cov.get("papers_with_full_text")
    parts = []
    if papers is not None:
        sentence = f"The knowledge base holds {papers} papers"
        if full is not None:
            sentence += f", {full} of them with full text and the rest with the abstract only"
        parts.append(sentence + ".")
    web = retrieved.get("web_queries") or []
    parts.append(
        f"Retrieval ran {len(retrieved.get('queries', []))} search queries in the knowledge base"
        + (f" and {len(web)} on Semantic Scholar (about 200 million papers; titles and abstracts only)" if web else "")
        + f", and kept the {len(retrieved.get('candidates', []))} candidate papers below."
        + ("" if web else " Papers outside the knowledge base were not searched.")
    )
    return " ".join(parts)


def _candidates_block(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "(no candidate papers were retrieved)"
    lines = []
    for i, c in enumerate(candidates, start=1):
        source = " | found on Semantic Scholar" if c.get("source") == "semantic_scholar" else ""
        lines.append(f"[C{i}] paper_id: {c['paper_id']} | title: {c['title']} | year: {c['year']}{source}")
        if c["evidence"] and all(e.get("chunk_type") == "abstract" for e in c["evidence"]):
            lines.append("  Only the abstract of this paper is shown: its data and results are usually not visible.")
        for e in c["evidence"]:
            text = " ".join(e["text"].split())
            lines.append(f"  - ({e['chunk_id']}) [{_passage_label(e)}] {text}")
        lines.append("")
    return "\n".join(lines).rstrip()


def judge_prompt(db: Database, library: PromptLibrary, run_id: UUID) -> tuple[RenderedPrompt, int]:
    """Render P3 for the next judge attempt; on a retry, include the verifier's feedback."""
    decomposed = DecomposerOutput.model_validate(_last_step(db, run_id, "decompose")["output"])
    retrieved = _last_step(db, run_id, "retrieve")
    verifications = db.get_steps(run_id, "verify")
    attempt = len(verifications) + 1
    if attempt > MAX_VERIFY_ATTEMPTS:
        raise NoveltyError(f"run {run_id} already used {MAX_VERIFY_ATTEMPTS} judge attempts")

    feedback = ""
    if verifications:
        reasons = [r for item in verifications[-1]["detail"]["rejected"] for r in item["reasons"]]
        feedback = (
            "\nFEEDBACK ON YOUR PREVIOUS ANSWER\n"
            "Your previous answer was checked automatically and these problems were found:\n"
            + "\n".join(f"- {r}" for r in reasons)
            + "\nAnswer again, fixing every problem. Use only the paper ids and chunk ids shown above."
        )

    variables = {
        "facets_block": _facets_block(decomposed.facets),
        "candidates_block": _candidates_block(retrieved["candidates"]),
        "feedback_block": feedback,
        "library_block": _library_block(retrieved),
    }
    prompt = library.load(JUDGE)
    # Older prompt versions (v1) do not use every variable; pass only what they declare.
    rendered = prompt.render({k: v for k, v in variables.items() if k in prompt.variables})
    db.add_step(run_id, "judge_prompt", "done", {"prompt": JUDGE, "prompt_version": rendered.version, "attempt": attempt})
    return rendered, attempt


def verify(db: Database, run_id: UUID, output: Any) -> dict[str, Any]:
    """The verifier: plain code, no LLM. Accepts only what retrieval can back up.

    Rejects any paper the retrieval did not return, any chunk id that was not
    shown for that paper, and any non-"unknown" verdict without evidence.
    Returns status "pass", "retry" (one more judge attempt allowed) or "final"
    (rejections remain; they are reported as unverified, never silently dropped).
    """
    retrieved = _last_step(db, run_id, "retrieve")
    attempt = len(db.get_steps(run_id, "verify")) + 1
    shown = {c["paper_id"]: {e["chunk_id"] for e in c["evidence"]} for c in retrieved["candidates"]}

    verified: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    summary = None
    try:
        parsed = JudgeOutput.model_validate(output)
    except ValidationError as exc:
        rejected.append({"item": None, "reasons": [f"invalid output format: {m}" for m in _field_errors(exc)][:10]})
        parsed = None

    if parsed is not None:
        summary = parsed.summary
        seen: set[str] = set()
        for match in parsed.matches:
            reasons = []
            if match.paper_id in seen:
                reasons.append(f"paper {match.paper_id} is listed more than once")
            elif match.paper_id not in shown:
                reasons.append(f"paper {match.paper_id} was not among the retrieved candidates")
            else:
                for cid in match.evidence:
                    if cid not in shown[match.paper_id]:
                        reasons.append(f"chunk {cid} was not shown for paper {match.paper_id}")
                labels = match.overlap.model_dump().values()
                if not match.evidence and any(label != "unknown" for label in labels):
                    reasons.append(f"paper {match.paper_id}: labels other than 'unknown' need at least one evidence chunk")
            seen.add(match.paper_id)
            item = match.model_dump()
            if reasons:
                rejected.append({"item": item, "reasons": reasons})
            else:
                verified.append(item)

    if not rejected:
        status = "pass"
    elif attempt < MAX_VERIFY_ATTEMPTS:
        status = "retry"
    else:
        status = "final"
    detail = {"attempt": attempt, "status": status, "verified": verified, "rejected": rejected, "summary": summary}
    db.add_step(run_id, "verify", "done" if status != "retry" else "failed", detail)
    return detail


def assemble(db: Database, run_id: UUID) -> dict[str, Any]:
    """The novelty branch's result, built only from stored, verified steps."""
    decompose = _last_step(db, run_id, "decompose")
    retrieved = _last_step(db, run_id, "retrieve")
    verifications = db.get_steps(run_id, "verify")
    if not verifications:
        raise NoveltyError("no verification step yet")
    last = verifications[-1]["detail"]
    if last["status"] == "retry":
        raise NoveltyError("the last verification asked for a retry; run the judge again first")

    titles = {c["paper_id"]: c for c in retrieved["candidates"]}

    def enrich(item: dict[str, Any]) -> dict[str, Any]:
        c = titles.get(item["paper_id"], {})
        return {
            **item,
            "title": c.get("title"),
            "year": c.get("year"),
            "url": c.get("url") or _url(item["paper_id"], None),
            "source": c.get("source", "local"),
        }

    judge_prompts = db.get_steps(run_id, "judge_prompt")
    facets = decompose["output"]["facets"]
    unspecified = unspecified_facets(facets)
    order = {c["paper_id"]: i for i, c in enumerate(retrieved["candidates"])}

    def strength(item: dict[str, Any]) -> tuple[int, int, int]:
        labels = item["overlap"]
        core = LABEL_WEIGHT[labels["task"]] + LABEL_WEIGHT[labels["method"]]
        total = sum(LABEL_WEIGHT[labels[f]] for f in FACETS)
        return (-core, -total, order.get(item["paper_id"], 99))

    # F-001: a paper is "closest work" only if the judge found some overlap. The
    # others were still checked; they are listed separately, never hidden.
    overlapping = sorted((i for i in last["verified"] if strength(i)[1] < 0), key=strength)
    not_close = sorted((i for i in last["verified"] if strength(i)[1] == 0), key=strength)
    coverage = {
        **retrieved["coverage"],
        "strategy": retrieved["strategy"],
        "queries": retrieved["queries"],
        "web_queries": retrieved.get("web_queries", []),
        "candidates_considered": len(retrieved["candidates"]),
        "note": "Not found in this knowledge base does not mean novel.",
    }
    plans = _plan_steps(db, run_id)
    plan_prompts = db.get_steps(run_id, "plan_prompt")
    return {
        "branch": "novelty",
        "implemented": True,
        "idea_facets": facets,
        "unspecified_facets": unspecified,
        "overlap_found": bool(overlapping),
        "closest_work": [enrich(i) for i in overlapping],
        "checked_not_close": [enrich(i) for i in not_close],
        # Written by code from verified labels only, so every statement in it is backed by the steps above.
        "summary": _summary(overlapping, not_close, unspecified, coverage),
        "unverified": [
            {"item": enrich(r["item"]) if r["item"] else None, "reasons": r["reasons"]} for r in last["rejected"]
        ],
        # Free text written by the LLM: shown, but labelled, because the verifier checks ids, not prose.
        "summary_llm_written": last.get("summary"),
        "coverage": coverage,
        "judge_attempts": len(verifications),
        # What the search agent decided in each round, and what it found (D29).
        "search_agent": [
            {
                "round": p_["round"],
                "decision": p_.get("decision"),
                "reason": p_.get("reason"),
                "gaps": p_.get("gaps", []),
                "queries": [
                    {k: q.get(k) for k in ("text", "where", "new_papers", "error", "skipped") if q.get(k) is not None}
                    for q in p_.get("queries", [])
                ],
                "new_candidates": p_.get("new_candidates", []),
                "stop_reason": p_.get("stop_reason"),
            }
            for p_ in plans
        ],
        "prompt_versions": {
            DECOMPOSER: decompose["prompt_version"],
            **({PLANNER: plan_prompts[-1]["detail"]["prompt_version"]} if plan_prompts else {}),
            JUDGE: judge_prompts[-1]["detail"]["prompt_version"] if judge_prompts else None,
        },
    }


def _summary(overlapping: list[dict[str, Any]], not_close: list[dict[str, Any]], unspecified: list[str], coverage: dict[str, Any]) -> str:
    papers = coverage.get("papers_in_knowledge_base")
    searched = f"Searched {papers} papers" if papers is not None else "Searched the knowledge base"
    full = coverage.get("papers_with_full_text")
    if papers is not None and full is not None:
        searched += f" ({full} with full text)"
    checked = len(overlapping) + len(not_close)
    web = len(coverage.get("web_queries") or [])
    also = f", plus {web} Semantic Scholar {'query' if web == 1 else 'queries'}" if web else ""
    lines = [f"{searched} with {len(coverage.get('queries', []))} queries{also}; {checked} candidate papers were judged."]
    if overlapping:
        parts = []
        for item in overlapping[:3]:
            shared = [f"{f} {item['overlap'][f]}" for f in FACETS if item["overlap"][f] in ("same", "partial")]
            parts.append(f"{item['paper_id']} ({', '.join(shared)})")
        lines.append(f"{len(overlapping)} overlap with the idea: " + "; ".join(parts) + ("; ..." if len(overlapping) > 3 else "") + ".")
    else:
        lines.append("None of them overlaps with the idea on any facet.")
    if not_close:
        lines.append(f"{len(not_close)} were checked and found different or unclear on every facet.")
    if unspecified:
        lines.append(f"The idea does not specify its {' or '.join(unspecified)}, so overlap there could not be judged.")
    lines.append("Not found here does not mean novel." if web else "Not found in this knowledge base does not mean novel.")
    return " ".join(lines)


# ---- helpers -------------------------------------------------------------------------------


def _require_run(db: Database, run_id: UUID) -> None:
    if db.get_run(run_id) is None:
        raise LookupError(f"run {run_id} not found")


def _last_step(db: Database, run_id: UUID, name: str) -> dict[str, Any]:
    _require_run(db, run_id)
    steps = db.get_steps(run_id, name)
    if not steps:
        raise NoveltyError(f"run {run_id} has no '{name}' step yet")
    return steps[-1]["detail"]
