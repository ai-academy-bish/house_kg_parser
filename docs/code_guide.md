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
├── cli.py             entry points (crawl / pull / build / validate)
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
│   ├── url_collector.py    Stage 1 — the sweep; returns observations, not just URLs
│   ├── listing_crawler.py  Stage 2 — detail pages, new listings only
│   ├── entity_crawler.py   Stages 3 & 4
│   ├── differ.py           what counts as a change  ← judgement lives here, alone
│   └── pipeline.py         sequencing + the run report
│
├── dataset/           HuggingFace packaging
│   ├── hf_builder.py  partitioned Parquet, embedded images, incremental push
│   ├── schema.py      explicit Arrow schemas for the appended subsets
│   ├── bootstrap.py   restore local state from the Hub (tables only, no photos)
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

One run = one dated **snapshot**.

```
cli.crawl
  └── Pipeline.run()
        │   snapshot_id = today (or --snapshot-id); mode = baseline | refresh
        │   previous    = the most recent earlier snapshot
        │
        ├── 1. UrlCollector.collect()  →  Sweep
        │      • builds 98 streams (deal × type × region)
        │      • fetches page 1 of each to learn its page count
        │      • fetches every page and parses EVERY CARD IN FULL
        │      → Sweep(cards, pages_scanned, pages_expected, pages_failed)
        │        each card = price, views, favourites, bump, promo   ← the whole point
        │
        ├── 2. Pipeline.record_sweep()
        │      • writes one Observation per live listing
        │      • Differ compares it to the previous snapshot → Change rows
        │      • returns the cards whose listings are new to the dataset
        │
        ├── 3. ListingCrawler.crawl(new only)
        │      • fetch page → ListingParser → download photos (once per listing)
        │      • upserts into listings.jsonl                ← a version, not a duplicate
        │
        ├── 4. Pipeline.record_delistings()
        │      • previously-active minus seen-now = gone
        │      • GUARDED by refresh.min_completeness        ← see below
        │
        ├── 5. EntityCrawler.crawl_entities(refresh=True)
        │      • re-reads ~900 agency/complex profiles every run
        │      • rating movement → Change rows; new reviews → Change rows
        │
        └── 6. EntityCrawler.crawl_users()
               • union of listing authors and review authors, new only by default
```

The run closes by writing a `snapshots` row and logging a `RunReport` — the
new/changed/delisted summary a scheduled job leaves behind.

Then, separately:

```
cli.build  →  HFDatasetBuilder.build()
                • state subsets  → one Parquet each, REWRITTEN (small)
                • observations   → data/observations/obs-<snapshot>.parquet, APPENDED
                • changes        → data/changes/chg-<snapshot>.parquet,      APPENDED
                • photos         → data/photos/photos-<snapshot>-N.parquet,  APPENDED
                                   only for snapshots not already on the Hub
                • writes the dataset card, optionally pushes to the Hub

cli.pull   →  Bootstrapper.run()
                • restores data/raw/ from the Hub — tables only, never the images

cli.validate → Validator.run()
                • keys, foreign keys, photos, price semantics, seller flags,
                  and the time-series invariants
```

### The three things that make a repeat run cheap

1. **The card is a complete observation.** `ResultsParser.parse_card` reads price,
   views, favourites, bump and promo off the result page. Verified against the
   detail page: the counters agree. So re-measuring the board costs ~2,600 fetches,
   not 25,473.
2. **Photos are fetched once.** `ListingCrawler` indexes `house_kg_id -> foto_ids`
   before crawling and carries the existing ids onto the new version of the listing.
   Re-downloading 227,000 images would be by far the most expensive possible bug.
3. **Only what changed is written.** `VersionedTable.upsert` appends a row only when
   a tracked field actually differs, so `listings.jsonl` grows with real edits rather
   than with the number of runs.

### The delisting guard

Nothing on house.kg announces a sale, so "gone from every stream" is the only
delisting signal there is — and a half-finished sweep produces exactly the same
signal for thousands of ads at once. `Pipeline.record_delistings` therefore refuses
to record anything when the sweep saw less than `refresh.min_completeness` of the
previous snapshot, or when `--limit` made the run partial by design. The snapshot is
flagged incomplete instead. **A gap is recoverable; a published false mass-delisting
is not.**

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
| `RefreshConfig` | `detail_refresh`, `refresh_entities`, `refresh_users`, `min_completeness` |
| `StorageConfig` | where `data/` lives → resolves to `ResolvedStorage` |
| `DatasetConfig` | output dir, `include_photos`, `max_shard_size`, `is_first_run`, `hub` |
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
~70 columns while the dataclass declares ~40.

