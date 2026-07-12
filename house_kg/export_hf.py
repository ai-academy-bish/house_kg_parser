#!/usr/bin/env python3
"""
Export the scraped tables into a HuggingFace dataset — one subset per table.

Subsets (HF "configs"):
    listings    one row per ad     -> FKs: author_user_id, company_slug, complex_slug
    users       one row per person  (ad authors UNION review authors)
    companies   one row per agency  (rating INLINE — 1:1, no separate ratings table)
    complexes   one row per ЖК      (same)
    reviews     one row per review  (own table; deterministic review_id)
    photos      one row per photo   (file_name + FK to the listing)

The pipeline already emits this shape, so this script only converts JSON ->
Parquet and writes the dataset card.
"""
import json
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

D = Path(__file__).resolve().parent
OUT = D / "hf_dataset"
DATA = OUT / "data"

SUBSETS = ["listings", "users", "companies", "complexes", "reviews", "photos"]


def load(name):
    return json.loads((D / f"{name}.json").read_text(encoding="utf-8"))


def write(name, rows):
    if not rows:
        print(f"  {name:11} SKIPPED (empty)")
        return 0
    table = pa.Table.from_pylist(rows)
    DATA.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, DATA / f"{name}.parquet", compression="snappy")
    mb = (DATA / f"{name}.parquet").stat().st_size / 1e6
    print(f"  {name:11} {len(rows):6} rows  {len(table.column_names):3} cols  {mb:6.2f} MB")
    return len(rows)


def build_photos(listings):
    """One row per photo file, pointing back at its listing."""
    by_id = {f.stem: f.name for f in (D / "foto").iterdir()}
    rows = []
    for r in listings:
        for fid in r.get("foto_ids") or []:
            fn = by_id.get(fid)
            if fn:
                rows.append({
                    "foto_id": fid,
                    "listing_id": r["id"],
                    "house_kg_id": r["house_kg_id"],
                    "file_name": f"foto/{fn}",   # resolves to an Image inside the repo
                })
    return rows


def main():
    if OUT.exists():
        shutil.rmtree(OUT)

    data = {n: load(n) for n in ["listings", "users", "companies", "complexes", "reviews"]}
    data["photos"] = build_photos(data["listings"])

    print("Writing subsets:")
    counts = {n: write(n, data[n]) for n in SUBSETS}

    # ship the image files so photos.file_name resolves
    shutil.copytree(D / "foto", OUT / "foto")
    (OUT / "README.md").write_text(card(counts), encoding="utf-8")
    shutil.copy(D / "DATASET.md", OUT / "DATASET.md")

    print(f"\nHF dataset -> {OUT}")
    print(f"  subsets: {', '.join(SUBSETS)}")


def card(c):
    configs = "".join(
        f"  - config_name: {n}\n"
        f"    data_files:\n"
        f"      - split: train\n"
        f"        path: data/{n}.parquet\n"
        for n in SUBSETS
    )
    return f"""---
language:
  - ru
  - ky
license: other
task_categories:
  - tabular-regression
tags:
  - real-estate
  - kyrgyzstan
  - house.kg
configs:
{configs}---

# house.kg — недвижимость Кыргызстана

Объявления о продаже и аренде недвижимости Кыргызстана с [house.kg](https://www.house.kg).
Имена полей английские, **значения — в оригинале** (русский), как на сайте.

## Subsets

| subset | строк | что это |
|---|---|---|
| `listings` | {c['listings']} | объявления (продажа + аренда), все типы и регионы КР |
| `users` | {c['users']} | пользователи: авторы объявлений ∪ авторы отзывов |
| `companies` | {c['companies']} | агентства / бизнес-аккаунты (рейтинг inline) |
| `complexes` | {c['complexes']} | жилые комплексы (рейтинг inline) |
| `reviews` | {c['reviews']} | отзывы на компании и ЖК |
| `photos` | {c['photos']} | фотографии объектов |

## Связи

```
listings.author_user_id  -> users.user_id            (только частники)
listings.company_slug    -> companies.slug
listings.complex_slug    -> complexes.slug
reviews.subject_slug     -> companies.slug | complexes.slug   (по subject_type)
reviews.user_id          -> users.user_id
photos.listing_id        -> listings.id
```

**Рейтинг лежит внутри** `companies` / `complexes` (связь 1:1), гистограмма звёзд
разложена в колонки `rating_5`…`rating_1`. Отдельной таблицы `ratings` нет намеренно —
это был бы джойн ради ничего.

**`review_id` — детерминированный хэш** (не uuid4), поэтому повторный сбор данных даёт
те же идентификаторы, и версии датасета можно сравнивать.

## Загрузка

```python
from datasets import load_dataset

ads  = load_dataset("<user>/house-kg", "listings", split="train")
revs = load_dataset("<user>/house-kg", "reviews",  split="train")
pics = load_dataset("<user>/house-kg", "photos",   split="train")
```

## Что важно знать перед анализом

* **Цены несопоставимы между сделками.** Продажа — полная стоимость, аренда — ставка
  (в месяц или за сутки). Всегда фильтруйте по `price_period` (`total` / `month` / `day`),
  иначе $198 000 усреднится с $1 200/мес.
* **`offer_type` vs `seller_type`.** Первое — кем продавец себя *назвал*, второе — кем он
  *является* по типу аккаунта. Расхождение помечено флагом `seller_mismatch`.
* **Сайт бишкек-центричен:** ~92% объявлений — Чуйская область / Бишкек. По малым
  регионам (Талас, Нарын, Баткен) статистика ненадёжна.
* **Даты на сайте относительные** («2 месяца назад»), поэтому есть и оригинал (`*_raw`),
  и вычисленная абсолютная дата (`*_date`).
* `rooms_n` пуст у участков, паркингов и коммерции — у них комнат не бывает.

Полный разбор источника и подводных камней — в `DATASET.md`.
"""


if __name__ == "__main__":
    main()
