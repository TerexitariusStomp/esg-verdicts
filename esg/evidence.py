"""evidence.py — per-company evidence pack builder (the paid product).

For a company: EX-### exhibits (hashed PDFs + page/figure renders),
statements.csv, chain-of-custody.md, manifest.json (hash-chained),
REPRODUCE.md — everything counsel needs to authenticate and reproduce.
Output: evidence/<slug>/ (gitignored).
"""
import argparse
import csv
import json
import os
import re

from .esg import db, manifest_append, now, sha256_file

SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def pack_company(con, c):
    slug = slugify(c["name"])
    out_dir = f"evidence/{slug}"
    os.makedirs(out_dir, exist_ok=True)
    mf = f"{out_dir}/manifest.jsonl"

    # exhibits: every hashed report + figure crops
    ex_rows = []
    i = 0
    for r in con.execute(
            "SELECT * FROM reports WHERE company_id=? AND sha256 IS NOT NULL "
            "ORDER BY year DESC", (c["id"],)).fetchall():
        i += 1
        ex = {"exhibit_id": f"EX-{i:03d}", "kind": "report_pdf",
              "sha256": r["sha256"], "source_url": r["url"],
              "acquired_at": r["acquired_at"],
              "method": r["acquisition_method"], "year": r["year"],
              "pages": r["pages"]}
        ex_rows.append(ex)
        if r["path"] and os.path.exists(r["path"]):
            manifest_append(mf, r["path"], "report_pdf", ex)
    # figure crops
    for fr in con.execute(
            """SELECT e.figure_crop, r.company_id FROM extractions e
               JOIN reports r ON r.id=e.report_id
               WHERE r.company_id=? AND e.figure_crop IS NOT NULL""",
            (c["id"],)).fetchall():
        i += 1
        p = fr["figure_crop"]
        if p and os.path.exists(p):
            ex_rows.append({"exhibit_id": f"EX-{i:03d}", "kind": "figure_crop",
                            "sha256": sha256_file(p), "path": p})
            manifest_append(mf, p, "figure_crop", {"exhibit_id": f"EX-{i:03d}"})

    # statements: every extraction (platform_render) + video claim (quoted)
    st_path = f"{out_dir}/statements.csv"
    with open(st_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["statement_id", "speaker_role", "auth_status", "series",
                    "pin", "metric_key", "value", "unit", "date", "snippet"])
        n = 0
        for r in con.execute(
                """SELECT e.*, r.id rid, r.year ryear FROM extractions e
                   JOIN reports r ON r.id=e.report_id
                   WHERE r.company_id=? AND e.snippet IS NOT NULL""",
                (c["id"],)).fetchall():
            n += 1
            w.writerow([f"S-{n:04d}", "official", "platform_render", "R",
                        f"report#{r['rid']}@p{r['page']}", r["metric_key"],
                        r["value"], r["unit"],
                        r["year_covered"] or r["ryear"], r["snippet"]])
        for r in con.execute(
                "SELECT * FROM claims WHERE company_id=?", (c["id"],)).fetchall():
            n += 1
            w.writerow([f"S-{n:04d}", "party_principal", "quoted", "V",
                        f"yt#{r['video_id']}@{int(r['start_s'])}",
                        r["metric_key"], r["value"], r["unit"],
                        r["date"], r["snippet"]])

    # flags + verification
    flags = [dict(r) for r in con.execute(
        "SELECT * FROM flags WHERE company_id=?", (c["id"],)).fetchall()]
    verif = [dict(r) for r in con.execute(
        "SELECT * FROM verification WHERE company_id=?",
        (c["id"],)).fetchall()]
    facilities = [dict(r) for r in con.execute(
        "SELECT * FROM facilities WHERE company_id=?", (c["id"],)).fetchall()]
    litigation = [dict(r) for r in con.execute(
        "SELECT * FROM litigation WHERE company_id=?", (c["id"],)).fetchall()]

    # chain of custody doc
    with open(f"{out_dir}/chain-of-custody.md", "w") as f:
        f.write(f"# Chain of custody — {c['name']}\n\n"
                f"Built {now()} by esg-verdicts evidence.py.\n\n"
                f"| Exhibit | Kind | SHA-256 | Source |\n|---|---|---|---|\n")
        for ex in ex_rows:
            f.write(f"| {ex['exhibit_id']} | {ex['kind']} | "
                    f"`{ex['sha256'][:16]}…` | {ex.get('source_url','local')} |\n")
        f.write("\nEvery SHA-256 verifies against the file at `path` — "
                "see manifest.jsonl for the hash-chained custody record.\n")

    # reproduce doc
    with open(f"{out_dir}/REPRODUCE.md", "w") as f:
        f.write(f"# Reproduce — {c['name']}\n\n"
                f"```bash\n# every number below regenerates from public sources\n"
                f"python -m esg.verify --limit 0   # divergence rows\n"
                f"python -m esg.evidence --slug {slug}\n```\n\n"
                f"- Extractions: {n} statements, every snippet a verbatim "
                f"substring of its source page (echo-gated at insert and "
                f"re-checked by `make verify`).\n"
                f"- Facilities: {len(facilities)} from Climate TRACE v7 "
                f"(CC-BY-4.0) + EPA GHGRP where present.\n"
                f"- Litigation: {len(litigation)} rows from Sabin/CP Radar "
                f"+ CourtListener.\n")

    # machine-readable pack summary
    with open(f"{out_dir}/pack.json", "w") as f:
        json.dump({"company": dict(c), "built": now(),
                   "exhibits": ex_rows, "flags": flags,
                   "verification": verif, "facilities": facilities,
                   "litigation": litigation}, f, indent=1, default=str)

    # SUMMARY.md — the client-facing diligence brief (render to PDF on sale)
    pub_flags = [x for x in flags if x.get("publishable")]
    pub_flags.sort(key=lambda x: SEV_RANK.get(x.get("severity"), 3))
    div = [v for v in verif if v.get("flag")]
    with open(f"{out_dir}/SUMMARY.md", "w") as f:
        f.write(f"# Evidence pack — {c['name']}\n\n"
                f"*Built {now()}. All assertions carry a posture label; "
                f"we document divergence, we do not assert intent.*\n\n")
        f.write("## Flags\n\n")
        if pub_flags:
            f.write("| Severity | Posture | Flag | Basis | Pin |\n"
                    "|---|---|---|---|---|\n")
            for x in pub_flags:
                f.write(f"| {x['severity']} | {x['posture']} | {x['flag']} "
                        f"| {x['basis']} | {x.get('pin') or ''} |\n")
        else:
            f.write("No publishable flags at this time.\n")
        f.write("\n## Divergence detail\n\n")
        if div:
            f.write("| Metric | Claimed | Ground truth | Divergence | "
                    "Source | Year |\n|---|---|---|---|---|---|\n")
            for v in div:
                f.write(f"| {v['metric']} | {v['claimed']} | "
                        f"{v['ground_truth']} | {v['divergence_pct']}% "
                        f"| {v['source']} | {v.get('year') or ''} |\n")
        else:
            f.write("No ground-truth divergence rows at this time.\n")
        f.write(f"\n## Contents\n\n- {len(ex_rows)} exhibits "
                f"(see chain-of-custody.md, each SHA-256 pinned)\n"
                f"- {n} verbatim-sourced statements (statements.csv)\n"
                f"- {len(facilities)} physical facilities attributed\n"
                f"- {len(litigation)} linked litigation matters\n\n"
                f"Custody: manifest.jsonl is hash-chained; reproduce every "
                f"figure per REPRODUCE.md.\n")
    return out_dir, len(ex_rows), n


