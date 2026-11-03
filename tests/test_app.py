import hashlib
import json
import random

import httpx
import pytest

from dtrpg_manager.api import DtrpgApi, unwrap_products
from dtrpg_manager.config import Config
from dtrpg_manager.db import Library
from dtrpg_manager.normalize import normalize_product, safe_path_part
from dtrpg_manager.suggest import suggest
from dtrpg_manager.sync import download_file, sync_library, target_path

PDF = b"%PDF-1.4 fake"
PDF_MD5 = hashlib.md5(PDF).hexdigest()

# Shape per glujan/drpg drpg/types.py, plus speculative filter data.
PRODUCTS = [
    {
        "productId": "100", "orderProductId": 1, "name": "Horror &amp; Dread",
        "publisher": {"name": "Spooky Press"}, "fileLastModified": "2024-01-01T00:00:00+00:00",
        "files": [{"index": 0, "filename": "dread.pdf", "checksums": [
            {"checksum": "old", "checksumDate": "2023-01-01T00:00:00"},
            {"checksum": PDF_MD5, "checksumDate": "2024-01-01T00:00:00"}]}],
        "filters": [{"filters_id": "10", "filters_name": "Genre", "parent_id": "0"},
                    {"filters_id": "11", "filters_name": "Horror", "parent_id": "10"}],
    },
    {
        "productId": "200", "orderProductId": 2, "name": "Space Opera Core",
        "publisher": {"name": "Star Co"}, "fileLastModified": "2024-02-01T00:00:00+00:00",
        "files": [{"index": 0, "filename": "core.pdf", "checksums": []},
                  {"index": 1, "filename": "maps/../../evil.pdf", "checksums": []}],
    },
]


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/auth_key"):
        assert request.url.params["applicationKey"] == "KEY"
        return httpx.Response(200, json={"token": "TOK", "refreshToken": "R", "refreshTokenTTL": 1})
    assert request.headers.get("Authorization") == "TOK"
    if path.endswith("/order_products"):
        page = int(request.url.params["page"])
        return httpx.Response(200, json=PRODUCTS if page == 1 else [])
    if path.endswith("/prepare"):
        return httpx.Response(200, json={"url": "https://dl.example/f.pdf", "status": "Preparing download..."})
    if path.endswith("/check"):
        return httpx.Response(200, json={"url": "https://dl.example/f.pdf", "status": "Complete"})
    return httpx.Response(404)


@pytest.fixture
def api():
    a = DtrpgApi("KEY", transport=httpx.MockTransport(handler), poll_interval=0)
    yield a
    a.close()


@pytest.fixture
def cfg(tmp_path):
    c = Config.load(tmp_path / "config.json")
    c.api_key = "KEY"
    c.library_path = str(tmp_path / "lib")
    return c


@pytest.fixture
def lib():
    return Library(":memory:")


def test_unwrap_both_shapes():
    assert unwrap_products([{"a": 1}]) == [{"a": 1}]
    wrapped = {"data": [{"id": "9", "attributes": {"name": "X", "productId": 5}}]}
    assert unwrap_products(wrapped) == [{"name": "X", "productId": 5, "orderProductId": "9"}]


def test_normalize_product():
    r = normalize_product(PRODUCTS[0])
    assert r["name"] == "Horror & Dread"
    assert r["publisher"] == "Spooky Press"
    assert r["files"][0]["checksum"] == PDF_MD5  # newest checksum wins
    assert {f["name"] for f in r["filters"]} == {"Genre", "Horror"}
    assert next(f for f in r["filters"] if f["name"] == "Horror")["parent_id"] == "10"


def test_normalize_grouped_filters():
    r = normalize_product({"orderProductId": 3, "name": "N", "filters": {"Genre": ["Fantasy", {"name": "Sci-Fi"}]}})
    assert {(f["group_name"], f["name"]) for f in r["filters"]} == {("Genre", "Fantasy"), ("Genre", "Sci-Fi")}


def test_safe_path_part():
    assert "/" not in safe_path_part("a/b")
    assert safe_path_part("..") == "_"
    assert safe_path_part('x:"y"') == "x - y"


def test_sync_and_search(api, lib, cfg):
    res = sync_library(api, lib, cfg, progress=lambda m: None)
    assert res == {"seen": 2, "new": 2, "removed": 0}
    assert lib.search({})["total"] == 2
    # DTRPG filter hierarchy: selecting parent "Genre" matches child "Horror"
    assert [i["name"] for i in lib.search({"filter_ids": ["10"]})["items"]] == ["Horror & Dread"]
    # Re-sync is idempotent
    assert sync_library(api, lib, cfg, progress=lambda m: None)["new"] == 0


