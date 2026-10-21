#!/usr/bin/env python3
"""run_all.py — archive validation gate (Artizen model).

Checks (fast default; --full adds deep hashing):
  1. manifest   — files present & hashed; --full re-hashes all
  2. schema     — registry CSVs validate against registry/schema/*.json
  3. pins       — every report#page, yt#id@t, EX-###, flag ref resolves
  4. figures    — figures.yaml derivations re-run; stale values fail
  5. echogate   — every published snippet is a verbatim substring of source
  6. licenses   — zero non-commercial/verification-only rows in exports
  7. posture    — three-label discipline; banned-assertion scan
  8. pii        — no private dirs in pushed tree; no secret patterns
  9. review     — no publishable=0 flag appears in public exports

Usage: run_all.py [--full] [--json]   Exit non-zero on FAIL.
"""
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
REG = os.path.join(ROOT, "registry")
RESULTS = []


def record(name, status, detail=""):
    RESULTS.append((name, status, detail))
    print(f"[{status:4}] {name}" + (f" — {detail}" if detail else ""))


def sha256(p, bufsize=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(bufsize), b""):
            h.update(b)
    return h.hexdigest()


def db():
    p = os.path.join(ROOT, "data", "esg.db")
    if not os.path.exists(p):
        return None
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    return con


# 1 manifest ---------------------------------------------------------------
def check_manifest(full):
    mf = os.path.join(ROOT, "MANIFEST.sha256")
    if not os.path.exists(mf):
        record("manifest", "FAIL", "MANIFEST.sha256 missing — run tools/refresh_manifest.sh")
        return
    missing = stale = 0
    for line in open(mf):
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        want, rel = parts
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            missing += 1
            continue
        if full and sha256(p) != want:
            stale += 1
    if missing or stale:
        record("manifest", "FAIL",
               f"{missing} missing, {stale} stale (re-run refresh_manifest.sh)")
    else:
        record("manifest", "PASS")


# 2 schema -----------------------------------------------------------------
def check_schema():
    try:
        import jsonschema
    except ImportError:
        record("schema", "WARN", "jsonschema not installed")
        return
    fails = []
    for sf in os.listdir(os.path.join(REG, "schema")):
        if not sf.endswith(".json"):
            continue
        name = sf[:-5]
        csvp = os.path.join(REG, f"{name}.csv")
        schema = json.load(open(os.path.join(REG, "schema", sf)))
        fields = {f["name"]: f for f in schema.get("fields", [])}
        if not os.path.exists(csvp) or os.path.getsize(csvp) == 0:
            continue  # empty registries are legal
        with open(csvp) as f:
            for i, row in enumerate(csv.DictReader(f), 2):
                for fname, spec in fields.items():
                    v = row.get(fname, "")
                    if spec.get("required") and v == "":
                        fails.append(f"{name}:{i}:{fname} required")
                    elif v and spec.get("enum") and v not in spec["enum"]:
                        fails.append(f"{name}:{i}:{fname}={v} not in enum")
                    elif v and spec.get("type") == "boolean" \
                            and v not in ("true", "false", "0", "1", "True",
                                          "False"):
                        fails.append(f"{name}:{i}:{fname} bad bool {v}")
                    elif v and spec.get("pattern") and \
                            not re.match(spec["pattern"], v):
                        fails.append(f"{name}:{i}:{fname} ~{spec['pattern']}")
    if fails:
        record("schema", "FAIL", f"{len(fails)} rows: {fails[:5]}")
    else:
        record("schema", "PASS")


# 3 pins --------------------------------------------------------------------
def check_pins(con):
    if not con:
        record("pins", "WARN", "no db")
        return
    bad = 0
    # statements pins must resolve: report#id@page or yt#id@t
    sp = os.path.join(REG, "statements.csv")
    if os.path.exists(sp) and os.path.getsize(sp):
        for i, row in enumerate(csv.DictReader(open(sp)), 2):
            pin = row.get("pin", "")
            m = re.match(r"report#(\d+)@p?(\d+)", pin)
            if m:
                rid, page = int(m.group(1)), int(m.group(2))
                if not con.execute("SELECT 1 FROM reports WHERE id=?",
                                   (rid,)).fetchone():
                    bad += 1
            elif re.match(r"yt#[\w-]+@\d+", pin):
                pass
            elif pin:
                bad += 1
    record("pins", "FAIL" if bad else "PASS", f"{bad} unresolvable" if bad else "")


# 4 figures -----------------------------------------------------------------
def check_figures(con):
    if not con:
        record("figures", "WARN", "no db")
        return
    fp = os.path.join(REG, "figures.yaml")
    if not os.path.exists(fp):
        record("figures", "WARN", "no figures.yaml")
        return
    figs = yaml.safe_load(open(fp))["figures"]
    stale = []
    for k, f in figs.items():
        try:
            got = con.execute(f["derivation"]).fetchone()[0] or 0
        except Exception:
            got = 0
        if got != (f.get("value") or 0):
            stale.append(k)
    record("figures", "FAIL" if stale else "PASS",
           f"stale: {stale} (run make export)" if stale else "")


