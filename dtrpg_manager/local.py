"""Local game books: files bought elsewhere or scanned, indexed in place.

Files are never copied, moved or modified. Each file becomes one product with
``source = 'local'`` and an id derived from its *content* (size + first and last
blocks), so renaming or moving a file keeps its tags and notes, and identical
copies in two places count once. Everything else in the app -- tags, the
auto-tag passes, related products, random picks -- works on them unchanged.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .db import Library, now

logger = logging.getLogger(__name__)

EXTENSIONS = {".pdf", ".epub", ".cbz", ".cbr", ".mobi", ".azw3", ".djvu"}
COVER_PX = 280


def content_id(path: Path) -> str:
    """Stable id from size + first 1 MiB + last 64 KiB (fast even for huge PDFs)."""
    size = path.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with path.open("rb") as fh:
        h.update(fh.read(1 << 20))
        if size > (1 << 20) + (1 << 16):
            fh.seek(-(1 << 16), os.SEEK_END)
            h.update(fh.read())
    return "local-" + h.hexdigest()[:16]


_JUNK_TITLE = re.compile(r"^(untitled|microsoft word|document\d*|scan\w*|layout \d+|\d+)\b|\.(indd|docx?|pdf)$", re.I)


def title_from_name(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem) if " " not in stem else stem  # CamelCase -> words
    stem = re.sub(r"\s*\((\d+)\)$", "", stem)  # "Book (1)"
    return re.sub(r"\s+", " ", stem).strip() or path.name


def pdf_info(path: Path, cover_dir: Path | None, pid: str) -> dict[str, Any]:
    """Title/author from PDF metadata and a cover thumbnail from the first page's first image."""
    out: dict[str, Any] = {}
    try:
        from pypdf import PdfReader

        logging.getLogger("pypdf").setLevel(logging.ERROR)
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        meta = reader.metadata or {}
        title = str(meta.get("/Title") or "").strip()
        if 3 < len(title) < 150 and not _JUNK_TITLE.search(title):
            out["title"] = title
        author = str(meta.get("/Author") or "").strip()
        if 1 < len(author) < 80:
            out["author"] = author
        if cover_dir is not None and reader.pages:
            images = list(reader.pages[0].images)
            if images:
                img = max(images, key=lambda i: len(i.data))
                out["cover"] = save_cover(img.data, cover_dir, pid)
    except Exception as e:  # malformed PDFs are common; the product still gets added
        logger.debug("pdf_info failed for %s: %s", path, e)
    return out


def save_cover(data: bytes, cover_dir: Path, pid: str) -> str | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        if im.width < 100 or im.height < 100:
            return None
        im = im.convert("RGB")
        im.thumbnail((COVER_PX, COVER_PX * 2))
        cover_dir.mkdir(parents=True, exist_ok=True)
        im.save(cover_dir / f"{pid}.jpg", "JPEG", quality=82)
        return f"/covers/{pid}.jpg"
    except Exception as e:
        logger.debug("cover failed for %s: %s", pid, e)
        return None


def iter_files(roots: list[Path], exclude: list[Path]) -> list[Path]:
    out = []
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            d = Path(dirpath)
            if any(d == ex or ex in d.parents for ex in exclude):
                dirnames[:] = []
                continue
            dirnames[:] = sorted(n for n in dirnames if not n.startswith("."))
            for name in sorted(filenames):
                if not name.startswith(".") and Path(name).suffix.lower() in EXTENSIONS:
                    out.append(d / name)
    return out


def scan(
    lib: Library,
    folders: list[str],
    *,
    exclude: list[str] = (),
    cover_dir: Path | None = None,
    log: Callable[[str], None] = print,
    cancel: Any = None,
) -> dict[str, int]:
    roots = [Path(f).expanduser().resolve() for f in folders if str(f).strip()]
    missing = [str(r) for r in roots if not r.is_dir()]
    for m in missing:
        log(f"Folder not found: {m}")
    excl = [Path(e).expanduser().resolve() for e in exclude if e]
    files = iter_files(roots, excl)
    log(f"{len(files)} book file(s) found in {len(roots) - len(missing)} folder(s)")
    known = {r["order_product_id"]: dict(r) for r in lib._q(
        """SELECT p.order_product_id, p.removed, f.local_path, f.local_size, f.last_modified
           FROM products p LEFT JOIN files f USING(order_product_id) WHERE p.source='local'""")}
    by_path = {v["local_path"]: k for k, v in known.items() if v["local_path"]}
    stats = {"found": len(files), "new": 0, "updated": 0, "unchanged": 0, "removed": 0, "errors": 0}
    seen: set[str] = set()
    for i, path in enumerate(files, 1):
        if cancel is not None and cancel.is_set():
            break
        try:
            st = path.stat()
            mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")
            prev = by_path.get(str(path))
            if prev and known[prev]["local_size"] == st.st_size and known[prev]["last_modified"] == mtime \
                    and not known[prev]["removed"]:
                seen.add(prev)
                stats["unchanged"] += 1
                continue
            pid = content_id(path)
            if pid in seen:  # identical copy elsewhere
                continue
            seen.add(pid)
            info = pdf_info(path, cover_dir, pid) if path.suffix.lower() == ".pdf" else {}
            rel = next((path.relative_to(r) for r in roots if r in path.parents), path.name)
            folder = str(Path(rel).parent) if str(Path(rel).parent) != "." else ""
            rec = {
                "order_product_id": pid, "product_id": None,
                "name": info.get("title") or title_from_name(path),
                "publisher": None, "date_purchased": None,
                "file_last_modified": mtime, "archived": 0,
                "cover_url": info.get("cover"), "thumb_url": info.get("cover"),
                "description": (f"Local file in folder: {folder}" if folder else "Local file")
                               + (f". Author: {info['author']}" if info.get("author") else ""),
                "slug": None, "filters": [],
                "files": [{"idx": 0, "filename": path.name, "checksum": None, "checksum_date": None,
                           "size": st.st_size, "last_modified": mtime}],
            }
            is_new = lib.upsert_product(rec, {"source": "local", "path": str(path)}, source="local")
            lib.mark_downloaded(pid, 0, str(path), size=st.st_size)
            stats["new" if is_new else "updated"] += 1
        except OSError as e:
            stats["errors"] += 1
            log(f"Could not read {path}: {e}")
        if i % 100 == 0:
            log(f"{i}/{len(files)} files checked")
    if not (cancel is not None and cancel.is_set()):
        # Don't drop books on a folder that is configured but currently unavailable
        # (e.g. an unplugged external drive); books from folders removed from the
        # settings, or whose file is gone from an available folder, are flagged removed.
        offline = [Path(m) for m in missing]
        gone = [k for k, v in known.items() if k not in seen and not v["removed"]
                and not any(o in Path(v["local_path"] or "/").parents for o in offline)]
        lib._x("UPDATE products SET removed=1 WHERE order_product_id IN (%s)" % ",".join("?" * len(gone)), gone) \
            if gone else None
        stats["removed"] = len(gone)
    log(f"Local scan finished: {stats}")
    return stats
