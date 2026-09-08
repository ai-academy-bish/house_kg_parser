# house.kg — Scraper & HuggingFace Dataset Builder

Scrapes **[house.kg](https://www.house.kg)** — the largest real-estate board in
Kyrgyzstan — into a clean, relational HuggingFace dataset: listings, users,
agencies, residential complexes, reviews and photos. Run it on a schedule and it
becomes a **panel**: the same board, re-measured week after week.

* **~25,800 listings** (sale + rent, all 7 property types, all 7 regions of Kyrgyzstan)
* **~230,000 photos**, embedded as a HuggingFace `Image` feature
* Coordinates, prices, views, favourites, post/bump dates, paid promotion,
  ratings and reviews
* **Incremental** — a repeat run costs ~20 minutes, not 3 hours (see below)
* **Resumable** — a crawl killed at 80% restarts at 80%
* Field names in English, values kept in the original language

## Repeat runs: how it stays cheap

The result-page card already carries everything that *moves* — price, views,
favourites, the bump marker, the paid-promotion badges. So a refresh re-measures
the whole board by sweeping ~2,600 result pages, and spends detail requests only
on advertisements it has never seen.

| | Full re-crawl | Incremental run |
|---|---:|---:|
| result pages | 2,600 | 2,600 |
| detail pages | 25,473 | ~2,600 (the ~10% that are new) |
| photos | 227k / 46 GB | ~23k / ~3.8 GB |
| wall clock | 2.5–3 h | **~20–30 min** |

The sweep is also the only way to learn what *disappeared*: nothing on house.kg
announces a sale, so a listing that stops appearing is what "delisted" means.

## Documentation

| Document | For whom |
|---|---|
| **[`docs/house_kg_dataset.md`](docs/house_kg_dataset.md)** | **Anyone using the data.** The single source of truth: every one of the 120 fields, every relation, the real volumes, and every pitfall that will silently corrupt an analysis (sale and rent prices are *not* comparable; reviews are capped at 20; `rooms_n` is empty for land, and that is correct). |
| **[`docs/code_guide.md`](docs/code_guide.md)** | **Anyone maintaining the scraper.** Module by module, class by class: what each does, why it is built that way, and which traps it exists to avoid. Read it before changing anything. |

---

## Quick start

```bash
make setup                    # create the venv (uv) and install dependencies
make parsing_run LIMIT=100    # try a small crawl first
make validate                 # check keys, foreign keys, photos, price semantics
make make_hf_dataset          # build the Parquet subsets
```

Then the baseline run (≈ 2.5–3 hours, ~46 GB of photos):

```bash
make parsing_run              # resumable — Ctrl-C and re-run any time
```

**Then set `dataset.is_first_run: false` in `config.yaml`** and schedule:

```bash
make snapshot                 # crawl + validate + publish one dated snapshot
```

Each run adds a snapshot; nothing already published is rewritten. Run
`make help` for the full command list.

## Requirements

* Python ≥ 3.10
* [`uv`](https://docs.astral.sh/uv/) (installed automatically by `make setup`)
* ~50 GB of free disk for a full crawl with photos

## Commands

| Command | What it does |
|---|---|
| `make help` | Colourised command reference |
| `make setup` | Create `venv/` with `uv` and install dependencies |
| `make login` | Authenticate with HuggingFace (`hf auth login`) — only needed to push |
| `make parsing_run` | Take one snapshot (resumable). `LIMIT=N` for a smaller run |
| `make snapshot` | A scheduled run end to end: crawl → validate → build → push |
| `make full_refresh` | Re-read every detail page too (~10x requests; run monthly) |
| `make pull` | Restore local state from the Hub — tables only, never the photos |
| `make validate` | Integrity checks: keys, foreign keys, photos, prices, time series |
| `make make_hf_dataset` | Build the Parquet subsets, and push if configured |
| `make clean` | Remove `data/`, `hf_dataset/` and `logs/` |

`SNAPSHOT=2026-09-08` labels a run explicitly; the default is today's date.
Re-running the same snapshot id resumes it — observations and changes are keyed,
so a re-run is a no-op rather than a duplicate.

## Configuration

Everything lives in [`config.yaml`](config.yaml) — regions, deals, property types,
worker count, photo options, and where the dataset goes.

```yaml
scope:
  regions:            # comment out any region to skip it
    - chui            # ~92% of the entire board
    - issyk_kul
    # - talas
  max_listings: null  # null = crawl everything

http:
  workers: 10         # 10 is fast and draws no throttling from the site

refresh:
  detail_refresh: new   # "all" re-reads every detail page (~10x requests)
  refresh_entities: true# re-read ~900 agency/complex ratings every run
  min_completeness: 0.9 # refuse to record delistings from a partial sweep

dataset:
  is_first_run: true    # false after the baseline: append instead of rebuild
  hub:
    push: true                    # upload after building
    repo_id: your-name/house-kg
    private: true
```

The HuggingFace token is read from the environment (`HF_TOKEN`) or from
`hf auth login` — **never put it in the YAML.**

`min_completeness` is the one setting worth understanding. A sweep that died half
way looks exactly like half the board being sold overnight, so when a run sees
less than this share of the previous snapshot it records **no** delistings and
flags the snapshot incomplete. A gap is recoverable; a false mass-delisting in a
published time series is not.

## Output

```
data/
  raw/         listings.jsonl, users.jsonl, companies.jsonl, complexes.jsonl,
               reviews.jsonl, photos.jsonl, snapshots.jsonl
    observations/<snapshot>.jsonl    one measurement of every live listing
    changes/<snapshot>.jsonl         one row per field that moved
  photos/      image files (uuid4 names, downloaded once per listing)
hf_dataset/
  data/*.parquet              current-state subsets, rewritten each run
  data/observations/*.parquet appended: one file per snapshot
  data/changes/*.parquet      appended: one file per snapshot
  data/photos/*.parquet       appended: only the newly fetched images
  README.md                   dataset card
logs/          one log file per run (full detail; the console shows a summary)
```

### The tables

Three tables carry the time dimension, and they answer different questions:

| Question | Table |
|---|---|
| What is this advertisement? | `listings` — current state, one row per `house_kg_id` |
| What was its price that week? | `listing_observations` — one row per snapshot |
| What actually happened? | `changes` — one row per field that moved |
| Was that run trustworthy? | `snapshots` — scope, counts, `complete` flag |

```
listings ──┬── author_user_id ──→ users        (private sellers only)
           ├── company_slug   ──→ companies
           └── complex_slug   ──→ complexes
         ←─── house_kg_id ───── listing_observations   (the panel)
         ←─── entity_key ────── changes                (where entity_type='listing')
reviews  ──┬── subject_slug   ──→ companies | complexes
           └── user_id        ──→ users
photos   ───── listing_id     ──→ listings
```

Ratings sit **inline** on `companies`/`complexes` (1:1 — a separate table would be
a join for nothing). Reviews are their own table because they are 1:N and shared:
in a 1,000-listing sample, 590 listings pointed at just 70 agencies.

`listings.id` is a **uuid5 of `house_kg_id`** and `review_id` is a content hash —
both reproducible, so ids survive a re-crawl and joins hold across snapshots.

### Loading the dataset

Each subset is one config holding many Parquet files, so `load_dataset` returns
the whole history concatenated — filter on `snapshot_id` to slice it.

```python
from datasets import load_dataset

ads    = load_dataset("<repo>", "listings",             split="train")
panel  = load_dataset("<repo>", "listing_observations", split="train")
changes= load_dataset("<repo>", "changes",              split="train")
photos = load_dataset("<repo>", "photos",               split="train")
photos[0]["image"]        # a decoded PIL image
```

`photo_index` is the photo metadata *without* the images — join on it when you
want the FKs but not tens of GB of pixels.

### Do I need to download the dataset to update it?

No. `data/raw/*.jsonl` is the source of truth; the Hub is only where the result is
published. `make pull` exists for the one case where that breaks — a machine that
lost its disk (a Vast.ai instance without a mounted volume loses `/workspace` on
recycle). It restores the tables, never the photos: `photo_index` is enough to
tell the crawler which listings already have images.

## Architecture

```
house_kg/
  config.py         typed configuration (dataclasses ← config.yaml)
  constants.py      deals, regions, label map, CSS selectors  ← patch here if the site changes
  http_client.py    thread-safe session pool with retries and back-off
  models.py         records (Listing, User, Entity, Review, Photo,
                    CardObservation, Observation, Change, Snapshot)
  storage.py        append-only JSONL tables + photo store    ← this is what makes it resumable
                    JsonlTable (dedup) · VersionedTable (history) · SnapshotStore
  parsers/          one parser per page type; results.py reads a full observation
                    off a result card, which is what makes refreshes cheap
  crawler/          crawl stages, the differ, and the pipeline that sequences them
  dataset/          HuggingFace packaging (partitioned Parquet, embedded images,
                    schema.py for cross-partition type stability, bootstrap.py)
  logging_utils/    rich console + file logging, multi-track progress bars
  utils/            transliteration, Russian dates, number extraction
```

Every class is designed to be subclassed: swap a `Transliterator`, override a
`Pipeline` stage, or add a parser without touching the rest.

## Development

```bash
make lint     # ruff + mypy
make test     # pytest
```

## License

This project is licensed under the **Apache License 2.0** — see [`LICENSE`](LICENSE).

That licence covers **the scraper: the code, the documentation and the schema.** It does
**not** and cannot cover the *content* it collects. Listing text and photographs on
house.kg belong to whoever posted them; we neither own that content nor relicense it. The
dataset is published as a compilation for research and education, and downstream users are
responsible for their own use of the underlying material.

## Legal

Scrapes only publicly visible pages, at a polite 10 concurrent requests. Intended for
research and education. Respect house.kg's terms of service and applicable law.
