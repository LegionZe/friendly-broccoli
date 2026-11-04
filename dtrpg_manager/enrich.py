"""Automatic tag enrichment, passes 1-3 (pass 4, the Haiku classifier, is in llm.py).

1. ``apply_dtrpg``   -- map DriveThruRPG categories onto user tags (applied directly,
                        source ``dtrpg``; deterministic).
2. ``run_rules``     -- regex rules over title / publisher / description / PDF text
                        (queued as suggestions for review, source ``rule:<id>``).
3. ``extract_pdfs``  -- read page count, PDF metadata and the first pages' text of
                        each product's main downloaded PDF (feeds passes 2 and 4).

Invariants: manual tags are never removed or downgraded; anything removed or
rejected by the user is remembered and not re-added (see Library.remove_tag /
Library.suggest).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from .db import PRODUCT_TYPES, TAG_KINDS, Library, now
from .normalize import clean_text

logger = logging.getLogger(__name__)

# Group names, parent/child structure and labels below are those observed in a
# live order_products response (2026-11-03): parentName is one of
# "Genre", "Rule System", "Product Type", "Format".
DEFAULT_CONFIG: dict[str, Any] = {
    "dtrpg": {
        # DTRPG top-level group -> user tag kind ("product_type" sets the field; null = ignore)
        "groups": {"Genre": "genre", "Rule System": "system", "Product Type": "product_type", "Format": None},
        # also add every ancestor category (Dark Fantasy -> Fantasy)
        "include_ancestors": True,
        # umbrella labels that carry no information as tags
        "skip": [
            "Miscellaneous", "Other systems", "Other unique systems", "Custom Systems", "Generic",
            "Cross-genre/Multi-genre", "Roleplaying", "Supplements & Expansions", "Core Rulebooks",
            "Media & Software", "Maps & Miniatures", "Setting Guides",
        ],
        # rename/merge DTRPG labels, e.g. {"Storyteller / Storytelling": "World of Darkness"}
        "rename": {},
        # DTRPG Product Type label -> product_type value (deepest match wins)
        "product_types": {
            "Core Rulebooks": "core-rules", "All-in-One Rulebooks": "core-rules",
            "Quickstarts & Starter Sets": "core-rules", "Live Action (LARP) Core": "core-rules",
            "Supplements & Expansions": "supplement",
            "Campaigns, Adventures & Modules": "adventure",
            "Setting Guides": "setting", "Campaign World Books": "setting", "Other Locations": "setting",
            "Maps & Miniatures": "map", "Visual Aids": "map", "Cards, Props, & Visual Aids": "tool",
            "Stock Art": "art", "RPG Fiction": "fiction", "Magazines & Zines": "other",
            "Media & Software": "tool",
        },
    },
    # Pass 2. fields: name, publisher, description, pdf_text. kind: a tag kind,
    # "product_type", or "players" (pattern must capture min and optionally max).
    "rules": [
        {"id": "dnd5e", "pattern": r"\b5e\b|\bD&D\s*5|\bdnd\s*5e?\b|\bSRD\s*5\.\d|fifth edition (?:fantasy|compatible)",
         # other games with a "5th edition" (observed: "Ars Magica 5E")
         "exclude": r"Ars Magica|Shadowrun|Call of Cthulhu|Vampire|Werewolf|\bMage\b|Warhammer|Paranoia",
         "kind": "system", "name": "D&D 5e"},
        {"id": "pf2e", "pattern": r"\bPathfinder\s*(?:2e|second edition|2nd edition)\b|\bPF2e?\b",
         "kind": "system", "name": "Pathfinder 2e"},
        {"id": "osr", "pattern": r"\bOSR\b|old[- ]school (?:renaissance|revival)", "kind": "tag", "name": "OSR"},
        {"id": "pbta", "pattern": r"powered by the apocalypse|\bPbtA\b", "kind": "system", "name": "PbtA"},
        {"id": "fitd", "pattern": r"forged in the dark|\bFitD\b", "kind": "system", "name": "Forged in the Dark"},
        {"id": "solo", "pattern": r"\bsolo (?:play|game|rpg|adventure|journal)|single[- ]player|\bsolitaire\b",
         "kind": "tag", "name": "solo"},
        {"id": "gmless", "pattern": r"\bGM[- ]?less\b|\bno GM\b|without a (?:GM|game ?master)",
         "kind": "tag", "name": "GM-less"},
        {"id": "oneshot", "pattern": r"\bone[- ]shots?\b", "kind": "tag", "name": "one-shot"},
        {"id": "zine", "pattern": r"\bzine\b", "kind": "tag", "name": "zine"},
        # maps: titles are the reliable signal; descriptions of adventures often
        # mention included maps, so "map" product type is set from the title only
        {"id": "battlemap", "kind": "category", "name": "battlemap", "fields": ["name", "description"],
         "pattern": r"\bbattle[- ]?maps?\b|\b(?:encounter|tactical|combat|gridded) maps?\b"},
        {"id": "mappack", "kind": "category", "name": "map pack", "fields": ["name"],
         "pattern": r"\bmaps? ?(?:packs?|sets?|bundles?|collections?|folios?)\b|\b(?:vtt|printable) maps?\b|\bmaps of\b|\bmaps\s*$"},
        {"id": "geomorph", "kind": "category", "name": "geomorphs", "fields": ["name", "description"],
         "pattern": r"\bgeomorphs?\b|\b(?:dungeon|map|hex) tiles?\b"},
        {"id": "floorplans", "kind": "category", "name": "floorplans", "fields": ["name"],
         "pattern": r"\bfloor ?plans?\b"},
        {"id": "tokens", "kind": "category", "name": "tokens", "fields": ["name"],
         "pattern": r"\btokens?\b|\bstandees?\b|\bpaper minis?\b"},
        # any title naming a map (not "mapping"/"mapper"); in a real library of ~2,400
        # titles these were all map products except "The Book of Treasure Maps"
        {"id": "maptype", "kind": "product_type", "name": "map", "fields": ["name"],
         "pattern": r"\bbattle[- ]?maps?\b|\bmaps?\b|\bfloor ?plans?\b|\bgeomorphs?\b|\b(?:dungeon|map|hex) tiles?\b",
         "exclude": r"\btreasure maps?\b"},
        {"id": "vtt", "kind": "tag", "name": "VTT-ready", "fields": ["name", "description"],
         "pattern": r"\bVTT\b|\bvirtual table ?top\b|\bFoundry ?VTT\b|\bRoll20\b|\bFantasy Grounds\b|\bOwlbear Rodeo\b"},
        {"id": "charsheet", "pattern": r"\bcharacter sheets?\b", "kind": "product_type", "name": "character-sheet",
         "fields": ["name"]},
        {"id": "players", "kind": "players",
         "pattern": r"\b(?:for\s+)?(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s+players\b|\bfor\s+(\d{1,2})\s+players\b"},
    ],
    "rule_confidence": {"name": 0.8, "publisher": 0.7, "description": 0.7, "pdf_text": 0.5},
}

DEFAULT_FIELDS = ["name", "description", "pdf_text"]


# -- config -------------------------------------------------------------------
def config_path(cfg_dir: Path) -> Path:
    return cfg_dir / "enrich.json"


def load_config(cfg_dir: Path) -> dict[str, Any]:
    conf = copy.deepcopy(DEFAULT_CONFIG)
    p = config_path(cfg_dir)
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(user.get("rules"), list):
            # merge by id: saved rules win; built-in rules added later still appear
            # (switch one off with "disabled": true rather than deleting it)
            have = {r.get("id") for r in user["rules"]}
            user["rules"] = user["rules"] + [r for r in conf["rules"] if r["id"] not in have]
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(conf.get(k), dict):
                conf[k].update(v)
            else:
                conf[k] = v
    return conf


def save_config(cfg_dir: Path, conf: dict[str, Any]) -> None:
    validate_config(conf)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    config_path(cfg_dir).write_text(json.dumps(conf, indent=2, ensure_ascii=False), encoding="utf-8")


def validate_config(conf: dict[str, Any]) -> None:
    kinds = set(TAG_KINDS) | {"product_type", "players"}
    for kind in conf["dtrpg"]["groups"].values():
        if kind is not None and kind not in kinds:
            raise ValueError(f"dtrpg.groups: unknown kind {kind!r}")
    for v in conf["dtrpg"]["product_types"].values():
        if v not in PRODUCT_TYPES:
            raise ValueError(f"dtrpg.product_types: {v!r} is not one of {PRODUCT_TYPES}")
    ids = set()
    for r in conf["rules"]:
        if r.get("id") in ids or not r.get("id"):
            raise ValueError(f"rule ids must be unique and non-empty: {r.get('id')!r}")
        ids.add(r["id"])
        if r.get("kind") not in kinds:
            raise ValueError(f"rule {r['id']}: unknown kind {r.get('kind')!r}")
        if r["kind"] != "players" and not r.get("name"):
            raise ValueError(f"rule {r['id']}: name is required")
        if r["kind"] == "product_type" and r["name"] not in PRODUCT_TYPES:
            raise ValueError(f"rule {r['id']}: {r['name']!r} is not a product type")
        try:
            re.compile(r["pattern"], re.I)
            if r.get("exclude"):
                re.compile(r["exclude"], re.I)
        except re.error as e:
            raise ValueError(f"rule {r['id']}: bad pattern: {e}") from e


# -- pass 1 -------------------------------------------------------------------
def apply_dtrpg(lib: Library, conf: dict[str, Any], opids: list[str] | None = None) -> dict[str, int]:
    d = conf["dtrpg"]
    groups, skip, rename, ptypes = d["groups"], set(d["skip"]), d["rename"], d["product_types"]
    flt = {r["id"]: dict(r) for r in lib._q("SELECT id, name, parent_id, group_name FROM dtrpg_filters")}

    def chain(fid: str) -> list[dict[str, Any]]:
        out, seen = [], set()
        while fid in flt and fid not in seen:
            seen.add(fid)
            out.append(flt[fid])
            if not d["include_ancestors"]:
                break
            fid = flt[fid]["parent_id"]
        return out  # leaf first

    rows = lib._q("SELECT order_product_id, filter_id FROM product_dtrpg_filters")
    by_product: dict[str, list[str]] = {}
    for r in rows:
        if opids is None or r["order_product_id"] in opids:
            by_product.setdefault(r["order_product_id"], []).append(r["filter_id"])
    stats = {"products": 0, "tags_added": 0, "product_types_set": 0}
    current_type = {r[0]: r[1] for r in lib._q("SELECT order_product_id, product_type FROM product_meta")}
    for opid, fids in by_product.items():
        stats["products"] += 1
        best_type: tuple[int, str] | None = None
        for fid in fids:
            for depth, f in enumerate(reversed(chain(fid))):  # root first -> depth grows to leaf
                kind = groups.get(f["group_name"] or "")
                if not kind:
                    continue
                if kind == "product_type":
                    if f["name"] in ptypes and (best_type is None or depth >= best_type[0]):
                        best_type = (depth, ptypes[f["name"]])
                    continue
                if f["name"] in skip:
                    continue
                name = rename.get(f["name"], f["name"])
                if lib.has_tag(opid, kind, name) or lib.is_rejected(opid, kind, name):
                    continue
                lib.add_tag(opid, kind, name, source="dtrpg", confidence=1.0)
                stats["tags_added"] += 1
        if best_type and not current_type.get(opid) and not lib.is_rejected(opid, "product_type", best_type[1]):
            lib.update_meta(opid, {"product_type": best_type[1]})
            stats["product_types_set"] += 1
    return stats


# -- pass 2 -------------------------------------------------------------------
def _texts(lib: Library, opids: list[str] | None) -> list[dict[str, Any]]:
    rows = lib._q(
        """SELECT p.order_product_id, p.name, p.publisher, p.description, t.text AS pdf_text
           FROM products p LEFT JOIN product_text t USING(order_product_id) WHERE p.removed=0"""
    )
    return [dict(r) for r in rows if opids is None or r["order_product_id"] in opids]


def _players(m: re.Match) -> str | None:
    nums = [int(g) for g in m.groups() if g]
    if not nums or any(n < 1 or n > 30 for n in nums):
        return None
    lo, hi = (nums[0], nums[1]) if len(nums) > 1 else (nums[0], nums[0])
    return f"{min(lo, hi)}-{max(lo, hi)}"


def run_rules(
    lib: Library, conf: dict[str, Any], *, preview: bool = False, opids: list[str] | None = None
) -> dict[str, Any]:
    """Apply regex rules. With ``preview`` nothing is written; returns per-rule match counts."""
    rules = [(r, re.compile(r["pattern"], re.I), re.compile(r["exclude"], re.I) if r.get("exclude") else None)
             for r in conf["rules"] if not r.get("disabled")]
    weights = conf.get("rule_confidence", {})
    counts = {r["id"]: 0 for r, _, _ in rules}
    examples: dict[str, list[str]] = {r["id"]: [] for r, _, _ in rules}
    queued = 0
    for p in _texts(lib, opids):
        for rule, rx, ex in rules:
            hit = None
            for field in rule.get("fields", DEFAULT_FIELDS):
                text = p.get(field)
                if text and (m := rx.search(text)) and not (ex and ex.search(text)):
                    hit = (field, m)
                    break
            if not hit:
                continue
            field, m = hit
            name = _players(m) if rule["kind"] == "players" else rule["name"]
            if not name or lib.is_rejected(p["order_product_id"], rule["kind"], name):
                continue
            if rule["kind"] in TAG_KINDS and lib.has_tag(p["order_product_id"], rule["kind"], name):
                continue
            counts[rule["id"]] += 1
            if len(examples[rule["id"]]) < 5:
                examples[rule["id"]].append(f"{p['name']} [{field}: …{m.group(0)}…]")
            if not preview:
                queued += lib.suggest(
                    p["order_product_id"], rule["kind"], name, f"rule:{rule['id']}", weights.get(field, 0.6)
                )
    # tidy names of local books (from file names) and publishers implied by catalogue codes
    from .names import clean_name

    counts["names"] = counts["publishers"] = 0
    examples["names"], examples["publishers"] = [], []
    for r in lib._q("SELECT order_product_id, name, publisher FROM products WHERE source='local' AND removed=0"):
        if opids is not None and r["order_product_id"] not in opids:
            continue
        new, pub = clean_name(r["name"])
        for kind, value, cur in (("name", new, r["name"]), ("publisher", pub, r["publisher"])):
            if not value or value == cur or (kind == "publisher" and cur):
                continue
            if lib.is_rejected(r["order_product_id"], kind, value):
                continue
            counts[kind + "s"] += 1
            if len(examples[kind + "s"]) < 5:
                examples[kind + "s"].append(f"{r['name']} → {value}")
            if not preview:
                queued += lib.suggest(r["order_product_id"], kind, value, "rule:names", 0.8 if kind == "name" else 0.7)
    return {"matches": counts, "examples": examples, "queued": queued, "preview": preview}


# -- pass 3 -------------------------------------------------------------------
PDF_PAGES = 3          # opening pages kept as `text` (rules, AI excerpt)
PDF_CHARS = 8000
SAMPLE_AT = (0.15, 0.3, 0.5, 0.7, 0.85)   # further pages sampled through the book
SAMPLE_CHARS = 12000
OUTLINE_MAX = 300
EXTRACT_VERSION = 2    # bump to re-read every PDF once after a change here


def _outline_titles(reader: Any) -> list[str]:
    out: list[str] = []

    def walk(items: Any) -> None:
        for it in items:
            if len(out) >= OUTLINE_MAX:
                return
            if isinstance(it, list):
                walk(it)
            else:
                t = str(getattr(it, "title", "") or "").strip()
                if t:
                    out.append(clean_text(re.sub(r"\s+", " ", t)[:150]))

    try:
        walk(reader.outline)
    except Exception:
        pass
    return out


def extract_pdfs(
    lib: Library, log: Callable[[str], None] = print, *, force: bool = False, cancel: Any = None
) -> dict[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        import sys
        raise RuntimeError(f"pypdf is not installed. Run: {sys.executable} -m pip install pypdf — then restart the app") from e
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    rows = lib._q(
        """SELECT order_product_id, local_path, size FROM files
           WHERE local_path IS NOT NULL AND lower(local_path) LIKE '%.pdf' ORDER BY order_product_id, size DESC"""
    )
    main: dict[str, str] = {}
    for r in rows:  # largest PDF per product = main book
        main.setdefault(r["order_product_id"], r["local_path"])
    done = {r[0]: (r[1], r[2], r[3]) for r in lib._q("SELECT order_product_id, path, mtime, version FROM product_text")}
    stats = {"pdfs": len(main), "extracted": 0, "skipped": 0, "errors": 0}
    log(f"{len(main)} products have a downloaded PDF")
    for i, (opid, path) in enumerate(main.items(), 1):
        if cancel is not None and cancel.is_set():
            break
        pth = Path(path)
        if not pth.exists():
            stats["skipped"] += 1
            continue
        mtime = pth.stat().st_mtime
        if not force and done.get(opid) == (path, mtime, EXTRACT_VERSION):
            stats["skipped"] += 1
            continue
        pages = meta = text = err = sample = outline = None
        try:
            reader = PdfReader(str(pth))
            if reader.is_encrypted:
                reader.decrypt("")
            pages = len(reader.pages)
            info = reader.metadata or {}
            meta = {k: clean_text(str(info.get(k))) for k in ("/Title", "/Subject", "/Keywords", "/Author") if info.get(k)}
            parts = [page.extract_text() or "" for page in reader.pages[:PDF_PAGES]]
            text = clean_text(re.sub(r"\s+", " ", " ".join(parts)).strip()[:PDF_CHARS])
            idx = sorted({int(pages * f) for f in SAMPLE_AT} - set(range(PDF_PAGES)))
            samples = [reader.pages[i].extract_text() or "" for i in idx if i < pages]
            sample = clean_text(re.sub(r"\s+", " ", " ".join(samples)).strip()[:SAMPLE_CHARS])
            read = parts + samples
            meta["chars_per_page"] = round(sum(len(x) for x in read) / max(1, len(read)), 1)
            outline = json.dumps(_outline_titles(reader), ensure_ascii=False)
            stats["extracted"] += 1
        except Exception as e:  # malformed PDFs are common; record and move on
            err = clean_text(f"{type(e).__name__}: {e}"[:300])
            stats["errors"] += 1
        sql = """INSERT OR REPLACE INTO product_text
               (order_product_id, path, mtime, pages, meta_json, text, error, extracted_at, outline, sample_text, version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)"""
        try:
            lib._x(sql, (opid, clean_text(path), mtime, pages, json.dumps(meta) if meta else None, text, err, now(),
                         outline, sample, EXTRACT_VERSION))
        except Exception as e:  # never let one PDF stop the whole pass
            stats["errors"] += 1
            log(f"Could not store text of {Path(path).name!r}: {type(e).__name__}: {e}")
            lib._x(sql, (opid, clean_text(path), mtime, pages, None, None, clean_text(f"store failed: {e}"[:300]),
                         now(), None, None, EXTRACT_VERSION))
        if i % 25 == 0:
            log(f"{i}/{len(main)} PDFs read")
    log(f"PDF pass finished: {stats}")
    res = classify_from_content(lib)
    d = res["diagnostics"]
    log(f"Content classification: {res['product_types']} product-type and {res['tags']} tag suggestions queued")
    log(f"  of {d['products']} products: {d['already_typed']} already have a type, {d['type_suggested']} got a "
        f"suggestion, {d['undecided']} undecided (left for the AI pass)")
    log(f"  {d['no_pdf_text']} have no PDF text (not a PDF / not downloaded / unreadable; title only), "
        f"{d['scanned_no_text_layer']} are scans without a text layer, {d['with_bookmarks']} have bookmarks")
    stats.update({f"suggested_{k}": v for k, v in res.items()})
    return stats


def classify_from_content(lib: Library, opids: list[str] | None = None, preview: bool = False) -> dict[str, Any]:
    """Queue product-type (and solo/gamebook) suggestions from each PDF's content.

    Only products without a product type get a type suggestion, so DTRPG's own
    Product Type (pass 1) and your choices are never second-guessed."""
    from .classify import classify

    # every live product: with PDF text where pass 3 read it, title-only otherwise
    rows = lib._q(
        """SELECT p.order_product_id, p.name, m.product_type, t.text, t.sample_text, t.outline, t.pages, t.meta_json,
                  t.error
           FROM products p JOIN product_meta m USING(order_product_id)
           LEFT JOIN product_text t USING(order_product_id)
           WHERE p.removed=0"""
    )
    out = {"product_types": 0, "tags": 0, "examples": []}
    diag = {"products": 0, "already_typed": 0, "no_pdf_text": 0, "scanned_no_text_layer": 0, "with_bookmarks": 0,
            "type_suggested": 0, "undecided": 0}
    for r in rows:
        if opids is not None and r["order_product_id"] not in opids:
            continue
        meta = json.loads(r["meta_json"] or "{}") if not r["error"] else {}
        outline = json.loads(r["outline"] or "[]") if not r["error"] else []
        text = f"{r['text'] or ''} {r['sample_text'] or ''}" if not r["error"] else ""
        res = classify(r["name"], outline, text, r["pages"] if not r["error"] else None, meta.get("chars_per_page"))
        opid = r["order_product_id"]
        diag["products"] += 1
        diag["no_pdf_text"] += r["text"] is None
        diag["scanned_no_text_layer"] += bool(res.get("scanned"))
        diag["with_bookmarks"] += bool(outline)
        if r["product_type"]:
            diag["already_typed"] += 1
        elif res["product_type"]:
            diag["type_suggested"] += 1
        else:
            diag["undecided"] += 1
        if res["product_type"] and not r["product_type"]:
            ptype, conf, ev = res["product_type"]
            if not lib.is_rejected(opid, "product_type", ptype):
                out["product_types"] += 1
                if len(out["examples"]) < 8:
                    out["examples"].append(f"{r['name']} → {ptype} ({ev})")
                if not preview:
                    lib.suggest(opid, "product_type", ptype, "content", conf, evidence=ev)
        for kind, name, conf, ev in res["tags"]:
            if not lib.has_tag(opid, kind, name) and not lib.is_rejected(opid, kind, name):
                out["tags"] += 1
                if not preview:
                    lib.suggest(opid, kind, name, "content", conf, evidence=ev)
    out["diagnostics"] = diag
    return out
