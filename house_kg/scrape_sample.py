#!/usr/bin/env python3
"""
house.kg prototype scraper.

Collects a random sample of SALE listings across all property types and all
Kyrgyzstan regions (region 1..7), parses every available feature, downloads the
photos into a flat ./foto folder (named with uuid4), and writes listings.json.

Field NAMES are English; field VALUES are kept in the original language.
"""
import json
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.house.kg"
OUT_DIR = Path(__file__).resolve().parent
FOTO_DIR = OUT_DIR / "foto"
FOTO_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.9",
}

# deal -> {property type -> listing-path slug on the site}
DEALS = {
    "sale": {
        "apartment": "kupit-kvartiru",
        "house": "kupit-dom",
        "commercial": "kupit-kommercheskaia-nedvijimost",
        "room": "kupit-komnatu",
        "land": "kupit-uchastok",
        "dacha": "kupit-dachu",
        "parking_garage": "kupit-parking-garaj",
    },
    "rent": {
        "apartment": "snyat-kvartiru",
        "house": "snyat-dom",
        "commercial": "snyat-kommercheskaia-nedvijimost",
        "room": "snyat-komnatu",
        "land": "snyat-uchastok",
        "dacha": "snyat-dachu",
        "parking_garage": "snyat-parking-garaj",
    },
}

TYPES = DEALS["sale"]  # backwards compat for the single-deal sample script

# Kyrgyzstan regions only (site region ids 1..7); 8+ are other countries
REGIONS = {
    1: "chui",        # Чуйская область / Бишкек
    2: "issyk_kul",   # Иссык-Кульская
    3: "talas",       # Таласская
    4: "naryn",       # Нарынская
    5: "jalal_abad",  # Джалал-Абадская
    6: "osh",         # Ошская
    7: "batken",      # Баткенская
}

# Russian .info-row labels -> English keys. Unknown labels fall back to a
# transliterated slug (see slugify_ru) so nothing is ever dropped.
LABEL_MAP = {
    "Тип предложения": "offer_type",
    "Тип объекта": "object_type",
    "Тип участка": "land_type",
    "Заведение": "establishment",
    "Серия": "building_series",
    "Дом": "building",
    "Этаж": "floor",
    "Этажность": "floors_total",
    "Площадь": "area",
    "Площадь участка": "land_area",
    "Площадь дома": "house_area",
    "Отопление": "heating",
    "Состояние": "condition",
    "Мебель": "furniture",
    "Пол": "flooring",
    "Высота потолков": "ceiling_height",
    "Правоустанавливающие документы": "legal_documents",
    "Количество комнат": "rooms",
    "Кол-во комнат": "rooms",        # rent pages use the short form
    "Кол-во этажей": "floors_total",
    "Поливная вода": "irrigation_water",
    "Местоположение": "location",
    "Период аренды": "rent_period",  # rent only: долгосрочная / посуточная
    "Депозит": "deposit",
    "Коммунальные услуги": "utilities_payment",
    "Можно с детьми": "children_allowed",
    "Можно с животными": "pets_allowed",
    "Предоплата": "prepayment",
    "Планировка": "layout",
    "Санузел": "bathroom",
    "Балкон": "balcony",
    "Входная дверь": "entrance_door",
    "Двери": "doors",
    "Окна": "windows",
    "Газ": "gas",
    "Канализация": "sewerage",
    "Питьевая вода": "drinking_water",
    "Электричество": "electricity",
    "Интернет": "internet",
    "Телефон": "phone_line",
    "Возможность рассрочки": "installment_available",
    "Возможность ипотеки": "mortgage_available",
    "Возможность обмена": "exchange_available",
    "Тип строения": "structure_type",
    "Материал стен": "wall_material",
    "Год постройки": "year_built",
    "Коммуникации": "utilities",
    "Отделочные работы": "finishing",
    "Правовой статус участка": "land_legal_status",
    "Назначение": "purpose",
    "Ремонт": "renovation",
    "Парковка": "parking",
    "Безопасность": "security",
    "Разное": "misc",
    "Правоустанавливающие": "legal_documents",
}

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify_ru(text: str) -> str:
    text = text.strip().lower()
    out = []
    for ch in text:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum():
            out.append(ch)
        elif ch in " -/":
            out.append("_")
    slug = "".join(out)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "field"


