# Publishing standard

This repo and site publish divergence flags about named companies. Legal defensibility depends entirely on editorial discipline.

## The three-label rule

Every substantive claim carries exactly one label:

- **documented** — backed by preserved primary evidence (filed document + page + hash). The evidence must exist before the label is used.
- **reported** — attributed to a named external source (registry, third-party dataset) not yet independently verified.
- **open** — an unresolved question, phrased as a question.

## Voice rules

- Frame conclusions as questions or observations: "filed Scope 1 differs from summed facility estimates by X% — whether this is material is for the reader/court to evaluate."
- Never assert fraud, greenwashing, deception, or intent as fact.
- Present the company's stated position where one exists; disputes are recorded in `registry/disputes.csv` and shown.
- Public flag names: `reported divergence`, `disclosure gap`, `insufficient data`, `unmatched` — never accusatory labels.

## Privacy rules

- No claimant/individual personal data. Corporate officers appear only in verifiable neutral contexts (report signatory, on-camera speaker at a public event).
- Video claims cite only public events; every claim pins `yt#<id>@<t>` and shows the verbatim quote.
- Nothing from `data/`, `evidence/`, `ops/` is copied to the public site or pushed.

## Legal-basis rule

Every `documented`/`reported` item carries an implicit legal basis: the source is public, its license permits our use (`registry/sources.csv`), and the transformation (extraction/flag) is disclosed in the open methodology.
