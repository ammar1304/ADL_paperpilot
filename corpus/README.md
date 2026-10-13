# Corpus

The papers knowledge base covers **retrieval-augmented generation (RAG) for question answering**: arXiv papers in cs.CL, cs.IR, cs.AI or cs.LG, from 2020 on. Why this domain: [docs/DECISIONS.md](../docs/DECISIONS.md) (D13).

| File | In git | What it is |
| --- | --- | --- |
| `manifest.csv` | yes | The exact papers and versions in the knowledge base. Everyone builds from this file. |
| `selection.json` | yes | The query and date that produced the manifest, and counts per year. |
| PDFs, metadata, extracted text | **no** | Cached in `.cache/pp-tools/`. arXiv content may be used for research but not redistributed. |

## 1. Select the papers (once, one person)

Needs internet access to arXiv. Takes about a minute: arXiv allows one request every 3 seconds.

```bash
docker compose up -d --wait
docker compose exec pp-tools python -m app.corpus.select
```

Defaults: up to 500 papers by arXiv relevance, of which 50 get their full PDF chunked. The 50 are the most relevant, capped per year so every year is represented. Options: `--max-papers`, `--fulltext`, `--query`, `--force` (replace an existing manifest).

## 2. Review the manifest

Open `manifest.csv` and check it with the team before committing:

- **Off-topic papers:** delete their rows.
- **The `fulltext` column (1 or 0):** the gold set and the audit tests need full-text papers the team understands and that have result tables. Swap papers in or out by changing 1 and 0. Keep about 50.
- **Then commit** `manifest.csv` and `selection.json`. From then on the corpus is frozen; changing it means re-running every evaluation.

## 3. Build the knowledge base

Through n8n (the way the pipeline runs):

```bash
curl -X POST http://localhost:5678/webhook/ingest -H 'Content-Type: application/json' -d '{}'
```

It answers at once with a `job_id`; follow it with `curl http://localhost:8000/jobs/<job_id>`. Body options: `{"limit": 20}` indexes only the first 20 rows (quick check); `{"recreate": false}` adds to the existing collections instead of rebuilding them.

The first build downloads the 50 PDFs (3 s apart) and embeds every chunk on CPU, so expect 10 to 30 minutes. Later builds reuse the cache.

Check what was built: `curl http://localhost:8000/corpus/stats`, and the full report in `.cache/pp-tools/build_report.json`. Look at `pdf_failures` (papers that fell back to their abstract) and `papers_without_headings` (papers where section detection failed).

## What gets indexed

Each paper is chunked twice, into two Qdrant collections:

| Collection | Strategy | Chunks |
| --- | --- | --- |
| `papers_fixed` | A, fixed-size | Windows of 512 tokens with 64 overlap over title, abstract and body |
| `papers_structured` | B, structure-aware | One per abstract, per section (split at paragraph boundaries above 512 tokens), per table with its caption, per figure caption |

Abstract-only papers give one chunk in each strategy. References and acknowledgements are excluded from both. Chunk ids look like `arxiv:2005.11401#sec:3.2` or `arxiv:2005.11401#tbl:2`; later milestones cite them as evidence.
