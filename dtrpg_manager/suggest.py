"""Random game suggestion."""

from __future__ import annotations

import random
from typing import Any

from .db import Library

# Statuses that make a product a better "play something new" pick.
STATUS_WEIGHT = {
    "want-to-play": 3.0,
    "unread": 1.5,
    "reading": 1.2,
    "read": 1.0,
    "playing": 0.8,
    "played": 0.5,
    "shelved": 0.2,
}


def suggest(
    lib: Library,
    filters: dict[str, Any],
    *,
    count: int = 1,
    weighted: bool = True,
    avoid_recent: int = 10,
    rng: random.Random | None = None,
    record: bool = True,
) -> list[dict[str, Any]]:
    """Pick ``count`` distinct products matching ``filters``.

    With ``weighted`` on, favourites and "want-to-play"/unread items are more
    likely, rated items get a small boost, and the last ``avoid_recent``
    suggestions are skipped unless nothing else matches.
    """
    rng = rng or random.SystemRandom()
    pool = lib.candidate_ids(filters)
    if not pool:
        return []
    recent = set(lib.recent_suggestions(avoid_recent)) if avoid_recent else set()
    fresh = [c for c in pool if c["order_product_id"] not in recent]
    pool = fresh or pool

    def weight(c: dict[str, Any]) -> float:
        if not weighted:
            return 1.0
        w = STATUS_WEIGHT.get(c["status"], 1.0)
        if c["favourite"]:
            w *= 2
        if c["rating"]:
            w *= 1 + c["rating"] / 10
        return w

    picks: list[dict[str, Any]] = []
    remaining = list(pool)
    while remaining and len(picks) < count:
        choice = rng.choices(remaining, weights=[weight(c) for c in remaining], k=1)[0]
        remaining.remove(choice)
        picks.append(choice)
    out = []
    for c in picks:
        if record:
            lib.record_suggestion(c["order_product_id"])
        prod = lib.product(c["order_product_id"])
        if prod:
            prod.pop("raw", None)
            out.append(prod)
    return out
