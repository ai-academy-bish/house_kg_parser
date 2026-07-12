#!/usr/bin/env python3
"""
Multithreaded house.kg scraper — benchmark run.

Reuses the parsers from scrape_sample.py, but fans the work out over a thread
pool. The work is pure network I/O, so threads (not processes) are the right
tool: the GIL is released while waiting on sockets.

Usage: python scrape_mt.py [n_listings] [n_workers] [deal] [out.json]
       deal = sale | rent
"""
import json
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

import scrape_sample as S

N_LISTINGS = int(sys.argv[1]) if len(sys.argv) > 1 else 100
N_WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 10
DEAL = sys.argv[3] if len(sys.argv) > 3 else "sale"
OUT_NAME = sys.argv[4] if len(sys.argv) > 4 else f"listings_{DEAL}.json"

OUT_DIR = Path(__file__).resolve().parent
_print_lock = threading.Lock()
_counter = {"done": 0}
unmapped = Counter()  # Russian labels with no English mapping yet


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def fetch_list_page(type_key, slug, region_id, page):
    """Fetch one listing page -> [(url, type, region), ...]"""
    url = f"{S.BASE}/{slug}?region={region_id}&page={page}"
    r = S.get(url)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for c in soup.select(".listing"):
        a = c.select_one("p.title a[href], a[href*='/details/']")
        if a and a.get("href"):
            out.append(
                (S.BASE + a["href"].split("?")[0], type_key, S.REGIONS[region_id])
            )
    return out


def build_pool(workers, types):
    """Fan out over type x region list pages until we have enough candidates."""
    jobs = []
    for type_key, slug in types.items():
        for region_id in S.REGIONS:
            for page in (1, 2, 3):
                jobs.append((type_key, slug, region_id, page))
    random.shuffle(jobs)

    pool, seen = [], set()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fetch_list_page, *j) for j in jobs[: workers * 8]]
        for f in as_completed(futures):
            for item in f.result():
                if item[0] not in seen:
                    seen.add(item[0])
                    pool.append(item)
    return pool


def work(item, total):
    url, type_key, region_key = item
    rec = S.parse_listing(url, type_key, region_key, deal=DEAL)
    with _print_lock:
        _counter["done"] += 1
        n = _counter["done"]
        if rec:
            print(
                f"[{n}/{total}] {type_key}/{region_key} -> "
                f"{len(rec['foto_ids'])} photos, {len(rec)} fields, "
                f"views={rec.get('views')}",
                flush=True,
            )
        else:
            print(f"[{n}/{total}] FAILED {url}", flush=True)
    return rec


def main():
    types = S.DEALS[DEAL]
    log(f"DEAL={DEAL}  building candidate pool with {N_WORKERS} workers...")
    pool = build_pool(N_WORKERS, types)
    log(f"Pool: {len(pool)} listing urls")

    random.shuffle(pool)
    sample = pool[:N_LISTINGS]
    log(f"Parsing {len(sample)} listings, {N_WORKERS} threads (photos included)...\n")

    t0 = time.perf_counter()
    records = []
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        futures = [ex.submit(work, item, len(sample)) for item in sample]
        for f in as_completed(futures):
            rec = f.result()
            if rec:
                records.append(rec)
    elapsed = time.perf_counter() - t0

    (OUT_DIR / OUT_NAME).write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    photos = sum(len(r["foto_ids"]) for r in records)
    mb = sum(f.stat().st_size for f in S.FOTO_DIR.iterdir()) / 1e6
    log("\n" + "=" * 62)
    log(f"deal     : {DEAL}")
    log(f"listings : {len(records)}")
    log(f"photos   : {photos}  ({mb:.1f} MB on disk)")
    log(f"workers  : {N_WORKERS}")
    log(f"TIME     : {elapsed:.1f}s  ({elapsed / max(len(records), 1):.2f}s per listing)")
    log(f"rate     : {len(records) / elapsed * 3600:.0f} listings/hour")
    log(f"saved    : {OUT_NAME}")
    log("=" * 62)


if __name__ == "__main__":
    main()
