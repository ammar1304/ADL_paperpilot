# Corpus

The papers knowledge base covers **retrieval-augmented generation (RAG) for question answering**: arXiv papers in cs.CL, cs.IR, cs.AI or cs.LG, from 2020 on. Why this domain: [docs/DECISIONS.md](../docs/DECISIONS.md) (D13).

| File | In git | What it is |
| --- | --- | --- |
| `manifest.csv` | yes | The exact papers and versions in the knowledge base. Everyone builds from this file. |
| `selection.json` | yes | The query and date that produced the manifest, counts per year, and the papers added by hand. |
| `additions.csv` | yes | Papers added by hand on top of the search (foundational work the search missed), each with its expected title and the reason. |
| PDFs, metadata, extracted text | **no** | Cached in `.cache/pp-tools/`. arXiv content may be used for research but not redistributed. |

## 1. Select the papers (once, one person)

Needs internet access to arXiv. Takes about a minute: arXiv allows one request every 3 seconds.

```bash
docker compose up -d --wait
docker compose exec pp-tools python -m app.corpus.select
```

Defaults: up to 500 papers by arXiv relevance, of which 50 get their full PDF chunked. The 50 are the most relevant, capped per year so every year is represented. Options: `--max-papers`, `--fulltext`, `--query`, `--force` (replace an existing manifest).

## 2. Add the papers the search missed

The search ranks by relevance among papers from 2020 on, and it favours recent work: the first selection had 1 paper from 2020 and 206 from 2025. Foundational papers (RAG, DPR, FiD, Self-RAG, ...) were missing, so the novelty check could not find them.

`additions.csv` lists them. Each row has the arXiv id, `fulltext` (1 or 0), the **expected title**, and why it is there. The command looks every id up on arXiv and refuses any whose real title does not match the expected one, so a mistyped id never adds the wrong paper:

```bash
docker compose exec pp-tools python -m app.corpus.add --dry-run   # report only
docker compose exec pp-tools python -m app.corpus.add             # update manifest.csv and selection.json
```

The output lists `added`, `upgraded_to_fulltext` (already in the manifest, now full text), `already_present`, `not_found` and `title_mismatch`. Fix the last two in `additions.csv` (check the paper on arxiv.org) and run it again; it is safe to repeat.

## 3. Review the manifest

Open `manifest.csv` and check it with the team before committing:

- **Off-topic papers:** delete their rows.
- **The `fulltext` column (1 or 0):** the gold set and the audit tests need full-text papers the team understands and that have result tables. Swap papers in or out by changing 1 and 0. Keep about 50.
- **Then commit** `manifest.csv` and `selection.json`. From then on the corpus is frozen; changing it means re-running every evaluation.

## 4. Build the knowledge base

Through n8n (the way the pipeline runs):

```bash
curl -X POST http://localhost:5678/webhook/ingest -H 'Content-Type: application/json' -d '{}'
```

It answers at once with a `job_id`; follow it with `curl http://localhost:8000/jobs/<job_id>`. Body options: `{"limit": 20}` indexes only the first 20 rows (quick check); `{"recreate": false}` adds to the existing collections instead of rebuilding them.

The first build downloads the PDFs (3 s apart) and embeds every chunk on CPU. On a laptop CPU, 500 papers with 50 full texts took 88 minutes, almost all of it embedding (about one chunk per second).

Later builds are much faster: downloads and extracted text are cached, and vectors already in Qdrant are reused for every chunk whose text and embedding model did not change. Adding 30 papers only embeds their chunks. The build report shows `chunks_embedded` and `chunks_reused` per collection. Set `REUSE_VECTORS=false` to force a full re-embedding.

Check what was built: `curl http://localhost:8000/corpus/stats`, and the full report in `.cache/pp-tools/build_report.json`. Look at `pdf_failures` (papers that fell back to their abstract) and `papers_without_headings` (papers where section detection failed).

## What gets indexed

Each paper is chunked twice, into two Qdrant collections:

| Collection | Strategy | Chunks |
| --- | --- | --- |
| `papers_fixed` | A, fixed-size | Windows of 512 tokens with 64 overlap over title, abstract and body |
| `papers_structured` | B, structure-aware | One per abstract, per section (split at paragraph boundaries above 512 tokens), per table with its caption, per figure caption |

Abstract-only papers give one chunk in each strategy. References and acknowledgements are excluded from both. Chunk ids look like `arxiv:2005.11401#sec:3.2` or `arxiv:2005.11401#tbl:2`; later milestones cite them as evidence.
