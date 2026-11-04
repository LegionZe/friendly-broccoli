"""Tidy titles of local books derived from file names.

Patterns come from a real local library (3,584 files, 2026-11-04): publisher
catalogue numbers ("TSR 9483 - ...", "WEG40129 ...", "01640 - ..."), list
numbering ("(03)", "2. "), trailing release stamps ("eBook 22Jan10",
"2020-07-22 Update", "FINAL", "v1.2"), separators used instead of spaces, and
ALL-CAPS or all-lowercase names. Anything a rule can't recover (run-together
words, numeric names) is left for the AI pass, which also sees the PDF text.
"""

from __future__ import annotations

import re

# catalogue prefixes that identify a publisher
CODE_PUBLISHERS = {
    "TSR": "TSR", "FASA": "FASA", "WEG": "West End Games", "FGU": "Fantasy Games Unlimited",
    "GDW": "Game Designers' Workshop", "SJG": "Steve Jackson Games", "ICE": "Iron Crown Enterprises",
    "WOTC": "Wizards of the Coast", "WW": "White Wolf", "PAL": "Palladium Books",
    "CHA": "Chaosium", "MUH": "Modiphius Entertainment",
}
_CODE_WORDS = "|".join(CODE_PUBLISHERS)
# a known prefix + number, any 2-4 letters + 4-7 digits (e.g. MUH052213), or a bare 4-7 digit number
LEAD_CODE = re.compile(
    rf"^(?:(?P<pub>{_CODE_WORDS})\s*-?\s*\d{{2,7}}[A-Z]?|[A-Z]{{2,4}}\d{{4,7}}[A-Z]?|\d{{4,7}})\s*(?:[-–:]\s*|\s+(?=[A-Za-z]))",
    re.I)
ANY_CODE = re.compile(rf"\b(?P<pub>{_CODE_WORDS})\s*-?\s*\d{{3,7}}\b", re.I)
LEAD_NUMBERING = re.compile(r"^(?:\(\d{1,3}\)\s*|\d{1,3}[.)]\s+)")
TRAILING_JUNK = re.compile(
    r"(?:[\s_\-–:,]*(?:\b(?:e-?book|final|ocr(?:ed)?|bookmarked|hq|lq|hi-?res|low-?res|print(?:er)?[- ]friendly|"
    r"web|screen|update[d]?|v\s?\d+(?:\.\d+)*|ver\s?\d+(?:\.\d+)*|\d{4}-\d{2}(?:-\d{2})?|\d{1,2}[a-z]{3}\d{2,4}|"
    r"\d{8}|copy)\b|\(\d+\)))+$",
    re.I,
)
SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs", "with", "from"}
_ROMAN = re.compile(r"^(?=[ivxlcdm]+$)m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$", re.I)


def _title_case(text: str, from_caps: bool = False) -> str:
    words = text.split(" ")
    out = []
    for i, w in enumerate(words):
        core = w.strip("()[]-–:,.!?'\"")
        if not core:
            out.append(w)
        elif from_caps and len(core) <= 3 and core.lower() not in SMALL:
            out.append(w)  # acronym in an ALL-CAPS title: TBZ, RPG, GM
        elif core.isalpha() and len(core) <= 4 and not re.search(r"[aeiouy]", core, re.I):
            out.append(w.upper())  # vowel-less abbreviations: gm, rq, tng, pf
        elif _ROMAN.match(core) and len(core) <= 4:
            out.append(w.upper())
        elif i and core.lower() in SMALL and not out[-1].endswith((":", "-", "–")):
            out.append(w.lower())
        elif any(c.isdigit() for c in core) and any(c.isalpha() for c in core) and len(core) <= 6:
            out.append(w.upper())  # module codes: gaz10 -> GAZ10, b2 -> B2
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def clean_name(raw: str) -> tuple[str, str | None]:
    """Return (tidy title, publisher implied by a catalogue code or None)."""
    name = raw.strip()
    name = re.sub(r"\.(pdf|epub|cbz|cbr|mobi|azw3|djvu|tmp)$", "", name, flags=re.I)
    publisher = None
    if m := ANY_CODE.search(name):
        publisher = CODE_PUBLISHERS.get(m.group("pub").upper())
    name = LEAD_NUMBERING.sub("", name)
    if (m := LEAD_CODE.match(name)) and len(name) - m.end() >= 3:
        name = name[m.end():]
    # separators used instead of spaces (no spaces at all, or underscores)
    if " " not in name and name.count("-") >= 2:
        name = name.replace("-", " ")
    name = name.replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    stripped = TRAILING_JUNK.sub("", name).strip(" -–:,")
    if len(stripped) >= 3:
        name = stripped
    letters = [c for c in name if c.isalpha()]
    if letters and not _run_together(name):
        if all(c.isupper() for c in letters) and len(letters) > 6:
            name = _title_case(name, from_caps=True)
        elif all(c.islower() for c in letters):
            name = _title_case(name)
    name = re.sub(r"\s+", " ", name).strip(" -–:,")
    return name or raw.strip(), publisher


def _run_together(name: str) -> bool:
    """Lowercase words long enough to be several words glued together ("intothenight")."""
    return any(len(w) >= 11 and w.isalpha() and w.islower() for w in re.split(r"[\s\-_]+", name))


def needs_ai(name: str) -> bool:
    """Names a rule can't fix: run-together words, temp-file or numeric names, or too short."""
    letters = sum(c.isalpha() for c in name)
    return (letters < 3 or re.fullmatch(r"[\d\s\-_.]+", name) is not None or _run_together(name)
            or re.fullmatch(r"acr[0-9a-f]{3,}", name, re.I) is not None)
