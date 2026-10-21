"""harvest.py — Scrapy downloader: reports rows -> SHA-256 content-addressed
PDFs in data/raw/. Idempotent (content-addressed + fetched_at check), resumable.

One process per proxy lane via ESG_PROXY; AutoThrottle paces per-domain.
Custody fields (acquired_at, acquisition_method) recorded on every fetch.
"""
import argparse
import os

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.pipelines.files import FilesPipeline

from .esg import db, now, sha256_bytes

RAW = "data/raw"
UA = {"User-Agent": "esg-verdicts/1.0 research crawler"}


class SHA256Store:
    """Write download bytes to data/raw/<sha256>.pdf; return (path, sha)."""

    @staticmethod
    def put(data: bytes):
        if len(data) < 10_000 or data[:5] != b"%PDF-":
            raise ValueError("not a pdf")
        sha = sha256_bytes(data)
        os.makedirs(RAW, exist_ok=True)
        path = f"{RAW}/{sha}.pdf"
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)
        return path, sha


class HarvestSpider(scrapy.Spider):
    name = "esg_harvest"
    custom_settings = {
        "CONCURRENT_REQUESTS": 6,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.5,
        "AUTOTHROTTLE_MAX_DELAY": 30.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [429, 500, 502, 503, 504],
        "DOWNLOAD_TIMEOUT": 180,
        "USER_AGENT": UA["User-Agent"],
        "DEFAULT_REQUEST_HEADERS": {
            "Referer": "https://www.responsibilityreports.com/"},
        "ROBOTSTXT_OBEY": False,   # PDF objects, not crawl paths
        "LOG_LEVEL": "INFO",
        "DOWNLOAD_MAXSIZE": 300 * 1024 * 1024,
    }

    async def start(self):
        con = db()
        proxy = os.environ.get("ESG_PROXY")
        rows = con.execute(
            """SELECT r.id, r.url, r.company_id FROM reports r
               LEFT JOIN companies c ON c.id = r.company_id
               WHERE r.sha256 IS NULL
               ORDER BY COALESCE(c.priority,0) DESC, r.id""").fetchall()
        if self.limit:
            rows = rows[: int(self.limit)]
        for r in rows:
            meta = {"report_id": r["id"]}
            if proxy:
                meta["proxy"] = proxy
            yield scrapy.Request(r["url"], meta=meta, callback=self.saved,
                                 errback=self.failed)

    def saved(self, response):
        con = db()
        rid = response.meta["report_id"]
        try:
            path, sha = SHA256Store.put(response.body)
        except ValueError as e:
            con.execute(
                "INSERT OR REPLACE INTO jobs(kind,status,payload) VALUES(?,?,?)",
                ("harvest", "bad_pdf", f'{{"report_id":{rid},"err":"{e}"}}'))
            con.commit()
            return
        con.execute(
            """UPDATE reports SET sha256=?, path=?, fetched_at=?,
               acquired_at=?, acquisition_method='http_get' WHERE id=?""",
            (sha, path, now(), now(), rid))
        con.commit()
        yield {"report_id": rid, "sha256": sha}

    def failed(self, failure):
        con = db()
        rid = failure.request.meta.get("report_id")
        status = getattr(getattr(failure.value, "response", None), "status", None)
        con.execute(
            "INSERT OR REPLACE INTO jobs(kind,status,payload) VALUES(?,?,?)",
            ("harvest", "failed",
             f'{{"report_id":{rid},"status":{status}}}'.replace("None", "null")))
        con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6,
                    help="concurrency hint (scrapy CONCURRENT_REQUESTS)")
    a = ap.parse_args()
    HarvestSpider.custom_settings["CONCURRENT_REQUESTS"] = a.workers
    p = CrawlerProcess()
    p.crawl(HarvestSpider, limit=a.limit)
    p.start()


if __name__ == "__main__":
    main()
