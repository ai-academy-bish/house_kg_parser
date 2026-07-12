# Code Guide — for maintainers

This document explains **how the scraper is built and why**, module by module and
class by class. Read it before changing anything.

The companion document, [`house_kg_dataset.md`](house_kg_dataset.md), describes the
*data*. This one describes the *code that produces it*.

---

## Table of contents

1. [Design principles](#1-design-principles)
2. [Package layout](#2-package-layout)
3. [Execution flow](#3-execution-flow)
4. [Module reference](#4-module-reference)
   - [`constants.py`](#41-constantspy)
   - [`config.py`](#42-configpy)
   - [`http_client.py`](#43-http_clientpy)
   - [`models.py`](#44-modelspy)
   - [`storage.py`](#45-storagepy)
   - [`utils/`](#46-utils)
   - [`parsers/`](#47-parsers)
   - [`crawler/`](#48-crawler)
   - [`dataset/`](#49-dataset)
   - [`logging_utils/`](#410-logging_utils)
   - [`validate.py`](#411-validatepy)
   - [`cli.py`](#412-clipy)
5. [Common maintenance tasks](#5-common-maintenance-tasks)
6. [Extending the code](#6-extending-the-code)
7. [Failure modes and debugging](#7-failure-modes-and-debugging)

---

## 1. Design principles

Five rules shaped every decision. If you change the code, keep them.

### 1.1 Selectors live in one place

Every CSS selector is a constant in `constants.Selectors`. house.kg **will** change
its markup; when it does, the fix is one file, not a hunt through the parsers.

### 1.2 Never lose data you cannot re-derive

Coverage is not truth. A characteristic whose Russian label is unmapped is
**transliterated**, not dropped. A relative date is kept **both** as the original
string and as a resolved absolute timestamp. A price keeps its raw form next to the
parsed number. If the site says one thing and we compute another (declared vs actual
seller), **both** are stored and the disagreement is flagged — the code never
silently picks a winner.

### 1.3 Resumability is not a feature, it is the architecture

A full crawl is ~26,000 listings, ~230,000 photos, ~46 GB, 3 hours. It *will* be
interrupted. So records are appended to JSONL the instant they are parsed; nothing
accumulates in memory; every table indexes its own keys on start-up and the crawler
skips what it already has.

### 1.4 The crawl stream is the source of truth for classification

`deal`, `type` and `region` are known from the URL we fetched, never inferred from
page text. This is why the crawler walks `(deal × type × region)` streams instead of
using the site's `?region=all` view — which would also drag in Russia and Kazakhstan.

### 1.5 Everything is a class you can subclass

Parsers take collaborators (a `Transliterator`, a `RussianDateParser`), stages are
methods on `Pipeline`, and configuration is dataclasses. Specialising the crawler
should never require editing it.

---

## 2. Package layout

```
house_kg/
├── constants.py       domain constants + ALL CSS selectors   ← patch here when the site changes
├── config.py          typed configuration (dataclasses ← config.yaml)
├── http_client.py     thread-safe session pool, retries, back-off
├── models.py          the dataset records
├── storage.py         append-only JSONL tables + photo store  ← this is what makes resume work
├── validate.py        integrity checks
├── cli.py             entry points (crawl / build / validate)
│
├── utils/             site-agnostic helpers
│   ├── text.py        Transliterator, clean_text
│   ├── dates.py       RussianDateParser
│   └── numbers.py     to_int, to_float, parse_price
│
├── parsers/           one parser per page type — the only modules that touch HTML
│   ├── base.py        BaseParser
│   ├── results.py     ResultsParser        (/kupit-*?page=N)
│   ├── listing.py     ListingParser        (/details/<id>)
│   ├── entity.py      EntityParser, RatingParser, ListingRatingsParser
│   └── user.py        UserParser           (/user/<hash>)
│
├── crawler/           stages + orchestration — the only modules that do network I/O
│   ├── url_collector.py    Stage 1
│   ├── listing_crawler.py  Stage 2
│   ├── entity_crawler.py   Stage 3 & 4
│   └── pipeline.py         sequencing
│
├── dataset/           HuggingFace packaging
│   ├── hf_builder.py  Parquet subsets, embedded images, hub push
│   └── card.py        the dataset README
│
└── logging_utils/     rich console + file logging, multi-track progress bars
    ├── logger.py
    └── progress.py
```

**The layering is strict**: `parsers/` never makes a request; `crawler/` never
parses HTML by hand. If you find yourself calling `BeautifulSoup` inside `crawler/`,
you are in the wrong module.

---

## 3. Execution flow

```
cli.crawl
  └── Pipeline.run()
        │
        ├── 1. UrlCollector.collect()
        │      • builds 98 streams (deal × type × region)
        │      • fetches page 1 of each to learn its page count
        │      • fetches every page, extracts /details/ URLs
        │      → list[ListingRef]  (url + deal + type + region)
        │
        ├── 2. ListingCrawler.crawl(refs)
        │      • skips refs already in storage        ← RESUME
        │      • for each: fetch page → ListingParser → download photos
        │      • appends to listings.jsonl + photos.jsonl immediately
        │
        ├── 3. EntityCrawler.crawl_entities()
        │      • reads company_slug / complex_slug from ALL stored listings
        │      • crawls each unique profile ONCE      ← the big win: 590 listings → 70 companies
        │      • appends entity + its reviews (own table, hashed ids)
        │
        └── 4. EntityCrawler.crawl_users()
               • union of listing authors and review authors
               • both live in /user/<hash>, so one table
```

Then, separately:

```
cli.build  →  HFDatasetBuilder.build()
                • JSONL → Parquet, one subset per table
                • photos → embedded Image feature, streamed into ~500 MB shards
                • writes the dataset card, optionally pushes to the Hub

cli.validate → Validator.run()
                • primary keys, foreign keys, photos, price semantics, seller flags
```

---

## 4. Module reference

### 4.1 `constants.py`

Pure data, no logic. **This is the first file to open when the site changes.**

| Name | Purpose |
|---|---|
| `BASE_URL` | `https://www.house.kg` |
| `DEALS` | `{deal: {property_type: url_slug}}` — the 14 stream slugs |
| `REGIONS` | `{1: "chui", ...}` — **Kyrgyzstan only.** Ids 8+ are foreign countries |
| `REGION_IDS_BY_NAME` | the reverse map |
| `LABEL_MAP` | Russian `.info-row` label → English field name |
| `PHOTO_URL_MARKER` | `house.kg/house/images/` — deliberately **subdomain-agnostic** |
| `REVIEW_CAP` | `20` — the site's hard limit, not ours |
| `Selectors` | every CSS selector, grouped by page |

#### `LABEL_MAP` — the two rules

1. **Synonyms must collapse.** Rent pages say `"Кол-во комнат"`, sale pages say
   `"Количество комнат"`. Both map to `rooms`. Miss this and the dataset grows two
   columns for one concept.
2. **Unmapped is not lost.** A label with no entry is transliterated by
   `Transliterator.slugify` and still becomes a column. When you spot a
   machine-generated column name (`kol_vo_etazhey`), that is the signal to add a
   proper mapping here.

#### `Selectors` — why a class

Grouped by page section (result page, listing detail, author, reviews, user). When
house.kg redesigns, you edit this class and nothing else. The parsers import from
it; they never inline a selector string.

---

### 4.2 `config.py`

Typed configuration. Every knob is a dataclass field, so a typo in `config.yaml`
fails loudly at load time rather than silently doing the wrong thing.

| Class | Controls |
|---|---|
| `ScopeConfig` | deals, property types, regions, `max_listings`, `max_pages_per_stream` |
| `HttpConfig` | `workers`, `timeout`, `max_retries`, `delay`, `user_agent` |
| `PhotoConfig` | `enabled`, `workers`, `max_per_listing` |
| `StorageConfig` | where `data/` lives → resolves to `ResolvedStorage` |
| `DatasetConfig` | output dir, `include_photos`, `max_shard_size`, `hub` |
| `HubConfig` | `push`, `repo_id`, `private`, `token_env` |
| `LoggingConfig` | `level`, `progress`, `color` |
| `Config` | the root; `Config.load("config.yaml")` |

**`ScopeConfig.__post_init__` validates the region list against `REGIONS`.** Passing
a foreign region is a hard error, not a silent inclusion — this is the guard that
keeps Kazakhstan out of a "Kyrgyzstan" dataset.

**The HF token is never in the YAML.** `HubConfig.token` reads it from the
environment (`HF_TOKEN`), which is what `hf auth login` populates.

**`Config.paths` creates directories on access** — call it, don't `mkdir` by hand.

---

### 4.3 `http_client.py`

#### `HttpClient`

One class, one job: fetch bytes politely and reliably from many threads.

**The critical detail: `requests.Session` is not thread-safe.** Sharing one session
across a pool corrupts connection state under load, and the symptom is random,
maddening failures. So `HttpClient.session` is a **`threading.local()`** property —
each worker thread lazily builds its own session, with a connection pool sized to
the worker count.

Retry policy:

| Status | Behaviour |
|---|---|
| `200` | return the response |
| `404` | return `None` **immediately** — retrying a 404 only burns politeness budget |
| `429`, `5xx` | back off harder (`3 × attempt` seconds), then retry |
| exception | linear back-off (`1.5 × attempt`), then retry |

After `max_retries` it logs a warning and returns `None`. **Callers must handle
`None`** — every one currently does.

---

### 4.4 `models.py`

The dataset records, as dataclasses. `Record.to_dict()` is what JSONL and Parquet
consume.

#### `Listing`

The big one — ~40 declared fields plus an open-ended `attributes` dict.

`Listing.to_dict()` **flattens `attributes` into the row** with `setdefault`, so a
characteristic can never clobber a core field. This is why the listings table has
~65 columns while the dataclass declares ~40.

#### `Rating` (inline, not a table)

Holds `score`, `count`, `scraped`, `distribution`.

* `count` is **what the site claims**; `scraped` is **what we actually read**. They
  differ for two unrelated reasons, so both are kept:
  * the site renders at most `REVIEW_CAP` (20) reviews and offers no way to load
    more;
  * a user can leave stars with no text, which the count includes but which produces
    no review row.
* `truncated` is `True` **only** when we hit the cap — that is the honest signal, and
  `scraped < count` alone is not.
* `to_columns()` flattens the histogram to `rating_5 … rating_1`, because nested
  dicts are painful to query in Parquet.

**A rating is 1:1 with its entity, so it is NOT a separate table.** A `ratings` table
keyed by `rating_id` would be a join that buys nothing.

#### `Review`

`__post_init__` mints `review_id` via `Review.make_id()` — a **sha1 hash** of
`(subject_type, subject_slug, user_id, date_raw, text[:200])`.

> **Never change this to a uuid4.** house.kg gives reviews no id of their own. A fresh
> uuid on every run would churn the keys, and two dataset versions could no longer be
> diffed or joined. The hash is what makes the dataset reproducible.

If you ever change what goes into the hash, you invalidate every existing
`review_id` — treat it as a breaking schema change.

#### `Entity`, `User`, `Photo`

`Entity` covers both companies and complexes (`kind` disambiguates); `to_dict()`
inlines the rating and leaves reviews to their own table. `User` is the union of
listing authors and reviewers. `Photo` links a file to its listing.

---

### 4.5 `storage.py`

**This module is why the crawler can be killed and resumed.**

#### `JsonlTable`

An append-only JSONL file with a key index.

* `__init__` calls `_load_keys()`, which streams the existing file and indexes the
  key column. **This is the resume mechanism** — the crawler asks `key in table` and
  skips.
* A corrupt final line (from a hard `kill -9` mid-write) is logged and skipped, not
  fatal.
* `append(row)` is **thread-safe** (`threading.Lock`) and de-duplicating: it returns
  `False` if the key is already present. It flushes on every write, so at most the
  in-flight record is lost.
* `rows()` streams rows back for the dataset builder — never loads the file into memory.

Key columns:

| Table | Key |
|---|---|
| `listings` | `house_kg_id` (the site's id — stable across crawls, unlike our uuid4) |
| `users` | `user_id` |
| `companies`, `complexes` | `slug` |
| `reviews` | `review_id` |
| `photos` | `foto_id` |

> `listings` is keyed on `house_kg_id`, **not** on our `id`: our uuid4 is fresh on
> every parse, so it could never detect a duplicate.

#### `PhotoStore`

Flat directory of uuid4-named images. Flat on purpose — the published dataset embeds
the bytes in Parquet, so directory structure would carry no meaning; the FK in the
`photos` table does.

#### `Storage`

Wires the six tables and the photo store to the configured paths. `summary()` returns
row counts, which the pipeline prints before and after a run.

---

### 4.6 `utils/`

Site-agnostic helpers, all pure functions or small dataclasses — trivially testable.

#### `text.Transliterator`

Cyrillic → latin slug (`"Кол-во этажей"` → `kol_vo_etazhey`). A **dataclass with a
`table` field**, so a subclass can swap the scheme (the Kyrgyz letters `ң ө ү` are
already in the default table) without touching the parser.

#### `dates.RussianDateParser`

Resolves `"1 день назад"`, `"сегодня"`, `"5 мая 2025"` to ISO.

> The site only ever shows **relative** dates. Such a value is meaningless once
> detached from the moment it was read, so the parsers store the raw string *and* a
> timestamp resolved against `pars_date`. Months and years are approximated
> (30/365 days) — which is as precise as a `"2 месяца назад"` source can ever be.

#### `numbers`

`to_int`, `to_float`, `parse_price`. `parse_price` takes only the part **before the
`/`**, so a `"/мес."` suffix never leaks into the number, and it tolerates NBSP and
thin-space thousand separators (house.kg uses both).

---

### 4.7 `parsers/`

The only modules that touch HTML. None of them make requests — they take a string of
HTML and return records, which makes them **testable against a saved page**.

#### `base.BaseParser`

Holds the collaborators (`RussianDateParser`, `Transliterator`) and a few helpers
(`soup()`, `now()`, `text_of()`). Subclass this, don't reach around it.

#### `results.ResultsParser`

Reads a result page: `listing_urls()` and `last_page()` (the number behind the
«Последняя» link). Only URLs are taken from result pages — every field comes from the
detail page, which carries far more.

#### `listing.ListingParser` — the core

`parse()` assembles a `Listing` from these pieces. Each private method encodes a rule
that was learned by breaking against the live site:

| Method | The trap it avoids |
|---|---|
| `_attributes` | unmapped labels are **transliterated, not dropped** |
| `_city` | the first address part is the *oblast* for regional ads — take the next part |
| `_coords` | `#map2gis` carries `data-lat` / `data-lon` (note: `lon`, not `lng`) |
| `_prices` | **the period comes from the `deal`, not from the price string** — see below |
| `_activity` | `.added-span` **wraps** `.upped-span`, so the bumped text leaks into the posted text unless split |
| `_seller` | the **author-link shape** decides owner vs company — see below |
| `_rooms` | room count lives in the **title**, not in the characteristics (which are filled ~3% of the time) |
| `photo_urls` | the CDN host rotates, and the size suffix must **not** be stripped |

**`_prices` — why the deal decides the period.** house.kg renders plenty of rent
prices bare: a rent house shows `"$ 2 486"` with no `/мес.` at all. Deriving the
period from the price *string* therefore mislabels those as sales. The deal is known
from the crawl stream and is authoritative; the suffix and `rent_period` only
separate `day` from `month` *within* rent.

**`_seller` — why not the contacts link.** The obvious detector (a
`/business/contact/` link ⇒ company) misses every business account that never
published contacts: 52 of 1,000 were misfiled as private owners. The reliable signal
is the shape of the author link in `#block-user`:

```
/user/<hash>   → a private person   (seller_type = owner,   author_user_id set)
/<slug>        → a business account (seller_type = company, company_slug = slug)
```

The contacts link remains only as a fallback for pages with no author block.

**`photo_urls` — two traps, both of which silently yield zero photos.**

1. The CDN host alternates between `cdn.house.kg` and `bucket.house.kg`, so the
   filter matches `house.kg/house/images/` and pins no subdomain.
2. `data-full` points at `..._1200x900.jpg`. **Stripping the size suffix to get "the
   original" returns a 404** — that URL does not exist. `_1200x900` *is* the largest.

#### `entity.RatingParser` / `EntityParser` / `ListingRatingsParser`

`RatingParser.parse_block()` reads one `.modal-body`: score, star histogram, review
list.

`ListingRatingsParser` exists because a **listing page's modal can carry two
independent ratings**:

```
.modal-body        → the agency  ("рейтинг компании")
.modal-body.alt    → the complex ("рейтинг жилого комплекса")
```

Merging them would be plainly wrong — a bad agency is not a bad building. In practice
the pipeline does **not** use this parser: it reads ratings from the entity profiles
instead, so an agency shared by 500 listings is fetched once rather than 500 times.
The class is kept because it documents the markup and is the right hook if you ever
need per-listing ratings.

`EntityParser` handles the profile pages (`/<slug>` and `/jilie-kompleksy/<slug>`).
`_short_name` truncates the company `<h1>`, which runs on into certification blurb.

#### `user.UserParser`

Reads `/user/<hash>`. Note `registered_raw` is extracted with a **date-shaped regex**,
not by taking everything after `"с"` — the block runs on into UI text
(`"12 января 2023 Написать Пожаловаться"`).

---

### 4.8 `crawler/`

The only modules that do network I/O. All concurrency lives here.

#### `url_collector.UrlCollector` — Stage 1

* `Stream` — one `(deal, type, region)` crawl stream; knows its page URLs.
* `ListingRef` — a URL plus the classification its stream implies. **This is how
  `deal`/`type`/`region` reach the parser without being guessed.**
* `collect()` runs in two phases on one progress track: size every stream (fetch page
  1, read the last-page number), then fetch every page and extract URLs.
* De-duplicates: an ad bumped mid-crawl can shift pages and be served twice.
* Honours `max_listings` by cancelling pending futures once the target is reached.

#### `listing_crawler.ListingCrawler` — Stage 2

* Filters refs against `storage.listings` first — **this is the resume step**.
* Per listing: fetch → parse → download photos → append. Photos are appended to
  `photos.jsonl` as they land, and their ids to `listing.foto_ids`.
* Exceptions are caught **per listing** and logged: one malformed page must never
  kill a 3-hour crawl.
* The photo progress track has **no total** — a listing's photo count is unknown until
  its page is parsed, so the bar counts up rather than pretending to know.

#### `entity_crawler.EntityCrawler` — Stages 3 & 4

* `crawl_entities(kind, slugs)` — crawls each unique company/complex profile **once**,
  writes the entity and splits its reviews into the reviews table.
* `crawl_users(ad_authors, reviewers)` — crawls the **union**; both kinds live in
  `/user/<hash>`, so a person who posts ads *and* writes reviews is one row, flagged
  on both counts.

#### `pipeline.Pipeline`

Sequences the four stages and reports. Each stage is a **method**, so a subclass can
override one without touching the rest.

`crawl_entities()` and `crawl_users()` read slugs from **all stored listings**, not
just this run's — so if a previous run stored listings but died before the entity
stage, re-running picks them up.

---

### 4.9 `dataset/`

#### `hf_builder.HFDatasetBuilder`

`build()` writes one Parquet subset per table, then the photo subset, then the card,
then optionally pushes.

`_align()` fills missing keys with `None`: listing characteristics are sparse (a land
plot has no `floor`), and Arrow needs a single schema across all rows.

**`_build_photos()` — read this before touching it.**

Photos are embedded as a HF `Image` feature (bytes inside Parquet), not shipped as
~230,000 loose files: a repo of that many small files is painfully slow to clone and
load, and embedding is the standard path for large image datasets.

The implementation **streams into byte-sized shards**: rows accumulate until the batch
exceeds `max_shard_size`, then the shard is written and the batch released. Peak
memory is therefore **one shard (~500 MB)** regardless of the total (~46 GB).

> Two earlier approaches failed and must not be reintroduced:
> * `Dataset.from_generator` **pickles the generator**, which closes over the rich
>   `Console` → `TypeError: cannot pickle 'ConsoleThreadLocals'`. It also materialises
>   one huge table.
> * Accumulating all shards in a list before writing defeats the entire point — that
>   is the whole dataset in RAM.

`_push()` uploads each subset as a separate **config** (HF's term for a subset), which
is what lets students call `load_dataset(repo, "listings")`.

#### `card.build_card`

Renders the dataset README: the YAML front-matter declaring the subset configs
(**without it the Hub will not recognise the multi-table layout**) plus the usage
notes and the pitfalls a reader must know before analysing.

---

### 4.10 `logging_utils/`

**There is not a single `print` in the codebase.** Everything goes through logging.

#### `logger.py`

`setup_logging()` wires two sinks, and is idempotent:

* **console** — `RichHandler`, colourised, `INFO` by default;
* **file** — `logs/<run>_<timestamp>.log`, **always `DEBUG`**, with timestamps and
  module names. A 3-hour crawl that dies at hour 2 can still be post-mortemed.

Third-party noise (`urllib3`, `datasets`, …) is pinned to `WARNING` on the console but
still lands in the file.

`CONSOLE` is a module-level `rich.Console` **shared with the progress bars** — this is
what stops a log line from tearing through a live bar.

#### `progress.py`

`ProgressTracker` owns a `rich.Progress` with one task per stage, each with its own
colour and icon (`STYLES`). With `enabled=False` every method is a no-op, so callers
never need an `if`.

`ColouredBarColumn` exists because **`BarColumn(complete_style=...)` wants a real
style, not a format string** — per-task colours must be applied at render time.

`track()` **resets** an existing task (new total *and* new description), because the
`urls` track is reused across two phases and would otherwise keep advertising the
finished one.

---

### 4.11 `validate.py`

`Validator.run()` asserts the invariants the dataset promises. These are not
decorative — **every one of them caught a real bug**:

| Check | The bug it caught |
|---|---|
| `review_id` is a hash, not a uuid4 | ids churning between runs |
| foreign keys resolve | entities referenced but never crawled |
| sale price is always `total` | 39 rent ads mislabelled as sales (bare price strings) |
| `seller_mismatch` agrees with the cross-tab | the business-account misdetection |
| truncation only at the 20-cap | conflating the site's cap with rating-only entries |
| every photo row has a file | half-written photo rows |

Add a check whenever you fix a data bug. That is what keeps it fixed.

---

### 4.12 `cli.py`

`argparse`, three subcommands: `crawl`, `build`, `validate`.

**`--config` is a top-level flag and must precede the subcommand:**

```bash
python -m house_kg.cli --config config.yaml crawl --limit 100
```

CLI flags (`--limit`, `--workers`, `--no-photos`, `--no-progress`) override the YAML
for that run only.

---

## 5. Common maintenance tasks

### The site changed its markup

1. Open the page in a browser, find the new selector.
2. Edit **`constants.Selectors`** — nothing else.
3. Re-run `make parsing_run LIMIT=20` and `make validate`.

If a *whole field* vanished, the parser will silently produce `None`. That is what the
coverage table in the dataset guide is for: compare and you will see the drop.

### The site added a characteristic

Nothing breaks — it arrives as a transliterated column automatically. To give it a
proper name, add one line to **`LABEL_MAP`**. Check first whether it is a synonym of
an existing concept (as `"Кол-во комнат"` is of `"Количество комнат"`); if so, map it
to the **same** key.

### Adding a field to `listings`

1. Add the field to the `Listing` dataclass in `models.py`.
2. Populate it in `ListingParser.parse()`.
3. Document it in `house_kg_dataset.md` §5.
4. Consider a check in `validate.py`.

Old JSONL rows will lack the field; `_align()` in the builder fills them with `None`.

### Adding a new table

1. A dataclass in `models.py`.
2. A `JsonlTable` in `Storage.__init__`, with the right key column.
3. A crawl stage (or extend `EntityCrawler`).
4. Add its name to `TABLE_SUBSETS` in `hf_builder.py`.
5. A description in `card.DESCRIPTIONS`.
6. Foreign-key checks in `validate.py`.

### Re-crawling to build a time series

`house_kg_id` and `review_id` are stable across crawls. Point `storage.root` at a new
directory, crawl again, and diff the two `listings.jsonl` on `house_kg_id` to see
price changes, view growth and bumps.

---

## 6. Extending the code

The classes are built to be subclassed rather than edited.

**A different pipeline order, or an extra stage:**

```python
class MyPipeline(Pipeline):
    def crawl_listings(self, refs):
        super().crawl_listings(refs)
        self.do_something_extra()
```

**A different transliteration scheme:**

```python
parser = ListingParser(translit=Transliterator(table=MY_TABLE))
```

**Scrape a sister site with the same engine:** subclass `BaseParser` with new
selectors and reuse `HttpClient`, `Storage`, `Pipeline` and `HFDatasetBuilder`
unchanged — none of them know anything about house.kg beyond `constants`.

---

## 7. Failure modes and debugging

| Symptom | Likely cause |
|---|---|
| **Zero photos on every listing** | The CDN host changed again, or someone "fixed" the size suffix. Check `PHOTO_URL_MARKER` and confirm `data-full` URLs still end in `_1200x900`. |
| **Every listing has `seller_type: owner`** | `#block-user` markup changed. The author-link shape is the detector; see `_seller`. |
| **Rent prices show `price_period: total`** | Someone re-derived the period from the price string. It must come from `deal`. |
| **`TypeError: cannot pickle …`** | Something passed a closure over the rich `Console` into a pickling boundary (`Dataset.from_generator`, `multiprocessing`). |
| **Random connection errors under load** | A `requests.Session` is being shared across threads. It must come from `HttpClient.session` (thread-local). |
| **Resume re-downloads everything** | The key column is wrong or missing. `listings` keys on `house_kg_id`, never on our uuid4 `id`. |
| **`validate` reports unresolved FKs** | The crawl was interrupted between the listing and entity stages. Just re-run `make parsing_run` — it resumes and fills them in. |

**Where to look:** `logs/<run>_<timestamp>.log` always holds `DEBUG`, including every
retry, every 404 and every give-up, even when the console only showed `INFO`.
