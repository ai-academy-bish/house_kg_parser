"""Thread-safe HTTP client with retries and back-off.

`requests.Session` is *not* thread-safe, so each worker thread gets its own via
`threading.local()`. Sharing one session across a pool silently corrupts
connection state under load — a classic and hard-to-debug scraper bug.
"""

from __future__ import annotations

import threading
import time
from typing import Final

import requests

from .config import HttpConfig
from .logging_utils import get_logger

logger = get_logger(__name__)

#: Statuses worth waiting on rather than giving up.
RETRYABLE: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class HttpClient:
    """Fetches pages and binary assets, one session per thread.

    Retries with a linear back-off, and backs off harder when the server signals
    throttling (429/503). A 404 is final and returns None immediately — retrying
    it only wastes politeness budget.
    """

    def __init__(self, config: HttpConfig) -> None:
        self.config = config
        self._local = threading.local()

    # -- session -----------------------------------------------------------

    @property
    def session(self) -> requests.Session:
        """The calling thread's own session (created on first use)."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": self.config.user_agent,
                    "Accept-Language": "ru,en;q=0.9",
                }
            )
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.config.workers * 2,
                pool_maxsize=self.config.workers * 2,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session

    # -- fetching ----------------------------------------------------------

    def get(self, url: str, **kwargs: object) -> requests.Response | None:
        """GET with retries. Returns None on 404 or on exhausted retries."""
        for attempt in range(self.config.max_retries):
            try:
                response = self.session.get(url, timeout=self.config.timeout, **kwargs)
                if response.status_code == 200:
                    if self.config.delay:
                        time.sleep(self.config.delay)
                    return response
                if response.status_code == 404:
                    logger.debug("404 %s", url)
                    return None
                if response.status_code in RETRYABLE:
                    backoff = 3 * (attempt + 1)
                    logger.debug(
                        "HTTP %s on %s — backing off %ss", response.status_code, url, backoff
                    )
                    time.sleep(backoff)
                    continue
                logger.debug("HTTP %s on %s", response.status_code, url)
            except requests.RequestException as exc:
                logger.debug("request failed (%s): %s", type(exc).__name__, url)
            time.sleep(1.5 * (attempt + 1))

        logger.warning("giving up after %d attempts: %s", self.config.max_retries, url)
        return None

    def get_text(self, url: str) -> str | None:
        response = self.get(url)
        return response.text if response is not None else None

    def get_bytes(self, url: str) -> bytes | None:
        response = self.get(url)
        return response.content if response is not None else None
