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
from collections.abc import Iterator
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.drivethrurpg.com/api/vBeta/"
DRIVETHRU_SITE_ID = 10  # value used by glujan/drpg (DriveThruSite.DEFAULT)
RETRY_STATUSES = {429, 502, 503, 504}


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
        self.client = httpx.Client(
            base_url=base_url,
            timeout=30.0,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "dtrpg-manager/0.1",
            },
        )

    # -- plumbing ---------------------------------------------------------
    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> DtrpgApi:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, url: str, *, auth: bool = True, **kw: Any) -> httpx.Response:
        if auth and self._token is None:
            self.authenticate()
        reauthed = False
        attempt = 0
        while True:
            resp = self.client.request(method, url, **kw)
            if resp.status_code == 401 and auth and not reauthed:
                logger.info("Token rejected, re-authenticating")
                self.authenticate()
                reauthed = True
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self._retries:
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
        resp = self._request("POST", "auth_key", auth=False, params={"applicationKey": self._api_key})
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
        self, page_size: int = 50, *, archived: bool = False, filters: bool = True
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            items = unwrap_products(self.products_page(page, page_size, archived=archived, filters=filters))
            if not items:
                return
            yield from items
            if len(items) < page_size:
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
