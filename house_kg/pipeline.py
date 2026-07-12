#!/usr/bin/env python3
"""
house.kg pipeline — relational prototype.

Three outputs, linked by stable slugs (no duplicated review blobs):

    listings.json   one row per ad (sale + rent), FK -> company_slug / complex_slug
    companies.json  one row per agency,  with its FULL review list
    complexes.json  one row per ЖК,      with its FULL review list
    foto/           every photo, flat, named with uuid4

Reviews belong to the company/complex, not to the ad — a big agency appears in
hundreds of ads, so storing reviews per-ad would duplicate them hundreds of
times AND stay capped at the 20 the ad page embeds. Crawling each entity once,
from its own profile page, fixes both.

Usage: python pipeline.py [n_listings] [n_workers]
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

N_LISTINGS = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
N_WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 10

OUT = Path(__file__).resolve().parent
_lock = threading.Lock()
_done = {"n": 0}
unmapped = Counter()


def log(m):
    with _lock:
        print(m, flush=True)


def fetch_list_page(deal, type_key, slug, region_id, page):
    r = S.get(f"{S.BASE}/{slug}?region={region_id}&page={page}")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for c in soup.select(".listing"):
        a = c.select_one("p.title a[href], a[href*='/details/']")
        if a and a.get("href"):
            out.append(
                (S.BASE + a["href"].split("?")[0], deal, type_key, S.REGIONS[region_id])
            )
    return out


def collect_urls(target, workers):
    """Fan out over deal x type x region x page until we have `target` urls."""
    jobs = []
    for deal, types in S.DEALS.items():
        for type_key, slug in types.items():
            for region_id in S.REGIONS:
                for page in range(1, 9):
                    jobs.append((deal, type_key, slug, region_id, page))
    random.shuffle(jobs)

    pool, seen = [], set()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fetch_list_page, *j) for j in jobs]
        for f in as_completed(futures):
            for item in f.result():
                if item[0] not in seen:
                    seen.add(item[0])
                    pool.append(item)
            if len(pool) >= target * 1.4:      # enough candidates -> stop early
                for fut in futures:
                    fut.cancel()
                break
    return pool


def parse_one(item, total):
    url, deal, type_key, region_key = item
    rec = S.parse_listing(url, type_key, region_key, deal=deal)
    with _lock:
        _done["n"] += 1
        n = _done["n"]
        if n % 50 == 0 or n == total:
            print(f"  ...{n}/{total} listings", flush=True)
    return rec


def crawl_entities(slugs, kind, url_fn, workers):
    """Crawl each unique company/complex profile exactly once."""
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(S.parse_entity_profile, url_fn(s), kind, s): s for s in slugs
        }
        for f in as_completed(futures):
            rec = f.result()
            if rec:
                out.append(rec)
    return out


def main():
    t_start = time.perf_counter()

    log(f"[1/3] Collecting listing urls (target {N_LISTINGS})...")
    pool = collect_urls(N_LISTINGS, N_WORKERS)
    random.shuffle(pool)
    sample = pool[:N_LISTINGS]
    log(f"      pool={len(pool)}  taking {len(sample)}")

    log(f"[2/3] Parsing listings + photos ({N_WORKERS} threads)...")
    t0 = time.perf_counter()
    listings = []
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        futures = [ex.submit(parse_one, it, len(sample)) for it in sample]
        for f in as_completed(futures):
            rec = f.result()
            if rec:
                listings.append(rec)
    t_listings = time.perf_counter() - t0

    # unique entity slugs -> crawl each ONCE
    comp_slugs = sorted({r["company_slug"] for r in listings if r["company_slug"]})
    cplx_slugs = sorted({r["complex_slug"] for r in listings if r["complex_slug"]})
    log(f"[3/3] Entities: {len(comp_slugs)} companies, {len(cplx_slugs)} complexes "
        f"(from {len(listings)} listings)")

    t0 = time.perf_counter()
    companies = crawl_entities(
        comp_slugs, "company", lambda s: f"{S.BASE}/{s}", N_WORKERS
    )
    complexes = crawl_entities(
        cplx_slugs, "complex", lambda s: f"{S.BASE}/jilie-kompleksy/{s}", N_WORKERS
    )

    # reviews -> their own table (1:N, the unit of analysis, same shape for both
    # entity kinds). Ratings stay INLINE on the entity: a rating is 1:1, so a
    # separate ratings table would be a join for nothing.
    reviews = S.split_reviews(companies, "company") + S.split_reviews(complexes, "complex")
    companies = [S.flatten_entity(c) for c in companies]
    complexes = [S.flatten_entity(c) for c in complexes]

    # users = listing authors UNION review authors — both live in /user/<hash>,
    # so a person who posts ads and also writes reviews is one row, not two.
    ad_authors = {r["author_user_id"] for r in listings if r["author_user_id"]}
    reviewers = {rv["user_id"] for rv in reviews if rv.get("user_id")}
    user_ids = sorted(ad_authors | reviewers)
    log(f"      users: {len(ad_authors)} ad authors + {len(reviewers)} reviewers "
        f"-> {len(user_ids)} unique")

    users = []
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        for u in ex.map(S.parse_user_profile, user_ids):
            if u:
                u["is_ad_author"] = u["user_id"] in ad_authors
                u["is_reviewer"] = u["user_id"] in reviewers
                users.append(u)
    t_entities = time.perf_counter() - t0

    for name, data in [
        ("listings.json", listings),
        ("users.json", users),
        ("companies.json", companies),
        ("complexes.json", complexes),
        ("reviews.json", reviews),
    ]:
        (OUT / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    total = time.perf_counter() - t_start
    photos = sum(len(r["foto_ids"]) for r in listings)
    mb = sum(f.stat().st_size for f in S.FOTO_DIR.iterdir()) / 1e6
    revs = len(reviews)
    # does the profile page truncate reviews like the ad page does (cap 20)?
    trunc = [c for c in companies + complexes
             if c["reviews_count"] and c["reviews_scraped"] < c["reviews_count"]]

    mism = sum(1 for r in listings if r.get("seller_mismatch"))
    log("\n" + "=" * 64)
    log(f"listings   : {len(listings)}  "
        f"(sale {sum(1 for r in listings if r['deal']=='sale')}, "
        f"rent {sum(1 for r in listings if r['deal']=='rent')})")
    log(f"users      : {len(users)}   companies: {len(companies)}   "
        f"complexes: {len(complexes)}")
    log(f"reviews    : {revs} (own table, deduplicated, from entity pages)")
    log(f"declared!=actual seller: {mism}")
    log(f"entities with truncated reviews (count > scraped): {len(trunc)}"
        + (f"  e.g. {trunc[0]['slug']} {trunc[0]['reviews_scraped']}/{trunc[0]['reviews_count']}"
           if trunc else ""))
    log(f"photos     : {photos}  ({mb:.0f} MB)")
    log(f"time       : listings {t_listings:.0f}s + entities {t_entities:.0f}s "
        f"= {total:.0f}s total")
    log(f"rate       : {len(listings)/t_listings*3600:.0f} listings/hour")
    log("=" * 64)


if __name__ == "__main__":
    main()
