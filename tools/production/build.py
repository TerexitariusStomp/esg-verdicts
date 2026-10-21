#!/usr/bin/env python3
"""production/build.py — Bates-style VOL bundles for counsel delivery.
Takes every pack under evidence/<slug>/ -> zips into production/VOL00N.zip
with an index CSV (pack, exhibits, statements, hash)."""
import csv
import json
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
EVD = os.path.join(ROOT, "evidence")
OUT = os.path.join(ROOT, "ops", "production")


def main():
    if not os.path.isdir(EVD):
        print("no evidence/ packs yet"); return
    os.makedirs(OUT, exist_ok=True)
    packs = sorted(d for d in os.listdir(EVD)
                   if os.path.isdir(os.path.join(EVD, d)))
    idx_rows = []
    for i, slug in enumerate(packs, 1):
        vol = f"VOL{i:03d}"
        zp = os.path.join(OUT, f"{vol}.zip")
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(os.path.join(EVD, slug)):
                for fn in files:
                    p = os.path.join(root, fn)
                    z.write(p, os.path.relpath(p, EVD))
        pj = os.path.join(EVD, slug, "pack.json")
        meta = json.load(open(pj)) if os.path.exists(pj) else {}
        idx_rows.append({"vol": vol, "slug": slug,
                         "exhibits": len(meta.get("exhibits", [])),
                         "flags": len(meta.get("flags", [])),
                         "zip": zp})
    with open(os.path.join(OUT, "index.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["vol", "slug", "exhibits",
                                        "flags", "zip"])
        w.writeheader()
        w.writerows(idx_rows)
    print(f"production: {len(idx_rows)} VOL bundles in {OUT}")


if __name__ == "__main__":
    main()
