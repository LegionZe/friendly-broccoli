"""SQLite storage for the local library catalogue and user metadata."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    order_product_id   TEXT PRIMARY KEY,
    product_id         TEXT,
    name               TEXT NOT NULL,
    publisher          TEXT,
    date_purchased     TEXT,
    file_last_modified TEXT,
    archived           INTEGER NOT NULL DEFAULT 0,
    cover_url          TEXT,
    raw_json           TEXT,
    first_seen         TEXT NOT NULL,
    last_synced        TEXT NOT NULL,
    removed            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    order_product_id TEXT NOT NULL REFERENCES products(order_product_id) ON DELETE CASCADE,
    idx              INTEGER NOT NULL,
    filename         TEXT NOT NULL,
    checksum         TEXT,
    checksum_date    TEXT,
    size             INTEGER,
    last_modified    TEXT,
    local_path       TEXT,
    downloaded_at    TEXT,
    PRIMARY KEY (order_product_id, idx)
);

-- DriveThruRPG's own category/genre filters (from getFilters=1)
CREATE TABLE IF NOT EXISTS dtrpg_filters (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    parent_id  TEXT,
    group_name TEXT
);
CREATE TABLE IF NOT EXISTS product_dtrpg_filters (
    order_product_id TEXT NOT NULL REFERENCES products(order_product_id) ON DELETE CASCADE,
    filter_id        TEXT NOT NULL REFERENCES dtrpg_filters(id) ON DELETE CASCADE,
    PRIMARY KEY (order_product_id, filter_id)
);

-- User-defined classification. kind: genre | category | system | setting | tag
CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (kind, name COLLATE NOCASE)
);
CREATE TABLE IF NOT EXISTS product_tags (
    order_product_id TEXT NOT NULL REFERENCES products(order_product_id) ON DELETE CASCADE,
    tag_id           INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (order_product_id, tag_id)
);

CREATE TABLE IF NOT EXISTS product_meta (
    order_product_id TEXT PRIMARY KEY REFERENCES products(order_product_id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'unread',
    rating           INTEGER,
    favourite        INTEGER NOT NULL DEFAULT 0,
    exclude_random   INTEGER NOT NULL DEFAULT 0,
    product_type     TEXT,
    min_players      INTEGER,
    max_players      INTEGER,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS suggestions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_product_id TEXT NOT NULL REFERENCES products(order_product_id) ON DELETE CASCADE,
    suggested_at     TEXT NOT NULL
);

-- Suggested classifications awaiting review (passes 2-4). kind is a tag kind,
-- or 'product_type' / 'players' (value holds the type or "min-max").
CREATE TABLE IF NOT EXISTS tag_suggestions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_product_id TEXT NOT NULL REFERENCES products(order_product_id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    name             TEXT NOT NULL,
    source           TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.5,
    is_new           INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TEXT NOT NULL,
    UNIQUE (order_product_id, kind, name COLLATE NOCASE)
);
CREATE INDEX IF NOT EXISTS ix_sugg_status ON tag_suggestions(status, source);

-- Text pulled from the main downloaded PDF (pass 3)
CREATE TABLE IF NOT EXISTS product_text (
    order_product_id TEXT PRIMARY KEY REFERENCES products(order_product_id) ON DELETE CASCADE,
    path             TEXT NOT NULL,
    mtime            REAL NOT NULL,
    pages            INTEGER,
    meta_json        TEXT,
    text             TEXT,
    error            TEXT,
    extracted_at     TEXT NOT NULL
);

-- Haiku batch jobs (pass 4)
CREATE TABLE IF NOT EXISTS llm_batches (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    model         TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    status        TEXT NOT NULL,
    imported_at   TEXT,
    summary_json  TEXT
);
CREATE TABLE IF NOT EXISTS llm_done (
    order_product_id TEXT PRIMARY KEY REFERENCES products(order_product_id) ON DELETE CASCADE,
    content_hash     TEXT NOT NULL,
    batch_id         TEXT,
    done_at          TEXT NOT NULL
);

-- Manual relationships between products
CREATE TABLE IF NOT EXISTS product_links (
    a     TEXT NOT NULL REFERENCES products(order_product_id) ON DELETE CASCADE,
    b     TEXT NOT NULL REFERENCES products(order_product_id) ON DELETE CASCADE,
    kind  TEXT NOT NULL,
    note  TEXT,
    PRIMARY KEY (a, b, kind)
);

CREATE INDEX IF NOT EXISTS ix_products_publisher ON products(publisher);
CREATE INDEX IF NOT EXISTS ix_tags_kind ON tags(kind);
"""

