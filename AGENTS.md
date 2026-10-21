# Agent notes — esg-verdicts

## Build / run

- Python 3.14. Venv: `uv venv .venv && uv pip install -r requirements.txt` (heavy/optional deps in `requirements-ml.txt`).
- Every stage is a CLI: `python -m esg.<stage>` (universe|catalog|harvest|pages|figures|extract|filings|geo|litigate|verify|flags|claims|greenwash|evaluate|evidence|export).
- Ops: `make verify | views | stamp | export | pack | backup | restore | review | eval`.
- systemd user timers in `systemd/` install to `~/.config/systemd/user/` — sequential oneshot chain, `Nice=15`, `MemoryMax=`.

## Hard rules

1. `data/`, `evidence/`, `ops/` are **private and gitignored — never commit or push them**. `git ls-files | grep -E 'data/|evidence/|ops/'` must be empty.
2. Echo-gate: no LLM-produced snippet enters `extractions`/`statements.csv` unless it is a verbatim substring of the hashed source. The verify gate re-checks this offline.
3. Posture labels `documented`/`reported`/`open` on all public assertions; no fraud/intent words as assertions.
4. `registry/sources.csv` licenses gate every export — verification-only sources (Carbon Mapper, Urgewald GCE/GOGEL) never leave `data/`.
5. Schemas before rows: `registry/schema/*.json` exists before its CSV gets rows.
6. `manifest()` hash-chains every produced artifact; `make stamp` OTS-anchors MANIFEST + meta (binds git HEAD).
7. Review holds: `severity>=high` or `match_confidence<0.85` or named verdict change → `review_holds`, excluded from public export until `make review` ack.
8. WAL + `busy_timeout=30000` always; single-writer stages.
9. freelm gateway `127.0.0.1:8080` is optional — everything works regex-only when down; `ESG_GW` env selects endpoint.
10. Don't assert a company is fraudulent — flags are `reported divergence`/`disclosure gap`/`insufficient data`.
