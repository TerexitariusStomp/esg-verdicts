PY := .venv/bin/python
PY3 := python3

.PHONY: all verify verify-full views stamp export pack backup restore review eval production deps-check loc

all: verify

# --- pipeline entrypoints (each stage is python -m esg.<stage>) ---
universe:
	$(PY) -m esg.universe

catalog:
	$(PY) -m esg.catalog

harvest:
	$(PY) -m esg.harvest --workers 6

pages:
	$(PY) -m esg.pages

figures:
	$(PY) -m esg.figures

extract:
	$(PY) -m esg.extract

filings:
	$(PY) -m esg.filings

geo:
	$(PY) -m esg.geo

litigate:
	$(PY) -m esg.litigate

verify-stage:
	$(PY) -m esg.verify

flags:
	$(PY) -m esg.flags

claims:
	$(PY) -m esg.claims

greenwash:
	$(PY) -m esg.greenwash

# --- ops surface ---
verify:
	$(PY3) tools/verify/run_all.py

verify-full:
	$(PY3) tools/verify/run_all.py --full

views:
	$(PY) tools/registry/build_views.py

stamp:
	ots stamp MANIFEST.sha256 && ots stamp MANIFEST-meta.txt
	git add -f MANIFEST.sha256 MANIFEST-meta.txt MANIFEST.sha256.ots MANIFEST-meta.txt.ots 2>/dev/null || true

export:
	$(PY) -m esg.export

eval:
	$(PY) -m esg.evaluate

review:
	$(PY) tools/review/cli.py

pack:
	$(PY) -m esg.evidence

production:
	$(PY) tools/production/build.py

manifest:
	bash tools/refresh_manifest.sh

backup:
	bash tools/backup_r2.sh

restore:
	bash tools/restore_r2.sh

# audits
loc:
	@cloc esg tools grid --quiet 2>/dev/null || true

deps-check:
	$(PY) tools/verify/deps_check.py