def index_packs():
    """evidence/INDEX.md — catalog of built packs (the sales funnel surface)."""
    idx = []
    if not os.path.isdir("evidence"):
        return
    for slug in sorted(os.listdir("evidence")):
        p = f"evidence/{slug}/pack.json"
        if os.path.exists(p):
            pk = json.load(open(p))
            n_flags = len([x for x in pk["flags"] if x.get("publishable")])
            idx.append((slug, pk["company"]["name"], len(pk["exhibits"]),
                        n_flags, pk["built"]))
    if not idx:
        return
    with open("evidence/INDEX.md", "w") as f:
        f.write("# Evidence packs\n\n"
                "| Slug | Company | Exhibits | Publishable flags | Built |\n"
                "|---|---|---|---|---|\n")
        for slug, name, nx, nf, built in idx:
            f.write(f"| {slug} | {name} | {nx} | {nf} | {built} |\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="")
    ap.add_argument("--company", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--top", type=int, default=0,
                    help="pack top-N companies with publishable flags, "
                         "severity-ordered (the sales funnel)")
    a = ap.parse_args()
    con = db()
    if a.slug:
        rows = con.execute("SELECT * FROM companies").fetchall()
        c = next((r for r in rows if slugify(r["name"]) == a.slug), None)
        if not c:
            print(f"no company matching slug {a.slug}")
            return
        d, nx, ns = pack_company(con, c)
        print(f"pack: {d} — {nx} exhibits, {ns} statements")
        index_packs()
        return
    q = ("SELECT DISTINCT c.* FROM companies c JOIN flags f "
         "ON f.company_id=c.id ORDER BY c.priority DESC LIMIT 50")
    if a.top:
        # severity-ordered publishable flags drive the funnel order
        q = f"""SELECT c.*, MIN(CASE f.severity WHEN 'high' THEN 0
                                 WHEN 'medium' THEN 1 ELSE 2 END) sev,
                       COUNT(*) nf
                FROM companies c JOIN flags f ON f.company_id=c.id
                WHERE f.publishable=1
                GROUP BY c.id ORDER BY sev, nf DESC, c.priority DESC
                LIMIT {a.top}"""
    if a.company:
        q = f"SELECT * FROM companies WHERE id={a.company}"
    for c in con.execute(q).fetchall():
        d, nx, ns = pack_company(con, c)
        print(f"pack: {d} — {nx} exhibits, {ns} statements")
    index_packs()


if __name__ == "__main__":
    main()