TAG_SOURCES_AUTO = ("dtrpg", "rule", "pdf", "llm")
LINK_KINDS = ("related", "same-line", "supplement-of", "requires", "sequel-of")
STATUSES = ("unread", "reading", "read", "want-to-play", "playing", "played", "shelved")
TAG_KINDS = ("genre", "category", "system", "setting", "tag")
PRODUCT_TYPES = (
    "core-rules", "supplement", "adventure", "setting", "bestiary", "map", "art",
    "character-sheet", "tool", "fiction", "other",
)
META_FIELDS = {
    "status", "rating", "favourite", "exclude_random", "product_type",
    "min_players", "max_players", "notes",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Library:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(products)")}
        for col in ("thumb_url", "description", "slug"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE products ADD COLUMN {col} TEXT")
        f_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(files)")}
        for col, typ in (("local_size", "INTEGER"), ("local_md5", "TEXT"), ("failed_at", "TEXT"),
                         ("fail_reason", "TEXT")):
            if col not in f_cols:
                self.conn.execute(f"ALTER TABLE files ADD COLUMN {col} {typ}")
        pt_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(product_tags)")}
        if "source" not in pt_cols:
            self.conn.execute("ALTER TABLE product_tags ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        if "confidence" not in pt_cols:
            self.conn.execute("ALTER TABLE product_tags ADD COLUMN confidence REAL")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _q(self, sql: str, args: Any = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def _x(self, sql: str, args: Any = ()) -> sqlite3.Cursor:
        with self._lock, self.conn:
            return self.conn.execute(sql, args)

    # -- sync -------------------------------------------------------------
    def upsert_product(self, rec: dict[str, Any], raw: dict[str, Any]) -> bool:
        """Insert/update one normalised product. Returns True if it was new."""
        ts = now()
        opid = rec["order_product_id"]
        with self._lock, self.conn:
            c = self.conn
            is_new = c.execute("SELECT 1 FROM products WHERE order_product_id=?", (opid,)).fetchone() is None
            c.execute(
                """INSERT INTO products (order_product_id, product_id, name, publisher, date_purchased,
                       file_last_modified, archived, cover_url, thumb_url, description, slug,
                       raw_json, first_seen, last_synced, removed)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                   ON CONFLICT(order_product_id) DO UPDATE SET
                       product_id=excluded.product_id, name=excluded.name, publisher=excluded.publisher,
                       date_purchased=COALESCE(excluded.date_purchased, products.date_purchased),
                       file_last_modified=excluded.file_last_modified, archived=excluded.archived,
                       cover_url=COALESCE(excluded.cover_url, products.cover_url),
                       thumb_url=COALESCE(excluded.thumb_url, products.thumb_url),
                       description=COALESCE(excluded.description, products.description),
                       slug=COALESCE(excluded.slug, products.slug),
                       raw_json=excluded.raw_json, last_synced=excluded.last_synced, removed=0""",
                (
                    opid, rec["product_id"], rec["name"], rec["publisher"], rec["date_purchased"],
                    rec["file_last_modified"], rec["archived"], rec["cover_url"],
                    rec.get("thumb_url"), rec.get("description"), rec.get("slug"),
                    json.dumps(raw, ensure_ascii=False), ts, ts,
                ),
            )
            c.execute("INSERT OR IGNORE INTO product_meta (order_product_id) VALUES (?)", (opid,))
            seen = []
            for f in rec["files"]:
                seen.append(f["idx"])
                c.execute(
                    """INSERT INTO files (order_product_id, idx, filename, checksum, checksum_date, size, last_modified)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(order_product_id, idx) DO UPDATE SET
                           filename=excluded.filename, checksum=excluded.checksum,
                           checksum_date=excluded.checksum_date, size=excluded.size,
                           last_modified=excluded.last_modified""",
                    (opid, f["idx"], f["filename"], f["checksum"], f["checksum_date"], f["size"], f["last_modified"]),
                )
            marks = ",".join("?" * len(seen))
            c.execute(
                f"DELETE FROM files WHERE order_product_id=? AND idx NOT IN ({marks or 'NULL'})",
                (opid, *seen),
            )
            if rec["filters"]:
                c.execute("DELETE FROM product_dtrpg_filters WHERE order_product_id=?", (opid,))
                for flt in rec["filters"]:
                    c.execute(
                        """INSERT INTO dtrpg_filters (id, name, parent_id, group_name) VALUES (?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                               parent_id=COALESCE(excluded.parent_id, dtrpg_filters.parent_id),
                               group_name=COALESCE(excluded.group_name, dtrpg_filters.group_name)""",
                        (flt["id"], flt["name"], flt["parent_id"], flt["group_name"]),
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO product_dtrpg_filters VALUES (?,?)", (opid, flt["id"])
                    )
        return is_new

    def mark_removed_except(self, seen_ids: set[str], archived_included: bool) -> int:
        """Flag products no longer returned by the API (never deletes user metadata)."""
        rows = self._q("SELECT order_product_id, archived FROM products WHERE removed=0")
        gone = [
            r["order_product_id"]
            for r in rows
            if r["order_product_id"] not in seen_ids and (archived_included or not r["archived"])
        ]
        with self._lock, self.conn:
            self.conn.executemany("UPDATE products SET removed=1 WHERE order_product_id=?", [(g,) for g in gone])
        return len(gone)

    # -- files ------------------------------------------------------------
    def files_for(self, opid: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM files WHERE order_product_id=? ORDER BY idx", (opid,))]

    def all_files(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._q(
                """SELECT f.*, p.name AS product_name, p.publisher, p.file_last_modified
                   FROM files f JOIN products p USING(order_product_id) WHERE p.removed=0
                   ORDER BY p.name, f.idx"""
            )
        ]

    def mark_downloaded(
        self, opid: str, idx: int, local_path: str, size: int | None = None, md5: str | None = None
    ) -> None:
        """Record where a file is; ``size``/``md5`` describe the bytes this app received."""
        if size is None:
            self._x(
                "UPDATE files SET local_path=?, downloaded_at=COALESCE(downloaded_at, ?) WHERE order_product_id=? AND idx=?",
                (local_path, now(), opid, idx),
            )
        else:
            self._x(
                """UPDATE files SET local_path=?, downloaded_at=?, local_size=?, local_md5=?,
                       failed_at=NULL, fail_reason=NULL
                   WHERE order_product_id=? AND idx=?""",
                (local_path, now(), size, md5, opid, idx),
            )

    # -- download holds -----------------------------------------------------
    def mark_failed(self, opid: str, idx: int, reason: str) -> None:
        self._x("UPDATE files SET failed_at=?, fail_reason=? WHERE order_product_id=? AND idx=?",
                (now(), reason[:300], opid, idx))

    def clear_failed(self, opid: str | None = None, idx: int | None = None) -> int:
        if opid is None:
            return self._x("UPDATE files SET failed_at=NULL, fail_reason=NULL WHERE failed_at IS NOT NULL").rowcount
        return self._x("UPDATE files SET failed_at=NULL, fail_reason=NULL WHERE order_product_id=? AND idx=?",
                       (opid, idx)).rowcount

    def held_files(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q(
            """SELECT f.order_product_id, f.idx, f.filename, f.failed_at, f.fail_reason, p.name AS product_name
               FROM files f JOIN products p USING(order_product_id)
               WHERE f.failed_at IS NOT NULL ORDER BY f.failed_at DESC""")]

    # -- metadata ---------------------------------------------------------
    def update_meta(self, opid: str, changes: dict[str, Any]) -> None:
        bad = set(changes) - META_FIELDS
        if bad:
            raise ValueError(f"Unknown metadata fields: {sorted(bad)}")
        if "status" in changes and changes["status"] not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if changes.get("rating") is not None and not 0 <= int(changes["rating"]) <= 5:
            raise ValueError("rating must be 0-5")
        if not changes:
            return
        self._x("INSERT OR IGNORE INTO product_meta (order_product_id) VALUES (?)", (opid,))
        cols = ", ".join(f"{k}=?" for k in changes)
        self._x(f"UPDATE product_meta SET {cols} WHERE order_product_id=?", (*changes.values(), opid))

    def get_or_create_tag(self, kind: str, name: str) -> int:
        kind, name = kind.strip().lower(), name.strip()
        if kind not in TAG_KINDS:
            raise ValueError(f"kind must be one of {TAG_KINDS}")
        if not name:
            raise ValueError("tag name is empty")
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id FROM tags WHERE kind=? AND name=? COLLATE NOCASE", (kind, name)
            ).fetchone()
            if row:
                return row["id"]
            return self.conn.execute("INSERT INTO tags (kind, name) VALUES (?,?)", (kind, name)).lastrowid

    def add_tag(
        self, opid: str, kind: str, name: str, source: str = "manual", confidence: float | None = None
    ) -> int:
        """Attach a tag. A manual add upgrades an automatic tag to manual;
        an automatic add never overwrites an existing tag's provenance."""
        tid = self.get_or_create_tag(kind, name)
        if source == "manual":
            self._x(
                """INSERT INTO product_tags (order_product_id, tag_id, source, confidence) VALUES (?,?, 'manual', NULL)
                   ON CONFLICT(order_product_id, tag_id) DO UPDATE SET source='manual', confidence=NULL""",
                (opid, tid),
            )
        else:
            self._x(
                "INSERT OR IGNORE INTO product_tags (order_product_id, tag_id, source, confidence) VALUES (?,?,?,?)",
                (opid, tid, source, confidence),
            )
        return tid

    def remove_tag(self, opid: str, tag_id: int) -> None:
        """Remove a tag and remember the decision so automatic passes don't re-add it."""
        row = self._q("SELECT kind, name FROM tags WHERE id=?", (tag_id,))
        self._x("DELETE FROM product_tags WHERE order_product_id=? AND tag_id=?", (opid, tag_id))
        if row:
            self._x(
                """INSERT INTO tag_suggestions (order_product_id, kind, name, source, confidence, status, created_at)
                   VALUES (?,?,?, 'manual', 0, 'rejected', ?)
                   ON CONFLICT(order_product_id, kind, name) DO UPDATE SET status='rejected'""",
                (opid, row[0]["kind"], row[0]["name"], now()),
            )

    def bulk_tag(self, opids: list[str], kind: str, name: str) -> int:
        tid = self.get_or_create_tag(kind, name)
        with self._lock, self.conn:
            self.conn.executemany(
                """INSERT INTO product_tags (order_product_id, tag_id, source) VALUES (?,?, 'manual')
                   ON CONFLICT(order_product_id, tag_id) DO UPDATE SET source='manual', confidence=NULL""",
                [(o, tid) for o in opids],
            )
        return tid

    # -- suggestions (review queue) -----------------------------------------
    def is_rejected(self, opid: str, kind: str, name: str) -> bool:
        return bool(self._q(
            "SELECT 1 FROM tag_suggestions WHERE order_product_id=? AND kind=? AND name=? COLLATE NOCASE AND status='rejected'",
            (opid, kind, name),
        ))

    def has_tag(self, opid: str, kind: str, name: str) -> bool:
        return bool(self._q(
            """SELECT 1 FROM product_tags pt JOIN tags t ON t.id=pt.tag_id
               WHERE pt.order_product_id=? AND t.kind=? AND t.name=? COLLATE NOCASE""",
            (opid, kind, name),
        ))

    def suggest(
        self, opid: str, kind: str, name: str, source: str, confidence: float, is_new: bool = False
    ) -> bool:
        """Queue a suggestion unless it is already applied or was decided. Returns True if queued/updated."""
        name = name.strip()
        if not name:
            return False
        if kind in TAG_KINDS and self.has_tag(opid, kind, name):
            return False
        confidence = max(0.0, min(1.0, float(confidence)))
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO tag_suggestions (order_product_id, kind, name, source, confidence, is_new, created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(order_product_id, kind, name) DO UPDATE SET
                       confidence=MAX(tag_suggestions.confidence, excluded.confidence),
                       source=CASE WHEN excluded.confidence > tag_suggestions.confidence
                                   THEN excluded.source ELSE tag_suggestions.source END
                   WHERE tag_suggestions.status='pending'""",
                (opid, kind, name, source, confidence, 1 if is_new else 0, now()),
            )
            return cur.rowcount > 0

    def suggestions(self, f: dict[str, Any], limit: int = 200, offset: int = 0) -> dict[str, Any]:
        where, args = self._sugg_where(f)
        total = self._q(f"SELECT COUNT(*) FROM tag_suggestions s WHERE {where}", args)[0][0]
        rows = self._q(
            f"""SELECT s.*, p.name AS product_name, p.publisher, p.thumb_url FROM tag_suggestions s
                JOIN products p USING(order_product_id) WHERE {where}
                ORDER BY s.confidence DESC, p.name COLLATE NOCASE LIMIT ? OFFSET ?""",
            (*args, limit, offset),
        )
        counts = {r[0]: r[1] for r in self._q(
            "SELECT source, COUNT(*) FROM tag_suggestions WHERE status='pending' GROUP BY source")}
        return {"total": total, "items": [dict(r) for r in rows], "pending_by_source": counts}

    @staticmethod
    def _sugg_where(f: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses, args = ["s.status=?"], [f.get("status") or "pending"]
        if src := f.get("source"):
            clauses.append("s.source LIKE ?")
            args.append(src + "%")
        if kind := f.get("kind"):
            clauses.append("s.kind=?")
            args.append(kind)
        if (mc := f.get("min_confidence")) not in (None, ""):
            clauses.append("s.confidence>=?")
            args.append(float(mc))
        if opid := f.get("order_product_id"):
            clauses.append("s.order_product_id=?")
            args.append(str(opid))
        if ids := f.get("ids"):
            clauses.append(f"s.id IN ({','.join('?' * len(ids))})")
            args += [int(i) for i in ids]
        return " AND ".join(clauses), args

    def decide_suggestions(self, f: dict[str, Any], accept: bool) -> int:
        where, args = self._sugg_where({**f, "status": "pending"})
        rows = self._q(f"SELECT * FROM tag_suggestions s WHERE {where}", args)
        for r in rows:
            if accept:
                self._apply_suggestion(dict(r))
            self._x("UPDATE tag_suggestions SET status=? WHERE id=?", ("accepted" if accept else "rejected", r["id"]))
        return len(rows)

    def _apply_suggestion(self, r: dict[str, Any]) -> None:
        opid, kind, name = r["order_product_id"], r["kind"], r["name"]
        if kind in TAG_KINDS:
            self.add_tag(opid, kind, name, source=r["source"].split(":")[0], confidence=r["confidence"])
        elif kind == "product_type" and name in PRODUCT_TYPES:
            self.update_meta(opid, {"product_type": name})
        elif kind == "players":
            lo, _, hi = name.partition("-")
            self.update_meta(opid, {"min_players": int(lo), "max_players": int(hi or lo)})

    # -- links ------------------------------------------------------------------
    def add_link(self, a: str, b: str, kind: str, note: str | None = None) -> None:
        if kind not in LINK_KINDS:
            raise ValueError(f"kind must be one of {LINK_KINDS}")
        if a == b:
            raise ValueError("cannot link a product to itself")
        self._x("INSERT OR REPLACE INTO product_links VALUES (?,?,?,?)", (a, b, kind, note))

    def remove_link(self, a: str, b: str, kind: str) -> None:
        self._x("DELETE FROM product_links WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND kind=?", (a, b, b, a, kind))

    def links(self, opid: str) -> list[dict[str, Any]]:
        rows = self._q(
            """SELECT l.kind, l.note, l.a, l.b, p.order_product_id, p.name, p.publisher, p.thumb_url
               FROM product_links l JOIN products p ON p.order_product_id = CASE WHEN l.a=? THEN l.b ELSE l.a END
               WHERE l.a=? OR l.b=?""",
            (opid, opid, opid),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["direction"] = "out" if r["a"] == opid else "in"
            out.append(d)
        return out

    def delete_tag(self, tag_id: int) -> None:
        self._x("DELETE FROM tags WHERE id=?", (tag_id,))

    def rename_tag(self, tag_id: int, name: str) -> None:
        self._x("UPDATE tags SET name=? WHERE id=?", (name.strip(), tag_id))

    def tags(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._q(
                """SELECT t.id, t.kind, t.name, COUNT(pt.order_product_id) AS count
                   FROM tags t LEFT JOIN product_tags pt ON pt.tag_id=t.id
                   GROUP BY t.id ORDER BY t.kind, t.name COLLATE NOCASE"""
            )
        ]

    def dtrpg_filters(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._q(
                """SELECT f.id, f.name, f.parent_id, f.group_name, COUNT(pf.order_product_id) AS count
                   FROM dtrpg_filters f LEFT JOIN product_dtrpg_filters pf ON pf.filter_id=f.id
                   GROUP BY f.id ORDER BY f.group_name, f.name COLLATE NOCASE"""
            )
        ]

    def publishers(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._q(
                """SELECT publisher AS name, COUNT(*) AS count FROM products
                   WHERE removed=0 AND publisher IS NOT NULL GROUP BY publisher ORDER BY publisher COLLATE NOCASE"""
            )
        ]

    # -- querying ---------------------------------------------------------
    def _where(self, f: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses = ["p.removed=0"]
        args: list[Any] = []
        if not f.get("include_archived"):
            clauses.append("p.archived=0")
        if q := (f.get("q") or "").strip():
            clauses.append("(p.name LIKE ? OR p.publisher LIKE ? OR m.notes LIKE ? OR p.description LIKE ?)")
            args += [f"%{q}%"] * 4
        if pub := f.get("publisher"):
            clauses.append("p.publisher=?")
            args.append(pub)
        if st := f.get("status"):
            sts = st if isinstance(st, list) else [st]
            clauses.append(f"m.status IN ({','.join('?' * len(sts))})")
            args += sts
        if pt := f.get("product_type"):
            clauses.append("m.product_type=?")
            args.append(pt)
        if f.get("favourite"):
            clauses.append("m.favourite=1")
        if (mr := f.get("min_rating")) not in (None, ""):
            clauses.append("COALESCE(m.rating,0)>=?")
            args.append(int(mr))
        if (pl := f.get("players")) not in (None, ""):
            clauses.append("(m.min_players IS NULL OR m.min_players<=?) AND (m.max_players IS NULL OR m.max_players>=?)")
            args += [int(pl), int(pl)]
        if dl := f.get("downloaded"):
            sub = "EXISTS (SELECT 1 FROM files x WHERE x.order_product_id=p.order_product_id AND x.local_path IS NOT NULL)"
            clauses.append(sub if dl in ("yes", True, "1") else f"NOT {sub}")
        tag_ids = f.get("tag_ids") or []
        if isinstance(tag_ids, (str, int)):
            tag_ids = [tag_ids]
        for tid in tag_ids:  # AND semantics: must have every selected tag
            clauses.append("EXISTS (SELECT 1 FROM product_tags t WHERE t.order_product_id=p.order_product_id AND t.tag_id=?)")
            args.append(int(tid))
        filter_ids = f.get("filter_ids") or []
        if isinstance(filter_ids, str):
            filter_ids = [filter_ids]
        for fid in filter_ids:
            # match the filter or any of its descendants
            clauses.append(
                """EXISTS (WITH RECURSIVE sub(id) AS (SELECT ? UNION SELECT d.id FROM dtrpg_filters d JOIN sub ON d.parent_id=sub.id)
                   SELECT 1 FROM product_dtrpg_filters pf WHERE pf.order_product_id=p.order_product_id AND pf.filter_id IN sub)"""
            )
            args.append(str(fid))
        if f.get("untagged"):
            clauses.append("NOT EXISTS (SELECT 1 FROM product_tags t WHERE t.order_product_id=p.order_product_id)")
        if f.get("for_random"):
            clauses.append("m.exclude_random=0")
        return " AND ".join(clauses), args

    _SORTS = {
        "name": "p.name COLLATE NOCASE",
        "publisher": "p.publisher COLLATE NOCASE, p.name COLLATE NOCASE",
        "purchased": "p.date_purchased DESC",
        "updated": "p.file_last_modified DESC",
        "rating": "COALESCE(m.rating,0) DESC, p.name COLLATE NOCASE",
    }

    def search(self, f: dict[str, Any], limit: int = 100, offset: int = 0) -> dict[str, Any]:
        where, args = self._where(f)
        order = self._SORTS.get(f.get("sort") or "name", self._SORTS["name"])
        base = f"FROM products p JOIN product_meta m USING(order_product_id) WHERE {where}"
        total = self._q(f"SELECT COUNT(*) AS n {base}", args)[0]["n"]
        rows = self._q(
            f"""SELECT p.order_product_id, p.product_id, p.name, p.publisher, p.date_purchased,
                       p.file_last_modified, p.archived, p.cover_url, p.thumb_url, p.description, p.slug, m.*,
                       (SELECT COUNT(*) FROM files x WHERE x.order_product_id=p.order_product_id) AS file_count,
                       (SELECT COUNT(*) FROM files x WHERE x.order_product_id=p.order_product_id AND x.local_path IS NOT NULL) AS downloaded_count
                {base} ORDER BY {order} LIMIT ? OFFSET ?""",
            (*args, limit, offset),
        )
        items = [dict(r) for r in rows]
        self._attach_tags(items)
        return {"total": total, "items": items}

    def candidate_ids(self, f: dict[str, Any]) -> list[dict[str, Any]]:
        where, args = self._where({**f, "for_random": True})
        return [
            dict(r)
            for r in self._q(
                f"""SELECT p.order_product_id, m.status, m.favourite, m.rating
                    FROM products p JOIN product_meta m USING(order_product_id) WHERE {where}""",
                args,
            )
        ]

    def _attach_tags(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        ids = [i["order_product_id"] for i in items]
        marks = ",".join("?" * len(ids))
        tags: dict[str, list] = {i: [] for i in ids}
        for r in self._q(
            f"""SELECT pt.order_product_id, t.id, t.kind, t.name, pt.source, pt.confidence FROM product_tags pt JOIN tags t ON t.id=pt.tag_id
                WHERE pt.order_product_id IN ({marks}) ORDER BY t.kind, t.name""",
            ids,
        ):
            tags[r["order_product_id"]].append(
                {"id": r["id"], "kind": r["kind"], "name": r["name"], "source": r["source"], "confidence": r["confidence"]}
            )
        flts: dict[str, list] = {i: [] for i in ids}
        for r in self._q(
            f"""SELECT pf.order_product_id, f.id, f.name, f.group_name FROM product_dtrpg_filters pf
                JOIN dtrpg_filters f ON f.id=pf.filter_id WHERE pf.order_product_id IN ({marks}) ORDER BY f.name""",
            ids,
        ):
            flts[r["order_product_id"]].append({"id": r["id"], "name": r["name"], "group": r["group_name"]})
        for i in items:
            i["tags"] = tags[i["order_product_id"]]
            i["dtrpg_filters"] = flts[i["order_product_id"]]

    def product(self, opid: str) -> dict[str, Any] | None:
        rows = self._q(
            """SELECT p.*, m.* FROM products p JOIN product_meta m USING(order_product_id)
               WHERE p.order_product_id=?""",
            (opid,),
        )
        if not rows:
            return None
        item = dict(rows[0])
        item["raw"] = json.loads(item.pop("raw_json") or "null")
        item["files"] = self.files_for(opid)
        pt = self._q("SELECT pages FROM product_text WHERE order_product_id=?", (opid,))
        item["pages"] = pt[0]["pages"] if pt else None
        self._attach_tags([item])
        return item

    def record_suggestion(self, opid: str) -> None:
        self._x("INSERT INTO suggestions (order_product_id, suggested_at) VALUES (?,?)", (opid, now()))

    def recent_suggestions(self, n: int) -> list[str]:
        return [
            r["order_product_id"]
            for r in self._q("SELECT order_product_id FROM suggestions ORDER BY id DESC LIMIT ?", (n,))
        ]

    def stats(self) -> dict[str, Any]:
        one = lambda sql: self._q(sql)[0][0]  # noqa: E731
        return {
            "products": one("SELECT COUNT(*) FROM products WHERE removed=0"),
            "archived": one("SELECT COUNT(*) FROM products WHERE removed=0 AND archived=1"),
            "files": one("SELECT COUNT(*) FROM files f JOIN products p USING(order_product_id) WHERE p.removed=0"),
            "downloaded": one("SELECT COUNT(*) FROM files WHERE local_path IS NOT NULL"),
            "publishers": one("SELECT COUNT(DISTINCT publisher) FROM products WHERE removed=0"),
            "untagged": one(
                """SELECT COUNT(*) FROM products p WHERE removed=0 AND NOT EXISTS
                   (SELECT 1 FROM product_tags t WHERE t.order_product_id=p.order_product_id)"""
            ),
            "by_status": {r[0]: r[1] for r in self._q("SELECT status, COUNT(*) FROM product_meta GROUP BY status")},
            "last_synced": one("SELECT MAX(last_synced) FROM products"),
        }

    # -- import / export --------------------------------------------------
    def export_metadata(self) -> list[dict[str, Any]]:
        res = self.search({"include_archived": True}, limit=10**9)
        keep = ("order_product_id", "product_id", "name", "publisher", "status", "rating", "favourite",
                "exclude_random", "product_type", "min_players", "max_players", "notes")
        return [
            {**{k: i.get(k) for k in keep}, "tags": [{"kind": t["kind"], "name": t["name"], "source": t["source"]} for t in i["tags"]]}
            for i in res["items"]
        ]

    def import_metadata(self, records: list[dict[str, Any]]) -> int:
        n = 0
        existing = {r[0] for r in self._q("SELECT order_product_id FROM products")}
        for r in records:
            opid = str(r.get("order_product_id"))
            if opid not in existing:
                continue
            self.update_meta(opid, {k: r[k] for k in META_FIELDS if k in r})
            for t in r.get("tags", []):
                self.add_tag(opid, t["kind"], t["name"], source=t.get("source") or "manual")
            n += 1
        return n
