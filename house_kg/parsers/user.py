"""User profile parser."""

from __future__ import annotations

import re

from ..constants import BASE_URL, Selectors
from ..models import User
from ..utils import clean_text, to_int
from .base import BaseParser


class UserParser(BaseParser):
    """Turns a `/user/<hash>` page into a `User`.

    Both listing authors and review authors live in this namespace, which is what
    lets the two be joined into a single `users` table.
    """

    @staticmethod
    def url_for(user_id: str) -> str:
        return f"{BASE_URL}/user/{user_id}"

    def parse(self, html: str, user_id: str) -> User:
        soup = self.soup(html)
        now = self.now()

        registered_raw = None
        info = soup.select_one(Selectors.USER_INFO)
        if info:
            # the block runs on into UI text ("...12 января 2023 Написать Пожаловаться"),
            # so capture the date itself rather than everything after "с"
            m = re.search(
                r"на House\.kg с\s+(\d{1,2}\s+[а-яё]+\s+\d{4})",
                info.get_text(" ", strip=True),
                re.I,
            )
            if m:
                registered_raw = m.group(1).strip()

        return User(
            user_id=user_id,
            url=self.url_for(user_id),
            pars_date=now.isoformat(),
            name=clean_text(self.text_of(soup, Selectors.USER_NAME)),
            ads_count=to_int(self.text_of(soup, Selectors.USER_ADS_COUNT)),
            registered_raw=registered_raw,
            registered_date=self.dates.parse(registered_raw, now),
        )
