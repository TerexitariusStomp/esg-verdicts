# Monetization — how the registry pays for itself

The public registry stays free. Revenue comes from the evidence layer
underneath it. Posture: we sell *documented divergence*, never accusations.

## Streams

| Stream | What | Price | Status |
|---|---|---|---|
| **Evidence packs** | per-company diligence: hashed exhibits, chain-of-custody, verbatim statements, divergence detail (`evidence/<slug>/`, built by `python -m esg.evidence --top N`) | $5k pilot → $25-75k/matter | infrastructure live |
| **Analyst tier** | `/v1/*` API on the grid worker: flag alerts, verdicts, coverage stats (key-gated, D1-mirrored public rows) | $500/mo indie / $2-5k/mo team | API live, billing via Stripe webhook (pending) |
| **Data license** | bulk API/parquet feed, commercial use (academics & press free) | $25-150k/yr | schema live; license audit gates every export (`make verify` check 6) |
| **Whistleblower tail** | TCR referrals via counsel on `documented` flags | 10-30% of sanctions | option value, not forecast |
| **Grants** | climate-integrity philanthropy (the Climate TRACE funding model) | $50-500k awards | after public coverage is demonstrable |

## Why buyers pay

Litigation funders commit ~$2.3B/yr at ~$8M avg deal size; a pack that
de-risks a matter costs <1% of the deal. Ratings shops (MSCI ~$354M ESG
segment, Sustainalytics) sell opinion scores with no physical-world check;
the divergence dataset doesn't exist anywhere else.

## Constraints (non-negotiable)

- `verification_only` sources (Carbon Mapper, Urgewald GCE/GOGEL) never
  leave `data/` — enforced by `tools/verify/run_all.py` check 6.
- Only `publishable=1` rows reach any commercial surface; posture labels
  (`documented`/`reported`/`open`) ship with every assertion.
- Packs document *discrepancy*, not intent — the legal posture is the
  product.