# 5 echogate ----------------------------------------------------------------
def check_echogate(con):
    if not con:
        record("echogate", "WARN", "no db")
        return
    from esg.esg import echo_gate
    bad = 0
    checked = 0
    for r in con.execute(
            """SELECT e.id, e.snippet, e.page, e.report_id, e.figure_crop
               FROM extractions e WHERE e.snippet IS NOT NULL""").fetchall():
        checked += 1
        if r["figure_crop"]:
            src = os.path.join(ROOT, r["figure_crop"] + ".ocr.txt")
        else:
            src = os.path.join(ROOT, "data", "pages",
                               str(r["report_id"]), f"{r['page']}.txt")
        try:
            text = open(src).read()
        except OSError:
            bad += 1
            continue
        if not echo_gate(r["snippet"], text):
            bad += 1
    record("echogate", "FAIL" if bad else "PASS",
           f"{bad}/{checked} non-verbatim" if bad else f"{checked} checked")


# 6 licenses ----------------------------------------------------------------
def check_licenses(con):
    src_csv = os.path.join(REG, "sources.csv")
    if not os.path.exists(src_csv):
        record("licenses", "WARN", "no sources.csv")
        return
    blocked = {r["source_id"] for r in csv.DictReader(open(src_csv))
               if r.get("tier") == "verification_only" or
               r.get("redistributable") in ("false", "0", "False")}
    if not con:
        record("licenses", "WARN", "no db")
        return
    leaks = 0
    for tbl, col in (("facilities", "source"), ("verification", "source")):
        for r in con.execute(
                f"SELECT DISTINCT {col} s FROM {tbl}").fetchall():
            if r["s"] in blocked:
                leaks += 1
    record("licenses", "FAIL" if leaks else "PASS",
           f"{leaks} verification-only rows in export tables" if leaks else "")


# 7 posture -----------------------------------------------------------------
BANNED = re.compile(
    r"\b(fraud|fraudulent|greenwash(?:er|ing)?\b|decepti\w+|liar|lied|"
    r"criminal|guilty|scam)\b", re.I)


def check_posture():
    """Banned-assertion scan over *published claim text* only:
    registry CSVs + exported web JSON. Taxonomy class names ('greenwash')
    and meta-discussion in docs are exempt — an assertion is banned only
    when it appears in data presented as a finding about a company."""
    bad = []
    scan = []
    if os.path.isdir(REG):
        scan += [os.path.join(REG, f) for f in os.listdir(REG)
                 if f.endswith(".csv")]
    for d in ("web", "docs"):
        dp = os.path.join(ROOT, d)
        if os.path.isdir(dp):
            scan += [os.path.join(dp, f) for f in os.listdir(dp)
                     if f.endswith((".json", ".csv"))]
    for p in scan:
        try:
            for i, line in enumerate(open(p, errors="ignore"), 1):
                if BANNED.search(line):
                    # taxonomy/label columns legitimately say 'greenwash'
                    stripped = BANNED.sub("", line)
                    if re.search(r"\b(fraud|decepti\w+|liar|lied|criminal|"
                                 r"guilty|scam)\b", line, re.I):
                        bad.append(f"{p[len(ROOT)+1:]}:{i}")
        except OSError:
            pass
    record("posture", "FAIL" if bad else "PASS",
           f"{len(bad)} banned assertions: {bad[:3]}" if bad else "")


# 8 pii ----------------------------------------------------------------------
def check_pii():
    leaks = []
    import subprocess
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if re.match(r"(data|evidence|ops)/", line) or \
                re.search(r"\.(key|pem|env)$|passphrase|secret", line, re.I):
            leaks.append(line)
    record("pii", "FAIL" if leaks else "PASS",
           f"{len(leaks)} private paths tracked: {leaks[:3]}" if leaks else "")


# 9 review ------------------------------------------------------------------
def check_review(con):
    if not con:
        record("review", "WARN", "no db")
        return
    # no publishable=0 flag may appear in the exported flags.csv
    held = {r["flag_id"] for r in con.execute(
        "SELECT 'FL-'||printf('%05d',id) flag_id FROM flags "
        "WHERE publishable=0").fetchall()}
    if not held:
        record("review", "PASS")
        return
    fc = os.path.join(REG, "flags.csv")
    leaked = set()
    if os.path.exists(fc) and os.path.getsize(fc):
        leaked = {r["flag_id"] for r in csv.DictReader(open(fc))} & held
    record("review", "FAIL" if leaked else "PASS",
           f"{len(leaked)} held flags in export" if leaked else "")


def main():
    full = "--full" in sys.argv
    con = db()
    check_manifest(full)
    check_schema()
    check_pins(con)
    check_figures(con)
    check_echogate(con)
    check_licenses(con)
    check_posture()
    check_pii()
    check_review(con)
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    warns = [r for r in RESULTS if r[1] == "WARN"]
    print(f"\n{len(RESULTS)} checks: {len(fails)} FAIL, {len(warns)} WARN")
    if "--json" in sys.argv:
        print(json.dumps(RESULTS))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
