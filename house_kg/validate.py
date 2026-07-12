#!/usr/bin/env python3
"""Integrity checks for the relational output: FKs resolve, no duplicates, no orphans."""
import json
import os
from collections import Counter
from pathlib import Path

D = Path(__file__).resolve().parent
listings = json.loads((D / "listings.json").read_text(encoding="utf-8"))
users = json.loads((D / "users.json").read_text(encoding="utf-8"))
companies = json.loads((D / "companies.json").read_text(encoding="utf-8"))
complexes = json.loads((D / "complexes.json").read_text(encoding="utf-8"))
foto = {f.split(".")[0] for f in os.listdir(D / "foto")}

ok = True


def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"  {'[OK]  ' if cond else '[FAIL]'} {label} {detail}")


print(f"listings={len(listings)}  companies={len(companies)}  complexes={len(complexes)}\n")

print("KEYS / DUPLICATES")
check("listing uuid unique", len({r["id"] for r in listings}) == len(listings))
check("listing house_kg_id unique",
      len({r["house_kg_id"] for r in listings}) == len(listings),
      f"({len({r['house_kg_id'] for r in listings})} uniq)")
check("company slug unique (PK)", len({c["slug"] for c in companies}) == len(companies))
check("complex slug unique (PK)", len({c["slug"] for c in complexes}) == len(complexes))

check("user_id unique (PK)", len({u["user_id"] for u in users}) == len(users))

print("\nFOREIGN KEYS")
cs, xs = {c["slug"] for c in companies}, {c["slug"] for c in complexes}
us = {u["user_id"] for u in users}
missing_c = [r["company_slug"] for r in listings if r["company_slug"] and r["company_slug"] not in cs]
missing_x = [r["complex_slug"] for r in listings if r["complex_slug"] and r["complex_slug"] not in xs]
missing_u = [r["author_user_id"] for r in listings if r["author_user_id"] and r["author_user_id"] not in us]
check("every company_slug resolves", not missing_c, f"missing={set(missing_c)}")
check("every complex_slug resolves", not missing_x, f"missing={set(missing_x)}")
check("every author_user_id resolves", not missing_u, f"missing={len(set(missing_u))}")
used_c = {r["company_slug"] for r in listings if r["company_slug"]}
check("no orphan companies", used_c == cs, f"orphans={cs - used_c}")

# every reviewer must resolve into users too (shared /user/ namespace)
reviewers = {rv["user_id"] for e in companies + complexes for rv in e["reviews"] if rv.get("user_id")}
check("every reviewer resolves", reviewers <= us, f"missing={len(reviewers - us)}")

print("\nSELLER: DECLARED vs ACTUAL")
ct = Counter((r.get("offer_type"), r["seller_type"]) for r in listings)
for (dec, act), n in ct.most_common():
    flag = "  <-- mismatch" if (dec and ("собственник" in dec) != (act == "owner")) else ""
    print(f"  {str(dec):18} -> {act:8} {n:5}{flag}")
check("seller_mismatch flag matches cross-tab",
      sum(1 for r in listings if r.get("seller_mismatch")) ==
      sum(n for (dec, act), n in ct.items()
          if dec and ("собственник" in dec) != (act == "owner")))

print("\nPHOTOS")
ids = [f for r in listings for f in r["foto_ids"]]
check("every foto_id has a file", all(i in foto for i in ids), f"({len(ids)} refs)")
check("no duplicate foto_id", len(ids) == len(set(ids)))

print("\nPRICE NORMALIZATION")
per = Counter(r["price_period"] for r in listings)
check("sale is always total",
      all(r["price_period"] == "total" for r in listings if r["deal"] == "sale"))
check("rent never 'total'",
      all(r["price_period"] != "total" for r in listings if r["deal"] == "rent"
          and r["price_usd"] is not None),
      f"periods={dict(per)}")
check("price_usd numeric where raw present",
      all(r["price_usd"] is not None for r in listings if r["price_usd_raw"]))

print("\nDEDUP WIN")
n_links = sum(1 for r in listings if r["company_slug"])
revs = sum(len(c["reviews"]) for c in companies + complexes)
print(f"  {n_links} listings -> {len(companies)} companies "
      f"({n_links / max(len(companies),1):.1f}x dedup)")
print(f"  {revs} reviews stored once (per-listing storage would repeat them)")
print(f"  sellers: {dict(Counter(r['seller_type'] for r in listings))}")

print("\nCOVERAGE")
for f in ["latitude", "views", "posted_date", "upped_date", "price_usd",
          "rooms_n", "area_m2", "author_user_id", "company_slug"]:
    n = sum(1 for r in listings if r.get(f) is not None)
    print(f"  {f:12} {n:5}/{len(listings)}  ({n/len(listings)*100:.0f}%)")

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
