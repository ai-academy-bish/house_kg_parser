"""Dataset card (README.md) for the HuggingFace repository."""

from __future__ import annotations


def build_card(counts: dict[str, int], subsets: list[str]) -> str:
    """Render the YAML front-matter (subset configs) plus a usage guide."""
    configs = "".join(
        "  - config_name: {name}\n"
        "    data_files:\n"
        "      - split: train\n"
        "        path: data/{pattern}\n".format(
            name=name,
            pattern=f"{name}-*.parquet" if name == "photos" else f"{name}.parquet",
        )
        for name in subsets
    )

    rows = "\n".join(
        f"| `{name}` | {counts.get(name, 0):,} | {DESCRIPTIONS[name]} |" for name in subsets
    )

    return f"""---
language:
  - ru
  - ky
license: other
task_categories:
  - tabular-regression
  - image-classification
tags:
  - real-estate
  - kyrgyzstan
  - house.kg
configs:
{configs}---

# house.kg — Kyrgyzstan Real Estate

Sale and rental listings scraped from [house.kg](https://www.house.kg), the largest
real-estate board in Kyrgyzstan. **Field names are English; values are kept in the
original language (Russian), exactly as the site renders them.**

## Subsets

| subset | rows | description |
|---|---:|---|
{rows}

## Relations

```
listings.author_user_id  -> users.user_id          (private sellers only)
listings.company_slug    -> companies.slug
listings.complex_slug    -> complexes.slug
reviews.subject_slug     -> companies.slug | complexes.slug   (per subject_type)
reviews.user_id          -> users.user_id
photos.listing_id        -> listings.id
```

Ratings live **inline** on `companies` / `complexes` (a rating is 1:1 with its
entity, so a separate `ratings` table would be a join for nothing). The star
histogram is flattened into `rating_5` … `rating_1`.

`review_id` is a **deterministic hash**, not a uuid4 — re-scraping reproduces the
same ids, so dataset versions can be diffed and joined.

## Usage

```python
from datasets import load_dataset

ads    = load_dataset("<repo>", "listings", split="train")
photos = load_dataset("<repo>", "photos",   split="train")   # decoded PIL images
photos[0]["image"]
```

## Read before you analyse

* **Prices are not comparable across deals.** A sale price is a total; a rent price
  is a rate. Always filter on `price_period` (`total` / `month` / `day`) — otherwise
  a $198,000 sale gets averaged with a $1,200/month rent.
* **`offer_type` vs `seller_type`.** The first is what the seller *claims*
  ("от собственника" / "от агента"); the second is what their account actually is.
  Disagreements are flagged by `seller_mismatch` (~4% of ads).
* **The board is Bishkek-centric:** ~92% of listings are in Chui/Bishkek. Statistics
  for small regions (Talas, Naryn, Batken) rest on very few rows.
* **Reviews are capped at 20 per entity** by the site itself — see `reviews_count`
  (what the site claims) vs `reviews_scraped` (what exists here) and the
  `reviews_truncated` flag.
* **Relative dates are resolved.** The site shows "2 месяца назад"; both the raw
  string (`*_raw`) and an absolute ISO date (`*_date`) are stored.
* `rooms_n` is empty for land, parking and commercial — those have no rooms.

The full field-by-field guide, including how each value is extracted and every
pitfall in the source, is in `DATASET_GUIDE.md`.
"""


DESCRIPTIONS: dict[str, str] = {
    "listings": "one row per advertisement (sale + rent, all property types and regions)",
    "users": "people: listing authors ∪ review authors (shared `/user/` namespace)",
    "companies": "agencies / business accounts, rating inline",
    "complexes": "residential complexes (ЖК), rating inline",
    "reviews": "reviews of companies and complexes",
    "photos": "listing photos, embedded as a HF `Image` feature",
}
