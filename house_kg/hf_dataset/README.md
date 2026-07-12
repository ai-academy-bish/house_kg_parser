---
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
  - config_name: listings
    data_files:
      - split: train
        path: data/listings.parquet
  - config_name: users
    data_files:
      - split: train
        path: data/users.parquet
  - config_name: companies
    data_files:
      - split: train
        path: data/companies.parquet
  - config_name: complexes
    data_files:
      - split: train
        path: data/complexes.parquet
  - config_name: reviews
    data_files:
      - split: train
        path: data/reviews.parquet
  - config_name: photos
    data_files:
      - split: train
        path: data/photos.parquet
---

# house.kg — недвижимость Кыргызстана

Объявления о продаже и аренде недвижимости Кыргызстана с [house.kg](https://www.house.kg).
Имена полей английские, **значения — в оригинале** (русский), как на сайте.

## Subsets

| subset | строк | что это |
|---|---|---|
| `listings` | 1000 | объявления (продажа + аренда), все типы и регионы КР |
| `users` | 554 | пользователи: авторы объявлений ∪ авторы отзывов |
| `companies` | 69 | агентства / бизнес-аккаунты (рейтинг inline) |
| `complexes` | 113 | жилые комплексы (рейтинг inline) |
| `reviews` | 213 | отзывы на компании и ЖК |
| `photos` | 8809 | фотографии объектов |

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
