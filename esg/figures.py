"""figures.py — numbers-in-pictures: extract metrics that only exist inside
charts, infographics, and image-based tables. The original core ask.

Tier 0 (always): PyMuPDF image blocks + drawn-vector regions on candidate
                 pages -> figure crops (hash-pinned PNG exhibits) -> OCR.
Tier 1 (if docling installed): Docling layout+TableFormer -> structured cells
                 + figure regions on full doc.
Tier 2 (if olmOCR/GPU):  VLM for hard scans (optional, env ESG_VLM=1).

Every crop lands as an exhibit candidate: data/figures/<report_id>/<page>_<n>.png
with sha256 recorded; OCR'd text goes through the same extract queue so
echo-gating applies to the OCR TEXT (the figure crop is the visual exhibit).
"""
import argparse
import json
import os

import fitz

from .esg import db, now, sha256_bytes

FIG_DIR = "data/figures"

def _preload_cudnn():
    """onnxruntime-gpu needs libcudnn.so.9 which lives in the pip
    nvidia-cudnn-cu12 wheel — dlopen it global before ort loads."""
    import ctypes
    import glob as _g
    import site
    cands = []
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        cands += _g.glob(os.path.join(sp, "nvidia/cudnn/lib/libcudnn.so*"))
    cands.append("libcudnn.so.9")
    for p in cands:
        try:
            ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
            return
        except OSError:
            continue


try:
    _preload_cudnn()
    import onnxruntime as _ort
    HAVE_GPU = ("CUDAExecutionProvider" in _ort.get_available_providers()
                and os.environ.get("CUDA_VISIBLE_DEVICES", "0") != "")
except Exception:
    HAVE_GPU = False
try:
    import rapidocr_onnxruntime  # noqa: F401  (optional)
    HAVE_ROCR = True
except Exception:
    HAVE_ROCR = False
try:
    import pytesseract  # noqa: F401
    HAVE_TESS = True
except Exception:
    HAVE_TESS = False


def candidate_pages(con, report_id):
    """Pages already flagged ESG-relevant by pages.py (extract jobs)."""
    return {int(json.loads(r[0]).get("page", -1))
            for r in con.execute(
                "SELECT payload FROM jobs WHERE kind='extract' AND "
                "json_extract(payload,'$.report_id')=?",
                (report_id,)).fetchall()}


def crop_images(report_id, pdf_path, pages=None, max_crops=60):
    """Tier 0: pull embedded raster images off CANDIDATE pages only (was: all
    pages — 6x slower, mostly photos). Returns crop metadata."""
    os.makedirs(f"{FIG_DIR}/{report_id}", exist_ok=True)
    doc = fitz.open(pdf_path)
    out = []
    pnos = pages if pages is not None else range(len(doc))
    for pno in pnos:
        if len(out) >= max_crops:
            break
        try:
            page = doc[pno - 1] if pages is not None else doc[pno]
        except Exception:
            continue
        for idx, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.width < 250 or pix.height < 150:
                    continue  # too small to carry a metric
                if pix.n - pix.alpha > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                path = f"{FIG_DIR}/{report_id}/{pno}_{idx}.png" \
                    if pages is not None else \
                    f"{FIG_DIR}/{report_id}/{pno+1}_{idx}.png"
                pix.save(path)
                with open(path, "rb") as f:
                    sha = sha256_bytes(f.read())
                out.append({"page": pno if pages is not None else pno + 1,
                            "path": path, "sha256": sha,
                            "w": pix.width, "h": pix.height})
            except Exception:
                continue
    return out


_ENGINE = None


def ocr_engine():
    global _ENGINE
    if _ENGINE is None and HAVE_ROCR:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR(det_use_cuda=HAVE_GPU, cls_use_cuda=HAVE_GPU,
                           rec_use_cuda=HAVE_GPU)
    return _ENGINE


def ocr_image(path):
    """OCR a crop -> text. GPU path ~0.2s/crop (20x CPU)."""
    eng = ocr_engine()
    if eng:
        res, _ = eng(path)
        return "\n".join(r[1] for r in res or [])
    if HAVE_TESS:
        from PIL import Image
        return pytesseract.image_to_string(Image.open(path))
    return ""


def process_report(con, rid, path):
    crops = crop_images(rid, path, pages=candidate_pages(con, rid))
    # hash-dedupe: reports reuse the same image across pages; OCR once.
    # Collect writes and flush AFTER the OCR loop — an implicit txn open
    # during slow inference holds the DB writer for minutes and starves
    # every other lane (observed: extract shards parked >60s on locks).
    seen = set()
    n_ocr = 0
    to_insert = []
    for c in crops:
        if c["sha256"] in seen:
            continue
        seen.add(c["sha256"])
        txt = ocr_image(c["path"]) if (HAVE_ROCR or HAVE_TESS) else ""
        if txt.strip():
            n_ocr += 1
            to_insert.append(
                ("extract", "pending",
                 f'{{"report_id":{rid},"page":{c["page"]},"figure_crop":'
                 f'"{c["path"]}","figure_sha256":"{c["sha256"]}","lang":"en",'
                 f'"source_text":""}}'))
            # store OCR text next to the crop for echo-gating
            with open(c["path"] + ".ocr.txt", "w") as f:
                f.write(txt)
    for kind, status, payload in to_insert:
        con.execute(
            "INSERT OR IGNORE INTO jobs(kind,status,payload) VALUES(?,?,?)",
            (kind, status, payload))
    return len(crops), n_ocr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", type=int, default=0)
    a = ap.parse_args()
    con = db()
    q = ("SELECT r.id, r.path FROM reports r WHERE r.sha256 IS NOT NULL"
         " AND EXISTS(SELECT 1 FROM jobs j WHERE j.kind='figures' AND "
         "j.status='pending' AND json_extract(j.payload,'$.report_id')=r.id)")
    if a.report:
        q = f"SELECT id, path FROM reports WHERE id={a.report}"
    elif a.limit:
        q += f" LIMIT {a.limit}"
    tc = to = 0
    for r in con.execute(q).fetchall():
        try:
            c, o = process_report(con, r["id"], r["path"])
            tc += c
            to += o
            con.execute(
                "UPDATE jobs SET status='done' WHERE kind='figures' AND "
                "status='pending' AND json_extract(payload,'$.report_id')=?",
                (r["id"],))
            con.commit()
            print(f"report {r['id']}: {c} crops, {o} with OCR text")
        except Exception as e:
            con.execute(
                "UPDATE jobs SET status='failed',"
                "payload=json_set(payload,'$.err',?) WHERE kind='figures' "
                "AND status='pending' AND "
                "json_extract(payload,'$.report_id')=?",
                (str(e), r["id"]))
            con.commit()
    print(f"figures: {tc} crops, {to} OCR'd")
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('figures',?,?,?,?)",
        (now(), "", 1, tc))
    con.commit()


if __name__ == "__main__":
    main()
