#!/usr/bin/env python3
"""tools/grid/manifest.py — build the signed static work manifest browsers
self-assign from. Replaces the /lease endpoint: units = pending extract jobs
(page text, capped), published to web/grid-manifest.json + signed with the
repo key so clients can verify provenance offline.

Signing: ed25519 via `openssl pkeyutl` if ESG_SIGN_KEY is set; otherwise the
manifest ships with sha256 checksums only (clients verify hashes, not sigs).
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DB = os.path.join(ROOT, "data", "esg.db")
OUT = os.path.join(ROOT, "web", "grid-manifest.json")
MAX_UNITS = 2000
MAX_TEXT = 12000


def build():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT j.id, j.payload FROM jobs j
           WHERE j.kind='extract' AND j.status='pending'
           ORDER BY j.id DESC LIMIT ?""", (MAX_UNITS,)).fetchall()
    units = []
    for r in rows:
        p = json.loads(r["payload"])
        if p.get("figure_crop"):
            continue  # OCR'd figure jobs need the crop file — keep server-side
        src = os.path.join(ROOT, "data", "pages",
                           str(p["report_id"]), f"{p['page']}.txt")
        if not os.path.exists(src):
            continue
        text = open(src, errors="ignore").read()[:MAX_TEXT]
        # publish the unit text where browsers can fetch it
        dst = os.path.join(ROOT, "web", "pages",
                           str(p["report_id"]), f"{p['page']}.txt")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w") as f:
            f.write(text)
        th = hashlib.sha256(text.encode()).hexdigest()
        units.append({
            "unit_id": f"pg-{p['report_id']}-{p['page']}",
            "job_id": r["id"], "report_id": p["report_id"],
            "page": p["page"], "content_hash": th,
            "text_url": f"/pages/{p['report_id']}/{p['page']}.txt",
        })
    manifest = {
        "version": 1,
        "generated": subprocess.check_output(
            ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"]).decode().strip(),
        "units": units,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    body = json.dumps(manifest, separators=(",", ":")).encode()
    manifest["sha256"] = hashlib.sha256(body).hexdigest()
    key = os.environ.get("ESG_SIGN_KEY")
    if key and os.path.exists(key):
        sig = subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", key],
            input=hashlib.sha256(body).digest(),
            capture_output=True)
        if sig.returncode == 0:
            manifest["signature_hex"] = sig.stdout.hex()
            manifest["sig_alg"] = "ed25519"
    with open(OUT, "w") as f:
        json.dump(manifest, f)
    print(f"manifest: {len(units)} units -> {OUT} "
          f"({'signed' if 'signature_hex' in manifest else 'unsigned'})")


if __name__ == "__main__":
    build()
