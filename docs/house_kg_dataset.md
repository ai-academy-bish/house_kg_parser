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

**Actually crawled: 25,473 listings** (sale 21,264, rent 4,209) and **227,294 photos
(36 GB)** — an average of **8.9 photos per ad**. The small shortfall against the
estimate above is normal: ads expire between URL discovery and fetching (20 returned
404), and a handful time out.

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
the GIL is released while waiting on a socket.

**The full crawl took 4h 32m** (~5,600 listings/hour including all photo downloads).
house.kg never returned a `429`; the only failures were read timeouts, of which 10
listings exhausted their retries and were skipped. Re-running the crawler picks them
up — resume skips the 25,473 already stored and retries only the gaps.

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

A rating and its reviews **do not belong to a listing** — they belong to the company
or the residential complex, which are shared by hundreds of listings. On the full
crawl:

| | listings referencing it | distinct entities | ratio |
|---|---:|---:|---:|
| companies | 19,640 | **179** | **110×** |
| complexes | 9,221 | **707** | 13× |

Storing reviews on the listing would copy the same agency's reviews **110 times over**
on average. In their own table they are stored once.

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

> ### ⚠️ A slug can name BOTH a company and a complex
> `companies` and `complexes` live in **different URL namespaces** (`/<slug>` versus
> `/jilie-kompleksy/<slug>`), so the same slug string can legitimately name one of
> each — and does. The agency **«Дипломат»** and the residential complex
> **«Дипломат»** are both `diplomat`.
>
> The dataset handles this correctly: the tables are separate, `listings.company_slug`
> only ever points at companies, `listings.complex_slug` only at complexes, and every
> review carries `subject_type`. **But never concatenate `companies` and `complexes`
> and join on `slug` alone** — you would fuse an estate agency with an apartment
> block. If you need one combined entity table, key it on `(kind, slug)`.

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
* Coverage figures are measured on the **full crawl of 25,473 listings** (sale + rent,
  all regions), not on a sample.

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
| `deal` | str | 100% | `sale` (21,264) \| `rent` (4,209) |
| `type` | str | 100% | `apartment`, `house`, `commercial`, `room`, `land`, `dacha`, `parking_garage` |
| `region` | str | 100% | `chui`, `issyk_kul`, `talas`, `naryn`, `jalal_abad`, `osh`, `batken` |
| `city` | str | 99.4% | town/village, original: "Бишкек", "Ош", "Кочкорка" |
| `address` | str | 99.4% | full address line, original |
| `title` | str | 100% | ad headline, original: `"3-комн. кв., 46 м2"`. **`rooms_n` and `area_m2` are parsed out of this** |
| `description` | str | 94.7% | the seller's free text, original. Often the only place terms, condition or contact preferences appear |
| `latitude` | float | 100% | from the 2GIS map widget |
| `longitude` | float | 100% | " |

#### Price

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `price_usd` | float | 100% | numeric USD |
| `price_kgs` | float | 100% | numeric KGS (som) |
| `price_usd_raw` | str | 100% | original string: `"$ 198 000"`, `"$ 1 200/мес."` |
| `price_kgs_raw` | str | 100% | original string |
| `price_period` | str | 100% | **`total`** (21,264) \| **`month`** (3,765) \| **`day`** (444). **Read §8.1 before using any price** |

#### Activity

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `views` | int | 100% | view counter. The only engagement metric the site publishes — there are no likes |
| `posted_raw` | str | 100% | as shown: `"2 месяца назад"` |
| `posted_date` | str | 100% | absolute ISO, resolved against `pars_date` — **use this one** |
| `upped_raw` | str | 92.7% | when the ad was last bumped/edited, as shown |
| `upped_date` | str | 92.7% | absolute ISO. Empty for the 7% never bumped |

