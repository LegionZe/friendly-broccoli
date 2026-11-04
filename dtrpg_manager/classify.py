"""Guess what kind of book a PDF is from its own content.

Inputs come from pass 3 (enrich.extract_pdfs): the title, the PDF bookmarks
(outline), text from the first pages plus pages sampled through the book, the
page count and the amount of text per page.

Each product type has *strong* signals (specific to that kind of book) and
*weak* ones (common in any RPG book). A type needs at least one strong signal,
and must clearly beat the runner-up, before anything is suggested: a wrong
suggestion costs the reviewer more than no suggestion, and books the rules
can't settle are left for the AI pass. Calibrated on 33 PDFs from a real
library (2026-11-04). Results go to the review queue (source ``content``) with
the evidence, never straight onto a product.
"""

from __future__ import annotations

import re
from typing import Any

# weights: where the signal was found
W = {"title": 4.0, "outline": 2.0, "text": 1.0}
WEAK_FACTOR = 0.35

# type -> list of (label, regex, strong?)
SIGNALS: dict[str, list[tuple[str, str, bool]]] = {
    "core-rules": [
        ("character creation", r"character (?:creation|generation)|creating (?:a|your) (?:character|hero)|making (?:a|your) character", True),
        ("how to play", r"how to play|basic rules|core (?:rules|mechanics?)|the core mechanic|rolling (?:the )?dice|playing the game", True),
        ("running the game", r"(?:game ?master|referee|gm)(?:'s)? (?:section|chapter|advice|guide)|running the game|for the (?:gm|game ?master)", True),
        ("advancement", r"\b(?:experience points|character advancement|levell?ing up|gaining levels)\b", True),
        ("skills", r"\bskills?\b", False),
        ("combat", r"\bcombat\b", False),
        ("attributes", r"\b(?:attributes|ability scores|characteristics)\b", False),
        ("equipment", r"\b(?:equipment|gear)\b", False),
        ("classes/ancestries", r"\b(?:classes|races|ancestr(?:y|ies)|species|lineages|archetypes|careers)\b", False),
    ],
    "adventure": [
        ("adventure background", r"adventure (?:background|summary|synopsis|overview)|\bsynopsis\b|background for the (?:gm|game ?master)", True),
        ("read-aloud text", r"read[- ]aloud|boxed text|read (?:the following|aloud)", True),
        ("numbered areas", r"\b(?:area|room|location|keyed area) ?[a-z]?\d{1,3}\b", True),
        ("pre-generated characters", r"pre-?generated characters|\bpregens?\b", True),
        ("party level", r"(?:characters|pcs) of (?:\d+)(?:st|nd|rd|th)?[- ]level|\blevels? \d+\s?[–-]\s?\d+\b|for \d+\s?[–-]\s?\d+ (?:characters|pcs|players)", True),
        ("hooks", r"adventure hooks?|getting the (?:pcs|players|characters) involved|starting the adventure", True),
        ("encounters", r"\bencounters?\b", False),
        ("conclusion", r"\b(?:conclusion|aftermath|wrapping up)\b", False),
    ],
    "campaign": [
        ("campaign", r"\bcampaign\b", False),
        ("adventure path", r"adventure path|campaign (?:book|arc)|\bsaga\b", True),
    ],
    "setting": [
        ("gazetteer/geography", r"\b(?:gazetteer|geography|atlas)\b", True),
        ("history", r"\b(?:history of|timeline|calendar)\b", True),
        ("religion", r"\b(?:religions?|pantheon|deities)\b", True),
        ("factions", r"\b(?:factions|organi[sz]ations|nations|kingdoms|empires)\b", False),
        ("places", r"\b(?:cities|settlements|places of interest|regions)\b", False),
        ("cultures", r"\b(?:cultures?|peoples|languages)\b", False),
    ],
    "bestiary": [
        ("bestiary", r"\b(?:bestiary|monster manual|creature catalog)\b", True),
        ("monsters", r"\b(?:monsters|creatures|beasts|adversaries|critters)\b", False),
    ],
    "tool": [
        ("generator/tables", r"\b(?:generator|random tables?|d100 tables?|toolkit)\b", True),
    ],
    "fiction": [
        ("fiction", r"\b(?:a novel|short stor(?:y|ies)|anthology)\b", True),
    ],
}
TITLE_HINTS = {
    # (no "...RPG" rule: "Temple of the Weaver for Shadowdark RPG" is an adventure)
    "core-rules": r"\bcore (?:rules|book|rulebook)\b|rule ?book|basic (?:set|rules)|player'?s (?:handbook|guide)|quick ?start|starter (?:set|kit)",
    "adventure": r"\badventure\b|\bmodule\b|\bscenario\b|one[- ]shot|\bmission\b",
    "campaign": r"\bcampaign\b(?! setting)|adventure path|\bsaga\b",
    "setting": r"campaign setting|\bsetting\b|gazetteer|\batlas\b|guide to|\bworld of\b",
    "bestiary": r"bestiary|monster|creature|critters|\bfoes\b|adversar|beasts",
    "tool": r"generator|tables|toolkit|\bdeck\b",
    "character-sheet": r"character sheet|record sheet",
    "map": r"\bmaps?\b|floor ?plans?|geomorph|battle ?map",
    "fiction": r"\bnovel\b|anthology|short stories",
}
SOLO_TITLE = r"\bsolo\b|single[- ]player|\balone\b|solitaire"
SOLO_TEXT = (r"\bsolo (?:play|game|rpg|adventure|journal|mode|rules)|single[- ]player|play(?:ing)? (?:it )?alone"
             r"|by yourself|journaling (?:game|prompts?)|without a (?:gm|game ?master)|\bgm[- ]?less\b")
