"""OCR for scanned PDFs (page images without a text layer), via ocrmypdf + Tesseract.

ocrmypdf (https://ocrmypdf.readthedocs.io/) is an external program:
``sudo apt install ocrmypdf`` on Debian/Ubuntu (it pulls in Tesseract and
Ghostscript; other languages: ``tesseract-ocr-<lang>``).

The original files are never modified. In *quick* mode only the pages the app
reads are recognised -- the first 3 and 5 sampled through the book, the same
pages pass 3 reads -- and only the recognised text is kept (``--output-type
none --sidecar``). *Full* mode recognises every page and can keep a searchable
copy in the app's own folder. Text is stored in its own table, product_ocr, so a
later "Read PDFs" run never erases it; the keyword rules, content
classification and the AI pass use it whenever the PDF itself has no text.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .db import Library, now
from .enrich import PDF_CHARS, PDF_PAGES, SAMPLE_AT, SAMPLE_CHARS, classify_from_content
from .normalize import clean_text

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_ocr (
    order_product_id TEXT PRIMARY KEY REFERENCES products(order_product_id) ON DELETE CASCADE,
    path             TEXT NOT NULL,
    mtime            REAL NOT NULL,
    mode             TEXT NOT NULL,
    text             TEXT,
    sample_text      TEXT,
    chars_per_page   REAL,
    pages_done       INTEGER,
    pdf_path         TEXT,
    error            TEXT,
    ocr_at           TEXT NOT NULL
);
"""
SCANNED_BELOW = 20  # characters per page below which a PDF counts as a scan (as in classify.py)
SKIPPED_MARK = "[OCR skipped on page"


def ensure_schema(lib: Library) -> None:
    with lib._lock, lib.conn:
        lib.conn.executescript(SCHEMA)


def command(cfg: Any) -> list[str] | None:
    """The configured ocrmypdf command, if it can be found (e.g. 'ocrmypdf' or 'python3.12 -m ocrmypdf')."""
    cmd = shlex.split(getattr(cfg, "ocr_command", "") or "ocrmypdf")
    return cmd if cmd and shutil.which(cmd[0]) else None


def page_numbers(pages: int) -> list[int]:
    """1-based pages that pass 3 reads: the first PDF_PAGES plus SAMPLE_AT fractions."""
    first = set(range(1, min(pages, PDF_PAGES) + 1))
    sampled = {int(pages * f) + 1 for f in SAMPLE_AT}
    return sorted(p for p in first | sampled if 1 <= p <= pages)


def candidates(lib: Library, force: bool = False) -> list[dict[str, Any]]:
    """Scanned PDFs (read by pass 3, almost no text) not yet OCR'd for their current file."""
    ensure_schema(lib)
    rows = lib._q(
        """SELECT t.order_product_id, t.path, t.mtime, t.pages, t.meta_json, p.name,
                  o.path AS o_path, o.mtime AS o_mtime, o.error AS o_error
           FROM product_text t JOIN products p USING(order_product_id)
           LEFT JOIN product_ocr o USING(order_product_id)
           WHERE p.removed=0 AND t.error IS NULL AND t.pages > 0"""
    )
    out = []
    for r in rows:
        cpp = json.loads(r["meta_json"] or "{}").get("chars_per_page")
        if cpp is None or cpp >= SCANNED_BELOW:
            continue
        if not force and r["o_path"] == r["path"] and r["o_mtime"] == r["mtime"]:
            continue
        out.append(dict(r))
    return out