def test_tags_meta_and_filtering(api, lib, cfg):
    sync_library(api, lib, cfg, progress=lambda m: None)
    lib.add_tag("1", "genre", "Horror")
    lib.add_tag("1", "system", "Call of Cthulhu")
    lib.add_tag("2", "genre", "horror")  # case-insensitive reuse
    assert len([t for t in lib.tags() if t["kind"] == "genre"]) == 1
    horror = next(t["id"] for t in lib.tags() if t["name"] == "Horror")
    coc = next(t["id"] for t in lib.tags() if t["name"] == "Call of Cthulhu")
    assert lib.search({"tag_ids": [horror]})["total"] == 2
    assert lib.search({"tag_ids": [horror, coc]})["total"] == 1
    lib.update_meta("2", {"status": "played", "rating": 4, "min_players": 2, "max_players": 4})
    assert lib.search({"status": ["played"]})["total"] == 1
    assert lib.search({"players": 6})["total"] == 1  # product 1 has no player limits
    with pytest.raises(ValueError):
        lib.update_meta("2", {"status": "bogus"})
    with pytest.raises(ValueError):
        lib.update_meta("2", {"name": "hack"})


def test_suggest(api, lib, cfg):
    sync_library(api, lib, cfg, progress=lambda m: None)
    lib.update_meta("2", {"exclude_random": 1})
    picks = suggest(lib, {}, count=5, rng=random.Random(1))
    assert [p["order_product_id"] for p in picks] == ["1"]
    assert lib.recent_suggestions(5) == ["1"]
    assert suggest(lib, {"q": "nothing-matches"}) == []


def test_download_checksum_and_traversal(api, lib, cfg):
    sync_library(api, lib, cfg, progress=lambda m: None)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PDF)))
    files = {(f["order_product_id"], f["idx"]): f for f in lib.all_files()}
    assert download_file(api, lib, cfg, files[("1", 0)], http=http) == "downloaded"
    path = target_path(cfg, "Horror & Dread", "Spooky Press", "dread.pdf")
    assert path.read_bytes() == PDF
    assert download_file(api, lib, cfg, lib.all_files()[0], http=http) == "up to date"
    # "../" in a filename cannot escape the library folder
    evil = target_path(cfg, "Space Opera Core", "Star Co", "maps/../../evil.pdf")
    assert evil.parent.name == "Space Opera Core"
    # checksum mismatch is rejected and leaves no file
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"tampered")))
    path.unlink()
    with pytest.raises(Exception, match="Checksum"):
        download_file(api, lib, cfg, files[("1", 0)], http=bad)
    assert not path.exists()


def test_web_api(api, lib, cfg):
    from dtrpg_manager.web import create_app

    sync_library(api, lib, cfg, progress=lambda m: None)
    client = create_app(cfg, lib).test_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/products").get_json()["total"] == 2
    r = client.post("/api/products/1/tags", json={"kind": "genre", "name": "Horror"})
    assert r.status_code == 200 and r.get_json()[0]["name"] == "Horror"
    assert client.patch("/api/products/1", json={"status": "want-to-play"}).status_code == 200
    assert client.patch("/api/products/1", json={"status": "nope"}).status_code == 400
    # CSRF guard: form posts and foreign origins rejected
    assert client.post("/api/sync", data="x").status_code == 415
    assert client.post("/api/sync", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    picks = client.get("/api/suggest?status=want-to-play").get_json()
    assert [p["order_product_id"] for p in picks] == ["1"]
    meta = client.get("/api/meta").get_json()
    assert meta["config"]["api_key"].startswith("••••") and "KEY" not in json.dumps(meta["config"]["api_key"])
    exported = client.get("/api/export.json").get_json()
    lib2 = Library(":memory:")
    for raw in PRODUCTS:
        lib2.upsert_product(normalize_product(raw), raw)
    assert lib2.import_metadata(exported) == 2
    assert lib2.product("1")["status"] == "want-to-play"
    assert "genre" in client.get("/api/export.csv").get_data(as_text=True).splitlines()[0]


def _many(n):
    return [{"orderProductId": i, "productId": str(i), "name": f"P{i}", "files": []} for i in range(1, n + 1)]


def test_timeout_falls_back_without_filters_then_smaller_pages():
    data = _many(23)
    calls = []

    def h(request):
        if request.url.path.endswith("/auth_key"):
            return httpx.Response(200, json={"token": "T"})
        p = request.url.params
        calls.append((p["getFilters"], p["pageSize"], p["page"]))
        if p["getFilters"] == "1" or int(p["pageSize"]) > 10:
            raise httpx.ReadTimeout("slow", request=request)
        size, page = int(p["pageSize"]), int(p["page"])
        return httpx.Response(200, json=data[(page - 1) * size: page * size])

    msgs = []
    a = DtrpgApi("K", transport=httpx.MockTransport(h), backoff=0)
    got = list(a.iter_products(50, progress=msgs.append))
    assert [g["orderProductId"] for g in got] == list(range(1, 24))
    assert any("without DriveThruRPG category" in m for m in msgs)
    assert any("page size 6" in m for m in msgs)  # 50 -> 25 -> 12 -> 6


def test_server_page_cap_does_not_truncate():
    data = _many(23)

    def h(request):
        if request.url.path.endswith("/auth_key"):
            return httpx.Response(200, json={"token": "T"})
        page = int(request.url.params["page"])
        return httpx.Response(200, json=data[(page - 1) * 10: page * 10])  # ignores pageSize=50

    a = DtrpgApi("K", transport=httpx.MockTransport(h), backoff=0)
    assert len(list(a.iter_products(50, progress=lambda m: None))) == 23
