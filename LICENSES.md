# Dependency & data license inventory

Canonical machine-readable inventory: `registry/deps.csv` (code) and `registry/sources.csv` (data). Copyleft is acceptable — this project is AGPL-3.0.

## Code dependencies

| Package | License | Notes |
|---|---|---|
| edgartools | MIT | SEC universe + filings |
| Scrapy | BSD-3 | crawl machinery |
| stem | LGPL-3 | Tor control |
| PyMuPDF / PyMuPDF4LLM | AGPL-3.0 | PDF text (copyleft OK) |
| Docling | MIT | layout/tables/OCR pipeline |
| olmOCR | Apache-2.0 | hard-scan VLM tier (optional, GPU) |
| RapidOCR / Tesseract / PaddleOCR | Apache-2.0 | OCR engines |
| instructor / outlines | MIT / Apache-2.0 | schema-constrained LLM output |
| Splink | MIT | entity resolution |
| geopandas / shapely / geopy | BSD/MIT | geo |
| RapidFuzz | MIT | fallback matching |
| spaCy | MIT | NLP for claims |
| sqlite-vec | MIT | embeddings |
| sentence-transformers | Apache-2.0 | embeddings |
| frictionless / jsonschema | MIT | registry validation |
| duckdb / duckdb-wasm | MIT | exports + browser queries |
| pyarrow / pandas | Apache-2.0/BSD | dataframes |
| pagefind | MIT | static search |
| Leaflet / MapLibre | BSD | maps |
| hono | MIT | grid worker |
| web-llm | Apache-2.0 | browser LLM tier |
| transformers.js | Apache-2.0 | browser fallback tier |
| onnxruntime-web | MIT | browser inference |
| tesseract.js | Apache-2.0 | browser OCR |
| opentimestamps (`ots`) | MIT | custody stamps |
| sigstore/cosign | Apache-2.0 | optional artifact signing |
| yt-dlp | Unlicense | video corpus expansion |
| faster-whisper | MIT | transcription |
| Jinja2 / WeasyPrint | BSD | pack rendering |
| langdetect | Apache-2.0 | language detection |

## License cautions (enforced in deps.csv `tier` column)

- **Marker / Surya** — code Apache-2.0 but model weights: AI Pubs Open Rail-M, free only <$5M revenue → prefer Docling/olmOCR.
- **Open Sustainability Analyst** — use only `report_analyst/` (RPL). `report_analyst_api|jobs|search_backend` use "Climate+Tech Open License for Good" — not imported.
- **pyannote models** — gated HF terms; deferred (corpus already exists).
- **ClimateBERT / DePlot / ChartGemma weights** — verify license before shipping; not on critical path.

## Data sources (registry/sources.csv is authoritative)

Commercial-OK: SEC EDGAR, GLEIF (CC0), EPA GHGRP (public domain), EU ETS, Australia NGER, Canada GHGRP, Climate TRACE (CC-BY-4.0), Sabin litigation DB (via Climate Policy Radar mapper), CA100+/TPI, CorporateRegister, WikiRate (CC-BY), responsibilityreports.com (public pages; links out, PDFs hosted by issuers).

**Verification-only tier — never republish:** Carbon Mapper plumes, Urgewald GCE/GOGEL exit lists.