GAMEBOOK = r"\bturn to (?:section |paragraph |entry )?\d{1,3}\b"
STATBLOCK = r"\b(?:AC|armou?r class)\b.{0,40}\b(?:HP|hit points|hit dice)\b"


def _n(regex: str, text: str) -> int:
    return len(re.findall(regex, text, re.I)) if text else 0


def classify(title: str, outline: list[str], text: str, pages: int | None, chars_per_page: float | None) -> dict[str, Any]:
    """Return {'product_type': (type, confidence, evidence) | None, 'tags': [(kind, name, conf, evidence)], 'scores'}."""
    title = title or ""
    outline_txt = "\n".join(outline or [])
    n = pages or 0
    scores: dict[str, float] = {}
    strong: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    for ptype, sigs in SIGNALS.items():
        s, k, ev = 0.0, 0, []
        for label, rx, is_strong in sigs:
            where = "outline" if _n(rx, outline_txt) else "text" if _n(rx, text) else None
            if not where:
                continue
            w = W[where] * (1.0 if is_strong else WEAK_FACTOR)
            s += w
            k += is_strong
            ev.append(f"{'bookmarks' if where == 'outline' else 'text'}: {label}")
        scores[ptype], strong[ptype], evidence[ptype] = s, k, ev
    for ptype in ("map", "character-sheet"):
        scores.setdefault(ptype, 0.0)
        strong.setdefault(ptype, 0)
        evidence.setdefault(ptype, [])
    for ptype, rx in TITLE_HINTS.items():
        if re.search(rx, title, re.I):
            scores[ptype] += W["title"]
            strong[ptype] += 1
            evidence[ptype].insert(0, "title")

    # repetition and shape
    areas = _n(SIGNALS["adventure"][2][1], text)
    if areas >= 4:
        scores["adventure"] += 2.0
        evidence["adventure"].append(f"{areas} numbered areas in sampled pages")
    parts = len({m.lower() for m in re.findall(r"\b(?:part|act|episode|chapter|book) (?:one|two|three|four|five|\d+|i{1,3}|iv|v)\b", outline_txt, re.I)})
    if parts >= 3 and strong["adventure"] >= 2 and n >= 64:
        # several parts, each with adventure content, in a long book: a campaign outranks "adventure"
        scores["campaign"] = max(scores["campaign"], scores["adventure"]) + 3.0
        strong["campaign"] += 1
        evidence["campaign"].append(f"{parts} parts/chapters with adventure content, {n} pages")
    stats = _n(STATBLOCK, text)
    if stats >= 6:
        scores["bestiary"] += 2.0
        strong["bestiary"] += 1
        evidence["bestiary"].append(f"{stats} stat blocks in sampled pages")
    if n >= 150 and strong["core-rules"] >= 2:
        scores["core-rules"] += 1.5
        evidence["core-rules"].append(f"{n} pages")
    if chars_per_page is not None and 0 < n <= 24 and chars_per_page < 200:
        scores["map"] += 3.0
        strong["map"] += 1
        evidence["map"].append(f"little text ({int(chars_per_page)} characters/page)")
    if 0 < n <= 3 and re.search(SIGNALS["core-rules"][4][1] + "|hit points|name", text, re.I) and "sheet" in title.lower():
        scores["character-sheet"] += 3.0
        strong["character-sheet"] += 1

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    result: dict[str, Any] = {"product_type": None, "tags": [], "scores": scores}
    if n > 3 and strong.get(best, 0) >= 1 and top >= 4 and top - second >= 2.5:
        conf = round(min(0.85, 0.45 + 0.04 * (top - second) + 0.02 * top), 2)
        result["product_type"] = (best, conf, "; ".join(evidence[best][:5]))

    solo = 4 * bool(re.search(SOLO_TITLE, title, re.I)) + 2 * min(_n(SOLO_TEXT, outline_txt), 2) + min(_n(SOLO_TEXT, text), 3)
    if solo >= 3:
        result["tags"].append(("tag", "solo", round(min(0.85, 0.5 + 0.05 * solo), 2), f"solo-play wording (score {solo})"))
    gb = _n(GAMEBOOK, text) + _n(GAMEBOOK, outline_txt)
    if gb >= 4:
        result["tags"].append(("category", "gamebook", round(min(0.85, 0.55 + 0.03 * gb), 2), f'"turn to N" ×{gb}'))
    return result
