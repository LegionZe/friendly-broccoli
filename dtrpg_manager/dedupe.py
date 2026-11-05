"""Duplicate products by title (e.g. a DriveThruRPG purchase and a local copy).

Titles are compared after the same tidying used for local names (catalogue
numbers, version/date stamps, "eBook", capitals), ignoring case, punctuation,
"&"/"and" and a leading "The/A/An". In each group the *keeper* is the copy with
the most tags; ties go to the DriveThruRPG copy (richest metadata), then the one
with more files downloaded. On a real library of 5,980 products this found 257
groups and would hide 342 copies (2026-11-05). Very generic titles ("Rulebook",
"Character Sheet", "Map", ...) are never grouped: different products share them.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .names import clean_name

GENERIC = {
    "rulebook", "rules", "core rules", "character sheet", "character sheets", "sheet", "map", "maps",
    "cover", "front", "back", "index", "errata", "handouts", "player handouts", "reference", "quick reference",
    "reference sheet", "gm screen", "screen", "tokens", "preview", "readme", "credits", "book", "pdf",
    "author", "contents", "table of contents", "character record", "untitled",
}


def title_key(name: str) -> str | None:
    n = clean_name(name or "")[0].casefold().replace("&", " and ")
    n = re.sub(r"[^\w\s]|_", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"^(?:the|a|an) ", "", n)
    if len(n) < 4 or n in GENERIC:
        return None
    return n


def groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """rows: order_product_id, name, source, tag_count, downloaded. Returns key -> members, keeper first."""
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if (k := title_key(r["name"])):
            by_key[k].append(r)
    out = {}
    for k, members in by_key.items():
        if len(members) > 1:
            members.sort(key=lambda r: (-r["tag_count"], r["source"] != "dtrpg", -r["downloaded"], r["order_product_id"]))
            out[k] = members
    return out
