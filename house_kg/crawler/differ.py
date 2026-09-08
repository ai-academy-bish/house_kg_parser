"""Change detection between consecutive snapshots.

What counts as a change is a judgement, not a mechanism, so it lives in one place
rather than being scattered through the crawl stages.

Two rules shape everything here:

* **Monotonic counters are not changes.** `views` and `favourites` only ever grow,
  so comparing them would flag every listing on every run and bury the real
  signal. They are recorded in `listing_observations` and ignored here.
* **Derived timestamps are not changes either.** house.kg renders relative dates
  ("2 месяца назад"), which the parser resolves against the moment of reading. An
  untouched listing therefore reports an `upped_date` that drifts *forward* by
  exactly the gap between runs — a week later, "2 месяца назад" resolves a week
  later too. Comparing timestamps would call that a bump every single run.
  What actually stays put is the listing's **age**, and only a real bump can make
  an advertisement younger, so age is what is compared.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..constants import (
    CHANGE_APPEARED,
    CHANGE_BUMPED,
    CHANGE_DELISTED,
    CHANGE_FIELD,
    CHANGE_REAPPEARED,
    CHANGE_REVIEW_ADDED,
    TRACKED_LISTING_FIELDS,
)
from ..models import CardObservation, Change

#: Slack for parsing jitter: the site rounds ("23 часа" vs "1 день"), so an age
#: must drop by more than this before it counts as a bump.
BUMP_TOLERANCE = timedelta(hours=1)


class Differ:
    """Emits `Change` rows for one snapshot."""

    def __init__(
        self,
        snapshot_id: str,
        observed_at: str,
        previous_snapshot_id: str | None = None,
        tracked: tuple[str, ...] = TRACKED_LISTING_FIELDS,
    ) -> None:
        self.snapshot_id = snapshot_id
        self.observed_at = observed_at
        self.previous_snapshot_id = previous_snapshot_id
        self.tracked = tracked

    # -- listings ----------------------------------------------------------

    def listing(
        self, card: CardObservation, previous: dict[str, Any] | None
    ) -> list[Change]:
        """Changes for one listing against its previous observation.

        `previous is None` means the listing is new to the dataset — or was
        delisted earlier and has come back, which the caller distinguishes by
        calling `reappeared` instead.
        """
        if previous is None:
            return [self._change(card.house_kg_id, "listing", CHANGE_APPEARED)]

        out: list[Change] = []
        current = card.to_dict()
        for field in self.tracked:
            old, new = previous.get(field), current.get(field)
            if old != new:
                out.append(
                    self._change(
                        card.house_kg_id, "listing", CHANGE_FIELD,
                        field=field, old=old, new=new,
                    )
                )

        bump = self._bump(previous, card, self.observed_at)
        if bump:
            out.append(
                self._change(
                    card.house_kg_id, "listing", CHANGE_BUMPED,
                    field="upped_date", old=previous.get("upped_date"), new=card.upped_date,
                )
            )
        return out

    @staticmethod
    def _bump(
        previous: dict[str, Any], card: CardObservation, observed_at: str
    ) -> bool:
        """True when the advertisement got *younger* — which only a bump can do.

        Ages, not timestamps: an untouched listing showing "2 месяца назад" has the
        same age at every reading, while its resolved `upped_date` slides forward
        with the clock. Comparing timestamps would report a bump on every run for
        every listing whose bump is old enough to be rendered coarsely.
        """
        new_age = _age(observed_at, card.upped_date)
        if new_age is None:
            return False
        old_age = _age(previous.get("observed_at"), previous.get("upped_date"))
        if old_age is None:
            return True  # never bumped before, bumped now
        return new_age < old_age - BUMP_TOLERANCE

    def delisted(self, house_kg_id: str) -> Change:
        return self._change(house_kg_id, "listing", CHANGE_DELISTED)

    def reappeared(self, house_kg_id: str) -> Change:
        return self._change(house_kg_id, "listing", CHANGE_REAPPEARED)

    # -- entities and reviews ---------------------------------------------

    def entity(
        self, kind: str, slug: str, diffs: list[tuple[str, Any, Any]]
    ) -> list[Change]:
        """One row per moved rating / review-count field."""
        return [
            self._change(slug, kind, CHANGE_FIELD, field=field, old=old, new=new)
            for field, old, new in diffs
        ]

    def review_added(self, review: dict[str, Any]) -> Change:
        """A review seen for the first time.

        `review_id` is a content hash, so "first time" is genuine novelty rather
        than an artefact of re-reading the same review under a new id.
        """
        return self._change(
            str(review.get("review_id")),
            "review",
            CHANGE_REVIEW_ADDED,
            field=f"{review.get('subject_type')}:{review.get('subject_slug')}",
            new=review.get("rating"),
        )

    # -- construction ------------------------------------------------------

    def _change(
        self,
        entity_key: str,
        entity_type: str,
        change_type: str,
        field: str | None = None,
        old: Any = None,
        new: Any = None,
    ) -> Change:
        return Change(
            snapshot_id=self.snapshot_id,
            observed_at=self.observed_at,
            entity_type=entity_type,
            entity_key=entity_key,
            change_type=change_type,
            field=field,
            old_value=None if old is None else str(old),
            new_value=None if new is None else str(new),
            prev_snapshot_id=self.previous_snapshot_id,
        )


def _age(observed_at: str | None, upped_date: str | None) -> timedelta | None:
    """How long before the reading the listing was bumped. None if never."""
    if not observed_at or not upped_date:
        return None
    try:
        return datetime.fromisoformat(observed_at) - datetime.fromisoformat(upped_date)
    except ValueError:
        return None
