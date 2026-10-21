# esg-verdicts

**Evidence-first ESG intelligence.** We don't score companies — we extract what they filed (verbatim, hash-pinned to page), compare it against physical facility data (Climate TRACE, EPA GHGRP), what their executives said on camera during the reporting window, and where they've been litigated — then publish the divergences.

Every published number is a verbatim snippet with a page pin, SHA-256 of the source document, and acquisition timestamp. Every publish is OpenTimestamps-anchored. Nothing here is a legal conclusion — see [PUBLISHING.md](PUBLISHING.md).

## Pipeline

```
universe → catalog → harvest → pages/figures → extract → filings
        → geo → litigate → verify → flags(+review) → claims/greenwash
        → export → evidence packs
```

All queue ordering is value-prioritized: `priority()` = Climate TRACE emissions rank × litigation presence × coverage — highest-signal evidence first.

## Layout

| Path | What |
|---|---|
| `esg/` | Pipeline stages (thin adapters over OSS + ~1.4k LOC custom core) |
| `registry/` | Canonical public layer: schema'd CSVs + figures.yaml + deps.csv + eval.csv |
| `web/` | Static public site: verdicts, evidence popovers, map, duckdb-wasm, pagefind |
| `grid/` | Lease-less browser compute: signed manifest self-assign + PoW-gated /submit |
| `tools/` | verify gate, views, review CLI, production packs, backup/restore |
| `systemd/` | Per-stage user timer units (daily/weekly/quarterly cadence) |
| `data/` `evidence/` `ops/` | **Private, gitignored — never pushed** |

## Conventions

- Posture labels on everything public: `documented` / `reported` / `open` — never fraud/intent assertions.
- Verbatim provenance: every metric's snippet must be an exact substring of its source (echo-gated at runtime, re-checked offline by `make verify`).
- Licenses gate exports: `registry/sources.csv` decides what may be republished.
- Custody: `MANIFEST.sha256` covers the whole tree incl. `.git`; `make stamp` OTS-anchors it with git HEAD.

## License

AGPL-3.0 — see LICENSE. Dependency licenses are audited in `LICENSES.md` and `registry/deps.csv`.
