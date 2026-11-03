"""Pass 4: tag suggestions from Claude Haiku 5.5 via the Message Batches API.

API usage follows the Anthropic Python SDK documentation (anthropic 2.x):
* batches: ``client.messages.batches.create / retrieve / results``; 50% of
  standard price, most finish within an hour, max 24 h
  (https://platform.claude.com/docs/en/build-with-claude/batch-processing)
* structured output: ``output_config={"format": {"type": "json_schema", ...}}``;
  supported on Claude Haiku 5.5; no numeric min/max in the schema, so ranges are
  validated here (https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
* Haiku 5.5 thinks by default and thinking counts toward max_tokens; effort
  ``low`` suits classification.

Results are only ever queued as suggestions (source ``llm``) for review.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from .db import PRODUCT_TYPES, TAG_KINDS, Library, now

MODEL = "claude-haiku-5-5"
# USD per million tokens, Claude Haiku 5.5, prompts up to 100K tokens
# (bundled Claude API reference, model table cached 2026-10-06). Batch = 50%.
PRICE_IN, PRICE_OUT = 0.10, 0.50
BATCH_DISCOUNT = 0.5
EST_OUTPUT_TOKENS = 350  # JSON answer + low-effort thinking; refined after the first batch
MAX_TOKENS = 2048
MIN_CONFIDENCE = 0.3
PDF_EXCERPT_CHARS = 3000

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(TAG_KINDS)},
                    "name": {"type": "string"},
                    "new": {"type": "boolean"},
                    "confidence": {"type": "number"},
                },
                "required": ["kind", "name", "new", "confidence"],
                "additionalProperties": False,
            },
        },
        "product_type": {"anyOf": [{"type": "string", "enum": list(PRODUCT_TYPES)}, {"type": "null"}]},
        "min_players": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "max_players": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    },
    "required": ["tags", "product_type", "min_players", "max_players"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You classify tabletop roleplaying game products from one person's DriveThruRPG library so they can browse and pick games.

Judge only from the information given for each product. Suggest tags that describe the product itself:
- genre: fiction genre or tone (e.g. dark fantasy, cosmic horror, space opera)
- system: the rules system or edition the product is written for
- setting: a named campaign world or setting
- category: what kind of content it is beyond its product type (e.g. bestiary, random tables, hexcrawl, dungeon)
- tag: other facets useful for choosing what to play (e.g. solo, one-shot, sandbox, investigation)

Prefer names from the existing vocabulary below, spelled exactly as listed. Propose a new name (new: true) only when nothing existing fits and the concept is specific enough to be useful for browsing. Do not repeat tags the product already has.

confidence is a number from 0 to 1: about 0.9 when the text states it directly, 0.6-0.8 when it is strongly implied, lower when it is a guess. Leave out anything below 0.3.

product_type is one of the listed values, or null if unclear. min_players and max_players only when the text states a player count; otherwise null.

Existing vocabulary:
"""


def _vocab(lib: Library) -> str:
    lines = []
    tags = lib.tags()
    for kind in TAG_KINDS:
        names = sorted({t["name"] for t in tags if t["kind"] == kind}, key=str.lower)[:400]
        lines.append(f"{kind}: {'; '.join(names) if names else '(none yet)'}")
    lines.append(f"product_type values: {', '.join(PRODUCT_TYPES)}")
    return "\n".join(lines)


def system_prompt(lib: Library) -> str:
    return INSTRUCTIONS + _vocab(lib)


def _product_rows(lib: Library) -> list[dict[str, Any]]:
    rows = lib._q(
        """SELECT p.order_product_id, p.name, p.publisher, p.description, m.product_type,
                  t.text AS pdf_text, t.pages
           FROM products p JOIN product_meta m USING(order_product_id)
           LEFT JOIN product_text t USING(order_product_id) WHERE p.removed=0 AND p.archived=0"""
    )
    items = [dict(r) for r in rows]
    lib._attach_tags(items)
    return items


def _filter_paths(lib: Library) -> dict[str, list[str]]:
    flt = {r["id"]: dict(r) for r in lib._q("SELECT id, name, parent_id, group_name FROM dtrpg_filters")}
    out: dict[str, list[str]] = {}
    for r in lib._q("SELECT order_product_id, filter_id FROM product_dtrpg_filters"):
        f, parts, seen = flt.get(r["filter_id"]), [], set()
        while f and f["id"] not in seen:
            seen.add(f["id"])
            parts.append(f["name"])
            f = flt.get(f["parent_id"])
        group = flt.get(r["filter_id"], {}).get("group_name") or ""
        if group == "Format":
            continue
        out.setdefault(r["order_product_id"], []).append(f"{group}: {' > '.join(reversed(parts))}")
    return out


