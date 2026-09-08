"""Dataset card (README.md) for the HuggingFace repository."""

from __future__ import annotations

#: subset -> (path pattern under data/, description)
SUBSETS: dict[str, tuple[str, str]] = {
    "listings": (
        "listings.parquet",
        "one row per advertisement — current state plus its lifecycle",
    ),
    "listing_observations": (
        "observations/*.parquet",
        "the panel: one row per (snapshot × listing), price/views/promotion",
    ),
    "changes": (
        "changes/*.parquet",
        "event log: every field that moved between two snapshots",
    ),
    "snapshots": (
        "snapshots.parquet",
        "one row per crawl run — scope, counts and whether it completed",
    ),
    "users": ("users.parquet", "people: listing authors ∪ review authors"),
    "companies": ("companies.parquet", "agencies / business accounts, rating inline"),
    "complexes": ("complexes.parquet", "residential complexes (ЖК), rating inline"),
    "reviews": ("reviews.parquet", "reviews of companies and complexes"),
    "photo_index": (
        "photo_index.parquet",
        "photo metadata without the images — joinable without downloading them",
    ),
    "photos": ("photos/*.parquet", "listing photos, embedded as a HF `Image` feature"),
}


def build_card(
    counts: dict[str, int], snapshots: list[str], first_run: bool = False
) -> str:
    """Render the YAML front-matter (subset configs) plus a usage guide."""
    present = [name for name in SUBSETS if counts.get(name)]
    configs = "".join(
        f"  - config_name: {name}\n"
        f"    data_files:\n"
        f"      - split: train\n"
        f"        path: data/{SUBSETS[name][0]}\n"
        for name in present
    )
    rows = "\n".join(
        f"| `{name}` | {counts.get(name, 0):,} | {SUBSETS[name][1]} |" for name in present
    )

    span = (
        f"{snapshots[0]} → {snapshots[-1]} ({len(snapshots)} snapshots)"
        if len(snapshots) > 1
        else (snapshots[0] if snapshots else "—")
    )
    note = (
        "This is the **baseline** snapshot; later runs append new partitions."
        if first_run
        else "New snapshots are **appended** — existing files are never rewritten."
    )

    return f"""---
language:
  - ru
  - ky
license: other
task_categories:
  - tabular-regression
  - time-series-forecasting
  - image-classification
tags:
  - real-estate
  - kyrgyzstan
  - house.kg
  - time-series
  - panel-data
configs:
{configs}---

# house.kg — Kyrgyzstan Real Estate, over time

Sale and rental listings scraped from [house.kg](https://www.house.kg), the largest
real-estate board in Kyrgyzstan, re-measured on a schedule. **Field names are
English; values are kept in the original language (Russian), exactly as the site
renders them.**

**Coverage:** {span}. {note}

## Subsets

| subset | rows | description |
|---|---:|---|
{rows}

## How the time dimension is organised

Three tables, three different questions:

| Question | Table |
|---|---|
| What is this advertisement? | `listings` — current state, one row per `house_kg_id` |
| What was its price that week? | `listing_observations` — one row per snapshot |
| What actually happened? | `changes` — one row per field that moved |

`listing_observations` is a **panel**: every live listing is measured on every run.
`changes` is the derived event log — cheaper to scan when you only care about
price cuts, bumps or delistings.

```python
from datasets import load_dataset

panel   = load_dataset("<repo>", "listing_observations", split="train")
changes = load_dataset("<repo>", "changes",             split="train")
ads     = load_dataset("<repo>", "listings",            split="train")

# every price cut, most recent first
cuts = changes.filter(lambda r: r["field"] == "price_usd"
                      and float(r["new_value"]) < float(r["old_value"]))
```

Each subset is one config holding many Parquet files, so `load_dataset` returns
the whole history concatenated — filter on `snapshot_id` to slice it.

## Relations

```
listings.house_kg_id            <- listing_observations.house_kg_id   (panel)
listings.house_kg_id            <- changes.entity_key   (where entity_type='listing')
listings.author_user_id         -> users.user_id        (private sellers only)
listings.company_slug           -> companies.slug
listings.complex_slug           -> complexes.slug
reviews.subject_slug            -> companies.slug | complexes.slug
photo_index.listing_id          -> listings.id      (metadata only, ~10 MB)
photos.listing_id               -> listings.id      (with embedded images)
listing_observations.snapshot_id-> snapshots.snapshot_id
```

`listings.id` is a **uuid5 of `house_kg_id`**, and `review_id` is a content hash —
both are reproducible, so ids are stable across snapshots and joins survive a
re-crawl.

## Read before you analyse

* **Check `snapshots.complete` first.** A run that was interrupted covers only part
  of the board. Delistings are *not* recorded for such a run (see
  `refresh.min_completeness` in the scraper), but its observations are still
  partial — treat an incomplete snapshot as a gap, not as a market movement.
* **A missing row is not a delisting.** A listing is delisted when it has an
  observation with `is_active = false`; after that it simply stops appearing.
* **`views` and `favourites` only ever grow**, so they are recorded in the panel but
  deliberately *not* tracked in `changes` — otherwise every listing would be
  "changed" on every run. `favourites` is null when the site rendered no counter.
* **Prices are not comparable across deals.** A sale price is a total; a rent price
  is a rate. Always filter on `price_period` (`total` / `month` / `day`).
* **A bump is a drop in *age*, not a change in `upped_date`.** The site renders
  relative dates ("2 месяца назад") which are resolved against the moment of
  reading, so an untouched listing's `upped_date` slides forward with the clock.
  Only a real bump makes an advertisement younger, so that is what `changes`
  reports — but the raw `upped_date` in the panel still carries the drift, so
  compare ages there too rather than differencing the timestamps yourself.
* **`offer_type` vs `seller_type`.** The first is what the seller *claims*; the
  second is what their account is. Disagreements are flagged by `seller_mismatch`.
* **Photos are fetched once per listing** and carry the `snapshot_id` that fetched
  them — they are not re-downloaded when a listing changes.
* **The board is Bishkek-centric:** ~92% of listings are in Chui/Bishkek.
* **Reviews are capped at 20 per entity** by the site itself — compare
  `reviews_count` with `reviews_scraped` and the `reviews_truncated` flag.

The full field-by-field guide is in `DATASET_GUIDE.md`.
"""
