"""catalog.py — Scrapy spiders: index responsibilityreports company pages,
then crawl each (priority-ordered) for report PDF links.

Modes:
  python -m esg.catalog index            # /Companies -> slug_map + name match
  python -m esg.catalog crawl [--limit N]# company pages -> reports rows

Scrapy replaces the v1 throttle/cooldown machinery: AUTOTHROTTLE per-domain
pacing; hard blocks (403/429/503) trigger backoff via retry middleware.
For multi-lane proxy runs, launch one process per lane with ESG_PROXY set —
per-process pacing == per-exit pacing (kills the cross-process burst bug).
"""
import argparse
import json
import os
import re
import urllib.request

import scrapy
from rapidfuzz import fuzz
from scrapy.crawler import CrawlerProcess

from .esg import db, now

BASE = "https://www.responsibilityreports.com"
PDF_RE = re.compile(r"HostedData/[^\"']+?(\d{4})\.pdf", re.I)
UA = {"User-Agent": "esg-verdicts/1.0 research crawler"}


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


_LEGAL = re.compile(
    r"\b(corporation|corp|incorporated|inc|company|co|limited|ltd|plc|llc|"
    r"holdings|holding|group|grou?p|sa|ag|nv|se|oyj|ab|as|spa|srl|gmbh|"
    r"class\s+[ab]|cl\s+[ab]|the)\b\.?", re.I)


def _norm(s):
    s = _LEGAL.sub(" ", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


def index(con):
    """Full /Companies index -> slug_map + match to companies by name."""
    os.makedirs("data/source", exist_ok=True)
    html = fetch(BASE + "/Companies")
    with open("data/source/rr_companies.html", "wb") as f:
        f.write(html)
    sel = scrapy.Selector(text=html.decode("utf-8", "ignore"))
    links = sel.css('a[href^="/Company/"]')
    names = {r[0]: r[1] for r in con.execute(
        "SELECT id,name FROM companies").fetchall()}
    norm_map = {_norm(v): k for k, v in names.items()}
    n_new = 0
    for a in links:
        slug = a.attrib["href"].split("/Company/", 1)[1].strip("/")
        disp = a.css("::text").get("").strip()
        cid = None
        if disp:
            n = _norm(disp)
            if n in norm_map:
                cid = norm_map[n]
            else:
                best = max(names.items(),
                           key=lambda kv: fuzz.ratio(_norm(kv[1]), n),
                           default=None)
                if best and fuzz.ratio(_norm(best[1]), n) >= 88:
                    cid = best[0]
        con.execute(
            "INSERT OR REPLACE INTO slug_map VALUES('responsibilityreports',?,?)",
            (slug, cid))
        n_new += 1
    con.commit()
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('responsibilityreports',?,?,?,?)",
        (now(), "", 7, n_new))
    con.commit()
    print(f"index: {n_new} slugs, "
          f"{con.execute('SELECT COUNT(*) FROM slug_map WHERE company_id IS NOT NULL').fetchone()[0]} matched")
    return n_new


class RRSpider(scrapy.Spider):
    name = "rr_company"
    custom_settings = {
        "CONCURRENT_REQUESTS": 8,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.0,
        "AUTOTHROTTLE_MAX_DELAY": 30.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [429, 500, 502, 503, 504],
        "DOWNLOAD_TIMEOUT": 60,
        "USER_AGENT": UA["User-Agent"],
        "ROBOTSTXT_OBEY": True,
        "LOG_LEVEL": "INFO",
    }

    async def start(self):
        con = db()
        proxy = os.environ.get("ESG_PROXY")
        rows = con.execute(
            """SELECT s.slug, s.company_id, c.priority FROM slug_map s
               LEFT JOIN companies c ON c.id = s.company_id
               WHERE s.source='responsibilityreports'
               ORDER BY COALESCE(c.priority,0) DESC, s.slug""").fetchall()
        if self.limit:
            rows = rows[: int(self.limit)]
        for r in rows:
            meta = {"slug": r["slug"], "company_id": r["company_id"],
                    "dont_obey_robotstxt": False}
            if proxy:
                meta["proxy"] = proxy
            # higher priority -> earlier (scrapy priority: higher first)
            yield scrapy.Request(
                f"{BASE}/Company/{r['slug']}", meta=meta,
                priority=int(r["priority"] or 0) + 1,
                callback=self.parse)

    def parse(self, response):
        con = db()
        slug = response.meta["slug"]
        cid = response.meta["company_id"]
        found = 0
        for m in PDF_RE.finditer(response.text):
            url = BASE + "/" + m.group(0)
            year = int(m.group(1))
            con.execute(
                """INSERT OR IGNORE INTO reports
                   (company_id, year, kind, url, acquisition_method)
                   VALUES(?,?,?,?,?)""",
                (cid, year, "sustainability_report", url, "scrape"))
            found += 1
        # also record that we crawled this page
        con.execute(
            "INSERT OR REPLACE INTO jobs(kind,status,payload) VALUES(?,?,?)",
            ("catalog", "done", json.dumps({"slug": slug, "found": found})))
        con.commit()
        yield {"slug": slug, "company_id": cid, "reports": found}


def crawl(limit=0):
    p = CrawlerProcess()
    p.crawl(RRSpider, limit=limit or 0)
    p.start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="index",
                    choices=["index", "crawl"])
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if a.mode == "index":
        index(db())
    else:
        crawl(a.limit)


if __name__ == "__main__":
    main()