def user_message(p: dict[str, Any], paths: list[str], include_pdf: bool) -> str:
    parts = [f"Title: {p['name']}", f"Publisher: {p.get('publisher') or 'unknown'}"]
    if paths:
        parts.append("DriveThruRPG categories: " + "; ".join(sorted(set(paths))))
    have = [f"{t['kind']}: {t['name']}" for t in p.get("tags", [])]
    parts.append("Already tagged: " + ("; ".join(have) if have else "nothing"))
    if p.get("product_type"):
        parts.append(f"Product type already set: {p['product_type']}")
    if p.get("description"):
        parts.append(f"Description: {p['description']}")
    if include_pdf and p.get("pdf_text"):
        parts.append(f"First pages of the PDF ({p.get('pages') or '?'} pages total): {p['pdf_text'][:PDF_EXCERPT_CHARS]}")
    return "\n".join(parts)


def candidates(lib: Library, scope: str = "thin", include_pdf: bool = True) -> list[dict[str, Any]]:
    """Products to classify, with their prompt and content hash.

    scope "thin": fewer than two genre/system/setting tags, or no product type.
    scope "all":  every product. Either way, products already classified with
    identical input (same hash) or waiting in an unfinished batch are skipped.
    """
    paths = _filter_paths(lib)
    done = {r[0]: r[1] for r in lib._q("SELECT order_product_id, content_hash FROM llm_done")}
    pending: set[str] = set()
    for r in lib._q("SELECT summary_json FROM llm_batches WHERE status NOT IN ('imported', 'failed')"):
        pending.update(v[0] for v in json.loads(r[0] or "{}").get("products", {}).values())
    out = []
    for p in _product_rows(lib):
        core = [t for t in p["tags"] if t["kind"] in ("genre", "system", "setting")]
        if scope == "thin" and len(core) >= 2 and p.get("product_type"):
            continue
        msg = user_message(p, paths.get(p["order_product_id"], []), include_pdf)
        h = hashlib.sha1(msg.encode()).hexdigest()
        if done.get(p["order_product_id"]) == h or p["order_product_id"] in pending:
            continue
        out.append({"order_product_id": p["order_product_id"], "name": p["name"], "message": msg, "hash": h})
    return out


def estimate(lib: Library, scope: str = "thin", include_pdf: bool = True) -> dict[str, Any]:
    cands = candidates(lib, scope, include_pdf)
    sys_tokens = len(system_prompt(lib)) / 3.5
    in_tokens = sum(sys_tokens + len(c["message"]) / 3.5 for c in cands)
    out_tokens = EST_OUTPUT_TOKENS * len(cands)
    standard = in_tokens / 1e6 * PRICE_IN + out_tokens / 1e6 * PRICE_OUT
    return {
        "model": MODEL,
        "products": len(cands),
        "est_input_tokens": int(in_tokens),
        "est_output_tokens": int(out_tokens),
        "est_cost_usd_standard": round(standard, 4),
        "est_cost_usd_batch": round(standard * BATCH_DISCOUNT, 4),
        "note": "Upper-bound estimate (~3.5 characters per token; prompt caching not counted). "
                "Haiku 5.5: $0.10 / $0.50 per million input / output tokens; batches are billed at 50%.",
        "sample": [c["name"] for c in cands[:10]],
    }


def make_client(api_key: str | None):
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("The anthropic package is not installed: pip install 'dtrpg-manager[llm]'") from e
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Set an Anthropic API key in Settings (or ANTHROPIC_API_KEY)")
    return anthropic.Anthropic(api_key=key)


def submit(lib: Library, client: Any, scope: str = "thin", include_pdf: bool = True, limit: int | None = None) -> dict:
    cands = candidates(lib, scope, include_pdf)[: limit or None]
    if not cands:
        return {"submitted": 0}
    system = [{"type": "text", "text": system_prompt(lib), "cache_control": {"type": "ephemeral"}}]
    requests, mapping = [], {}
    for c in cands:
        cid = f"p{c['order_product_id']}"[:64]
        mapping[cid] = [c["order_product_id"], c["hash"]]
        requests.append({
            "custom_id": cid,
            "params": {
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "system": system,
                "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                "messages": [{"role": "user", "content": c["message"]}],
            },
        })
    batch = client.messages.batches.create(requests=requests)
    lib._x(
        "INSERT INTO llm_batches (id, created_at, model, request_count, status, summary_json) VALUES (?,?,?,?,?,?)",
        (batch.id, now(), MODEL, len(requests), batch.processing_status, json.dumps({"products": mapping})),
    )
    return {"submitted": len(requests), "batch_id": batch.id, "status": batch.processing_status}