> **`Listing.id` is a uuid5 of `house_kg_id`, and must stay derived.** It was a
> `uuid4()` originally, which is fine for a one-shot crawl and fatal for a repeated
> one: `photos.listing_id` points at it, so a fresh uuid on re-read orphans that
> listing's photos. Anything in this dataset that participates in a join is either
> taken from the site or derived deterministically from something that is.

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
listing authors and reviewers. `Photo` links a file to its listing and carries the
`snapshot_id` that fetched it — which is what lets the builder ship only new shards.

#### The time-series records

| Record | Role |
|---|---|
| `CardObservation` | what one result card yields — the parser's output |
| `Observation` | one row of the panel; `obs_id` is `snapshot_id` + `house_kg_id`, which makes a re-run idempotent |
| `Change` | one row of the event log, long format, shared by all entity types |
| `Snapshot` | run metadata, including the `complete` flag |

`CardObservation` and `Observation` are deliberately separate. The first is what the
site gave us (including fields nothing stores, like the card's title and address);
the second is the published schema. Collapsing them would let a parser change
silently alter the dataset's columns.

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

| Table | Key | Kind |
|---|---|---|
| `listings` | `house_kg_id` | versioned |
| `users` | `user_id` | versioned |
| `companies`, `complexes` | `slug` | versioned |
| `snapshots` | `snapshot_id` | versioned |
| `reviews` | `review_id` | de-duplicating |
| `photos` | `foto_id` | de-duplicating |
| observations (per snapshot) | `obs_id` | de-duplicating |
| changes (per snapshot) | `change_id` | de-duplicating |

> `listings` is keyed on `house_kg_id` — the site's own id — **not** on `id`. That
> holds even though `id` is now a derived uuid5 and would work: `house_kg_id` is what
> the site guarantees, and everything else is downstream of it.

De-duplicating tables hold one row per key and drop later duplicates; versioned
tables hold history and fold to the latest row per key. Reviews and photos are
immutable once seen, so de-duplication is correct for them; a listing's price is
not, so it is not.

#### `PhotoStore`

Flat directory of uuid4-named images. Flat on purpose — the published dataset embeds
the bytes in Parquet, so directory structure would carry no meaning; the FK in the
`photos` table does.

#### `VersionedTable`

`JsonlTable` refuses a key it already holds. That is exactly right while one crawl
resumes, and exactly wrong when the point of the run is to re-read something that
may have changed. `VersionedTable` appends a new **version** instead — but only when
a tracked field actually differs, so the file grows with real edits rather than with
the number of runs.

* `tracked` — the fields whose movement is worth a new row. For listings this is
  `LISTING_CONTENT_FIELDS` (price, title, description, address, geometry, seller…),
  deliberately excluding `views`, `pars_date` and the `*_raw`/`*_date` pairs: all of
  those move on every read without the advertisement having changed at all.
* `upsert(row)` returns `("new" | "changed" | "unchanged", diffs)` — the diffs feed
  straight into `Differ`.
* `current(key)` returns the tracked state of the latest version; `latest()` folds
  the whole file to one row per key, last version wins.
* **Only the tracked fields are held in memory**, not whole rows — indexing 25k
  listings costs megabytes rather than hundreds of them.

> `latest()`, not `rows()`, is what the builder and the validator must use. `rows()`
> now yields history, so a uniqueness check over it would report every edit as a
> duplicate primary key.

#### `SnapshotStore`

Per-snapshot observation and change files: `data/raw/observations/<id>.jsonl` and
`.../changes/<id>.jsonl`. One file per run rather than one growing file, for three
reasons: reading the *previous* snapshot back to diff against touches 25k rows
instead of the whole history; a bad run is discarded by deleting one file; and the
layout is already the partitioning the Hub upload needs.

`previous_of()` and `active_listings()` are what the delisting logic rests on.

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

Two jobs. The old one: `listing_urls()` and `last_page()`.

The new one, and the reason repeat runs are cheap: **`cards()` / `parse_card()` read
a complete observation off the result page.** The card carries price in both
currencies, price per m², the view counter, the favourites counter, the bump marker
and the paid-promotion badges — everything that moves between runs. Verified against
the detail page: the card's view count and `.view-count` agree.

| Method | The trap it avoids |
|---|---|
| `_period` | same rule as the detail parser: **the `deal` decides**, the suffix only separates daily from monthly. Plenty of rent prices render with no suffix at all |
| `_card_dates` | the single date span means *bumped* **or** *posted*, never both — the bump icon is what distinguishes them. Reading it as one field would silently merge two different events |
| `_promo` | promotion kinds are read from the class list (`is-vip`, `is-top`, …) rather than a fixed set of columns, so a paid tier the site adds later still lands in the data. It already caught an `is-up` that was not in the original list |
| `_complex_slug` | only accepts hrefs under `/jilie-kompleksy/`, so a card linking elsewhere cannot inject a bogus FK |

> **What the card does NOT carry**: coordinates, characteristics, description,
> author, photos. Those stay on the detail page, which is why a listing new to the
> dataset still costs a detail fetch.

> **The «Собственник» badge is paid.** It is recorded as `owner_badge`, never as
> `seller_type`: its absence does not mean an agency, only that nobody paid for the
> marker. `seller_type` remains a detail-page inference.

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

#### `url_collector.UrlCollector` — Stage 1 (the sweep)

* `Stream` — one `(deal, type, region)` crawl stream; knows its page URLs.
* `ListingRef` — a URL plus the classification its stream implies. **This is how
  `deal`/`type`/`region` reach the parser without being guessed.**
* `collect()` runs in two phases on one progress track: size every stream (fetch page
  1, read the last-page number), then fetch every page and extract URLs.
* De-duplicates: an ad bumped mid-crawl can shift pages and be served twice.
* Honours `max_listings` by cancelling pending futures once the target is reached.

#### `listing_crawler.ListingCrawler` — Stage 2

* Filters refs against `storage.listings` first — **this is the resume step**, and
  on a repeat run it is also what confines detail fetches to genuinely new ads.
  `force=True` (from `refresh.detail_refresh: all`) re-reads everything instead.
* Per listing: fetch → parse → download photos → **upsert**. `upsert`, not `append`:
  a re-read that differs must become a new version, not be silently dropped.
* **`_known_photos` is the expensive-mistake guard.** Before crawling, the stage
  indexes `house_kg_id -> foto_ids` from `photos.jsonl`. A listing that already has
  images is never re-downloaded, and its existing ids are copied onto the new version
  — otherwise a re-read would blank `foto_ids` for an ad whose price merely moved.
* Exceptions are caught **per listing** and logged: one malformed page must never
  kill a 3-hour crawl.
* The photo progress track has **no total** — a listing's photo count is unknown until
  its page is parsed, so the bar counts up rather than pretending to know.

#### `entity_crawler.EntityCrawler` — Stages 3 & 4

* `crawl_entities(kind, slugs, refresh=...)` — with `refresh=True` (the default from
  the pipeline) it re-reads **every** known profile rather than only new ones. There
  are ~900 agencies and complexes, their ratings and review counts genuinely move,
  and that movement is one of the things the time series exists to capture. Diffs go
  straight to the `changes` table; a review whose content hash is new is a
  `review_added` event.
* `crawl_users(ad_authors, reviewers, refresh=...)` — the opposite case: ~4,500
  profiles that rarely change, so new-only unless `refresh.refresh_users` says
  otherwise.

#### `differ.Differ`

**What counts as a change is a judgement, not a mechanism**, so it lives in one
module rather than being scattered through the stages. Two rules shape it:

* **Monotonic counters are not changes.** `views` and `favourites` only ever grow.
  Comparing them would flag every listing on every run and bury the real signal, so
  they are recorded in the panel and ignored here.
* **A bump is a drop in *age*, not a change in `upped_date`.** This one is subtle and
  it was a real bug. The site renders relative dates; the parser resolves them
  against the moment of reading. An untouched ad showing "2 месяца назад" therefore
  yields an `upped_date` that slides **forward** by exactly the gap between runs —
  so comparing timestamps reports a bump every single run, forever, for every ad
  whose bump is old enough to be rendered coarsely. What is invariant is the age
  (`observed_at − upped_date`), and only a real bump can make an ad younger.
  `BUMP_TOLERANCE` (1 hour) absorbs the site's rounding jitter.

#### `pipeline.Pipeline`

Sequences the stages, owns the snapshot lifecycle, and produces the `RunReport`.
Each stage is a **method**, so a subclass can override one without touching the rest.

* `record_sweep()` writes the observations and the diffs, and returns the cards whose
  listings are new — the only ones stage 3 will spend detail requests on.
* `record_delistings()` is the guarded one; see §3.
* `crawl_entities()` and `crawl_users()` read slugs from **all stored listings**, not
  just this run's — so if a previous run stored listings but died before the entity
  stage, re-running picks them up.
* `RunReport.render()` is the summary a scheduled job leaves behind: new, changed
  (broken down by field), delisted, reappeared, and the entity movement.

> **Reappearance requires a previous snapshot.** When there is none — the baseline
> run, or a re-run of it after an interruption — a known listing is neither new nor
> comparable, and inventing a `reappeared` event for the whole board would be noise.
> `record_sweep()` skips the diff entirely in that case.

---

### 4.9 `dataset/`

#### `hf_builder.HFDatasetBuilder`

`build()` writes the current-state subsets in full, appends one partition per new
snapshot, then the card, then optionally pushes.

**A fixed set of subsets, a growing set of files.** `STATE_SUBSETS` are rewritten
every run (they are small, and rewriting is what keeps "current state" current);
`observations`, `changes` and `photos` only ever gain files, one per snapshot.

> **Why not a dated config per run?** `listings_2026_09_08` and friends would give a
> year 50-odd HuggingFace configs, force `load_dataset` to be called once per week
> and concatenated by hand, and make the dataset viewer useless. One config holding
> many files reads back as a single table with a `snapshot_id` column.

`published_snapshots()` asks **the Hub** which partitions already exist rather than
trusting a local marker file. That is what makes the build safe on a rebuilt machine:
the repository is the record of what exists. The file listing is fetched once and
cached per build.

`_lifecycle()` derives each listing's `last_seen` / `is_active` / `delisted_snapshot`
/ `observations_n` from the observation history. Deliberately *not* stored in
`listings.jsonl`: writing `last_seen` back every run would append 25,000 rows a week
to a file that should only grow when an ad is actually edited. The panel already
holds the answer, so the answer is computed from the panel.

`_align()` fills missing keys with `None`: listing characteristics are sparse (a land
plot has no `floor`), and Arrow needs a single schema across all rows.

#### `schema.py` — the bug that only appears on the second run

Parquet type inference is **per file**. A column that happens to be entirely null in
one snapshot's partition is written as Arrow `null`, which then refuses to
concatenate with a later partition where the same column holds strings —
`load_dataset` fails for every user, and only from the second run onwards.

`features_for(record)` derives an explicit `Features` schema from the record
dataclass, so the appended subsets are written with a pinned schema. Deriving it from
the dataclass rather than writing it out twice keeps the published contract and the
record definition from drifting apart.

#### `bootstrap.Bootstrapper`

Restores `data/raw/` from the published dataset. **Normally never needed** —
`data/raw/*.jsonl` is the source of truth and the Hub is only the publication target.
It exists for the machine that loses its disk: a Vast.ai instance without a mounted
volume loses `/workspace` on recycle, and a scheduled crawler that lost its state
would otherwise re-download the entire board and republish it as brand new.

Only the tabular subsets are pulled (a few tens of MB). `ALLOW_PATTERNS` deliberately
excludes `data/photos/` — `photo_index` carries the same rows without the bytes,
which is all the crawler needs to know an ad's images were already collected.

`DERIVED_LISTING_COLUMNS` are stripped on the way back in: they are recomputed by
`_lifecycle()` and must not be written into the crawler's source of truth. Note the
strip applies to `listings` only — the observations own `is_active` outright.

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
| `listing.id` is derived from `house_kg_id` | a random uuid orphaning photos on re-crawl |
| every settled observation resolves to a listing | a sweep that recorded ads whose detail fetch never succeeded |
| no listing is delisted twice | drift in the active-set logic |
| every observation partition has a `snapshots` row | a partition published without its provenance |

Add a check whenever you fix a data bug. That is what keeps it fixed.

**Use `latest()`, not `rows()`.** The versioned tables now keep history, so a
uniqueness check over `rows()` would report every legitimate edit as a duplicate
primary key. This is the one trap when adding a check.

The orphan check is deliberately split. An observation with no listing row is
**normal in the newest snapshot** — the card was read, the detail fetch failed, and
the next run retries it. In an older snapshot it never healed, so it is a real
defect. Only the settled case fails the run.

---

### 4.12 `cli.py`

`argparse`, four subcommands: `crawl`, `pull`, `build`, `validate`.

**`--config` is a top-level flag and must precede the subcommand:**

```bash
python -m house_kg.cli --config config.yaml crawl --limit 100
python -m house_kg.cli --config config.yaml crawl --snapshot-id 2026-09-08 --full
python -m house_kg.cli --config config.yaml pull --force
```

CLI flags override the YAML for that run only: `--limit`, `--workers`,
`--no-photos`, `--no-progress`, `--snapshot-id`, `--full` (= `detail_refresh: all`),
`--first-run`, `--no-push`.

**`crawl` exits 2 when the snapshot is incomplete.** A scheduled run is judged by
whether the sweep was complete, not by whether the process survived — an incomplete
snapshot must not pass silently in CI.

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

> **A new field does not backfill itself.** `VersionedTable.upsert` only writes a new
> version when a *tracked* field moved, so listings already stored keep their old
> shape until they change for some other reason — even under `--full`. That is the
> versioning working as intended, not a bug. If you need the field populated
> everywhere, add it to `LISTING_CONTENT_FIELDS` for one full refresh and then take
> it back out, or accept that it fills in gradually. Fields on the *panel* have no
> such problem: every snapshot writes every observation fresh.

### Adding a new table

1. A dataclass in `models.py`.
2. A `JsonlTable` in `Storage.__init__`, with the right key column.
3. A crawl stage (or extend `EntityCrawler`).
4. Add its name to `TABLE_SUBSETS` in `hf_builder.py`.
5. A description in `card.DESCRIPTIONS`.
6. Foreign-key checks in `validate.py`.

### Running it on a schedule

The baseline run needs `dataset.is_first_run: true`; **set it to false afterwards**,
or every run rebuilds the repository from scratch instead of appending. The crawler
warns loudly at the end of a baseline run for exactly this reason.

```bash
make snapshot                  # crawl + validate + build & push, labelled with today's date
make snapshot SNAPSHOT=2026-09-08
make full_refresh              # re-read every detail page too; worth doing monthly
```

`cli.crawl` exits **2** when the snapshot is incomplete, so a scheduler can tell a
partial run from a clean one without parsing logs.

Re-running the same `--snapshot-id` resumes it: observations and change rows are
keyed, so a re-run is a no-op rather than a duplicate.

### Tracking a new field over time

1. Parse it into `CardObservation` (if it is on the card) or `Listing` (if it is not).
2. Add it to `Observation` — `schema.py` derives the Arrow type from the dataclass,
   so nothing else is needed for the published schema.
3. Only if its movement is *meaningful*, add it to `TRACKED_LISTING_FIELDS`. Ask
   first whether it is monotonic: a counter that only grows will mark every listing
   changed on every run.

### Changing what counts as a change

Everything is in `differ.py` and two tuples in `constants.py`:
`TRACKED_LISTING_FIELDS` (price, period, promo) and `TRACKED_ENTITY_FIELDS` (name,
rating, review counts, the histogram). Before adding a field, check it against the
two rules in `differ.py`'s docstring — most candidate fields fail one of them.

### The machine lost `data/`

```bash
make pull      # restores the tables from the Hub; add FORCE=1 to overwrite
```

Photos are not restored and do not need to be: they are already on the Hub, and
`photo_index` tells the crawler which listings not to re-fetch. Do **not** work
around a missing `data/` by setting `is_first_run: true` — that would republish the
whole board as a new baseline and orphan the existing history.

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
| **Resume re-downloads everything** | The key column is wrong or missing. `listings` keys on `house_kg_id`, never on `id`. |
| **`validate` reports unresolved FKs** | The crawl was interrupted between the listing and entity stages. Just re-run `make parsing_run` — it resumes and fills them in. |
| **Photos re-downloaded on a repeat run** | `_known_photos` came back empty — check `photos.jsonl` is present and `photos.enabled` is true. This is the single most expensive bug available here. |
| **Every listing reports a bump every run** | Someone compared `upped_date` timestamps instead of ages. See `Differ._bump`. |
| **Every listing reports "changed" every run** | A monotonic counter (`views`, `favourites`) was added to `TRACKED_LISTING_FIELDS`. |
| **`load_dataset` fails only from the 2nd snapshot** | A partitioned subset was written without an explicit schema; an all-null column was typed `null` and will not concatenate. See `schema.py`. |
| **A run reports thousands of delistings** | The sweep was partial. `min_completeness` should have caught it — check `snapshots.complete` and the sweep's `pages_failed` before believing the number. |
| **The whole board looks new again** | `is_first_run` was left `true`, or `data/raw/` was lost and not restored with `make pull`. |
| **`validate` fails on duplicate primary keys** | Something iterated a versioned table with `rows()` instead of `latest()`. |

| **A column you just added is "missing" from `load_dataset`** | Not the dataset — the `datasets` cache. A local folder is cached by path and config name, so a rebuild at the same path can be served from the previous schema. Re-check with `HF_DATASETS_CACHE=$(mktemp -d)` before believing it. |

**Where to look:** `logs/<run>_<timestamp>.log` always holds `DEBUG`, including every
retry, every 404 and every give-up, even when the console only showed `INFO`.

**Reading the `snapshots` table is the fastest triage there is.** `complete`,
`pages_scanned`, `listings_seen` and the new/changed/delisted counts for every run
the machine has ever done — a bad run usually announces itself there before you open
a log.
