"""Numeric extraction from the site's free-form strings."""

from __future__ import annotations

import re


def to_int(value: object | None) -> int | None:
    """'15 объявлений' -> 15. Strips every non-digit."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def to_float(value: object | None) -> float | None:
    """'4850 м2' -> 4850.0 ; '2.5 м.' -> 2.5"""
    if value is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(value).replace("\xa0", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def parse_price(raw: str | None) -> float | None:
    """'$ 1 200/мес.' -> 1200.0

    Takes only the part before the '/' so a per-period suffix never leaks into
    the number, and tolerates NBSP / thin-space thousand separators.
    """
    if not raw:
        return None
    head = raw.split("/")[0].replace("\xa0", "").replace(" ", "")
    cleaned = re.sub(r"[^\d,.]", "", head).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None
