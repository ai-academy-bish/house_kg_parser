# house.kg — Scraper & HuggingFace Dataset Builder

Scrapes **[house.kg](https://www.house.kg)** — the largest real-estate board in
Kyrgyzstan — into a clean, relational HuggingFace dataset: listings, users,
agencies, residential complexes, reviews and photos.

* **~25,800 listings** (sale + rent, all 7 property types, all 7 regions of Kyrgyzstan)
* **~230,000 photos**, embedded as a HuggingFace `Image` feature
* Coordinates, prices, views, post/bump dates, ratings and reviews
* **Resumable** — a crawl killed at 80% restarts at 80%
* Field names in English, values kept in the original language

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

Then the full run (≈ 2.5–3 hours, ~46 GB of photos):

```bash
make parsing_run              # resumable — Ctrl-C and re-run any time
```

Run `make help` for the full command list.

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
| `make parsing_run` | Scrape the site (resumable). `LIMIT=N` for a smaller run |
| `make validate` | Integrity checks: primary keys, foreign keys, photos, prices |
| `make make_hf_dataset` | Build the Parquet subsets, and push if configured |
| `make clean` | Remove `data/`, `hf_dataset/` and `logs/` |

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

dataset:
  hub:
    push: true                    # upload after building
    repo_id: your-name/house-kg
    private: true
```

The HuggingFace token is read from the environment (`HF_TOKEN`) or from
`hf auth login` — **never put it in the YAML.**

## Output

```
data/
  raw/         listings.jsonl, users.jsonl, companies.jsonl,
               complexes.jsonl, reviews.jsonl, photos.jsonl
  photos/      image files (uuid4 names)
hf_dataset/
  data/*.parquet    one subset per table + sharded photo subset
  README.md         dataset card
logs/          one log file per run (full detail; the console shows a summary)
```

### Loading the dataset

```python
from datasets import load_dataset

ads    = load_dataset("<repo>", "listings", split="train")
photos = load_dataset("<repo>", "photos",   split="train")
photos[0]["image"]        # a decoded PIL image
```

### The tables

```
listings ──┬── author_user_id ──→ users        (private sellers only)
           ├── company_slug   ──→ companies
           └── complex_slug   ──→ complexes
reviews  ──┬── subject_slug   ──→ companies | complexes
           └── user_id        ──→ users
photos   ───── listing_id     ──→ listings
```

Ratings sit **inline** on `companies`/`complexes` (1:1 — a separate table would be
a join for nothing). Reviews are their own table because they are 1:N and shared:
in a 1,000-listing sample, 590 listings pointed at just 70 agencies.

## Architecture

```
house_kg/
  config.py         typed configuration (dataclasses ← config.yaml)
  constants.py      deals, regions, label map, CSS selectors  ← patch here if the site changes
  http_client.py    thread-safe session pool with retries and back-off
  models.py         dataset records (Listing, User, Entity, Review, Photo)
  storage.py        append-only JSONL tables + photo store    ← this is what makes it resumable
  parsers/          one parser per page type
  crawler/          crawl stages + the pipeline that sequences them
  dataset/          HuggingFace packaging (Parquet subsets, embedded images)
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

## Legal

Scrapes only publicly visible pages, at a polite 10 concurrent requests. Intended
for research and education. Respect house.kg's terms of service and applicable law.
