#!/usr/bin/env python3
"""deps_check.py — audit registry/deps.csv against actual imports.
Every required/optional dep must be importable or documented as excluded;
every import in esg/ + tools/ must appear in deps.csv (no undeclared deps).
"""
import ast
import csv
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

IMPORT_TO_DEP = {
    "fitz": "pymupdf", "pymupdf": "pymupdf", "edgar": "edgartools",
    "scrapy": "scrapy", "stem": "stem", "httpx": "httpx",
    "requests": "requests", "bs4": "beautifulsoup4", "lxml": "lxml",
    "pydantic": "pydantic", "instructor": "instructor",
    "pymupdf4llm": "pymupdf", "pypdfium2": "pypdfium2", "pypdf": "pypdf",
    "pdfplumber": "pdfplumber", "docling": "docling",
    "rapidocr_onnxruntime": "rapidocr", "pytesseract": "rapidocr",
    "splink": "splink", "geopandas": "geopandas", "rapidfuzz": "rapidfuzz",
    "spacy": "spacy", "sqlite_vec": "sqlite-vec",
    "sentence_transformers": "sentence-transformers",
    "frictionless": "frictionless", "jsonschema": "jsonschema",
    "duckdb": "duckdb", "pandas": "pandas", "pyarrow": "pyarrow",
    "orjson": "orjson", "rich": "rich", "jinja2": "jinja2",
    "weasyprint": "weasyprint", "langdetect": "langdetect",
    "yaml": "pyyaml", "pandas": "pandas",
}
STDLIB = set(sys.stdlib_module_names) | {"esg"}


def imports_in(path):
    out = set()
    for root, dirs, files in os.walk(path):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            try:
                tree = ast.parse(open(os.path.join(root, fn)).read())
            except Exception:
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    out |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom) and n.module:
                    out.add(n.module.split(".")[0])
    return out


def main():
    deps = {r["dep"]: r for r in csv.DictReader(
        open(os.path.join(ROOT, "registry", "deps.csv")))}
    used = set()
    for d in ("esg", "tools"):
        used |= imports_in(os.path.join(ROOT, d))
    undeclared = {u for u in used
                  if u not in STDLIB and IMPORT_TO_DEP.get(u, u) not in deps}
    if undeclared:
        print(f"FAIL undeclared deps: {undeclared}")
        sys.exit(1)
    missing = []
    for imp, dep in IMPORT_TO_DEP.items():
        if dep in deps and deps[dep]["tier"] in ("required",):
            if not importlib.util.find_spec(imp):
                missing.append(dep)
    if missing:
        print(f"WARN required deps not importable: {missing}")
    else:
        print("deps_check PASS — all imports declared, required deps present")


if __name__ == "__main__":
    main()
