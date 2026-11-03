"""Library sync and file downloads."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .api import USER_AGENT, ApiError, DtrpgApi
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
    skipped = len(getattr(api, "last_skipped", []) or [])
    # Only flag products as removed after a complete listing.
    removed = 0 if skipped else lib.mark_removed_except(seen, cfg.include_archived)
    summary = {"seen": len(seen), "new": new, "removed": removed, "skipped": skipped}
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
    """Return a reason string if the file should be (re)downloaded, else None.

    Like glujan/drpg's ``_need_download``: a file is current if it exists and
    DriveThruRPG's ``fileLastModified`` is not newer than the local file. If this
    app downloaded the file, its recorded size must also still match (cheap
    corruption check). ``use_checksum`` additionally hashes the file and compares
    it with the recorded MD5 (or, for files from elsewhere, DTRPG's checksum).
    DTRPG's own size/checksum are not used for the quick check: they describe
    earlier watermarked copies and often differ from a fresh download.
    """
    if not path.exists():
        return "missing"
    if file_last_modified:
        try:
            remote = datetime.fromisoformat(file_last_modified.replace("Z", "+00:00")).timestamp()
            if remote > path.stat().st_mtime + 1:
                return "outdated"
        except ValueError:
            pass
    recorded_here = file_row.get("local_path") == str(path)
    if recorded_here and file_row.get("local_size") and path.stat().st_size != file_row["local_size"]:
        return "size changed"
    if use_checksum:
        expected = file_row.get("local_md5") if recorded_here else None
        expected = expected or file_row.get("checksum")
        if expected and md5_file(path) != expected:
            return "checksum mismatch"
    return None


class ContentCheckError(ApiError):
    """The downloaded bytes failed a check (checksum, not a PDF); file not saved."""


def on_hold(file_row: dict[str, Any], hours: float) -> datetime | None:
    """If the file failed a content check less than ``hours`` ago, return when it may be retried."""
    if not file_row.get("failed_at") or hours <= 0:
        return None
    try:
        until = datetime.fromisoformat(file_row["failed_at"]) + timedelta(hours=hours)
    except ValueError:
        return None
    return until if until > datetime.now(timezone.utc) else None


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
        reason = needs_download(path, file_row, file_row.get("file_last_modified"), cfg.verify_existing)
        if reason is None:
            lib.mark_downloaded(opid, file_row["idx"], str(path))
            return "up to date"
    info = api.prepare_download(opid, file_row["idx"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    own_client = http is None
    http = http or httpx.Client(timeout=120.0, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    h = hashlib.md5()
    size = 0
    head = b""
    try:
        with http.stream("GET", info["url"], follow_redirects=True) as resp:
            if not resp.is_success:
                raise DownloadHTTPError(resp.status_code, _describe(resp), _retry_after(resp))
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
                    if len(head) < 8:
                        head += chunk[:8]
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if own_client:
            http.close()
    digest = h.hexdigest()
    if path.suffix.lower() == ".pdf" and not head.startswith(b"%PDF"):
        tmp.unlink(missing_ok=True)
        raise ContentCheckError(f"{file_row['filename']} is not a PDF (got {head[:8]!r}); not saved")
    if file_row.get("checksum") and digest != file_row["checksum"]:
        if cfg.validate_checksums:
            tmp.unlink(missing_ok=True)
            raise ContentCheckError(f"Checksum mismatch for {file_row['filename']} ({digest} != {file_row['checksum']})")
        logger.info("%s: MD5 differs from DriveThruRPG's record (normal for re-watermarked files)", file_row["filename"])
    tmp.replace(path)
    if flm := file_row.get("file_last_modified"):
        try:
            ts = datetime.fromisoformat(flm.replace("Z", "+00:00")).timestamp()
            os.utime(path, (ts, ts))
        except ValueError:
            pass
    lib.mark_downloaded(opid, file_row["idx"], str(path), size=size, md5=digest)
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


class AdaptiveLimit:
    """Concurrency cap that halves when the server pushes back (429/5xx) and
    grows by one after every ``grow_after`` consecutive successes, up to ``maximum``."""

    def __init__(self, maximum: int, grow_after: int = 10) -> None:
        self.maximum = self.limit = max(1, maximum)
        self.grow_after = grow_after
        self.active = self.streak = 0
        self.cv = threading.Condition()

    def acquire(self, cancel: threading.Event) -> None:
        with self.cv:
            while self.active >= self.limit:
                if cancel.is_set():
                    raise JobCancelled
                self.cv.wait(0.5)
            self.active += 1

    def release(self) -> None:
        with self.cv:
            self.active -= 1
            self.cv.notify_all()

    def success(self) -> int | None:
        with self.cv:
            self.streak += 1
            if self.limit < self.maximum and self.streak >= self.grow_after:
                self.limit += 1
                self.streak = 0
                self.cv.notify_all()
                return self.limit
        return None

    def pushback(self) -> int | None:
        with self.cv:
            self.streak = 0
            if self.limit > 1:
                self.limit = max(1, self.limit // 2)
                return self.limit
        return None


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
    """Download files in parallel without hammering the server.

    Up to ``cfg.threads`` downloads run at once; the cap halves whenever the
    file server answers 429/5xx and creeps back up after a run of successes
    (AdaptiveLimit). Retries honour Retry-After (else back off 15 s, 30 s) with
    a freshly prepared link each time, and a circuit breaker pauses once and
    then stops the job if every file keeps failing. Background: drpg users
    report 503s from DriveThruRPG (glujan/drpg issues #143, #155).
    """
    cancel = cancel or threading.Event()

    def wait(seconds: float) -> None:
        if sleep:
            sleep(seconds)
        elif cancel.wait(seconds):
            raise JobCancelled

    counts = {"downloaded": 0, "up to date": 0, "failed": 0, "on hold": 0}
    lock = threading.Lock()
    state = {"consecutive": 0, "paused": False, "abort": None, "detail_logged": False}
    own_api, own_http = api is None, http is None
    api = api or DtrpgApi(cfg.api_key, read_timeout=cfg.request_timeout)
    http = http or httpx.Client(
        timeout=httpx.Timeout(300.0, connect=15.0), follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
    pending = [f for f in files if force or needs_download(
        target_path(cfg, f["product_name"], f.get("publisher"), f["filename"]), f,
        f.get("file_last_modified"), cfg.verify_existing) is not None]
    counts["up to date"] = len(files) - len(pending)
    if not force and cfg.hold_failed_hours > 0:
        held = [f for f in pending if on_hold(f, cfg.hold_failed_hours)]
        if held:
            pending = [f for f in pending if f not in held]
            counts["on hold"] = len(held)
            soonest = min(on_hold(f, cfg.hold_failed_hours) for f in held)
            log(f"{len(held)} file(s) on hold after failed checks; next retry from "
                f"{soonest.astimezone().strftime('%Y-%m-%d %H:%M')} (Settings › Downloads on hold)")
    log(f"{len(pending)} file(s) to download, {counts['up to date']} already up to date")
    try:
        api.authenticate()

        limiter = AdaptiveLimit(max(1, int(cfg.threads)))

        def one(f: dict[str, Any]) -> None:
            label = f"{f['product_name']} / {f['filename']}"
            for attempt in range(1, max_attempts + 1):
                if cancel.is_set() or state["abort"]:
                    return
                limiter.acquire(cancel)
                try:
                    res = download_file(api, lib, cfg, f, http=http, force=True)
                except DownloadHTTPError as e:
                    if not state["detail_logged"]:
                        state["detail_logged"] = True
                        log(f"Download server response: {e.detail}")
                    if e.status in RETRYABLE and (new := limiter.pushback()) is not None:
                        log(f"Server pushed back (HTTP {e.status}); parallel downloads reduced to {new}")
                    if e.status in RETRYABLE and attempt < max_attempts:
                        limiter.release()
                        delay = e.retry_after if e.retry_after is not None else min(300.0, 15.0 * 2 ** (attempt - 1))
                        log(f"HTTP {e.status} for {label}; retry {attempt}/{max_attempts - 1} in {delay:.0f}s")
                        wait(delay)
                        continue
                    limiter.release()
                    err: Exception = e
                    server_side = e.status in RETRYABLE
                except ContentCheckError as e:
                    limiter.release()
                    lib.mark_failed(f["order_product_id"], f["idx"], str(e))
                    err, server_side = e, False
                except (ApiError, httpx.HTTPError, OSError, ValueError) as e:
                    limiter.release()
                    err, server_side = e, isinstance(e, (httpx.TransportError,))
                except BaseException:
                    limiter.release()
                    raise
                else:
                    limiter.release()
                    with lock:
                        counts[res] += 1
                        state["consecutive"] = 0
                    log(f"Downloaded {label}")
                    if (new := limiter.success()) is not None:
                        log(f"Parallel downloads back up to {new}")
                    if cfg.download_delay:
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