def ocr_book(cmd: list[str], path: Path, pages: int, *, mode: str = "quick", language: str = "eng",
             keep_pdf: Path | None = None, timeout: int = 1800) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="dtrpg-ocr-") as tmp:
        side = Path(tmp) / "text.txt"
        args = [*cmd, "-q", "--skip-text", "-l", language, "--sidecar", str(side)]
        wanted = page_numbers(pages) if mode == "quick" else list(range(1, pages + 1))
        if mode == "quick":
            args += ["--pages", ",".join(map(str, wanted))]
        if keep_pdf is not None:
            keep_pdf.parent.mkdir(parents=True, exist_ok=True)
            args += ["--output-type", "pdf", str(path), str(keep_pdf)]
        else:
            args += ["--output-type", "none", str(path), "-"]
        proc = subprocess.run(args, capture_output=True, timeout=timeout, text=True)
        if proc.returncode not in (0, 6):  # 6 = page already has text (fine with --skip-text)
            raise RuntimeError((proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[-300:])
        raw = side.read_text(encoding="utf-8", errors="replace") if side.exists() else ""
    chunks = raw.split("\f")
    texts = [c.strip() for c in chunks if c.strip() and not c.strip().startswith(SKIPPED_MARK)]
    first, rest = texts[:PDF_PAGES], texts[PDF_PAGES:]
    joined = lambda xs, n: clean_text(" ".join(" ".join(x.split()) for x in xs)[:n])  # noqa: E731
    return {
        "text": joined(first, PDF_CHARS),
        "sample_text": joined(rest, SAMPLE_CHARS),
        "chars_per_page": round(sum(len(t) for t in texts) / max(1, len(wanted)), 1),
        "pages_done": len(wanted),
    }


def run(lib: Library, cfg: Any, log: Callable[[str], None] = print, *, mode: str = "quick", force: bool = False,
        cancel: Any = None, ocr_dir: Path | None = None, limit: int | None = None) -> dict[str, Any]:
    cmd = command(cfg)
    if not cmd:
        raise RuntimeError("OCR needs ocrmypdf. Install it with: sudo apt install ocrmypdf "
                           "(then restart the app), or set the OCR command in Settings.")
    todo = candidates(lib, force)[: limit or None]
    lang = getattr(cfg, "ocr_language", "") or "eng"
    keep = bool(getattr(cfg, "ocr_keep_pdf", False)) and mode == "full"
    log(f"{len(todo)} scanned PDF(s) to OCR ({mode} mode, language {lang}"
        + (", keeping searchable copies" if keep else "") + ")")
    stats = {"todo": len(todo), "done": 0, "failed": 0, "with_text": 0}
    started = time.monotonic()
    done_ids: list[str] = []
    for i, r in enumerate(todo, 1):
        if cancel is not None and cancel.is_set():
            log("Stopped by user")
            break
        path = Path(r["path"])
        if not path.exists():
            continue
        safe = "".join(c if c.isalnum() or c == "-" else "_" for c in r["order_product_id"])
        keep_to = (ocr_dir / f"{safe}.pdf") if keep and ocr_dir else None
        try:
            res = ocr_book(cmd, path, r["pages"], mode=mode, language=lang, keep_pdf=keep_to)
            err = None
            stats["done"] += 1
            stats["with_text"] += bool(res["text"] or res["sample_text"])
        except (subprocess.SubprocessError, OSError, RuntimeError) as e:
            res, err = {}, clean_text(f"{type(e).__name__}: {e}"[:300])
            stats["failed"] += 1
            log(f"OCR failed for {r['name']}: {err}")
        lib._x(
            """INSERT OR REPLACE INTO product_ocr (order_product_id, path, mtime, mode, text, sample_text,
                   chars_per_page, pages_done, pdf_path, error, ocr_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (r["order_product_id"], r["path"], r["mtime"], mode, res.get("text"), res.get("sample_text"),
             res.get("chars_per_page"), res.get("pages_done"), str(keep_to) if keep_to and not err else None,
             err, now()),
        )
        done_ids.append(r["order_product_id"])
        if i % 5 == 0 or i == len(todo):
            per = (time.monotonic() - started) / i
            log(f"{i}/{len(todo)} OCR'd; about {per:.0f}s per book, ~{per * (len(todo) - i) / 60:.0f} min left")
    if done_ids:
        res = classify_from_content(lib, opids=done_ids)
        log(f"Content classification of OCR'd books: {res['product_types']} type and {res['tags']} tag suggestions")
        stats["suggested"] = res["product_types"] + res["tags"]
    log(f"OCR finished: {stats}")
    return stats


def status(lib: Library, cfg: Any) -> dict[str, Any]:
    ensure_schema(lib)
    done = lib._q("SELECT COUNT(*), SUM(error IS NOT NULL) FROM product_ocr")[0]
    return {"available": command(cfg) is not None, "command": getattr(cfg, "ocr_command", "ocrmypdf"),
            "language": getattr(cfg, "ocr_language", "eng"), "pending": len(candidates(lib)),
            "done": done[0] or 0, "failed": done[1] or 0}
