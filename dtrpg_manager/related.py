"""Relationships and proximity between products.

* similar(): cosine similarity over TF-IDF-weighted features -- DriveThruRPG
  categories (leaf full weight, ancestors half), user tags, publisher, and words
  from title and description. Each hit explains itself with the strongest
  shared features. Pure Python; the index is rebuilt when the library changes.
* same_line(): same publisher and a shared title prefix (the text before
  ":" / " - " / "(" or the first two or more significant words).
* Manual links are stored in product_links (see Library.add_link).
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter, defaultdict
from typing import Any

from .db import Library

STOP = set("""a an and are as at be by for from has have in into is it its of on or that the this to with your you
our their they them we will can more new all one two three edition volume vol part book books game games rpg
roleplaying role playing pdf supplement adventure adventures rules rule core set pack collection guide""".split())
WORD = re.compile(r"[a-z0-9][a-z0-9'&+-]{2,}")
SEP = re.compile(r"\s*(?::| - | – | — |\(|\[|,)\s*")
WEIGHTS = {"f": 1.0, "fa": 0.5, "tag": 1.0, "system": 1.4, "setting": 1.4, "pub": 0.6, "t": 0.7, "d": 0.25}
GENERIC_FILTERS = {"Roleplaying", "Miscellaneous", "Other systems", "Generic", "PDF", "Digital",
                   "Human-Created Without AI", "Creation Method"}


def _words(text: str | None) -> list[str]:
    return [w for w in WORD.findall((text or "").lower()) if w not in STOP and not w.isdigit()]


class RelatedIndex:
    def __init__(self, lib: Library) -> None:
        self.lib = lib
        self._lock = threading.Lock()
        self._fp: tuple | None = None
        self.vec: dict[str, dict[str, float]] = {}
        self.inv: dict[str, list[tuple[str, float]]] = {}
        self.label: dict[str, str] = {}

    def _fingerprint(self) -> tuple:
        q = self.lib._q
        return (
            tuple(q("SELECT COUNT(*), MAX(last_synced) FROM products WHERE removed=0")[0]),
            q("SELECT COUNT(*) FROM product_tags")[0][0],
            q("SELECT COUNT(*) FROM tags")[0][0],
        )

    def _ensure(self) -> None:
        fp = self._fingerprint()
        with self._lock:
            if fp == self._fp:
                return
            self._build()
            self._fp = fp

    def _build(self) -> None:
        q = self.lib._q
        raw: dict[str, Counter] = defaultdict(Counter)
        label: dict[str, str] = {}
        products = q("SELECT order_product_id, name, publisher, description FROM products WHERE removed=0")
        live = {r["order_product_id"] for r in products}
        flt = {r["id"]: dict(r) for r in q("SELECT id, name, parent_id, group_name FROM dtrpg_filters")}
        for r in q("SELECT order_product_id, filter_id FROM product_dtrpg_filters"):
            if r["order_product_id"] not in live:
                continue
            f, first, seen = flt.get(r["filter_id"]), True, set()
            while f and f["id"] not in seen:
                seen.add(f["id"])
                if f["group_name"] != "Format" and f["name"] not in GENERIC_FILTERS:
                    key = f"f:{f['id']}"
                    label[key] = f"{f['group_name']}: {f['name']}"
                    w = WEIGHTS["f"] if first else WEIGHTS["fa"]
                    raw[r["order_product_id"]][key] = max(raw[r["order_product_id"]][key], w)
                first = False
                f = flt.get(f["parent_id"])
        for r in q("""SELECT pt.order_product_id, t.id, t.kind, t.name FROM product_tags pt
                      JOIN tags t ON t.id=pt.tag_id"""):
            if r["order_product_id"] in live:
                key = f"tag:{r['id']}"
                label[key] = f"{r['kind']}: {r['name']}"
                raw[r["order_product_id"]][key] = WEIGHTS.get(r["kind"], WEIGHTS["tag"])
        for r in products:
            opid = r["order_product_id"]
            if r["publisher"]:
                key = f"pub:{r['publisher'].lower()}"
                label[key] = f"publisher: {r['publisher']}"
                raw[opid][key] = WEIGHTS["pub"]
            for w in set(_words(r["name"])):
                label[f"t:{w}"] = f"title: {w}"
                raw[opid][f"t:{w}"] = WEIGHTS["t"]
            for w in set(_words(r["description"])):
                label.setdefault(f"d:{w}", f"description: {w}")
                raw[opid][f"d:{w}"] = max(raw[opid][f"d:{w}"], WEIGHTS["d"])
        n = max(1, len(live))
        df = Counter(k for feats in raw.values() for k in feats)
        vec, inv = {}, defaultdict(list)
        for opid, feats in raw.items():
            v = {}
            for k, w in feats.items():
                # unique features can't link anything; near-universal ones carry no signal
                if df[k] < 2 or (n >= 20 and df[k] > n * 0.5):
                    continue
                v[k] = w * (math.log(n / df[k]) + 0.1)
            norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
            v = {k: x / norm for k, x in v.items()}
            vec[opid] = v
            for k, x in v.items():
                inv[k].append((opid, x))
        self.vec, self.inv, self.label = vec, dict(inv), label

    def similar(self, opid: str, n: int = 12, min_score: float = 0.05) -> list[dict[str, Any]]:
        self._ensure()
        v = self.vec.get(opid)
        if not v:
            return []
        scores: dict[str, float] = defaultdict(float)
        for k, x in v.items():
            for other, y in self.inv.get(k, ()):
                if other != opid:
                    scores[other] += x * y
        top = sorted(((s, o) for o, s in scores.items() if s >= min_score), reverse=True)[:n]
        out = []
        for s, other in top:
            ov = self.vec[other]
            shared = sorted(((v[k] * ov[k], k) for k in v.keys() & ov.keys()), reverse=True)[:3]
            out.append({"order_product_id": other, "score": round(s, 3),
                        "why": [self.label.get(k, k) for _, k in shared]})
        return out

    def scores_for(self, opid: str) -> dict[str, float]:
        return {r["order_product_id"]: r["score"] for r in self.similar(opid, n=10**6, min_score=0.0)}


def series_key(name: str) -> str | None:
    """Normalised series prefix of a title, or None."""
    head = SEP.split(name, maxsplit=1)[0].strip().lower()
    head = re.sub(r"[^a-z0-9& ]+", " ", head)
    words = [w for w in head.split() if w]
    if len(words) >= 2 or (len(words) == 1 and len(words[0]) >= 5 and words[0] not in STOP):
        return " ".join(words[:4])
    return None


def same_line(lib: Library, opid: str) -> list[dict[str, Any]]:
    row = lib._q("SELECT name, publisher FROM products WHERE order_product_id=?", (opid,))
    if not row or not row[0]["publisher"]:
        return []
    name, pub = row[0]["name"], row[0]["publisher"]
    mine = series_key(name)
    my_words = [w for w in re.sub(r"[^a-z0-9& ]+", " ", name.lower()).split()]
    out = []
    for r in lib._q(
        "SELECT order_product_id, name FROM products WHERE publisher=? AND removed=0 AND order_product_id<>?",
        (pub, opid),
    ):
        other = series_key(r["name"])
        words = re.sub(r"[^a-z0-9& ]+", " ", r["name"].lower()).split()
        common = 0
        for a, b in zip(my_words, words):
            if a != b:
                break
            common += 1
        significant = [w for w in my_words[:common] if w not in STOP]
        if (mine and other and (mine == other or other.startswith(mine) or mine.startswith(other))) or (
            common >= 2 and significant
        ):
            out.append({"order_product_id": r["order_product_id"], "name": r["name"]})
    return sorted(out, key=lambda x: x["name"].lower())


def related(lib: Library, index: RelatedIndex, opid: str, n: int = 12) -> dict[str, Any]:
    sims = index.similar(opid, n)
    line = same_line(lib, opid)
    ids = {s["order_product_id"] for s in sims} | {x["order_product_id"] for x in line}
    info = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in lib._q(
            f"""SELECT p.order_product_id, p.name, p.publisher, p.thumb_url, m.status FROM products p
                JOIN product_meta m USING(order_product_id) WHERE p.order_product_id IN ({marks})""",
            list(ids),
        ):
            info[r["order_product_id"]] = dict(r)
    return {
        "similar": [{**info.get(s["order_product_id"], {}), **s} for s in sims],
        "same_line": [{**info.get(x["order_product_id"], {}), **x} for x in line],
        "links": lib.links(opid),
    }
