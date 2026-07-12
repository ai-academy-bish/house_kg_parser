"""Domain constants for house.kg.

Everything here is derived from the live site and verified against it; see
`docs/house_kg_dataset.md` for the reasoning behind each mapping.
"""

from __future__ import annotations

from typing import Final

BASE_URL: Final[str] = "https://www.house.kg"

#: Photos are never rendered larger than this, and the suffix-less URL 404s.
PHOTO_URL_MARKER: Final[str] = "house.kg/house/images/"

#: house.kg renders at most this many reviews and offers no way to load more.
REVIEW_CAP: Final[int] = 20

#: Listings per result page (fixed by the site).
LISTINGS_PER_PAGE: Final[int] = 10

# --------------------------------------------------------------------------
# Deals and property types
# --------------------------------------------------------------------------

#: deal -> {property type -> URL slug}. The crawl stream tells us the deal and
#: the type for free, so neither is ever guessed from page text.
DEALS: Final[dict[str, dict[str, str]]] = {
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

PROPERTY_TYPES: Final[tuple[str, ...]] = tuple(DEALS["sale"])

# --------------------------------------------------------------------------
# Regions
# --------------------------------------------------------------------------

#: Kyrgyzstan only. Site region ids 8+ are other countries (Russia, Kazakhstan,
#: UAE, ...) and must never enter the dataset.
REGIONS: Final[dict[int, str]] = {
    1: "chui",  # Чуйская область / Бишкек
    2: "issyk_kul",
    3: "talas",
    4: "naryn",
    5: "jalal_abad",
    6: "osh",
    7: "batken",
}

REGION_IDS_BY_NAME: Final[dict[str, int]] = {v: k for k, v in REGIONS.items()}

# --------------------------------------------------------------------------
# Characteristic labels (.info-row) -> English field names
# --------------------------------------------------------------------------

#: Russian label -> English key. Unmapped labels are NOT dropped: they fall back
#: to a transliterated slug, so new site fields still land in the dataset.
#:
#: Note "Кол-во комнат" (rent) and "Количество комнат" (sale) deliberately map to
#: the same key — otherwise the dataset grows two columns for one concept.
LABEL_MAP: Final[dict[str, str]] = {
    "Тип предложения": "offer_type",
    "Тип объекта": "object_type",
    "Тип участка": "land_type",
    "Тип строения": "structure_type",
    "Заведение": "establishment",
    "Серия": "building_series",
    "Дом": "building",
    "Этаж": "floor",
    "Этажность": "floors_total",
    "Кол-во этажей": "floors_total",
    "Площадь": "area",
    "Площадь участка": "land_area",
    "Площадь дома": "house_area",
    "Количество комнат": "rooms",
    "Кол-во комнат": "rooms",
    "Отопление": "heating",
    "Состояние": "condition",
    "Мебель": "furniture",
    "Пол": "flooring",
    "Высота потолков": "ceiling_height",
    "Правоустанавливающие документы": "legal_documents",
    "Правоустанавливающие": "legal_documents",
    "Планировка": "layout",
    "Санузел": "bathroom",
    "Балкон": "balcony",
    "Входная дверь": "entrance_door",
    "Двери": "doors",
    "Окна": "windows",
    "Газ": "gas",
    "Канализация": "sewerage",
    "Питьевая вода": "drinking_water",
    "Поливная вода": "irrigation_water",
    "Электричество": "electricity",
    "Интернет": "internet",
    "Телефон": "phone_line",
    "Местоположение": "location",
    "Возможность рассрочки": "installment_available",
    "Возможность ипотеки": "mortgage_available",
    "Возможность обмена": "exchange_available",
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
    # rent-only
    "Период аренды": "rent_period",
    "Депозит": "deposit",
    "Предоплата": "prepayment",
    "Коммунальные услуги": "utilities_payment",
    "Можно с детьми": "children_allowed",
    "Можно с животными": "pets_allowed",
}

# --------------------------------------------------------------------------
# CSS selectors — the single place to patch when the site's markup changes
# --------------------------------------------------------------------------


class Selectors:
    """CSS selectors for every page we parse.

    Kept in one class so a markup change on house.kg is a one-file fix rather
    than a hunt through the parsers.
    """

    # result pages
    LISTING_CARD = ".listing"
    CARD_LINK = "p.title a[href], a[href*='/details/']"
    PAGINATION = ".pagination a"

    # listing detail
    TITLE = "h1"
    ADDRESS = ".address"
    PRICE_USD = ".price-dollar"
    PRICE_KGS = ".price-som"
    DESCRIPTION = ".description-text, .comment, .details-comment"
    INFO_ROW = ".info-row"
    INFO_LABEL = ".label"
    INFO_VALUE = ".info"
    MAP = "#map2gis, [data-lat]"
    PHOTO_ANCHOR = "a[data-full]"
    PHOTO_IMG = "img[data-src], img[src]"

    # activity block
    ADDED = ".added-span"
    UPPED = ".upped-span"
    VIEWS = ".view-count"

    # author / seller
    AUTHOR_BLOCK = "#block-user"
    AUTHOR_LINK = "a.name[href], a.user-img[href]"
    AUTHOR_NAME = "a.name"
    AUTHOR_ADS_COUNT = "a.ads-count"
    BUSINESS_CONTACT = 'a[href*="/business/contact/"]'

    # residential complex
    COMPLEX_LINK = '.c-name a[href*="jilie-kompleksy"]'

    # ratings / reviews
    REVIEWS_MODAL = "#reviews-modal"
    MODAL_BODY = ".modal-body"
    RATING_SCORE = ".rating.score span"
    RATING_COUNT = ".rating.rate-count span"
    RATING_BARS = ".rating.bars .left > ul > li.row"
    REVIEW_ITEM = ".rating.reviews > ul > li"
    REVIEW_AUTHOR = ".user-name"
    REVIEW_AUTHOR_LINK = "a.user-img[href], a.user-name[href]"
    REVIEW_STARS = ".rating-block.in-review .rating.value li.fill"
    REVIEW_TEXT = ".review.body"
    REVIEW_DATE = ".review.footer span"

    # user profile
    USER_NAME = ".user-name"
    USER_INFO = ".user-info"
    USER_ADS_COUNT = ".ads-count"
