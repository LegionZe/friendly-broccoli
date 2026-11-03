"""Library sync and file downloads."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from .api import ApiError, DtrpgApi
from .config import Config
from .db import Library, now
from .normalize import normalize_product, safe_path_part

logger = logging.getLogger(__name__)


def sync_library(api: DtrpgApi, lib: Library, cfg: Config, progress: Callable[[str], None] = print) -> dict:
    progress("Authenticating")
    api.authenticate()
    seen: set[str] = set()
    new = 0
    for archived in (False, True) if cfg.include_archived else (False,):
        progress("Fetching archived products" if archived else "Fetching library")
        for raw in api.iter_products(
            cfg.page_size, archived=archived, filters=cfg.fetch_filters, progress=progress
        ):
            try:
                rec = normalize_product(raw)
            except ValueError as e:
                logger.warning("Skipping product: %s", e)
                continue
            if archived:
                rec["archived"] = 1
            new += lib.upsert_product(rec, raw)
            seen.add(rec["order_product_id"])
    removed = lib.mark_removed_except(seen, cfg.include_archived)
    summary = {"seen": len(seen), "new": new, "removed": removed}
    progress(f"Sync complete: {summary}")
    return summary


def target_path(cfg: Config, product_name: str, publisher: str | None, filename: str) -> Path:
    root = Path(cfg.library_path).expanduser().resolve()
    parts = [safe_path_part(product_name), safe_path_part(filename)]
    if not cfg.omit_publisher:
        parts.insert(0, safe_path_part(publisher or "Others"))
    path = root.joinpath(*parts).resolve()
    if root not in path.parents:
        raise ValueError(f"Refusing to write outside library: {path}")
    return path


def md5_file(path: Path) -> str:
    h = hashlib.md5()  # DTRPG checksums are MD5 (glujan/drpg sync.py, --validate)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def needs_download(path: Path, file_row: dict[str, Any], file_last_modified: str | None, use_checksum: bool) -> str | None:
    """Return a reason string if the file should be (re)downloaded, else None."""
    if not path.exists():
        return "missing"
    if file_last_modified:
        try:
            remote = datetime.fromisoformat(file_last_modified.replace("Z", "+00:00")).timestamp()
            if remote > path.stat().st_mtime + 1:
                return "outdated"
        except ValueError:
            pass
    if use_checksum and file_row.get("checksum") and md5_file(path) != file_row["checksum"]:
        return "checksum mismatch"
    return None


def download_file(
    api: DtrpgApi,
    lib: Library,
    cfg: Config,
    file_row: dict[str, Any],
    *,
    http: httpx.Client | None = None,
    force: bool = False,
) -> str:
    opid = file_row["order_product_id"]
    path = target_path(cfg, file_row["product_name"], file_row.get("publisher"), file_row["filename"])
    if not force:
        reason = needs_download(path, file_row, file_row.get("file_last_modified"), cfg.validate_checksums)
        if reason is None:
            lib.mark_downloaded(opid, file_row["idx"], str(path))
            return "up to date"
    info = api.prepare_download(opid, file_row["idx"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    own_client = http is None
    http = http or httpx.Client(timeout=120.0, follow_redirects=True)
    h = hashlib.md5()
    try:
        with http.stream("GET", info["url"], follow_redirects=True) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
                    h.update(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if own_client:
            http.close()
    if cfg.validate_checksums and file_row.get("checksum") and h.hexdigest() != file_row["checksum"]:
        tmp.unlink(missing_ok=True)
        raise ApiError(f"Checksum mismatch for {file_row['filename']} ({h.hexdigest()} != {file_row['checksum']})")
    tmp.replace(path)
    if flm := file_row.get("file_last_modified"):
        try:
            ts = datetime.fromisoformat(flm.replace("Z", "+00:00")).timestamp()
            os.utime(path, (ts, ts))
        except ValueError:
            pass
    lib.mark_downloaded(opid, file_row["idx"], str(path))
    return "downloaded"


# -- background jobs for the web UI ---------------------------------------
class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, fn: Callable[[Callable[[str], None]], Any]) -> str:
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "kind": kind, "state": "running", "log": [], "started": now(), "result": None}
        with self._lock:
            self._jobs[jid] = job

        def log(msg: str) -> None:
            with self._lock:
                job["log"].append(msg)
                del job["log"][:-200]

        def run() -> None:
            try:
                job["result"] = fn(log)
                job["state"] = "done"
            except Exception as e:  # surfaced to the UI
                logger.exception("Job %s failed", jid)
                log(f"ERROR: {e}")
                job["state"] = "failed"
            job["finished"] = now()

        threading.Thread(target=run, daemon=True).start()
        return jid

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(j, log=list(j["log"][-30:])) for j in reversed(self._jobs.values())][:20]

    def running(self, kind: str) -> bool:
        with self._lock:
            return any(j["kind"] == kind and j["state"] == "running" for j in self._jobs.values())


def download_many(
    cfg: Config, lib: Library, files: list[dict[str, Any]], log: Callable[[str], None], force: bool = False
) -> dict[str, int]:
    counts = {"downloaded": 0, "up to date": 0, "failed": 0}
    lock = threading.Lock()
    with DtrpgApi(cfg.api_key, read_timeout=cfg.request_timeout) as api, httpx.Client(timeout=120.0, follow_redirects=True) as http:
        api.authenticate()

        def one(f: dict[str, Any]) -> None:
            try:
                res = download_file(api, lib, cfg, f, http=http, force=force)
            except Exception as e:
                res = "failed"
                log(f"FAILED {f['product_name']} / {f['filename']}: {e}")
            else:
                if res == "downloaded":
                    log(f"Downloaded {f['product_name']} / {f['filename']}")
            with lock:
                counts[res] += 1

        with ThreadPoolExecutor(max_workers=max(1, cfg.threads)) as pool:
            list(pool.map(one, files))
    log(f"Finished: {counts}")
    return counts