def batches(lib: Library) -> list[dict[str, Any]]:
    out = []
    for r in lib._q("SELECT * FROM llm_batches ORDER BY created_at DESC LIMIT 20"):
        d = dict(r)
        summary = json.loads(d.pop("summary_json") or "{}")
        summary.pop("products", None)
        d["summary"] = summary
        out.append(d)
    return out


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    tags = []
    for t in data.get("tags") or []:
        try:
            kind, name, conf = t["kind"], str(t["name"]).strip(), float(t["confidence"])
        except (KeyError, TypeError, ValueError):
            continue
        if kind in TAG_KINDS and 0 < len(name) <= 60 and conf >= MIN_CONFIDENCE:
            tags.append({"kind": kind, "name": name, "new": bool(t.get("new")), "confidence": min(conf, 1.0)})
    pt = data.get("product_type")
    lo, hi = data.get("min_players"), data.get("max_players")
    players = None
    if isinstance(lo, int) and 1 <= lo <= 30:
        hi = hi if isinstance(hi, int) and lo <= hi <= 30 else lo
        players = f"{lo}-{hi}"
    return {"tags": tags, "product_type": pt if pt in PRODUCT_TYPES else None, "players": players}


def collect(lib: Library, client: Any, batch_id: str) -> dict[str, Any]:
    row = lib._q("SELECT * FROM llm_batches WHERE id=?", (batch_id,))
    if not row:
        raise ValueError(f"Unknown batch {batch_id}")
    if row[0]["status"] == "imported":
        return {"status": "imported", "already": True}
    batch = client.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        lib._x("UPDATE llm_batches SET status=? WHERE id=?", (batch.processing_status, batch_id))
        rc = batch.request_counts
        return {"status": batch.processing_status, "processing": rc.processing, "succeeded": rc.succeeded}
    mapping = json.loads(row[0]["summary_json"] or "{}").get("products", {})
    current_types = {r[0]: r[1] for r in lib._q("SELECT order_product_id, product_type FROM product_meta")}
    stats = {"succeeded": 0, "errored": 0, "refused": 0, "unparsable": 0, "suggestions": 0,
             "input_tokens": 0, "output_tokens": 0}
    for res in client.messages.batches.results(batch_id):
        opid, h = mapping.get(res.custom_id, [None, None])
        if opid is None:
            continue
        if res.result.type != "succeeded":
            stats["errored"] += 1
            continue
        msg = res.result.message
        usage = getattr(msg, "usage", None)
        stats["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        stats["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        if msg.stop_reason == "refusal":
            stats["refused"] += 1
            continue
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            data = _clean(json.loads(text))
        except (ValueError, TypeError):
            stats["unparsable"] += 1
            continue
        stats["succeeded"] += 1
        for t in data["tags"]:
            if not lib.is_rejected(opid, t["kind"], t["name"]):
                stats["suggestions"] += lib.suggest(opid, t["kind"], t["name"], "llm", t["confidence"], t["new"])
        if data["product_type"] and data["product_type"] != current_types.get(opid) \
                and not lib.is_rejected(opid, "product_type", data["product_type"]):
            stats["suggestions"] += lib.suggest(opid, "product_type", data["product_type"], "llm", 0.6)
        if data["players"] and not lib.is_rejected(opid, "players", data["players"]):
            stats["suggestions"] += lib.suggest(opid, "players", data["players"], "llm", 0.6)
        lib._x("INSERT OR REPLACE INTO llm_done VALUES (?,?,?,?)", (opid, h, batch_id, now()))
    cost = (stats["input_tokens"] / 1e6 * PRICE_IN + stats["output_tokens"] / 1e6 * PRICE_OUT) * BATCH_DISCOUNT
    stats["approx_cost_usd"] = round(cost, 4)
    lib._x(
        "UPDATE llm_batches SET status='imported', imported_at=?, summary_json=? WHERE id=?",
        (now(), json.dumps({"products": mapping, **stats}), batch_id),
    )
    return {"status": "imported", **stats}
