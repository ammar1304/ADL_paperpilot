# Progress log

A milestone is done only when its behaviour has been run and checked. "Built" without "verified" stays open.

## Milestone 0: foundation (2026-10-11 to 2026-10-12)

**Goal:** a stack that every later milestone plugs into: n8n router, sub-workflow per branch, error workflow, run state in Postgres, pp-tools service, Qdrant running.

### Verified

Run in the development sandbox (Linux), with the same components installed natively, because container registries were blocked there: n8n 2.42.6 on Node 24.21.0, Postgres 16.15, pp-tools on Python 3.13.

| What | How | Result |
| --- | --- | --- |
| Request validation, run state, status rules | `pytest` (unit tests plus Postgres integration tests) | 21 passed |
| `/health` reports each dependency truthfully | Tests with Postgres down, Qdrant down, both down, both up (Qdrant simulated by a small HTTP stub) | Pass |
| Workflow import and publish | `n8n/entrypoint.sh` on an empty n8n database | 4 workflows imported and published |
| Restart safety | 3 consecutive starts of the entrypoint on the same database | Pass, after fixing the unpublish step (D4) |
| Novelty and audit requests | `scripts/smoke_test.py --no-compose --allow-degraded-qdrant` | HTTP 200, run stored as `done` |
| Invalid request | Smoke test | HTTP 422 with field errors, no row stored |
| Error workflow | Smoke test with the fault header | HTTP 500, run marked `failed` with the error message and node |
| Fault hook disabled | `PP_ALLOW_TEST_FAULTS=false` plus the header | HTTP 200, header ignored |
| pp-tools down | Request sent with pp-tools stopped | HTTP 500 after about 4 s (3 tries), no row |
| Export round trip | Export router with the n8n CLI, normalize, compare | Nodes, parameters, connections and settings identical |
| Postgres init script | Run against a local Postgres | Database created |
| Compose file and shell scripts | `docker compose config`, `sh -n`, `bash -n` | Valid |

### Not verified yet: run on a team machine

Run `docker compose up -d --build --wait` then `python scripts/smoke_test.py` on a clean clone, and record the result here.

- [ ] All four images pull with the pinned tags
- [ ] pp-tools image builds
- [ ] Qdrant healthcheck (bash `/dev/tcp` probe) turns healthy; the smoke test then passes without `--allow-degraded-qdrant`
- [ ] n8n container: entrypoint runs under the image's shell, `n8n_data` volume is writable
- [ ] Postgres 17 works with n8n and pp-tools (tests ran on 16)
- [ ] `scripts/export_workflows.sh` against the n8n container
- [ ] Smoke test on Windows (Docker Desktop) and macOS

### Known issues

- The webhook returns 404 for a few seconds after n8n reports healthy.
- A failed workflow returns n8n's generic `{"message": "Error in workflow"}` without the `run_id`.
- n8n logs a missing Python task runner and several deprecation notices at start. Harmless: no Code node is used.
- n8n and pp-tools share one Postgres user. Acceptable locally; split before any shared deployment.
- Starlette prints a deprecation warning for its httpx-based test client.

## Next: milestone 1, corpus and chunking

Blocked on team decisions: paper domain, LLM provider, embedding model.