# requests.Session is not thread-safe -> give every worker thread its own,
# each with a connection pool sized for concurrent use.
_local = threading.local()


def get_session():
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _local.session = s
    return s


def get(url, **kw):
    for attempt in range(4):
        try:
            r = get_session().get(url, timeout=30, **kw)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            if r.status_code in (429, 503):  # throttled -> back off harder
                time.sleep(3 * (attempt + 1))
                continue
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def collect_listing_urls(type_key, slug, region_id, want):
    """Grab listing detail URLs from the first pages of a type+region stream."""
    found = []
    page = 1
    while len(found) < want and page <= 30:
        url = f"{BASE}/{slug}?region={region_id}&page={page}"
        r = get(url)
        if not r:
            break
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select(".listing")
        if not cards:
            break
        for c in cards:
            a = c.select_one("p.title a[href], a[href*='/details/']")
            if a and a.get("href"):
                found.append(BASE + a["href"].split("?")[0])
        page += 1
        time.sleep(0.4)
    return found


def _int_or_none(raw):
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    return int(digits) if digits else None


def _float_or_none(raw):
    """'4850 м2' / '103' -> 4850.0 / 103.0"""
    if raw is None:
        return None
    m = re.search(r"[\d]+(?:[.,]\d+)?", str(raw).replace("\xa0", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _price_number(raw):
    """'$ 1 200/мес.' -> 1200.0 (handles NBSP / thin-space thousand separators)."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d,.]", "", raw.split("/")[0].replace("\xa0", ""))
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_price(soup):
    """Raw price strings + normalized numbers (period is decided in price_period)."""
    def txt(sel):
        el = soup.select_one(sel)
        return el.get_text(" ", strip=True) if el else None

    usd_raw, kgs_raw = txt(".price-dollar"), txt(".price-som")
    return {
        "price_usd_raw": usd_raw,
        "price_kgs_raw": kgs_raw,
        "price_usd": _price_number(usd_raw),
        "price_kgs": _price_number(kgs_raw),
    }


def price_period(deal, usd_raw, kgs_raw, rent_period):
    """total | month | day  — what the price actually means.

    A sale price is a total ("$ 198 000"); a rent price is a rate. Mixing the
    two in one untyped column lets a $198k sale be averaged with a $1.2k/month
    rent, so the period is explicit.

    The price string alone is NOT a reliable signal: house.kg renders plenty of
    rent prices with no "/мес." suffix at all (bare "$ 2 486" on a rent house).
    The deal — which we know from the crawl stream (/snyat-* vs /kupit-*) — is
    authoritative; the suffix and "Период аренды" only distinguish daily vs
    monthly within rent.
    """
    if deal != "rent":
        return "total"
    blob = f"{usd_raw or ''} {kgs_raw or ''} {rent_period or ''}".lower()
    if "сут" in blob or "ноч" in blob or "посуточ" in blob:
        return "day"
    return "month"


def parse_entities(soup):
    """Seller (company vs private owner) and residential complex, as FK slugs.

    Both are shared across many listings, so the listing only stores the slug —
    the entity itself (with its full review list) is crawled once into its own
    table. Slugs come straight from the URLs and are stable natural keys; keying
    on the display name instead would create duplicates (several distinct
    agencies share near-identical names).
    """
    out = {
        "seller_type": "owner",
        "company_slug": None,
        "company_url": None,
        "author_user_id": None,
        "author_name": None,
        "author_url": None,
        "author_ads_count": None,
        "complex_slug": None,
        "complex_name": None,
        "complex_url": None,
    }

    # The author block is the authoritative seller signal. Its link has two shapes:
    #   /user/<hash>  -> a private person (same /user/ namespace as review authors,
    #                    so listing authors and reviewers can be joined)
    #   /<slug>       -> a business account; the slug IS the company profile page
    # Do NOT infer "company" from the /business/contact/ link alone: that only
    # exists for companies that published contacts, so business accounts without
    # them get misfiled as private owners.
    blk = soup.select_one("#block-user")
    if blk:
        link = blk.select_one("a.name[href], a.user-img[href]")
        href = link["href"] if link else ""
        if href.startswith("/user/"):
            out["author_user_id"] = href.split("/user/")[-1].strip("/")
            out["author_url"] = BASE + href
        elif href.startswith("/") and not href.startswith("/login"):
            out["seller_type"] = "company"
            out["company_slug"] = href.strip("/")
            out["company_url"] = BASE + href

        name = blk.select_one("a.name")
        if name:
            out["author_name"] = name.get_text(" ", strip=True)
        cnt = blk.select_one("a.ads-count")
        if cnt:
            digits = re.sub(r"\D", "", cnt.get_text(" ", strip=True))
            out["author_ads_count"] = int(digits) if digits else None

    # fallback: contacts link, for pages that render no author block at all
    if not out["company_slug"]:
        a = soup.select_one('a[href*="/business/contact/"]')
        if a:
            m = re.search(r"/business/contact/([^/]+)/", a["href"])
            if m:
                out["seller_type"] = "company"
                out["company_slug"] = m.group(1)
                out["company_url"] = f"{BASE}/{m.group(1)}"

    jk = soup.select_one('.c-name a[href*="jilie-kompleksy"]')
    if jk:
        slug = jk["href"].split("/jilie-kompleksy/")[-1].strip("/")
        out["complex_slug"] = slug
        out["complex_name"] = jk.get_text(" ", strip=True)
        out["complex_url"] = f"{BASE}/jilie-kompleksy/{slug}"
    return out


def parse_user_profile(user_id, now=None):
    """Crawl /user/<hash> -> the users table row.

    Listing authors and review authors share this /user/ namespace, so both kinds
    of user resolve here and can be joined.
    """
    now = now or datetime.now(timezone.utc)
    url = f"{BASE}/user/{user_id}"
    r = get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "lxml")

    name_el = soup.select_one(".user-name")
    info = soup.select_one(".user-info")
    ads = soup.select_one(".ads-count")

    registered_raw = None
    if info:
        m = re.search(r"на House\.kg с ([^|]+)", info.get_text(" ", strip=True))
        if m:
            registered_raw = m.group(1).strip()

    return {
        "user_id": user_id,
        "url": url,
        "pars_date": now.isoformat(),
        "name": name_el.get_text(" ", strip=True) if name_el else None,
        "ads_count": _int_or_none(ads.get_text(" ", strip=True)) if ads else None,
        "registered_raw": registered_raw,
        "registered_date": parse_ru_date(registered_raw, now),
    }


def parse_entity_profile(url, kind, slug, now=None):
    """Crawl a company / complex profile page -> its rating + full review list."""
    now = now or datetime.now(timezone.utc)
    r = get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    # the profile page carries a single rating block (its own)
    body = soup.select_one("#reviews-modal .modal-body") or soup
    d = _parse_rating_body(body, now)
    h1 = soup.select_one("h1")
    return {
        "slug": slug,
        "kind": kind,                      # company | complex
        "name": h1.get_text(" ", strip=True) if h1 else None,
        "url": url,
        "pars_date": now.isoformat(),
        "rating": d["rating"],
        "reviews_count": d["count"],
        "rating_distribution": d["distribution"],
        "reviews": d["reviews"],
    }


RU_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}

# relative-unit -> timedelta kwargs; house.kg renders "N дней назад" etc.
_UNITS = [
    (("секунд",), "seconds"),
    (("минут",), "minutes"),
    (("час",), "hours"),
    (("дня", "дней", "день", "сутк"), "days"),
    (("недел",), "weeks"),
    (("месяц",), "months"),
    (("год", "года", "лет"), "years"),
]


def parse_ru_date(text, now=None):
    """'1 день назад' / 'сегодня' / '5 мая 2025' -> absolute ISO datetime.

    The site shows relative dates, which decay with time, so we resolve them
    against the parse timestamp and keep the raw string alongside.
    """
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.strip().lower()

    if "сегодня" in t:
        return now.date().isoformat()
    if "вчера" in t:
        return (now - timedelta(days=1)).date().isoformat()

    m = re.search(r"(\d+)\s+([а-яё]+)", t)
    if m and "назад" in t:
        n = int(m.group(1))
        word = m.group(2)
        for needles, unit in _UNITS:
            if any(word.startswith(x) for x in needles):
                if unit == "months":
                    delta = timedelta(days=30 * n)
                elif unit == "years":
                    delta = timedelta(days=365 * n)
                else:
                    delta = timedelta(**{unit: n})
                return (now - delta).isoformat()

    # absolute form: "5 мая 2025"
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", t)
    if m:
        mon = RU_MONTHS.get(m.group(2)[:3])
        if mon:
            try:
                return datetime(int(m.group(3)), mon, int(m.group(1))).date().isoformat()
            except ValueError:
                pass
    return None


def parse_stats(soup, now):
    """Posted / bumped dates and view count from the .added-upped-info block."""
    out = {
        "posted_raw": None, "posted_date": None,
        "upped_raw": None, "upped_date": None,
        "views": None,
    }
    added = soup.select_one(".added-span")
    if added:
        raw = re.sub(r"^\s*Добавлено\s*", "", added.get_text(" ", strip=True)).strip()
        # .added-span wraps .upped-span, so strip any bumped text that leaked in
        raw = re.split(r"Поднято", raw)[0].strip()
        out["posted_raw"] = raw or None
        out["posted_date"] = parse_ru_date(raw, now)

    upped = soup.select_one(".upped-span")
    if upped:
        raw = re.sub(r"^\s*Поднято\s*", "", upped.get_text(" ", strip=True)).strip()
        out["upped_raw"] = raw or None
        out["upped_date"] = parse_ru_date(raw, now)

    vc = soup.select_one(".view-count")
    if vc:
        digits = re.sub(r"\D", "", vc.get_text(" ", strip=True))
        out["views"] = int(digits) if digits else None
    return out


def parse_coords(soup):
    m = soup.select_one("#map2gis, [data-lat]")
    if not m:
        return None, None
    lat = m.get("data-lat")
    lon = m.get("data-lon") or m.get("data-lng")
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None


def _parse_rating_body(body, now):
    """One .modal-body -> rating, count, star histogram and the review list."""
    out = {"rating": None, "count": None, "distribution": None, "reviews": []}
    if body is None:
        return out

    score = body.select_one(".rating.score span")
    if score:
        try:
            out["rating"] = float(score.get_text(strip=True).replace(",", "."))
        except ValueError:
            pass

    cnt = body.select_one(".rating.rate-count span")
    if cnt:
        digits = re.sub(r"\D", "", cnt.get_text(" ", strip=True))
        out["count"] = int(digits) if digits else None

    # star histogram: 5 rows, top row = 5 stars ... bottom row = 1 star
    rows = body.select(".rating.bars .left > ul > li.row")
    if rows:
        dist = {}
        for i, row in enumerate(rows):
            span = row.select_one("span")
            digits = re.sub(r"\D", "", span.get_text(strip=True)) if span else ""
            dist[str(5 - i)] = int(digits) if digits else 0
        out["distribution"] = dist

    for li in body.select(".rating.reviews > ul > li"):
        name = li.select_one(".user-name")
        link = li.select_one("a.user-img[href], a.user-name[href]")
        text = li.select_one(".review.body")
        foot = li.select_one(".review.footer span")
        raw_date = foot.get_text(" ", strip=True) if foot else None
        user_id = None
        if link and link.get("href", "").startswith("/user/"):
            user_id = link["href"].split("/user/")[-1].strip("/")
        out["reviews"].append({
            "author": name.get_text(" ", strip=True) if name else None,
            "user_id": user_id,
            "rating": len(li.select(".rating-block.in-review .rating.value li.fill")) or None,
            "text": text.get_text(" ", strip=True) if text else None,
            "date_raw": raw_date,
            "date": parse_ru_date(raw_date, now),
        })
    return out


def parse_reviews(soup, now):
    """Ratings + reviews from #reviews-modal.

    These do NOT belong to the listing. The modal can carry TWO independent
    rating blocks, and conflating them would be wrong (a bad agency is not a
    bad building):
        .modal-body        -> the agency / company  ("рейтинг компании")
        .modal-body.alt    -> the residential complex ("рейтинг жилого комплекса")
    Either, both, or neither may be present. The page embeds at most 20 reviews
    per block even when `count` is higher — the rest live on the company/complex
    page itself.
    """
    out = {}
    modal = soup.select_one("#reviews-modal")
    bodies = modal.select(".modal-body") if modal else []

    company_body = next(
        (b for b in bodies if "alt" not in (b.get("class") or [])), None
    )
    complex_body = next((b for b in bodies if "alt" in (b.get("class") or [])), None)

    for prefix, body in (("company", company_body), ("complex", complex_body)):
        d = _parse_rating_body(body, now)
        out[f"{prefix}_rating"] = d["rating"]
        out[f"{prefix}_reviews_count"] = d["count"]
        out[f"{prefix}_rating_distribution"] = d["distribution"]
        out[f"{prefix}_reviews"] = d["reviews"]
    return out


def parse_photos(soup):
    """Return de-duplicated full-resolution photo URLs from the gallery.

    The detail-page gallery stores images on <a data-full="..._1200x900.jpg">.
    Fall back to any cdn image url in img/data-src as a safety net.
    """
    urls = []
    seen = set()
    # data-full holds the largest offered size (_1200x900); the bare, suffix-less
    # URL is NOT served (404), so keep data-full verbatim. Only fall back to img
    # thumbnails (upscaled to 1200x900) when no data-full anchors exist.
    for a in soup.select("a[data-full]"):
        src = a.get("data-full", "")
        if "house.kg/house/images/" in src and src not in seen:
            seen.add(src)
            urls.append(src)
    if not urls:
        for img in soup.select("img[data-src], img[src]"):
            src = img.get("data-src") or img.get("src") or ""
            if "house.kg/house/images/" not in src:
                continue
            src = re.sub(r"_\d+x\d+(?=\.\w+$)", "_1200x900", src)
            if src not in seen:
                seen.add(src)
                urls.append(src)
    return urls


def download_photo(url):
    r = get(url)
    if not r:
        return None
    ext = ".jpg"
    m = re.search(r"\.(jpe?g|png|webp)$", url, re.I)
    if m:
        ext = "." + m.group(1).lower()
    fid = uuid.uuid4().hex
    (FOTO_DIR / (fid + ext)).write_bytes(r.content)
    return fid


def parse_listing(url, type_key, region_key, deal="sale"):
    r = get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "lxml")

    now = datetime.now(timezone.utc)
    house_id = url.rstrip("/").split("/details/")[-1]
    h1 = soup.select_one("h1")
    address_el = soup.select_one(".address")
    address = address_el.get_text(" ", strip=True) if address_el else None
    # first address part is usually the city; but for regions it can be the
    # oblast ("...область"), in which case the next part is the actual town/village
    city = None
    if address:
        parts = [p.strip() for p in address.split(",") if p.strip()]
        city = parts[0]
        if "област" in city.lower() and len(parts) > 1:
            city = parts[1]

    record = {
        "id": str(uuid.uuid4()),
        "pars_date": now.isoformat(),
        "source_url": url,
        "house_kg_id": house_id,
        "deal": deal,          # sale | rent
        "type": type_key,
        "region": region_key,
        "city": city,
        "address": address,
        "title": h1.get_text(" ", strip=True) if h1 else None,
    }
    record.update(parse_price(soup))
    record.update(parse_stats(soup, now))
    # ratings/reviews are NOT stored on the listing: they belong to the company
    # or the complex, which are shared by many listings. Only the FK slugs go here.
    record.update(parse_entities(soup))

    lat, lon = parse_coords(soup)
    record["latitude"] = lat
    record["longitude"] = lon

    desc = soup.select_one(".description-text, .comment, .details-comment")
    if desc:
        record["description"] = desc.get_text("\n", strip=True)

    # all .info-row features, flattened, English keys / original values
    for row in soup.select(".info-row"):
        label_el = row.select_one(".label")
        value_el = row.select_one(".info")
        if not label_el or not value_el:
            continue
        label = label_el.get_text(" ", strip=True)
        value = value_el.get_text(" ", strip=True)
        key = LABEL_MAP.get(label) or slugify_ru(label)
        if key not in record:  # never clobber the core fields
            record[key] = value

    # photos
    foto_ids = []
    for purl in parse_photos(soup):
        fid = download_photo(purl)
        if fid:
            foto_ids.append(fid)
        time.sleep(0.15)
    record["foto_ids"] = foto_ids

    # decided last: needs rent_period, which comes from the .info-row loop above
    record["price_period"] = price_period(
        deal, record.get("price_usd_raw"), record.get("price_kgs_raw"),
        record.get("rent_period"),
    )

    # Room count lives in the TITLE ("3-комн. кв., 46 м2"), not in the .info-row
    # list (that field is filled on only ~3% of ads), so parse it out. Area is
    # backfilled from the title too when the info-row is missing.
    title_txt = record.get("title") or ""
    m = re.search(r"(\d+)\s*-?\s*комн", title_txt, re.I)
    record["rooms_n"] = int(m.group(1)) if m else _int_or_none(record.get("rooms"))
    m = re.search(r"([\d.,]+)\s*м2", title_txt)
    record["area_m2"] = (
        _float_or_none(m.group(1)) if m else _float_or_none(record.get("area"))
    )

    # Declared vs actual seller. `offer_type` is what the author CLAIMS
    # ("от собственника" / "от агента"); `seller_type` is what their account
    # actually is (§5 of DATASET.md). They disagree in ~4% of ads — a real signal,
    # so both are kept and the mismatch is flagged rather than silently resolved.
    declared = (record.get("offer_type") or "").lower()
    record["declared_owner"] = "собственник" in declared if declared else None
    if record["declared_owner"] is None:
        record["seller_mismatch"] = None
    else:
        actual_owner = record["seller_type"] == "owner"
        record["seller_mismatch"] = record["declared_owner"] != actual_owner

    return record


def main():
    random.seed()
    # Build a candidate pool: a few URLs from each type across random regions,
    # then sample 10. Region 1 (Chui/Bishkek) dominates supply, others are thin.
    pool = []
    type_items = list(TYPES.items())
    random.shuffle(type_items)
    for type_key, slug in type_items:
        region_id = random.choice(list(REGIONS))
        urls = collect_listing_urls(type_key, slug, region_id, want=4)
        for u in urls:
            pool.append((u, type_key, REGIONS[region_id]))
        if len(pool) >= 40:
            break

    random.shuffle(pool)
    sample = pool[:10]

    print(f"Pool size {len(pool)}, parsing {len(sample)} listings...")
    records = []
    for i, (url, type_key, region_key) in enumerate(sample, 1):
        print(f"[{i}/{len(sample)}] {type_key}/{region_key}  {url}")
        rec = parse_listing(url, type_key, region_key)
        if rec:
            records.append(rec)
            print(f"    -> {len(rec['foto_ids'])} photos, "
                  f"{len(rec)} fields, coords={rec['latitude']},{rec['longitude']}")
        time.sleep(0.5)

    out = OUT_DIR / "listings.json"
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved {len(records)} listings -> {out}")
    print(f"Photos in {FOTO_DIR} ({len(list(FOTO_DIR.iterdir()))} files)")


if __name__ == "__main__":
    main()
