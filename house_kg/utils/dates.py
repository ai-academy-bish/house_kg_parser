"""Russian date parsing.

house.kg shows *relative* dates ("2 месяца назад"). Such a value is meaningless
once detached from the moment it was read, so the pipeline stores both:

* `*_raw`  — the original string, exactly as the site rendered it;
* `*_date` — an absolute ISO timestamp, resolved against the parse time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

RU_MONTHS: dict[str, int] = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}

#: word-stem -> timedelta unit. Months/years are approximated (30/365 days),
#: which is as precise as a "2 месяца назад" source can ever be.
RELATIVE_UNITS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("секунд",), "seconds"),
    (("минут",), "minutes"),
    (("час",), "hours"),
    (("дня", "дней", "день", "сутк"), "days"),
    (("недел",), "weeks"),
    (("месяц",), "months"),
    (("год", "года", "лет"), "years"),
)


@dataclass(slots=True)
class RussianDateParser:
    """Resolves Russian relative/absolute dates to ISO strings."""

    months: dict[str, int] = field(default_factory=lambda: dict(RU_MONTHS))
    units: tuple[tuple[tuple[str, ...], str], ...] = RELATIVE_UNITS

    def parse(self, text: str | None, now: datetime | None = None) -> str | None:
        """'1 день назад' | 'сегодня' | '5 мая 2025' -> ISO string (or None)."""
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
            return self._from_relative(int(m.group(1)), m.group(2), now)

        m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", t)
        if m:
            month = self.months.get(m.group(2)[:3])
            if month:
                try:
                    return datetime(int(m.group(3)), month, int(m.group(1))).date().isoformat()
                except ValueError:
                    return None
        return None

    def _from_relative(self, n: int, word: str, now: datetime) -> str | None:
        for stems, unit in self.units:
            if any(word.startswith(s) for s in stems):
                if unit == "months":
                    delta = timedelta(days=30 * n)
                elif unit == "years":
                    delta = timedelta(days=365 * n)
                else:
                    delta = timedelta(**{unit: n})
                return (now - delta).isoformat()
        return None