#### Seller (see §6)

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `offer_type` | str | 100% | **claimed**: `"от собственника"` \| `"от агента"` (one ad states neither) |
| `declared_owner` | bool | 100% | `offer_type` reduced to a boolean |
| `seller_type` | str | 100% | **actual**, from the account type: `owner` \| `company` |
| `seller_mismatch` | bool | 100% | claim ≠ reality — **true for 1,408 ads (5.5%)** |
| `author_user_id` | str | 22.9% | FK → `users.user_id`. **Private sellers only** — company ads have no personal author |
| `author_url` | str | 22.9% | the author's profile URL |
| `author_name` | str | 100% | display name; often just `"Пользователь"`. Present for companies too (the company's name) |
| `author_ads_count` | int | 100% | how many ads that author has |
| `company_slug` | str | 77.1% | FK → `companies.slug`. **Most of the board is agencies** |
| `company_url` | str | 77.1% | the agency's profile URL |
| `complex_slug` | str | 36.2% | FK → `complexes.slug` |
| `complex_name` | str | 36.2% | the complex's display name, original |
| `complex_url` | str | 36.2% | the complex's profile URL |

#### Derived numerics

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `rooms_n` | int | 72.4% | rooms, parsed from `title`. **Empty for land / parking / commercial — that is correct, they have no rooms** (§8.4) |
| `area_m2` | float | 86.1% | area in m², numeric, from `title` and `area` |

#### Photos

| Field | Type | Cov. | Description |
|---|---|---:|---|
| `foto_ids` | list[str] | 98.5% | `foto_id`s → the `photos` subset. **8.9 photos per ad on average**; 1.5% of ads have none |

#### Characteristics (from the `.info-row` table)

These come straight from the site's own characteristic list. **Which ones appear
depends entirely on the property type** — a land plot has no `floor`, an apartment
has no `land_area`. Values are original strings, never parsed into numbers (except
where a derived numeric exists above).

Coverage below is measured across the **whole board (25,473 ads)**, so a field used
only by one property type looks "rare" even though it is near-universal *within* that
type. Always condition on `type` before reading anything into these numbers.

**Building and condition**

| Field | Cov. | Example |
|---|---:|---|
| `area` | 86.1% | `"4850 м2"` — numeric version: `area_m2` |
| `building` | 81.4% | `"кирпичный, 2009 г."` — material and year in one string |
| `condition` | 76.3% | `"евроремонт"` |
| `floor` | 69.1% | `"3 этаж из 3"` |
| `heating` | 65.2% | `"электрическое"` |
| `building_series` | 64.5% | `"элитка"` — Soviet-era series (`105 серия`, `хрущевка`, …) |
| `legal_documents` | 47.8% | `"красная книга"` — title deeds |
| `ceiling_height` | 40.6% | `"2.5 м."` |
| `misc` | 30.6% | `"госакт"` — catch-all |
| `bathroom` | 30.1% | `"совмещенный"` |
| `furniture` | 26.8% | `"частично меблирована"` |
| `security` | 24.1% | `"охрана, домофон"` |
| `entrance_door` | 20.2% | `"бронированная"` |
| `balcony` | 18.0% | `"нет"` |
| `flooring` | 17.9% | `"ламинат"` |
| `parking` | 13.9% | `"рядом охраняемая стоянка"` |
| `floors_total` | 13.1% | `"2"` |
| `object_type` | 7.2% | `"отели, хостелы"` — commercial only |

**Utilities**

| Field | Cov. | Example |
|---|---:|---|
| `gas` | 27.1% | `"нет"` |
| `internet` | 20.8% | `"оптика"` |
| `phone_line` | 18.4% | `"нет"` |
| `electricity` | 8.6% | `"есть"` |
| `drinking_water` | 8.3% | `"центральное водоснабжение"` |
| `sewerage` | 7.8% | `"септик"` |
| `utilities` | 7.8% | `"свет"` |
| `irrigation_water` | 0.3% | `"постоянно"` — land plots only |

**Land**

| Field | Cov. | Example |
|---|---:|---|
| `land_area` | 30.7% | `"6 соток"` |
| `location` | 2.9% | `"в пригороде"` |

**Sale only**

| Field | Cov. | Example |
|---|---:|---|
| `mortgage_available` | 25.6% | `"есть"` |
| `exchange_available` | 14.2% | `"обмен не предлагать"` |
| `installment_available` | 12.6% | `"нет"` |

**Rent only**

| Field | Cov. | Example |
|---|---:|---|
| `rent_period` | 14.4% | `"на долгий срок"`, `"посуточно"`, `"помесячно"` — this is what separates `price_period` `day` from `month`. 14.4% of the *whole board* ≈ 87% of rentals |

**Degenerate field**

| Field | Cov. | Notes |
|---|---:|---|
| `rooms` | **0.2%** | the site's own room-count characteristic, as a **string**. Effectively never filled — which is exactly why `rooms_n` is parsed from the title instead. **Use `rooms_n`, not this** |

> ### The column set is open-ended
> A characteristic whose Russian label is not in the crawler's `LABEL_MAP` is **not
> dropped** — its label is transliterated into a latin key and it still becomes a
> column. So if house.kg adds a field tomorrow, it lands in the dataset automatically,
> just under a machine-generated name (e.g. `kol_vo_etazhey`). If you see a column not
> listed above, that is what happened — and it is a signal that `LABEL_MAP` should be
> extended.
>
> The converse also holds: `LABEL_MAP` carries a few entries (`deposit`, `pets_allowed`,
> `year_built`, `wall_material`, `layout`, …) that **never fired on the full crawl** —
> the site simply does not publish them. They are kept as a safety net, but there is no
> such column in the data.

### 5.2 `users`

Listing authors and review authors share the same `/user/<hash>` namespace, so this
table is their **union** — a person who both posts ads and writes reviews is one row.

Full crawl: **4,577 users** = 4,210 ad authors + 448 reviewers, of whom **81 do both**.
Every one of them has a `registered_date`.

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
| `rating` | float | mean score, 1–5. **Null when nobody has rated the entity** — which is most of them: only **33 of 179 companies (18%)** and **253 of 707 complexes (35%)** carry a rating |
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

### Measured disagreement (full crawl, 25,473 listings)

| Declared | Actual | Count | |
|---|---|---:|---|
| от агента | company | 19,586 | agrees |
| от собственника | owner | 4,478 | agrees |
| **от агента** | **owner** | **1,365** | agents with no business account |
| **от собственника** | **company** | **43** | **a business account claiming to be a private owner** |
| *(none)* | owner | 1 | the ad states no offer type |

Both signals are stored, and `seller_mismatch` flags the disagreement — **1,408 ads
(5.5%)** disagree with themselves.

> **A caution about small samples.** On a 1,000-listing sample the last category was
> **zero**, and an earlier draft of this document concluded there was an asymmetry:
> "nobody claims to be an owner while posting from a company account." The full crawl
> shows **43 of them**. The category is genuinely rare (0.17%), which is exactly why a
> thousand rows could not see it — a useful lesson to hand to students before they
> generalise from a subsample of this very dataset.

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

### 8.8 🔴 Never join `companies` and `complexes` on `slug` alone

A slug can name **both** an agency and a residential complex — `diplomat` is both
the agency «Дипломат» and the complex «Дипломат». Each table is internally correct,
and every foreign key in the dataset is unambiguous, but a naive union of the two
entity tables keyed on `slug` will fuse them. Key on `(kind, slug)` if you need a
combined table.

### 8.9 🟢 There are no "likes"

house.kg publishes only a **view counter**. The share buttons (FB/VK/Telegram)
carry no counts. If you need engagement, `views` is all there is.

---

## 9. Known limitations

| Limitation | Detail |
|---|---|
| **Reviews capped at 20** | The site renders at most 20 reviews *anywhere* — on the ad page and on the entity profile alike. Its `?page=` pagination belongs to the company's **listings**, not its reviews, and returns the same 20; the JS bundle exposes only add/edit/delete endpoints. An agency with 38 reviews therefore yields 20. `reviews_count` vs `reviews_scraped` and `reviews_truncated` make this visible per entity. In practice the cap bit **only 1 of 886 entities** — almost nothing is lost. |
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
