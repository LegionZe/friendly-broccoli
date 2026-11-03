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


class DownloadHTTPError(ApiError):
    """Non-2xx response from the file (watermark/CDN) server."""

    def __init__(self, status: int, detail: str, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status} from download server: {detail}")
        self.status = status
        self.retry_after = retry_after
        self.detail = detail


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return max(0.0, float(resp.headers.get("Retry-After", "")))
    except ValueError:
        return None


def _describe(resp: httpx.Response) -> str:
    """Short, credential-free description of an error response for the job log."""
    resp.read()
    body = " ".join(resp.text[:300].split())
    hdrs = ", ".join(
        f"{k}={resp.headers[k]}" for k in ("server", "retry-after", "cf-ray", "content-type") if k in resp.headers
    )
    return f"{resp.reason_phrase} [{hdrs}] {body}"


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
            if not resp.is_success:
                raise DownloadHTTPError(resp.status_code, _describe(resp), _retry_after(resp))
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
class JobCancelled(Exception):
    pass


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, fn: Callable[..., Any]) -> str:
        """``fn(log, cancel_event)`` runs in a background thread."""
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "kind": kind, "state": "running", "log": [], "started": now(), "result": None}
        cancel = threading.Event()
        with self._lock:
            self._jobs[jid] = job
            self._cancel[jid] = cancel

        def log(msg: str) -> None:
            with self._lock:
                job["log"].append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
                del job["log"][:-300]

        def run() -> None:
            try:
                job["result"] = fn(log, cancel)
                job["state"] = "cancelled" if cancel.is_set() else "done"
            except JobCancelled:
                log("Stopped by user")
                job["state"] = "cancelled"
            except Exception as e:  # surfaced to the UI
                logger.exception("Job %s failed", jid)
                log(f"ERROR: {e}")
                job["state"] = "failed"
            job["finished"] = now()

        threading.Thread(target=run, daemon=True).start()
        return jid

    def cancel(self, jid: str) -> bool:
        with self._lock:
            ev = self._cancel.get(jid)
        if ev:
            ev.set()
        return ev is not None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(j, log=list(j["log"][-40:])) for j in reversed(self._jobs.values())][:20]

    def running(self, kind: str) -> bool:
        with self._lock:
            return any(j["kind"] == kind and j["state"] == "running" for j in self._jobs.values())


RETRYABLE = {429, 500, 502, 503, 504}


def download_many(
    cfg: Config,
    lib: Library,
    files: list[dict[str, Any]],
    log: Callable[[str], None],
    force: bool = False,
    cancel: threading.Event | None = None,
    *,
    api: DtrpgApi | None = None,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
    max_attempts: int = 3,
    breaker_threshold: int = 5,
    breaker_pause: float = 300.0,
) -> dict[str, int]:
    """Download files politely.

    The file server (watermark.drivethrurpg.com) sits behind Cloudflare and
    answers with 503 when overloaded or rate-limiting (glujan/drpg issues
    #143 and #155). So: one file at a time by default, a delay between files,
    Retry-After / exponential back-off with a freshly prepared link on each
    retry, and a circuit breaker that pauses once and then stops the job if
    every file keeps failing, instead of hammering hundreds of URLs.
    """
    cancel = cancel or threading.Event()

    def wait(seconds: float) -> None:
        if sleep:
            sleep(seconds)
        elif cancel.wait(seconds):
            raise JobCancelled

    counts = {"downloaded": 0, "up to date": 0, "failed": 0}
    lock = threading.Lock()
    state = {"consecutive": 0, "paused": False, "abort": None, "detail_logged": False}
    own_api, own_http = api is None, http is None
    api = api or DtrpgApi(cfg.api_key, read_timeout=cfg.request_timeout)
    http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0), follow_redirects=True)
    pending = [f for f in files if force or needs_download(
        target_path(cfg, f["product_name"], f.get("publisher"), f["filename"]), f,
        f.get("file_last_modified"), cfg.validate_checksums) is not None]
    counts["up to date"] = len(files) - len(pending)
    log(f"{len(pending)} file(s) to download, {counts['up to date']} already up to date")
    try:
        api.authenticate()

        def one(f: dict[str, Any]) -> None:
            label = f"{f['product_name']} / {f['filename']}"
            for attempt in range(1, max_attempts + 1):
                if cancel.is_set() or state["abort"]:
                    return
                try:
                    res = download_file(api, lib, cfg, f, http=http, force=True)
                except DownloadHTTPError as e:
                    if not state["detail_logged"]:
                        state["detail_logged"] = True
                        log(f"Download server response: {e.detail}")
                    if e.status in RETRYABLE and attempt < max_attempts:
                        delay = e.retry_after if e.retry_after is not None else min(300.0, 15.0 * 2 ** (attempt - 1))
                        log(f"HTTP {e.status} for {label}; retry {attempt}/{max_attempts - 1} in {delay:.0f}s")
                        wait(delay)
                        continue
                    err: Exception = e
                    server_side = e.status in RETRYABLE
                except (ApiError, httpx.HTTPError, OSError, ValueError) as e:
                    err, server_side = e, isinstance(e, (httpx.TransportError,))
                else:
                    with lock:
                        counts[res] += 1
                        state["consecutive"] = 0
                    log(f"Downloaded {label}")
                    wait(cfg.download_delay)
                    return
                break
            with lock:
                counts["failed"] += 1
                state["consecutive"] = state["consecutive"] + 1 if server_side else 0
                tripped = state["consecutive"] >= breaker_threshold
            log(f"FAILED {label}: {err}")
            if tripped:
                if not state["paused"]:
                    state["paused"] = True
                    log(f"{breaker_threshold} files in a row failed with server errors; pausing {breaker_pause/60:.0f} min")
                    wait(breaker_pause)
                    with lock:
                        state["consecutive"] = 0
                else:
                    state["abort"] = (
                        "DriveThruRPG's download server keeps returning errors; stopped. "
                        "This is on their side (see Download server response above). Try again later; "
                        "files already downloaded are kept and will be skipped."
                    )

        workers = max(1, int(cfg.threads))
        if workers == 1:
            for f in pending:
                if cancel.is_set() or state["abort"]:
                    break
                one(f)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(one, pending))
    finally:
        if own_api:
            api.close()
        if own_http:
            http.close()
    if state["abort"]:
        log(state["abort"])
    if cancel.is_set():
        log("Stopped by user")
    log(f"Finished: {counts}")
    return counts
