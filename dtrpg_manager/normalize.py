"""Turn raw API product dicts into flat records.

Only these product keys are confirmed by glujan/drpg (``drpg/types.py``):
``productId``, ``name``, ``orderProductId``, ``fileLastModified``,
``publisher.name`` and ``files[].{index, filename, checksums[].{checksum, checksumDate}}``.

Everything else (purchase date, covers, file sizes, and especially the
genre/category "filters" returned when ``getFilters=1``) is *not* documented for
the vBeta API. Lookups for those keys therefore try several candidate names,
including the snake_case names documented for the older v1 API in
jramboz/DTRPG_API ``Products.md`` (``filters_name``, ``filters_id``,
``parent_id``, ``cover_url``, ``date_purchased`` ...). The full raw JSON is kept
in the database so records can be re-parsed once the real structure is known --
run ``dtrpg-manager diagnose`` to dump it.
"""

from __future__ import annotations

import html
import re
from typing import Any


def first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        cur: Any = d
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, ""):
            return cur
    return default


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _text(v: Any) -> str | None:
    return html.unescape(str(v)).strip() if v not in (None, "") else None


def newest_checksum(checksums: Any) -> tuple[str | None, str | None]:
    if isinstance(checksums, dict):
        checksums = [checksums]
    if not isinstance(checksums, list) or not checksums:
        return None, None
    best = max(
        (c for c in checksums if isinstance(c, dict)),
        key=lambda c: str(first(c, "checksumDate", "checksum_date", default="")),
        default=None,
    )
    if not best:
        return None, None
    return first(best, "checksum"), first(best, "checksumDate", "checksum_date")


def normalize_files(raw: dict[str, Any]) -> list[dict[str, Any]]:
    files = first(raw, "files", "downloads", default=[]) or []
    if isinstance(files, dict):
        files = list(files.values())
    out = []
    for pos, f in enumerate(files):
        if not isinstance(f, dict):
            continue
        idx = _int(first(f, "index", "bundle_id", "bundleId"))
        checksum, checksum_date = newest_checksum(first(f, "checksums", default=[]))
        out.append(
            {
                "idx": idx if idx is not None else pos,
                "filename": _text(first(f, "filename", "fileName", "name")) or f"file-{pos}",
                "checksum": checksum,
                "checksum_date": checksum_date,
                "size": _int(first(f, "raw_filesize_bytes", "filesize", "fileSize", "size", "bytes")),
                "last_modified": first(f, "lastModified", "last_modified"),
            }
        )
    return out


_FILTER_LIST_KEYS = ("filters", "productFilters", "filterValues", "categories", "genres")


def normalize_filters(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract DTRPG category/genre filters, whatever their exact shape."""
    found: list[Any] = []
    for key in _FILTER_LIST_KEYS:
        v = raw.get(key)
        if isinstance(v, list):
            found.extend(v)
        elif isinstance(v, dict):
            # either {id: {...}} / {id: name} or {"Genre": [..]} groupings
            for k, sub in v.items():
                if isinstance(sub, dict):
                    found.append({"filterId": k, **sub})
                elif isinstance(sub, list):
                    for s in sub:
                        found.append({"group": k, **s} if isinstance(s, dict) else {"group": k, "name": s})
                else:
                    found.append({"filterId": k, "name": sub})
    out: dict[str, dict[str, Any]] = {}
    for f in found:
        if isinstance(f, str):
            f = {"name": f}
        if not isinstance(f, dict):
            continue
        name = _text(first(f, "filters_name", "filterName", "name", "title", "label", "value"))
        if not name:
            continue
        fid = first(f, "filters_id", "filterId", "filter_id", "id")
        parent = first(f, "parent_id", "parentId", "parentFilterId", default=None)
        group = _text(first(f, "group", "parentName", "parent_name", "category", "type"))
        fid = str(fid) if fid is not None else f"name:{(group + '/') if group else ''}{name}"
        parent = str(parent) if parent not in (None, 0, "0", "") else None
        out[fid] = {"id": fid, "name": name, "parent_id": parent, "group_name": group}
    return list(out.values())


def normalize_product(raw: dict[str, Any]) -> dict[str, Any]:
    opid = first(raw, "orderProductId", "order_product_id", "id")
    pid = first(raw, "productId", "products_id", "product_id")
    if opid is None:
        opid = pid
    if opid is None:
        raise ValueError(f"Product without an id: {str(raw)[:200]}")
    archived = first(raw, "archived", "is_archived", default=0)
    return {
        "order_product_id": str(opid),
        "product_id": str(pid) if pid is not None else None,
        "name": _text(first(raw, "name", "products_name", "productName")) or f"Product {opid}",
        "publisher": _text(
            raw["publisher"].get("name")
            if isinstance(raw.get("publisher"), dict)
            else first(raw, "publisherName", "publishers_name", "publisher")
        ),
        "date_purchased": first(raw, "datePurchased", "date_purchased", "orderDate", "purchaseDate"),
        "file_last_modified": first(raw, "fileLastModified", "last_modified", "lastModified"),
        "archived": 1 if str(archived).lower() in ("1", "true") else 0,
        "cover_url": first(
            raw,
            "cover_url_fullsize",
            "coverUrlFullsize",
            "cover_url",
            "coverUrl",
            "cover",
            "image",
            "thumbnail",
            "products_thumbnail100",
        ),
        "files": normalize_files(raw),
        "filters": normalize_filters(raw),
    }


_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_path_part(part: str) -> str:
    """Make a string safe as a single path component on Windows/macOS/Linux.

    Same idea as ``PathNormalizer.normalize`` in glujan/drpg ``drpg/sync.py``,
    with extra guards against ``..`` and empty names.
    """
    part = html.unescape(part)
    part = _WIN_BAD.sub(" - ", part)
    part = re.sub(r"( - )+", " - ", part)
    part = re.sub(r"\s+", " ", part).strip(" .-")
    return part[:150] or "_"
