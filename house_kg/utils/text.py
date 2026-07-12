"""Text normalisation: transliteration and slugs.

house.kg labels its characteristics in Russian. Known labels are mapped to English
keys via `LABEL_MAP`; anything unknown is transliterated instead of being dropped,
so a new site field still reaches the dataset (under a latinised name) and can be
promoted to a proper mapping later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Cyrillic -> latin. A dataclass rather than a module constant so a subclass can
#: swap in a different scheme (e.g. Kyrgyz-specific letters) without patching code.
DEFAULT_TRANSLIT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Kyrgyz-specific
    "ң": "n", "ө": "o", "ү": "u",
}


@dataclass(slots=True)
class Transliterator:
    """Turns arbitrary Russian/Kyrgyz text into a snake_case ASCII slug."""

    table: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TRANSLIT))
    separators: str = " -/"

    def slugify(self, text: str) -> str:
        """'Кол-во этажей' -> 'kol_vo_etazhey'. Never returns an empty string."""
        out: list[str] = []
        for ch in text.strip().lower():
            if ch in self.table:
                out.append(self.table[ch])
            elif ch.isalnum():
                out.append(ch)
            elif ch in self.separators:
                out.append("_")
        slug = re.sub(r"_+", "_", "".join(out)).strip("_")
        return slug or "field"


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace; empty becomes None."""
    if value is None:
        return None
    cleaned = re.sub(r"[ \t\xa0]+", " ", value).strip()
    return cleaned or None
