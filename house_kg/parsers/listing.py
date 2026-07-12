"""Listing detail page parser.

Every non-obvious rule here was learned by breaking against the live site; the
comments record *why*, because the naive version of each looks correct and is not.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from bs4 import BeautifulSoup, Tag

from ..constants import BASE_URL, LABEL_MAP, PHOTO_URL_MARKER, Selectors
from ..models import Listing
from ..utils import clean_text, parse_price, to_float, to_int
from .base import BaseParser


class ListingParser(BaseParser):
    """Turns a `/details/<id>` page into a `Listing`."""

    def parse(self, html: str, url: str, deal: str, property_type: str, region: str) -> Listing:
        soup = self.soup(html)
        now = self.now()

        attributes = self._attributes(soup)
        address = clean_text(self.text_of(soup, Selectors.ADDRESS))
        title = clean_text(self.text_of(soup, Selectors.TITLE))
        latitude, longitude = self._coords(soup)
        seller = self._seller(soup)
        prices = self._prices(soup, deal, attributes.get("rent_period"))
        activity = self._activity(soup, now)

        offer_type = attributes.get("offer_type")
        declared_owner = ("собственник" in offer_type.lower()) if offer_type else None
        mismatch = (
            None
            if declared_owner is None
            else declared_owner != (seller["seller_type"] == "owner")
        )

        return Listing(
            id=str(uuid.uuid4()),
            house_kg_id=url.rstrip("/").split("/details/")[-1],
            source_url=url,
            pars_date=now.isoformat(),
            deal=deal,
            type=property_type,
            region=region,
            city=self._city(address),
            address=address,
            title=title,
            description=clean_text(self.text_of(soup, Selectors.DESCRIPTION)),
            latitude=latitude,
            longitude=longitude,
            **prices,
            **activity,
            **seller,
            offer_type=offer_type,
            declared_owner=declared_owner,
            seller_mismatch=mismatch,
            rooms_n=self._rooms(title, attributes),
            area_m2=self._area(title, attributes),
            foto_ids=[],  # filled by the photo downloader
            attributes=attributes,
        )

    # -- pieces ------------------------------------------------------------

    def _attributes(self, soup: BeautifulSoup) -> dict[str, str]:
        """`.info-row` label/value pairs, keyed in English.

        Unknown labels are transliterated rather than dropped, so a field the site
        adds tomorrow still lands in the dataset instead of vanishing silently.
        """
        out: dict[str, str] = {}
        for row in soup.select(Selectors.INFO_ROW):
            label_el = row.select_one(Selectors.INFO_LABEL)
            value_el = row.select_one(Selectors.INFO_VALUE)
            if not label_el or not value_el:
                continue
            label = label_el.get_text(" ", strip=True)
            value = clean_text(value_el.get_text(" ", strip=True))
            if value is None:
                continue
            key = LABEL_MAP.get(label) or self.translit.slugify(label)
            out.setdefault(key, value)
        return out

    @staticmethod
    def _city(address: str | None) -> str | None:
        """First address part — unless it is the oblast, then the next one.

        "Иссык-Кульская область, с. Григорьевка" must yield the village, not the
        region we already know from the crawl stream.
        """
        if not address:
            return None
        parts = [p.strip() for p in address.split(",") if p.strip()]
        if not parts:
            return None
        city = parts[0]
        if "област" in city.lower() and len(parts) > 1:
            return parts[1]
        return city

    @staticmethod
    def _coords(soup: BeautifulSoup) -> tuple[float | None, float | None]:
        node = soup.select_one(Selectors.MAP)
        if not node:
            return None, None
        lat = node.get("data-lat")
        lon = node.get("data-lon") or node.get("data-lng")
        try:
            return float(lat), float(lon)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None, None

    def _prices(
        self, soup: BeautifulSoup, deal: str, rent_period: str | None
    ) -> dict[str, object]:
        """Raw price strings, numeric values, and what the price actually MEANS.

        A sale price is a total ("$ 198 000"); a rent price is a rate. Mixing them
        in one untyped column lets a $198k sale be averaged with a $1.2k/month
        rent, so `price_period` is explicit.

        The price string alone is NOT a reliable period signal: house.kg renders
        plenty of rent prices with no "/мес." suffix at all (a bare "$ 2 486" on a
        rent house). The *deal* — known from the crawl stream — is authoritative;
        the suffix and "Период аренды" only separate daily from monthly.
        """
        usd_raw = clean_text(self.text_of(soup, Selectors.PRICE_USD))
        kgs_raw = clean_text(self.text_of(soup, Selectors.PRICE_KGS))

        if deal != "rent":
            period = "total"
        else:
            blob = f"{usd_raw or ''} {kgs_raw or ''} {rent_period or ''}".lower()
            period = "day" if ("сут" in blob or "ноч" in blob or "посуточ" in blob) else "month"

        return {
            "price_usd_raw": usd_raw,
            "price_kgs_raw": kgs_raw,
            "price_usd": parse_price(usd_raw),
            "price_kgs": parse_price(kgs_raw),
            "price_period": period,
        }

    def _activity(self, soup: BeautifulSoup, now: datetime) -> dict[str, object]:
        """Posted / bumped dates and the view counter.

        There is no "likes" counter on house.kg — only views. What looks like likes
        is a row of social share buttons with no count attached.
        """
        posted_raw = upped_raw = None

        added = soup.select_one(Selectors.ADDED)
        if added:
            raw = re.sub(r"^\s*Добавлено\s*", "", added.get_text(" ", strip=True))
            # .added-span wraps .upped-span, so strip any bumped text that leaked in
            posted_raw = clean_text(re.split(r"Поднято", raw)[0])

        upped = soup.select_one(Selectors.UPPED)
        if upped:
            upped_raw = clean_text(
                re.sub(r"^\s*Поднято\s*", "", upped.get_text(" ", strip=True))
            )

        return {
            "views": to_int(self.text_of(soup, Selectors.VIEWS)),
            "posted_raw": posted_raw,
            "posted_date": self.dates.parse(posted_raw, now),
            "upped_raw": upped_raw,
            "upped_date": self.dates.parse(upped_raw, now),
        }

    def _seller(self, soup: BeautifulSoup) -> dict[str, object]:
        """Who is actually selling, plus the residential complex.

        The author block is the authoritative signal, and its link has two shapes:

            /user/<hash>  -> a private person (same namespace as review authors,
                             so listing authors and reviewers can be joined)
            /<slug>       -> a business account; the slug IS the company profile

        Do NOT infer "company" from the `/business/contact/` link alone: that only
        exists for companies which published contacts, so business accounts without
        them get misfiled as private owners (52 of 1000 in an early run).
        """
        out: dict[str, object] = {
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

        block = soup.select_one(Selectors.AUTHOR_BLOCK)
        if block:
            link = block.select_one(Selectors.AUTHOR_LINK)
            href = link["href"] if isinstance(link, Tag) and link.get("href") else ""
            if href.startswith("/user/"):
                out["author_user_id"] = href.split("/user/")[-1].strip("/")
                out["author_url"] = BASE_URL + href
            elif href.startswith("/") and not href.startswith("/login"):
                out["seller_type"] = "company"
                out["company_slug"] = href.strip("/")
                out["company_url"] = BASE_URL + href

            out["author_name"] = clean_text(self.text_of(block, Selectors.AUTHOR_NAME))
            out["author_ads_count"] = to_int(self.text_of(block, Selectors.AUTHOR_ADS_COUNT))

        # fallback for pages that render no author block at all
        if not out["company_slug"]:
            contact = soup.select_one(Selectors.BUSINESS_CONTACT)
            if isinstance(contact, Tag):
                m = re.search(r"/business/contact/([^/]+)/", contact.get("href", ""))
                if m:
                    out["seller_type"] = "company"
                    out["company_slug"] = m.group(1)
                    out["company_url"] = f"{BASE_URL}/{m.group(1)}"

        complex_link = soup.select_one(Selectors.COMPLEX_LINK)
        if isinstance(complex_link, Tag):
            slug = complex_link["href"].split("/jilie-kompleksy/")[-1].strip("/")
            out["complex_slug"] = slug
            out["complex_name"] = clean_text(complex_link.get_text(" ", strip=True))
            out["complex_url"] = f"{BASE_URL}/jilie-kompleksy/{slug}"

        return out

    @staticmethod
    def _rooms(title: str | None, attributes: dict[str, str]) -> int | None:
        """Room count lives in the TITLE ("3-комн. кв., 46 м2").

        The dedicated characteristic is filled on only ~3% of ads, so relying on it
        alone throws away the feature. Land, parking and commercial have no rooms
        at all — None there is correct, not missing data.
        """
        if title:
            m = re.search(r"(\d+)\s*-?\s*комн", title, re.I)
            if m:
                return int(m.group(1))
        return to_int(attributes.get("rooms"))

    @staticmethod
    def _area(title: str | None, attributes: dict[str, str]) -> float | None:
        if title:
            m = re.search(r"([\d.,]+)\s*м2", title)
            if m:
                return to_float(m.group(1))
        return to_float(attributes.get("area"))

    # -- photos ------------------------------------------------------------

    @staticmethod
    def photo_urls(html: str) -> list[str]:
        """Full-size gallery URLs, de-duplicated, order preserved.

        Two traps, both of which silently yield zero photos:

        * the CDN host varies between `cdn.house.kg` and `bucket.house.kg`, so the
          filter must not pin a subdomain;
        * the size suffix must NOT be stripped — `..._1200x900.jpg` is the largest
          image on offer and the suffix-less URL returns 404. `data-full` already
          points at that largest size.
        """
        soup = BeautifulSoup(html, "lxml")
        urls: list[str] = []
        seen: set[str] = set()

        for anchor in soup.select(Selectors.PHOTO_ANCHOR):
            src = anchor.get("data-full", "")
            if PHOTO_URL_MARKER in src and src not in seen:
                seen.add(src)
                urls.append(src)

        if not urls:  # fall back to thumbnails, upscaled to the gallery size
            for img in soup.select(Selectors.PHOTO_IMG):
                src = img.get("data-src") or img.get("src") or ""
                if PHOTO_URL_MARKER not in src:
                    continue
                src = re.sub(r"_\d+x\d+(?=\.\w+$)", "_1200x900", src)
                if src not in seen:
                    seen.add(src)
                    urls.append(src)
        return urls
