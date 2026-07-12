# house.kg — Complete Dataset Guide

This is the **single source of truth** for the house.kg dataset: what the source
website is, how every field is obtained, how the tables relate, and — most
importantly — every trap in the data that will silently corrupt an analysis if
you do not know about it.

Every claim here was verified against the live site. Where the obvious approach is
wrong, the document says so and explains why.

---

## Table of contents

1. [The source website](#1-the-source-website)
2. [Volumes: what actually exists](#2-volumes-what-actually-exists)
3. [How the crawler works](#3-how-the-crawler-works)
4. [The data model](#4-the-data-model)
5. [Field reference](#5-field-reference)
6. [Sellers: declared vs actual](#6-sellers-declared-vs-actual)
7. [Ratings and reviews](#7-ratings-and-reviews)
8. [Pitfalls — read this before analysing](#8-pitfalls--read-this-before-analysing)
9. [Known limitations](#9-known-limitations)
10. [Recipes](#10-recipes)

---

## 1. The source website

[house.kg](https://www.house.kg) is the largest real-estate classifieds board in
Kyrgyzstan. It carries both **sales** and **rentals**, and also lists property in
other countries (Russia, Kazakhstan, UAE, ...) — those are **excluded** from this
dataset.

### Deals

| Deal | URL prefix | `deal` value |
|---|---|---|
| Sale | `/kupit-*` | `sale` |
| Rent | `/snyat-*` | `rent` |

### Property types

Seven types, identical for both deals:

| `type` | Sale URL | Rent URL | Russian |
|---|---|---|---|
| `apartment` | `/kupit-kvartiru` | `/snyat-kvartiru` | Квартиры |
| `house` | `/kupit-dom` | `/snyat-dom` | Дома |
| `commercial` | `/kupit-kommercheskaia-nedvijimost` | `/snyat-kommercheskaia-nedvijimost` | Коммерческая |
| `room` | `/kupit-komnatu` | `/snyat-komnatu` | Комнаты |
| `land` | `/kupit-uchastok` | `/snyat-uchastok` | Участки |
| `dacha` | `/kupit-dachu` | `/snyat-dachu` | Дачи |
| `parking_garage` | `/kupit-parking-garaj` | `/snyat-parking-garaj` | Паркинги/гаражи |

### Regions

The site filters by `?region=<id>`. **Kyrgyzstan is ids 1–7 only.** Ids 8 and above
are foreign countries and never enter this dataset.

| id | `region` | Oblast |
|---|---|---|
| 1 | `chui` | Chui oblast / Bishkek |
| 2 | `issyk_kul` | Issyk-Kul |
| 3 | `talas` | Talas |
| 4 | `naryn` | Naryn |
| 5 | `jalal_abad` | Jalal-Abad |
| 6 | `osh` | Osh |
| 7 | `batken` | Batken |
| 8+ | — | ❌ Kazakhstan, Russia, USA, UAE, Uzbekistan, Tajikistan, Turkey … |

Result pages hold exactly **10 listings**; the last page number comes from the
«Последняя» link in `.pagination`.

---

## 2. Volumes: what actually exists

Measured across all 98 (deal × type × region) combinations:

| Deal | Type | Chui/Bishkek | Issyk-Kul | Talas | Naryn | Jalal-Abad | Osh | Batken | **Total** |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| sale | apartment | 13,220 | 230 | 10 | 10 | 10 | 50 | 10 | **13,540** |
| sale | land | 3,210 | 220 | 0 | 10 | 10 | 20 | 0 | **3,470** |
| sale | house | 2,940 | 140 | 10 | 10 | 10 | 20 | 0 | **3,130** |
| sale | commercial | 970 | 70 | 10 | 10 | 10 | 10 | 10 | **1,090** |
| sale | dacha | 120 | 20 | 0 | 0 | 0 | 0 | 0 | **140** |
| sale | parking_garage | 60 | 0 | 0 | 0 | 0 | 0 | 0 | **60** |
| sale | room | 20 | 0 | 0 | 0 | 0 | 0 | 0 | **20** |
| rent | apartment | 2,410 | 40 | 0 | 0 | 10 | 20 | 10 | **2,490** |
| rent | commercial | 980 | 10 | 0 | 0 | 0 | 10 | 0 | **1,000** |
| rent | house | 640 | 50 | 0 | 0 | 10 | 10 | 0 | **710** |
| rent | room | 30 | 10 | 0 | 10 | 0 | 0 | 0 | **50** |
| rent | land | 40 | 0 | 0 | 0 | 0 | 10 | 0 | **50** |
| rent | dacha | 10 | 10 | 0 | 0 | 0 | 0 | 0 | **20** |
| rent | parking_garage | 10 | 0 | 0 | 0 | 0 | 0 | 0 | **10** |

**≈ 25,800 listings** in total (sale ≈ 21,400, rent ≈ 4,300), roughly **230,000
photos** (~46 GB).

> ### ⚠️ The board is overwhelmingly Bishkek
> **~92% of all listings are in Chui/Bishkek.** Talas, Naryn and Batken have a
> handful of listings each. Any per-region statistic outside Chui, Issyk-Kul and
> Osh rests on a few dozen rows at most — do not build conclusions on them.

---

## 3. How the crawler works

### Crawl by stream, not by `region=all`

The crawler walks each **(deal × type × region)** stream separately rather than
using the site's `?region=all` view. Two reasons:

1. `region=all` also returns foreign countries, which we must exclude;
2. the stream URL **tells** us the deal, type and region — so all three are known
   for free, and are never guessed from page text.

### Pipeline stages

```
1. URL discovery   walk every stream's pages, collect /details/ URLs
2. Listings        fetch each detail page, parse it, download its photos
3. Entities        crawl the companies and complexes the listings referenced
4. Users           crawl the union of listing authors and review authors
```

Entities come **after** listings because listings are what *discover* them: only
once a listing is parsed do we know which company, complex and user it points at.
Each entity is then crawled exactly **once** — a few hundred pages instead of tens
of thousands.

### Resumability

Everything is append-only JSONL, written the moment a record is parsed. On start-up
each table indexes the keys it already holds and the crawler skips them. A run
killed at 80% resumes at 80%; nothing is held in memory until the end.

### Concurrency

10 threads. The work is pure network I/O, so threads (not processes) are correct —
the GIL is released while waiting on a socket. 10 workers gave ~12,000 listings/hour
with no throttling (`429`) observed. A full crawl takes **≈ 2.5–3 hours**.

---

## 4. The data model

Five tables plus an image subset, linked by stable natural keys.

```
listings ──┬── author_user_id ──→ users        (private sellers only)
           ├── company_slug   ──→ companies
           └── complex_slug   ──→ complexes

reviews  ──┬── subject_slug   ──→ companies | complexes   (per subject_type)
           └── user_id        ──→ users

photos   ───── listing_id     ──→ listings
```

### Why reviews are a separate table

A rating and its reviews **do not belong to a listing** — they belong to the
company or the residential complex, which are shared by hundreds of listings. In a
1,000-listing sample, ~590 listings pointed at only ~70 companies. Storing reviews
per-listing would copy the same agency's reviews hundreds of times.

### Why ratings are NOT a separate table

A rating is strictly **1:1** with its entity: one score, one histogram. A `ratings`
table keyed by `rating_id` would be a join that buys nothing. So the rating lives
inline on the entity, with the star histogram flattened into `rating_5` … `rating_1`
(nested dicts are awkward in Parquet).

### Keys

| Table | PK | Origin | Stable? |
|---|---|---|---|
| `listings` | `id` (uuid4) + `house_kg_id` | `/details/<id>` | yes |
| `users` | `user_id` | `/user/<hash>` | yes |
| `companies` | `slug` | `/<slug>` | yes |
| `complexes` | `slug` | `/jilie-kompleksy/<slug>` | yes |
| `reviews` | `review_id` | **sha1 hash** of its content | yes |
| `photos` | `foto_id` | uuid4 | per crawl |

> **Key on the slug, never on the name.** Several agencies have near-identical
> display names ("Кыргыз Недвижимость" is three different slugs). Keying on the
> name would merge distinct companies.

> **`review_id` is a hash, not a uuid4.** The site gives reviews no id, so we mint
> one — deterministically, from `(subject_type, subject_slug, user_id, date_raw,
> text)`. A fresh uuid on each run would churn the keys and make two dataset
> versions impossible to diff or join.

---

## 5. Field reference

**Complete** — every column in every subset is listed below, nothing omitted.

Two conventions hold throughout:

* **Field names are English; values stay in the original Russian**, exactly as the
  site renders them. `condition` is a column name; its value is `"евроремонт"`.
* Coverage figures come from a **1,000-listing sample** (sale + rent, all regions).
  Fields marked *type-specific* appear only for certain property types and are too
  rare to give a meaningful global figure.

---

### 5.1 `listings`

#### Identity and provenance

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `id` | str | 100% | our uuid4 — the PK that `photos.listing_id` points at |
| `house_kg_id` | str | 100% | the site's own listing id, from `/details/<id>`. **Stable across crawls** — use it to diff snapshots |
| `source_url` | str | 100% | link to the ad |
| `pars_date` | str | 100% | when we scraped it (ISO 8601, UTC). All relative dates are resolved against this |

#### Classification

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `deal` | str | 100% | `sale` \| `rent` |
| `type` | str | 100% | `apartment`, `house`, `commercial`, `room`, `land`, `dacha`, `parking_garage` |
| `region` | str | 100% | `chui`, `issyk_kul`, `talas`, `naryn`, `jalal_abad`, `osh`, `batken` |
| `city` | str | 100% | town/village, original: "Бишкек", "Ош", "Кочкорка" |
| `address` | str | 100% | full address line, original |
| `title` | str | 100% | ad headline, original: `"3-комн. кв., 46 м2"`. **`rooms_n` and `area_m2` are parsed out of this** |
| `description` | str | 94% | the seller's free text, original. Often the only place where terms, condition or contact preferences appear |
| `latitude` | float | 100% | from the 2GIS map widget |
| `longitude` | float | 100% | " |

#### Price

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `price_usd` | float | 100% | numeric USD |
| `price_kgs` | float | 100% | numeric KGS (som) |
| `price_usd_raw` | str | 100% | original string: `"$ 198 000"`, `"$ 1 200/мес."` |
| `price_kgs_raw` | str | 100% | original string |
| `price_period` | str | 100% | **`total`** (sale) \| **`month`** \| **`day`** (rent). **Read §8.1 before using any price** |

#### Activity

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `views` | int | 100% | view counter. The only engagement metric the site publishes — there are no likes |
| `posted_raw` | str | 100% | as shown: `"2 месяца назад"` |
| `posted_date` | str | 100% | absolute ISO, resolved against `pars_date` — **use this one** |
| `upped_raw` | str | 87% | when the ad was last bumped/edited, as shown |
| `upped_date` | str | 87% | absolute ISO. Empty for the 13% never bumped |

#### Seller (see §6)

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `offer_type` | str | 100% | **claimed**: `"от собственника"` \| `"от агента"` |
| `declared_owner` | bool | 100% | `offer_type` reduced to a boolean |
| `seller_type` | str | 100% | **actual**, from the account type: `owner` \| `company` |
| `seller_mismatch` | bool | 100% | claim ≠ reality (~4% of ads) |
| `author_user_id` | str | 39% | FK → `users.user_id`. **Private sellers only** — company ads have no personal author |
| `author_url` | str | 39% | the author's profile URL |
| `author_name` | str | 100% | display name; often just `"Пользователь"` |
| `author_ads_count` | int | 100% | how many ads that author has |
| `company_slug` | str | 60% | FK → `companies.slug` |
| `company_url` | str | 60% | the agency's profile URL |
| `complex_slug` | str | 13% | FK → `complexes.slug` |
| `complex_name` | str | 13% | the complex's display name, original |
| `complex_url` | str | 13% | the complex's profile URL |

#### Derived numerics

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `rooms_n` | int | 53% | rooms, parsed from `title`. **Empty for land / parking / commercial — that is correct, they have no rooms** (§8.4). Apartments 99%, rooms 100%, houses 73% |
| `area_m2` | float | 79% | area in m², numeric, from `title` and `area` |

#### Photos

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `foto_ids` | list[str] | 97% | `foto_id`s → the `photos` subset. 3% of ads have no photos at all; average ~9 |

#### Characteristics (from the `.info-row` table)

These come straight from the site's own characteristic list. **Which ones appear
depends entirely on the property type** — a land plot has no `floor`, an apartment
has no `land_area`. Values are original strings, never parsed into numbers (except
where a derived numeric exists above).

**General**

| Field | Cov. | Example | Notes |
|---|---:|---|---|
| `area` | 79% | `"4850 м2"` | numeric version: `area_m2` |
| `condition` | 67% | `"евроремонт"` | |
| `building` | 62% | `"кирпичный, 2009 г."` | material + year in one string |
| `heating` | 54% | `"электрическое"` | |
| `legal_documents` | 40% | `"красная книга"` | title deeds |
| `floor` | 38% | `"3 этаж из 3"` | |
| `building_series` | 34% | `"элитка"` | Soviet-era series names (`105 серия`, `хрущевка`, …) |
| `floors_total` | 25% | `"2"` | |
| `misc` | 25% | `"госакт"` | catch-all |
| `security` | 22% | `"охрана, домофон"` | |
| `ceiling_height` | 29% | `"2.5 м."` | |
| `furniture` | 29% | `"частично меблирована"` | |
| `bathroom` | 26% | `"совмещенный"` | |
| `flooring` | 17% | `"ламинат"` | |
| `entrance_door` | 13% | `"бронированная"` | |
| `balcony` | 12% | `"нет"` | |
| `parking` | 11% | `"рядом охраняемая стоянка"` | |
| `layout` | *type-specific* | `"индивидуальная"` | |
| `renovation` | *type-specific* | | |
| `doors` | *type-specific* | | |
| `windows` | *type-specific* | `"пластиковые"` | |
| `object_type` | 17% | `"отели, хостелы"` | commercial only |
| `purpose` | *type-specific* | | commercial: intended use |
| `establishment` | *type-specific* | | commercial: venue type |
| `structure_type` | *type-specific* | | |
| `wall_material` | *type-specific* | | |
| `year_built` | *type-specific* | | |
| `finishing` | *type-specific* | | |

**Utilities**

| Field | Cov. | Example |
|---|---:|---|
| `gas` | 18% | `"нет"` |
| `internet` | 20% | `"оптика"` |
| `phone_line` | 19% | `"нет"` |
| `electricity` | 17% | `"есть"` |
| `drinking_water` | 15% | `"центральное водоснабжение"` |
| `sewerage` | 14% | `"септик"` |
| `utilities` | 6% | `"свет"` |
| `irrigation_water` | 3% | `"постоянно"` — land plots |

**Land / house specific**

| Field | Cov. | Example |
|---|---:|---|
| `land_area` | 53% | `"6 соток"` |
| `house_area` | *type-specific* | |
| `land_type` | *type-specific* | |
| `land_legal_status` | *type-specific* | |
| `location` | 3% | `"в пригороде"` |

**Sale only**

| Field | Cov. | Example |
|---|---:|---|
| `mortgage_available` | 23% | `"есть"` |
| `installment_available` | 12% | `"нет"` |
| `exchange_available` | 15% | `"обмен не предлагать"` |

**Rent only**

| Field | Cov. | Example | Notes |
|---|---:|---|---|
| `rent_period` | 29% | `"на долгий срок"` | also `"посуточно"`, `"помесячно"` — this is what separates `price_period` `day` from `month` |
| `deposit` | *rent* | | |
| `prepayment` | *rent* | | |
| `utilities_payment` | *rent* | | who pays the bills |
| `children_allowed` | *rent* | | |
| `pets_allowed` | *rent* | | |

**Raw duplicate**

| Field | Cov. | Notes |
|---|---:|---|
| `rooms` | 3% | the site's own room-count characteristic, as a **string**. Almost never filled — this is exactly why `rooms_n` is parsed from the title instead. Prefer `rooms_n` |

> ### The column set is open-ended
> A characteristic whose Russian label is not in the crawler's `LABEL_MAP` is **not
> dropped** — its label is transliterated into a latin key and it still becomes a
> column (e.g. an unmapped `"Кол-во этажей"` would arrive as `kol_vo_etazhey`). So
> if house.kg adds a field tomorrow, it lands in the dataset automatically, just
> under a machine-generated name. If you see a column not listed above, that is what
> happened — and it is a signal that `LABEL_MAP` should be extended.

---

### 5.2 `users`

Listing authors and review authors share the same `/user/<hash>` namespace, so this
table is their **union** — a person who both posts ads and writes reviews is one row.

| Field | Type | Description |
|---|---|---|
| `user_id` | str | **PK** — the hash from `/user/<hash>` |
| `url` | str | profile URL |
| `pars_date` | str | when we scraped the profile |
| `name` | str | display name, often just `"Пользователь"` |
| `ads_count` | int | how many ads the user has posted |
| `registered_raw` | str | as shown: `"12 января 2023"` |
| `registered_date` | str | absolute ISO date — lets you compute account age |
| `is_ad_author` | bool | this user posted at least one listing in the dataset |
| `is_reviewer` | bool | this user wrote at least one review in the dataset |

> Companies have **no owner user**: their profile exposes no `/user/` link, so the
> chain "company → the person who created it" cannot be built.

---

### 5.3 `companies` and `complexes`

Identical schema; two tables because they are semantically different things (an
agency is not a building). `kind` disambiguates if you concatenate them.

| Field | Type | Description |
|---|---|---|
| `slug` | str | **PK** — from `/<slug>` (company) or `/jilie-kompleksy/<slug>` (complex) |
| `kind` | str | `company` \| `complex` |
| `name` | str | display name, original |
| `url` | str | profile URL |
| `pars_date` | str | when we scraped the profile |
| `rating` | float | mean score, 1–5. **Null when the entity has no ratings at all** (~65% of companies) |
| `reviews_count` | int | **what the site claims** |
| `reviews_scraped` | int | **how many rows actually exist** in `reviews` for this entity |
| `reviews_truncated` | bool | true only when the site's hard 20-review cap was hit (§8.5, §8.6) |
| `rating_5` | int | how many 5-star ratings |
| `rating_4` | int | 4-star |
| `rating_3` | int | 3-star |
| `rating_2` | int | 2-star |
| `rating_1` | int | 1-star |

The five `rating_N` columns are the star histogram, flattened out of a nested dict
because nested structures are awkward to query in Parquet. They sum to
`reviews_count`, not to `reviews_scraped`.

---

### 5.4 `reviews`

| Field | Type | Description |
|---|---|---|
| `review_id` | str | **PK** — a deterministic sha1 (16 hex chars). Re-scraping reproduces it, so dataset versions can be diffed |
| `subject_type` | str | `company` \| `complex` — **which table `subject_slug` points at** |
| `subject_slug` | str | FK → `companies.slug` or `complexes.slug` |
| `user_id` | str | FK → `users.user_id` |
| `author` | str | display name at the time of writing |
| `rating` | int | 1–5 stars given in this review |
| `text` | str | the review body, original |
| `date_raw` | str | as shown: `"2 месяца назад"` |
| `date` | str | absolute ISO |

---

### 5.5 `photos`

| Field | Type | Description |
|---|---|---|
| `foto_id` | str | **PK** — uuid4. Appears in `listings.foto_ids` |
| `listing_id` | str | FK → `listings.id` |
| `house_kg_id` | str | the site's listing id — the convenient join key if you diff snapshots |
| `url` | str | the original CDN URL. Note the host rotates (`cdn.` ↔ `bucket.`) and images may vanish upstream — the bytes here are the durable copy |
| `image` | Image | HuggingFace `Image` feature — **decodes straight to a PIL image**. Resolution is the largest the site offers (1200×900) |
| `file_name` | str | *raw JSONL only* — the file name on disk. Not present in the published Parquet, where the bytes are embedded instead |

## 6. Sellers: declared vs actual

This is the subtlest part of the site, and one of the most interesting research
angles it offers.

**Declared** — the `Тип предложения` characteristic (`offer_type`): "от собственника"
or "от агента". This is simply **what the poster typed**.

**Actual** — derived from the shape of the author link in `#block-user`:

| Author link | What they really are | `seller_type` |
|---|---|---|
| `/user/<hash>` | a personal account | `owner` |
| `/<slug>` (e.g. `/megapolis`) | a business account — the slug **is** the company profile | `company` |

### Measured disagreement (1,000 listings)

| Declared | Actual | Count |
|---|---|---:|
| от агента | company | 592 |
| от собственника | owner | 365 |
| **от агента** | **owner** | **43** ← agents with no business account |
| от собственника | company | **0** |

Both signals are stored, and `seller_mismatch` flags the disagreement. Note the
asymmetry: **nobody claimed to be an owner while posting from a company account.**

---

## 7. Ratings and reviews

The reviews modal (`#reviews-modal`) can carry **two independent ratings at once**:

| Block | Whose rating |
|---|---|
| `.modal-body` | the **agency / company** ("рейтинг компании") |
| `.modal-body.alt` | the **residential complex** ("рейтинг жилого комплекса") |

They must never be merged: a bad agency is not a bad building. In this dataset they
are separate rows in separate tables (`companies` vs `complexes`), and every review
carries `subject_type`.

Full review lists are read from the entity's own profile page
(`house.kg/<slug>` for companies, `house.kg/jilie-kompleksy/<slug>` for complexes).

---

## 8. Pitfalls — read this before analysing

Every item below is a bug that actually happened during development. The naive
approach looks right and is wrong.

### 8.1 🔴 Sale and rent prices are NOT comparable

A sale price is a **total** ($198,000). A rent price is a **rate** ($1,200/month,
$137/night). Averaging them together is meaningless.

**Always filter on `price_period`:**

```python
sales   = ads.filter(lambda r: r["price_period"] == "total")
monthly = ads.filter(lambda r: r["price_period"] == "month")
daily   = ads.filter(lambda r: r["price_period"] == "day")
```

Note also that ~30% of rentals are **daily** (`посуточно`) — mixing those with
monthly rents is the same error one level down.

### 8.2 🔴 The rent price often has no "/мес." suffix

house.kg renders plenty of rent prices bare — a rent house simply shows "$ 2 486".
Deriving the period from the price *string* therefore silently mislabels those as
sales. The **deal** (known from the crawl stream) is authoritative; the suffix and
`rent_period` only separate daily from monthly. This dataset already resolves it;
the lesson is for anyone re-deriving fields from the raw strings.

### 8.3 🔴 A business account is not the same as "has contacts"

Detecting a company by the presence of a `/business/contact/` link misses every
business account that never published contacts — 52 of 1,000 in an early run were
misfiled as private owners. Use the author-link shape (§6).

### 8.4 🟡 `rooms_n` is empty for land, parking and commercial

That is **correct, not missing data** — those property types have no rooms. Coverage
by type: apartments 99%, rooms 100%, houses 73%, land/parking/commercial 0%.
Do not impute them.

### 8.5 🟡 Reviews are capped at 20 per entity

See §9.

### 8.6 🟡 `reviews_count` counts ratings without text

A user can leave stars without writing anything. Such a "review" is counted by the
site and affects the rating, but has no body and therefore no row in `reviews`. So
`reviews_scraped < reviews_count` does **not** always mean truncation — check the
`reviews_truncated` flag, which is raised only at the hard 20 cap.

### 8.7 🟡 Dates are relative at the source

The site prints "2 месяца назад". Such a value decays — it is meaningless without
knowing when it was read. Both forms are stored: `*_raw` (original) and `*_date`
(absolute, resolved against `pars_date`). **Use `*_date` for anything time-based.**

### 8.8 🟢 There are no "likes"

house.kg publishes only a **view counter**. The share buttons (FB/VK/Telegram)
carry no counts. If you need engagement, `views` is all there is.

---

## 9. Known limitations

| Limitation | Detail |
|---|---|
| **Reviews capped at 20** | The site renders at most 20 reviews *anywhere* — on the ad page and on the entity profile alike. Its `?page=` pagination belongs to the company's **listings**, not its reviews, and returns the same 20; the JS bundle exposes only add/edit/delete endpoints. An agency with 38 reviews therefore yields 20. `reviews_count` vs `reviews_scraped` and `reviews_truncated` make this visible per entity. |
| **No company owner** | Company profiles expose no user account, so companies cannot be linked to a person. |
| **Regional sparsity** | ~92% of the board is Bishkek. Small regions have too few rows for reliable statistics. |
| **Snapshot, not history** | Each crawl is a point-in-time snapshot. `views`, prices and `upped_date` change over time; re-crawl to build a time series (`house_kg_id` and `review_id` are stable, so versions can be diffed). |
| **Photo URLs expire** | The `photos.url` column points at the CDN, which rotates hosts (`cdn.` ↔ `bucket.`) and may drop images. The bytes are embedded in the dataset, so the images themselves are safe. |

---

## 10. Recipes

```python
from datasets import load_dataset

ads       = load_dataset("<repo>", "listings",  split="train")
users     = load_dataset("<repo>", "users",     split="train")
companies = load_dataset("<repo>", "companies", split="train")
reviews   = load_dataset("<repo>", "reviews",   split="train")
photos    = load_dataset("<repo>", "photos",    split="train")
```

**Median price per m² of Bishkek apartments (sales only):**

```python
import statistics

rows = [
    r for r in ads
    if r["deal"] == "sale" and r["type"] == "apartment"
    and r["city"] == "Бишкек" and r["price_usd"] and r["area_m2"]
]
print(statistics.median(r["price_usd"] / r["area_m2"] for r in rows))
```

**Agents posing as private owners:**

```python
suspicious = [r for r in ads if r["seller_mismatch"]]
```

**Join listings to their agency's rating:**

```python
rating_by_slug = {c["slug"]: c["rating"] for c in companies}
for r in ads:
    r_rating = rating_by_slug.get(r["company_slug"])   # None for private sellers
```

**Photos of one listing:**

```python
listing_id = ads[0]["id"]
imgs = [p["image"] for p in photos if p["listing_id"] == listing_id]
```
