"""Low-level client for the DriveThruRPG "vBeta" REST API.

The endpoints are not officially documented. They are taken from:

* glujan/drpg, ``drpg/api.py`` (MIT licence) -- https://github.com/glujan/drpg
  - ``POST auth_key?applicationKey=<key>``  -> ``{"token", "refreshToken", "refreshTokenTTL"}``
  - ``GET  order_products`` (``page``, ``pageSize``, ``library``, ``archived``,
    ``getChecksum``, ``getFilters``)
  - ``GET  order_products/<orderProductId>/prepare`` and ``.../check``
    (``siteId=10``, ``index``, ``getChecksums``) -> ``{"url", "status"}``
* JamesSkemp/DriveThruRpg, ``DriveThruRpgApi/ApiClient.cs`` -- same base URL and
  ``auth_key`` / ``order_products`` calls, but models the product list as
  ``{"data": [{"id", "attributes": {...}}]}``.

Both response shapes are accepted by :func:`unwrap_products`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.drivethrurpg.com/api/vBeta/"
DRIVETHRU_SITE_ID = 10  # value used by glujan/drpg (DriveThruSite.DEFAULT)
RETRY_STATUSES = {429, 502, 503, 504}
# Honest, self-identifying client name for every request (API and file downloads).
USER_AGENT = "dtrpg-manager/0.1 (+https://github.com/LegionZe/friendly-broccoli)"


class ApiError(Exception):
    """Raised when the DriveThruRPG API returns an unusable response."""


class DtrpgApi:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = API_URL,
        transport: httpx.BaseTransport | None = None,
        retries: int = 4,
        backoff: float = 1.0,
        read_timeout: float = 120.0,
        poll_interval: float = 2.0,
        poll_timeout: float = 300.0,
    ) -> None:
        if not api_key:
            raise ApiError("No DriveThruRPG API key configured")
        self._api_key = api_key
        self._retries = retries
        self._backoff = backoff
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._token: str | None = None
        try:  # HTTP/2 as used by glujan/drpg (httpx[http2]); falls back to HTTP/1.1
            import h2  # noqa: F401

            http2 = transport is None
        except ImportError:
            http2 = False
        self.client = httpx.Client(
            base_url=base_url,
            # The library listing is generated server-side and can take well
            # over 30 s for large accounts (observed ReadTimeout at 30 s).
            timeout=httpx.Timeout(read_timeout, connect=15.0),
            http2=http2,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

    # -- plumbing ---------------------------------------------------------
    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> DtrpgApi:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(
        self, method: str, url: str, *, auth: bool = True, retries: int | None = None, **kw: Any
    ) -> httpx.Response:
        retries = self._retries if retries is None else retries
        if auth and self._token is None:
            self.authenticate()
        reauthed = False
        attempt = 0
        while True:
            try:
                resp = self.client.request(method, url, **kw)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                if attempt >= retries:
                    raise ApiError(f"{type(e).__name__} on {url} after {attempt + 1} attempts") from e
                delay = self._backoff * (2**attempt)
                logger.warning("%s on %s, retrying in %.1fs", type(e).__name__, url, delay)
                time.sleep(delay)
                attempt += 1
                continue
            if resp.status_code == 401 and auth and not reauthed:
                logger.info("Token rejected, re-authenticating")
                self.authenticate()
                reauthed = True
                continue
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                delay = self._backoff * (2**attempt)
                logger.warning("HTTP %s on %s, retrying in %.1fs", resp.status_code, url, delay)
                time.sleep(delay)
                attempt += 1
                continue
            return resp

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        if not resp.is_success:
            raise ApiError(f"HTTP {resp.status_code} from {resp.request.url}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(f"Non-JSON response from {resp.request.url}: {resp.text[:300]}") from e

    # -- endpoints --------------------------------------------------------
    def authenticate(self) -> dict[str, Any]:
        # Key goes in the JSON body, not the query string, so it never appears
        # in URLs or request logs. Verified 2026-11-03: the endpoint reads
        # {"applicationKey": ...} from a JSON body (HTTP 496 "Invalid
        # application key." for a bad key), but not from a form body or a
        # Bearer header (HTTP 400 "Missing application key."). glujan/drpg
        # and JamesSkemp/DriveThruRpg send it as ?applicationKey= instead.
        resp = self._request("POST", "auth_key", auth=False, json={"applicationKey": self._api_key})
        if not resp.is_success:
            # Observed 2026-11-03: an invalid key returns HTTP 496 with
            # {"error": {"message": "Invalid application key.", ...}}
            try:
                msg = resp.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                msg = resp.text[:200]
            raise ApiError(f"DriveThruRPG rejected the API key (HTTP {resp.status_code}): {msg}")
        data = self._json(resp)
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise ApiError(f"Unexpected auth response: {str(data)[:300]}")
        self._token = token
        # glujan/drpg sends the raw token (no "Bearer " prefix).
        self.client.headers["Authorization"] = token
        return data

    def products_page(
        self, page: int, page_size: int = 50, *, archived: bool = False, filters: bool = True
    ) -> Any:
        """Return the raw JSON for one page of the customer's library."""
        resp = self._request(
            "GET",
            "order_products",
            retries=1,  # iter_products has its own fallback strategy
            params={
                "getChecksum": 1,
                "getFilters": 1 if filters else 0,
                "page": page,
                "pageSize": page_size,
                "library": 1,
                "archived": 1 if archived else 0,
            },
        )
        return self._json(resp)

    def iter_products(
        self,
        page_size: int = 50,
        *,
        archived: bool = False,
        filters: bool = True,
        progress: Callable[[str], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every product, degrading gracefully if pages time out.

        Pages are requested until an empty one comes back (as glujan/drpg's
        ``customer_products`` does), so a server-side page-size cap is harmless.
        On a failed page: first drop ``getFilters`` (drpg sends 0), then halve
        the page size down to 5 and resume from the number of products already
        received. Duplicates are suppressed by ``orderProductId``.
        """
        say = progress or (lambda m: logger.info(m))
        page = 1
        largest_page = 0  # actual items per page the server returns
        seen: set[str] = set()
        say("Requesting product list (the first page can take a minute or more)")
        while True:
            try:
                items = unwrap_products(self.products_page(page, page_size, archived=archived, filters=filters))
            except ApiError as e:
                if filters:
                    filters = False
                    say(f"Page {page} failed ({e}); retrying without DriveThruRPG category data")
                elif page_size > 5:
                    page_size = max(5, min(page_size, largest_page or page_size) // 2)
                    page = len(seen) // page_size + 1
                    say(f"Page failed ({e}); retrying with page size {page_size} from page {page}")
                else:
                    raise
                continue
            if not items:
                return
            largest_page = max(largest_page, len(items))
            new = 0
            for it in items:
                key = str(it.get("orderProductId") or it.get("id") or id(it))
                if key in seen:
                    continue
                seen.add(key)
                new += 1
                yield it
            say(f"Fetched page {page} ({len(seen)} products so far)")
            if new == 0:  # server ignored paging -> avoid an endless loop
                return
            page += 1

    def prepare_download(self, order_product_id: int | str, index: int) -> dict[str, Any]:
        """Ask the server to watermark/prepare a file and poll until it has a URL."""
        params = {"siteId": DRIVETHRU_SITE_ID, "index": index, "getChecksums": 0}
        data = self._json(self._request("GET", f"order_products/{order_product_id}/prepare", params=params))
        deadline = time.monotonic() + self._poll_timeout
        while str(data.get("status", "")).lower().startswith("preparing"):
            if time.monotonic() > deadline:
                raise ApiError(f"Timed out waiting for {order_product_id}/{index} to be prepared")
            time.sleep(self._poll_interval)
            data = self._json(self._request("GET", f"order_products/{order_product_id}/check", params=params))
        if not isinstance(data, dict) or not data.get("url"):
            raise ApiError(f"No download URL for {order_product_id}/{index}: {str(data)[:300]}")
        return data


def unwrap_products(payload: Any) -> list[dict[str, Any]]:
    """Normalise both observed product-list shapes into a flat list of dicts."""
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("products", []))
    if not isinstance(payload, list):
        raise ApiError(f"Unexpected product list payload: {str(payload)[:300]}")
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("attributes"), dict):
            merged = dict(item["attributes"])
            merged.setdefault("orderProductId", item.get("id"))
            out.append(merged)
        else:
            out.append(item)
    return out
