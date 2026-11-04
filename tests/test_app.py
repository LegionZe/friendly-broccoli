import hashlib
import json
import random
import threading
from pathlib import Path

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
        assert "applicationKey" not in request.url.params  # never in the URL
        assert json.loads(request.content)["applicationKey"] == "KEY"
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
    assert res == {"seen": 2, "new": 2, "removed": 0, "skipped": 0}
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
    # by default a checksum that differs from DTRPG's (often stale) record is kept
    other = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"%PDF-rewatermarked")))
    path.unlink()
    assert download_file(api, lib, cfg, files[("1", 0)], http=other) == "downloaded"
    assert path.read_bytes() == b"%PDF-rewatermarked"
    # strict mode rejects it and leaves no file
    cfg.validate_checksums = True
    path.unlink()
    with pytest.raises(Exception, match="Checksum"):
        download_file(api, lib, cfg, files[("1", 0)], http=other)
    assert not path.exists()
    # an HTML error page saved as .pdf is never accepted
    cfg.validate_checksums = False
    html = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>oops")))
    with pytest.raises(Exception, match="not a PDF"):
        download_file(api, lib, cfg, files[("1", 0)], http=html)
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
    got = list(a.iter_products(50, progress=msgs.append, page_retries=1, retry_wait=0))
    assert [g["orderProductId"] for g in got] == list(range(1, 24))
    assert any("without category data" in m for m in msgs)
    assert any("page size 6" in m for m in msgs)  # 50 -> 25 -> 12 -> 6


def test_transient_502_keeps_categories():
    data = _many(7)
    state = {"fail": 2}
    seen_filters = []

    def h(request):
        if request.url.path.endswith("/auth_key"):
            return httpx.Response(200, json={"token": "T"})
        p = request.url.params
        page = int(p["page"])
        if page == 2 and state["fail"]:
            state["fail"] -= 1
            return httpx.Response(502, text="upstream request failed")
        seen_filters.append(p["getFilters"])
        return httpx.Response(200, json=data[(page - 1) * 5: page * 5])

    a = DtrpgApi("K", transport=httpx.MockTransport(h), backoff=0, retries=0)
    got = list(a.iter_products(5, progress=lambda m: None, page_retries=3, retry_wait=0))
    assert len(got) == 7
    assert set(seen_filters) == {"1"}  # never degraded for a transient error


def test_server_page_cap_does_not_truncate():
    data = _many(23)

    def h(request):
        if request.url.path.endswith("/auth_key"):
            return httpx.Response(200, json={"token": "T"})
        page = int(request.url.params["page"])
        return httpx.Response(200, json=data[(page - 1) * 10: page * 10])  # ignores pageSize=50

    a = DtrpgApi("K", transport=httpx.MockTransport(h), backoff=0)
    assert len(list(a.iter_products(50, progress=lambda m: None))) == 23


def _dl_setup(api, lib, cfg):
    sync_library(api, lib, cfg, progress=lambda m: None)
    cfg.download_delay = 0
    return [f for f in lib.all_files() if f["order_product_id"] == "1"]


def test_download_retries_503_with_fresh_link(api, lib, cfg):
    from dtrpg_manager.sync import download_many

    files = _dl_setup(api, lib, cfg)
    responses = iter([httpx.Response(503, headers={"Retry-After": "7", "server": "cloudflare"}, text="busy"),
                      httpx.Response(200, content=PDF)])
    http = httpx.Client(transport=httpx.MockTransport(lambda r: next(responses)))
    slept, msgs = [], []
    res = download_many(cfg, lib, files, msgs.append, api=api, http=http, sleep=slept.append)
    assert res["downloaded"] == 1 and res["failed"] == 0
    assert 7.0 in slept  # honoured Retry-After
    assert any("server=cloudflare" in m for m in msgs)  # response detail surfaced


def test_download_circuit_breaker_stops(api, lib, cfg):
    from dtrpg_manager.sync import download_many

    sync_library(api, lib, cfg, progress=lambda m: None)
    cfg.download_delay = 0
    cfg.threads = 1  # sequential, so the hit count is deterministic
    files = [dict(f, idx=i, filename=f"f{i}.pdf", checksum=None) for i, f in enumerate(lib.all_files() * 10)]
    hits = []

    def always_503(r):
        hits.append(r)
        return httpx.Response(503, text="down")

    msgs = []
    res = download_many(cfg, lib, files, msgs.append, api=api, http=httpx.Client(transport=httpx.MockTransport(always_503)),
                        sleep=lambda s: None, max_attempts=2, breaker_threshold=3)
    assert res["failed"] == 6  # 3 -> pause -> 3 more -> stop
    assert len(hits) == 12  # stopped instead of trying all 30 files
    assert any("stopped" in m for m in msgs)


def test_single_download_job(api, lib, cfg):
    from dtrpg_manager.web import create_app

    sync_library(api, lib, cfg, progress=lambda m: None)
    client = create_app(cfg, lib).test_client()
    import dtrpg_manager.web as web
    started = threading.Event()
    release = threading.Event()
    orig = web.download_many
    web.download_many = lambda *a, **k: (started.set(), release.wait(5))
    try:
        assert client.post("/api/download", json={"all": True}).status_code == 200
        started.wait(5)
        assert client.post("/api/download", json={"all": True}).status_code == 409
        job = client.get("/api/jobs").get_json()[0]["id"]
        assert client.post(f"/api/jobs/{job}/cancel", json={}).status_code == 200
    finally:
        release.set()
        web.download_many = orig


def test_httpx_logging_silenced(cfg, tmp_path, monkeypatch):
    import logging
    from dtrpg_manager import cli

    monkeypatch.setattr(cli, "cmd_export", lambda a: 0)
    cli.main(["--config", str(tmp_path / "c.json"), "export", "x.json"])
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# Layout of a live vBeta order_products item (2026-11-03); values are invented.
LIVE_SHAPE = {
    "orderId": 1, "productId": 13617, "name": "Sample Game", "datePurchased": None, "archived": 0,
    "orderProductId": 5091002, "fileLastModified": "2006-08-29T00:00:00-05:00",
    "files": [{"index": 0, "filename": "sample.pdf", "size": 4266490, "sizeMB": "4.27",
               "checksums": [{"checksum": "abc", "checksumDate": "2021-08-14T19:01:33-05:00"}]}],
    "filters": [
        {"filterId": 100, "parentFilterId": 10, "name": "Fantasy", "parentName": "Genre"},
        {"filterId": 120, "parentFilterId": 100, "name": "Dark Fantasy", "parentName": "Genre"},
        {"filterId": 1840, "parentFilterId": 1800, "name": "Chronicles of Darkness", "parentName": "Rule System"},
    ],
    "publisher": {"name": "Sample Press", "publisherId": 385, "slug": "sample-press"},
    "product": {"image": "385/13617.jpg", "thumbnail": "385/13617-thumb140.jpg",
                "description": {"name": "Sample Game Classic", "slug": "sample-game", "shortDescription": "A test."}},
    "order": {"dateCreated": "2010-03-08T04:11:05-06:00", "orderId": 1},
}


def test_live_shape():
    r = normalize_product(LIVE_SHAPE)
    assert r["date_purchased"] == "2010-03-08T04:11:05-06:00"
    assert r["cover_url"] == "https://d1vzi28wh99zvq.cloudfront.net/images/385/13617.jpg"
    assert r["thumb_url"].endswith("385/13617-thumb140.jpg")
    assert r["description"] == "A test." and r["slug"] == "sample-game"
    assert r["files"][0]["size"] == 4266490
    dark = next(f for f in r["filters"] if f["name"] == "Dark Fantasy")
    assert dark == {"id": "120", "name": "Dark Fantasy", "parent_id": "100", "group_name": "Genre"}
    lib = Library(":memory:")
    lib.upsert_product(r, LIVE_SHAPE)
    assert lib.search({"filter_ids": ["100"]})["total"] == 1  # parent matches descendants
    assert lib.search({"q": "A test"})["total"] == 1  # description searchable


def test_migration_adds_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite3"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE products (order_product_id TEXT PRIMARY KEY, product_id TEXT, name TEXT NOT NULL,"
                    " publisher TEXT, date_purchased TEXT, file_last_modified TEXT, archived INTEGER NOT NULL DEFAULT 0,"
                    " cover_url TEXT, raw_json TEXT, first_seen TEXT NOT NULL, last_synced TEXT NOT NULL,"
                    " removed INTEGER NOT NULL DEFAULT 0);")
    c.close()
    lib = Library(db)
    lib.upsert_product(normalize_product(LIVE_SHAPE), LIVE_SHAPE)
    assert lib.product("5091002")["thumb_url"]


def test_poison_product_is_skipped_not_fatal():
    data = _many(40)
    bad = 23  # 1-based position whose presence makes any page fail

    def h(request):
        if request.url.path.endswith("/auth_key"):
            return httpx.Response(200, json={"token": "T"})
        size, page = int(request.url.params["pageSize"]), int(request.url.params["page"])
        lo, hi = (page - 1) * size, page * size
        if lo < bad <= hi:
            return httpx.Response(502, text="upstream request failed")
        return httpx.Response(200, json=data[lo:hi])

    a = DtrpgApi("K", transport=httpx.MockTransport(h), backoff=0, retries=0)
    msgs = []
    got = list(a.iter_products(10, progress=msgs.append, page_retries=0, retry_wait=0))
    ids = [g["orderProductId"] for g in got]
    assert ids == [i for i in range(1, 41) if i != bad]
    assert a.last_skipped == [bad]
    assert any("Skipping product #23" in m for m in msgs)


# ---------------------------------------------------------------- enrichment
from types import SimpleNamespace as NS

from dtrpg_manager import enrich, llm
from dtrpg_manager.related import RelatedIndex, related, series_key


def _lib_with(products):
    lib = Library(":memory:")
    for raw in products:
        lib.upsert_product(normalize_product(raw), raw)
    return lib


def _prod(opid, name, filters=(), desc="", publisher="Pub"):
    return {"orderProductId": opid, "productId": opid, "name": name, "publisher": {"name": publisher},
            "files": [], "filters": list(filters), "product": {"description": {"shortDescription": desc}}}


GENRE_DARK = [{"filterId": 100, "parentFilterId": 10, "name": "Fantasy", "parentName": "Genre"},
              {"filterId": 120, "parentFilterId": 100, "name": "Dark Fantasy", "parentName": "Genre"}]
PT_ADV = [{"filterId": 2150, "parentFilterId": 20, "name": "Supplements & Expansions", "parentName": "Product Type"},
          {"filterId": 2110, "parentFilterId": 2150, "name": "Campaigns, Adventures & Modules", "parentName": "Product Type"}]
SYS = [{"filterId": 10146, "parentFilterId": 9999, "name": "GUMSHOE", "parentName": "Rule System"},
       {"filterId": 9999, "parentFilterId": 30, "name": "Other systems", "parentName": "Rule System"}]


def test_pass1_dtrpg_mapping_and_respects_removals():
    lib = _lib_with([_prod(1, "Crypt", GENRE_DARK + PT_ADV + SYS)])
    conf = enrich.DEFAULT_CONFIG
    stats = enrich.apply_dtrpg(lib, conf)
    tags = {(t["kind"], t["name"], t["source"]) for t in lib.product("1")["tags"]}
    assert ("genre", "Dark Fantasy", "dtrpg") in tags and ("genre", "Fantasy", "dtrpg") in tags  # ancestor added
    assert ("system", "GUMSHOE", "dtrpg") in tags
    assert not any(n == "Other systems" for _, n, _ in tags)  # umbrella label skipped
    assert lib.product("1")["product_type"] == "adventure"  # deepest Product Type wins
    assert stats["tags_added"] == 3
    # removing a DTRPG tag is remembered; re-running doesn't re-add it
    fantasy = next(t["id"] for t in lib.product("1")["tags"] if t["name"] == "Fantasy")
    lib.remove_tag("1", fantasy)
    enrich.apply_dtrpg(lib, conf)
    assert not any(t["name"] == "Fantasy" for t in lib.product("1")["tags"])
    # a manual add upgrades provenance; manual tags survive
    lib.add_tag("1", "genre", "Dark Fantasy")
    assert next(t for t in lib.product("1")["tags"] if t["name"] == "Dark Fantasy")["source"] == "manual"


def test_pass2_rules_queue_and_review():
    lib = _lib_with([_prod(1, "Solo Delve: a 5e one-shot", desc="For 2-4 players"), _prod(2, "Plain Book")])
    conf = enrich.load_config(Path("/nonexistent"))
    prev = enrich.run_rules(lib, conf, preview=True)
    assert prev["matches"]["dnd5e"] == 1 and prev["queued"] == 0
    assert lib.suggestions({})["total"] == 0
    res = enrich.run_rules(lib, conf)
    names = {(s["kind"], s["name"]) for s in lib.suggestions({})["items"]}
    assert {("system", "D&D 5e"), ("tag", "one-shot"), ("players", "2-4")} <= names
    # running again doesn't duplicate
    enrich.run_rules(lib, conf)
    assert lib.suggestions({})["total"] == res["queued"]
    # accept players + system, reject one-shot
    ids = {(s["kind"], s["name"]): s["id"] for s in lib.suggestions({})["items"]}
    lib.decide_suggestions({"ids": [ids[("players", "2-4")], ids[("system", "D&D 5e")]]}, accept=True)
    lib.decide_suggestions({"ids": [ids[("tag", "one-shot")]]}, accept=False)
    p = lib.product("1")
    assert (p["min_players"], p["max_players"]) == (2, 4)
    assert next(t for t in p["tags"] if t["name"] == "D&D 5e")["source"] == "rule"
    enrich.run_rules(lib, conf)  # rejected stays rejected
    assert not any(s["name"] == "one-shot" for s in lib.suggestions({})["items"])


def test_enrich_config_validation(tmp_path):
    conf = enrich.load_config(tmp_path)
    conf["rules"].append({"id": "bad", "pattern": "(", "kind": "tag", "name": "x"})
    with pytest.raises(ValueError, match="bad pattern"):
        enrich.save_config(tmp_path, conf)
    conf["rules"][-1] = {"id": "ok", "pattern": "dragon", "kind": "tag", "name": "dragons"}
    enrich.save_config(tmp_path, conf)
    assert enrich.load_config(tmp_path)["rules"][-1]["name"] == "dragons"


def test_pass3_pdf_extraction(tmp_path):
    from pypdf import PdfWriter
    pdf = tmp_path / "book.pdf"
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_metadata({"/Title": "Book of Things"})
    with pdf.open("wb") as fh:
        w.write(fh)
    lib = _lib_with([{**_prod(1, "Book"), "files": [{"index": 0, "filename": "book.pdf", "size": 10}]}])
    lib.mark_downloaded("1", 0, str(pdf))
    stats = enrich.extract_pdfs(lib, lambda m: None)
    assert stats["extracted"] == 1
    assert lib.product("1")["pages"] == 1
    assert enrich.extract_pdfs(lib, lambda m: None)["skipped"] == 1  # unchanged file not re-read


class FakeBatches:
    def __init__(self, reply):
        self.reply, self.created = reply, None

    def create(self, requests):
        self.created = requests
        return NS(id="msgbatch_1", processing_status="in_progress")

    def retrieve(self, bid):
        return NS(processing_status="ended", request_counts=NS(processing=0, succeeded=len(self.created)))

    def results(self, bid):
        for r in self.created:
            text = json.dumps(self.reply)
            msg = NS(stop_reason="end_turn", content=[NS(type="text", text=text)],
                     usage=NS(input_tokens=1000, output_tokens=100))
            yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=msg))


def test_pass4_llm_batch_roundtrip():
    lib = _lib_with([_prod(1, "Haunted Keep", desc="A gothic horror dungeon for 3-5 players")])
    est = llm.estimate(lib)
    assert est["products"] == 1 and est["model"] == "claude-haiku-5-5" and est["est_cost_usd_batch"] < 0.01
    reply = {"tags": [{"kind": "genre", "name": "gothic horror", "new": True, "confidence": 0.9},
                      {"kind": "category", "name": "dungeon", "new": True, "confidence": 0.8},
                      {"kind": "tag", "name": "guess", "new": True, "confidence": 0.1},      # below threshold
                      {"kind": "bogus", "name": "x", "new": False, "confidence": 0.9}],      # invalid kind
             "product_type": "adventure", "min_players": 3, "max_players": 5}
    fake = NS(messages=NS(batches=FakeBatches(reply)))
    sub = llm.submit(lib, fake)
    req = fake.messages.batches.created[0]
    assert sub["submitted"] == 1 and req["params"]["model"] == "claude-haiku-5-5"
    assert req["params"]["output_config"]["format"]["type"] == "json_schema"
    assert req["params"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert llm.candidates(lib) == []  # pending batch is not resubmitted
    res = llm.collect(lib, fake, "msgbatch_1")
    assert res["status"] == "imported" and res["succeeded"] == 1
    got = {(s["kind"], s["name"]) for s in lib.suggestions({"source": "llm"})["items"]}
    assert got == {("genre", "gothic horror"), ("category", "dungeon"), ("product_type", "adventure"), ("players", "3-5")}
    assert llm.candidates(lib) == []  # same input already classified


def test_related_similarity_lines_and_links():
    lib = _lib_with([
        _prod(1, "Delta Green: Agent's Handbook", SYS + GENRE_DARK, "conspiracy horror agents", "Arc Dream"),
        _prod(2, "Delta Green: Handler's Guide", SYS + GENRE_DARK, "conspiracy horror handler", "Arc Dream"),
        _prod(3, "Night's Black Agents", SYS, "vampire spy thriller agents", "Pelgrane"),
        _prod(4, "Sunny Meadows", [], "cheerful farming picnic", "Other"),
        _prod(5, "Trail of Cthulhu", SYS + GENRE_DARK, "investigation horror", "Pelgrane"),
    ])
    idx = RelatedIndex(lib)
    sims = idx.similar("1")
    assert sims[0]["order_product_id"] == "2" and sims[0]["why"]
    assert "4" not in [s["order_product_id"] for s in sims]
    assert series_key("Delta Green: Agent's Handbook") == "delta green"
    res = related(lib, idx, "1")
    assert [x["order_product_id"] for x in res["same_line"]] == ["2"]
    lib.add_link("1", "3", "related")
    assert related(lib, idx, "3")["links"][0]["direction"] == "in"
    with pytest.raises(ValueError):
        lib.add_link("1", "1", "related")
    # "something like this" restricts the random pool to similar products
    picks = suggest(lib, {}, count=3, similarity=idx.scores_for("1"), rng=random.Random(0))
    assert "4" not in [p["order_product_id"] for p in picks]


def test_web_enrich_endpoints(cfg, tmp_path):
    from dtrpg_manager.web import create_app

    cfg._path = tmp_path / "config.json"
    lib = _lib_with([_prod(1, "Crypt: a solo adventure", GENRE_DARK), _prod(2, "Tower: a solo adventure", GENRE_DARK)])
    c = create_app(cfg, lib).test_client()
    assert c.post("/api/enrich/dtrpg", json={}).get_json()["tags_added"] == 4
    assert c.post("/api/enrich/rules", json={"preview": False}).get_json()["queued"] == 2
    assert c.get("/api/suggestions").get_json()["total"] == 2
    assert c.post("/api/suggestions/accept", json={"source": "rule"}).status_code == 400  # needs all_matching
    assert c.post("/api/suggestions/accept", json={"source": "rule", "all_matching": True}).get_json()["decided"] == 2
    assert c.get("/api/products/1/related").get_json()["similar"][0]["order_product_id"] == "2"
    assert c.post("/api/products/1/links", json={"target": "2", "kind": "sequel-of"}).status_code == 200
    assert c.post("/api/enrich/llm/submit", json={}).status_code == 400  # must confirm cost
    conf = c.get("/api/enrich/config").get_json()
    assert c.put("/api/enrich/config", json=conf).status_code == 200
    assert (tmp_path / "enrich.json").exists()


def test_adaptive_limit():
    from dtrpg_manager.sync import AdaptiveLimit
    lim = AdaptiveLimit(8, grow_after=2)
    assert lim.pushback() == 4 and lim.pushback() == 2
    assert lim.success() is None and lim.success() == 3
    ev = threading.Event()
    lim.acquire(ev); lim.acquire(ev); lim.acquire(ev)
    assert lim.active == 3
    for _ in range(3):
        lim.release()


def test_parallel_downloads_back_off_on_503(api, lib, cfg):
    from dtrpg_manager.sync import download_many

    sync_library(api, lib, cfg, progress=lambda m: None)
    cfg.download_delay, cfg.threads, cfg.validate_checksums = 0, 4, False
    files = [dict(f, idx=i, filename=f"f{i}.pdf", checksum=None, size=None)
             for i, f in enumerate(lib.all_files() * 6)]
    calls = {"n": 0}
    lock = threading.Lock()

    def flaky(r):
        with lock:
            calls["n"] += 1
            n = calls["n"]
        return httpx.Response(503, text="busy") if n in (2, 3) else httpx.Response(200, content=PDF)

    msgs = []
    res = download_many(cfg, lib, files, msgs.append, api=api,
                        http=httpx.Client(transport=httpx.MockTransport(flaky)), sleep=lambda s: None)
    assert res["downloaded"] == len(files) and res["failed"] == 0
    assert any("reduced to" in m for m in msgs)


def test_quick_uptodate_check(tmp_path):
    from dtrpg_manager.sync import needs_download
    f = tmp_path / "a.pdf"
    f.write_bytes(b"12345")
    # DTRPG's own size/checksum are ignored by the quick check (they describe older copies)
    assert needs_download(f, {"size": 999, "checksum": "nope"}, None, use_checksum=False) is None
    # a file this app downloaded must keep its recorded size
    mine = {"local_path": str(f), "local_size": 5, "local_md5": hashlib.md5(b"12345").hexdigest()}
    assert needs_download(f, mine, None, use_checksum=False) is None
    assert needs_download(f, {**mine, "local_size": 6}, None, use_checksum=False) == "size changed"
    # verify mode compares with the recorded MD5 first
    assert needs_download(f, {**mine, "checksum": "stale"}, None, use_checksum=True) is None
    assert needs_download(f, {"checksum": "nope"}, None, use_checksum=True) == "checksum mismatch"
    assert needs_download(tmp_path / "missing.pdf", {}, None, use_checksum=False) == "missing"
    assert needs_download(f, {}, "2999-01-01T00:00:00+00:00", use_checksum=False) == "outdated"


def test_saved_rules_still_get_new_builtin_rules(tmp_path):
    old = {"rules": [{"id": "solo", "pattern": "alone", "kind": "tag", "name": "solo"},
                     {"id": "zine", "pattern": "zine", "kind": "tag", "name": "zine", "disabled": True}]}
    (tmp_path / "enrich.json").write_text(json.dumps(old))
    conf = enrich.load_config(tmp_path)
    by_id = {r["id"]: r for r in conf["rules"]}
    assert by_id["solo"]["pattern"] == "alone"            # saved version wins
    assert "battlemap" in by_id and "mappack" in by_id     # new built-ins appear
    lib = _lib_with([_prod(1, "Zine of Battle Maps")])
    res = enrich.run_rules(lib, conf, preview=True)
    assert "zine" not in res["matches"]                    # disabled rule skipped
    assert res["matches"]["battlemap"] == 1


def test_map_rules():
    lib = _lib_with([
        _prod(1, "Tavern Battle Map"), _prod(2, "Castle Map Pack"), _prod(3, "Geomorphs: Caverns"),
        _prod(4, "The Lost Mine", desc="An adventure with full-colour maps."),
        _prod(5, "Ruins", desc="Includes battlemaps for every encounter"),
    ])
    enrich.run_rules(lib, enrich.DEFAULT_CONFIG)
    got = {(s["order_product_id"], s["kind"], s["name"]) for s in lib.suggestions({})["items"]}
    assert ("1", "category", "battlemap") in got and ("1", "product_type", "map") in got
    assert ("2", "category", "map pack") in got and ("2", "product_type", "map") in got
    assert ("3", "category", "geomorphs") in got
    assert not any(o == "4" for o, _, _ in got)            # adventure merely mentioning maps
    assert ("5", "category", "battlemap") in got and ("5", "product_type", "map") not in got


def test_failed_checks_are_held_for_24h(api, lib, cfg):
    from dtrpg_manager.sync import download_many, on_hold

    sync_library(api, lib, cfg, progress=lambda m: None)
    cfg.download_delay, cfg.threads, cfg.validate_checksums = 0, 1, True  # strict: mismatch fails
    files = [f for f in lib.all_files() if f["order_product_id"] == "1"]
    hits = []

    def bad(r):
        hits.append(r)
        return httpx.Response(200, content=b"%PDF-not-the-expected-bytes")

    http = httpx.Client(transport=httpx.MockTransport(bad))
    run = lambda **k: download_many(cfg, lib, [f for f in lib.all_files() if f["order_product_id"] == "1"],
                                    lambda m: None, api=api, http=http, sleep=lambda s: None, **k)
    assert run()["failed"] == 1
    held = lib.held_files()
    assert len(held) == 1 and "Checksum mismatch" in held[0]["fail_reason"]
    assert on_hold(lib.all_files()[0] if lib.all_files()[0]["failed_at"] else held[0], 24)
    n = len(hits)
    res = run()
    assert res["on hold"] == 1 and res["failed"] == 0 and len(hits) == n   # skipped, nothing fetched
    run(force=True)                                                         # single-file download ignores hold
    assert len(hits) == n + 1
    cfg.hold_failed_hours = 0                                               # 0 = never hold
    assert run()["on hold"] == 0
    cfg.hold_failed_hours = 24
    lib.clear_failed()
    assert lib.held_files() == []
    # a successful download clears the hold
    lib.mark_failed("1", 0, "Checksum mismatch")
    cfg.validate_checksums = False
    lib.clear_failed("1", 0)
    assert run()["downloaded"] == 1 and lib.held_files() == []


def test_web_download_holds(api, lib, cfg):
    from dtrpg_manager.web import create_app

    sync_library(api, lib, cfg, progress=lambda m: None)
    lib.mark_failed("1", 0, "Checksum mismatch for x")
    c = create_app(cfg, lib).test_client()
    r = c.get("/api/download-holds").get_json()
    assert r["hours"] == 24 and r["items"][0]["retry_after"]
    assert c.post("/api/download-holds/clear", json={"order_product_id": "1", "idx": 0}).get_json()["cleared"] == 1
    assert c.get("/api/download-holds").get_json()["items"] == []
    assert c.patch("/api/config", json={"hold_failed_hours": 48}).get_json()["hold_failed_hours"] == 48


def test_map_rules_wider():
    lib = _lib_with([_prod(1, "Modern Floorplans: Diner"), _prod(2, "City Map 01"),
                     _prod(3, "DCC #46: The Book of Treasure Maps"), _prod(4, "VTT Character Token Pack"),
                     _prod(5, "Mapping the Unknown")])
    enrich.run_rules(lib, enrich.DEFAULT_CONFIG)
    got = {(s["order_product_id"], s["name"]) for s in lib.suggestions({})["items"]}
    assert {("1", "floorplans"), ("1", "map"), ("2", "map"), ("4", "tokens")} <= got
    assert ("3", "map") not in got and not any(o == "5" for o, _ in got)


def test_must_have_and_any_one_of():
    lib = _lib_with([_prod(1, "Horror SF", GENRE_DARK), _prod(2, "Space"), _prod(3, "Western"), _prod(4, "Both")])
    h = lib.get_or_create_tag("genre", "horror"); s = lib.get_or_create_tag("genre", "sci-fi")
    solo = lib.get_or_create_tag("tag", "solo")
    for opid, tid in (("1", h), ("1", s), ("2", s), ("4", h), ("4", solo)):
        lib._x("INSERT INTO product_tags (order_product_id, tag_id) VALUES (?,?)", (opid, tid))
    ids = lambda f: sorted(i["order_product_id"] for i in lib.search(f)["items"])
    assert ids({"any_tag_ids": [h, s]}) == ["1", "2", "4"]                    # OR
    assert ids({"tag_ids": [h, s]}) == ["1"]                                  # AND
    assert ids({"tag_ids": [solo], "any_tag_ids": [h, s]}) == ["4"]           # must AND (any OR)
    assert ids({"any_tag_ids": [s], "any_filter_ids": ["100"]}) == ["1", "2"]  # OR across tags + DTRPG (with descendants)
    from dtrpg_manager.web import _filters_from_args
    from werkzeug.datastructures import MultiDict
    f = _filters_from_args(MultiDict([("any_tag", "3"), ("any_tag", "4"), ("tag", "1")]))
    assert f["any_tag_ids"] == ["3", "4"] and f["tag_ids"] == ["1"]


def test_dtrpg_counts_roll_up_to_parents():
    lib = _lib_with([_prod(1, "A", GENRE_DARK[1:]), _prod(2, "B", GENRE_DARK), _prod(3, "C", GENRE_DARK[:1])])
    # product 1 has only "Dark Fantasy" (child); 2 has both; 3 only "Fantasy" (parent)
    lib.upsert_product(normalize_product(_prod(9, "Dummy", GENRE_DARK)), {})  # ensure both rows exist
    fl = {f["name"]: f for f in lib.dtrpg_filters()}
    assert fl["Fantasy"]["count"] == lib.search({"filter_ids": ["100"]})["total"] == 4
    assert fl["Dark Fantasy"]["count"] == 3 and fl["Dark Fantasy"]["direct"] == 3
    assert fl["Fantasy"]["direct"] == 3


# ---------------------------------------------------------------- local books
def _make_pdf(path, title=None, with_image=False):
    from pypdf import PdfWriter
    path.parent.mkdir(parents=True, exist_ok=True)
    if with_image:
        from PIL import Image
        Image.new("RGB", (300, 420), (120, 30, 30)).save(path, "PDF")
        if title:
            from pypdf import PdfReader
            w = PdfWriter(clone_from=PdfReader(str(path)))
            w.add_metadata({"/Title": title})
            with path.open("wb") as fh:
                w.write(fh)
        return path
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    if title:
        w.add_metadata({"/Title": title})
    with path.open("wb") as fh:
        w.write(fh)
    return path


def test_local_scan(tmp_path):
    from dtrpg_manager.local import scan

    books, dl = tmp_path / "books", tmp_path / "dtrpg"
    _make_pdf(books / "Shadowrun" / "SR5_Core_Rulebook.pdf", title="Shadowrun Fifth Edition Core", with_image=True)
    _make_pdf(books / "misc" / "Weird_Thing (1).pdf")
    (books / "novels").mkdir()
    (books / "novels" / "story.epub").write_bytes(b"PK epub-ish")
    (books / ".hidden.pdf").write_bytes(b"%PDF hidden")
    _make_pdf(dl / "Pub" / "Bought.pdf")                       # the DTRPG download folder: excluded
    lib = Library(":memory:")
    lib.upsert_product(normalize_product(PRODUCTS[0]), PRODUCTS[0])  # a DTRPG product alongside
    covers = tmp_path / "covers"
    st = scan(lib, [str(books), str(dl)], exclude=[str(dl)], cover_dir=covers, log=lambda m: None)
    assert st["found"] == 3 and st["new"] == 3
    local = {i["name"]: i for i in lib.search({"source": "local"})["items"]}
    assert set(local) == {"Shadowrun Fifth Edition Core", "Weird Thing", "Story"}
    sr = local["Shadowrun Fifth Edition Core"]
    assert sr["thumb_url"].startswith("/covers/local-") and (covers / sr["thumb_url"].split("/")[-1]).exists()
    assert "Shadowrun" in sr["description"]                      # folder name feeds rules / AI
    assert lib.search({"source": "dtrpg"})["total"] == 1
    # unchanged rescan does nothing
    assert scan(lib, [str(books)], exclude=[str(dl)], log=lambda m: None)["unchanged"] == 3
    # tags + edits survive a rename/move (content-based id)
    lib.add_tag(sr["order_product_id"], "system", "Shadowrun 5e")
    lib.update_info(sr["order_product_id"], {"name": "SR5 Core", "publisher": "Catalyst"})
    (books / "Shadowrun" / "SR5_Core_Rulebook.pdf").rename(books / "core.pdf")
    scan(lib, [str(books)], exclude=[str(dl)], log=lambda m: None)
    p = lib.product(sr["order_product_id"])
    assert p["name"] == "SR5 Core" and p["publisher"] == "Catalyst" and not p["removed"]
    assert p["files"][0]["local_path"].endswith("core.pdf") and p["tags"][0]["name"] == "Shadowrun 5e"
    # an identical copy counts once
    import shutil
    shutil.copy(books / "core.pdf", books / "misc" / "copy.pdf")
    assert scan(lib, [str(books)], exclude=[str(dl)], log=lambda m: None)["new"] == 0
    # deleted file -> removed; unavailable folder -> kept
    (books / "misc" / "Weird_Thing (1).pdf").unlink()
    assert scan(lib, [str(books)], exclude=[str(dl)], log=lambda m: None)["removed"] == 1
    st = scan(lib, [str(books), str(tmp_path / "unplugged")], exclude=[str(dl)], log=lambda m: None)
    assert st["removed"] == 0
    # DTRPG sync's removal pass and downloads leave local books alone
    assert lib.mark_removed_except({PRODUCTS[0]["orderProductId"].__str__()}, False) == 0
    assert all(f["order_product_id"].startswith("local-") is False for f in lib.all_files())
    with pytest.raises(ValueError):
        lib.update_info("1", {"name": "x"})                       # DTRPG products aren't editable


def test_local_web(cfg, tmp_path):
    from dtrpg_manager.web import create_app

    cfg._path = tmp_path / "config.json"
    books = tmp_path / "books"
    _make_pdf(books / "A Book.pdf", with_image=True)
    lib = Library(":memory:")
    c = create_app(cfg, lib).test_client()
    assert c.post("/api/local/scan", json={}).status_code == 400          # no folders yet
    assert c.patch("/api/config", json={"local_folders": "not a list"}).status_code == 400
    assert c.patch("/api/config", json={"local_folders": [str(books), "  "]}).get_json()["local_folders"] == [str(books)]
    from dtrpg_manager.local import scan
    scan(lib, cfg.local_folders, cover_dir=tmp_path / "covers", log=lambda m: None)
    item = c.get("/api/products?source=local").get_json()["items"][0]
    assert c.get(f"/files/{item['order_product_id']}/0").status_code == 200   # inside a local folder
    assert c.get(item["thumb_url"]).status_code == 200
    assert c.get("/covers/..%2Fconfig.json").status_code == 404
    r = c.patch(f"/api/products/{item['order_product_id']}/info", json={"publisher": "Me"})
    assert r.get_json()["publisher"] == "Me"
    cfg.local_folders = []                                                    # folder removed from settings
    assert c.get(f"/files/{item['order_product_id']}/0").status_code == 404


def test_clean_name_examples():
    from dtrpg_manager.names import clean_name, needs_ai
    cases = {
        "01640 - The Battletech Compendium": ("The Battletech Compendium", None),
        "TSR 9241 - GAZ10 - The Orcs of Thar": ("GAZ10 - The Orcs of Thar", "TSR"),
        "WEG40129 Adventure Journal 1-4": ("Adventure Journal 1-4", "West End Games"),
        "fasa 7108 - shadowrun - rigger black book": ("Shadowrun - Rigger Black Book", "FASA"),
        "(03)Dark Passions eBook 22Jan10": ("Dark Passions", None),
        "Monster of the Week - Tome of Mysteries Playbooks - 2020-07-22 Update":
            ("Monster of the Week - Tome of Mysteries Playbooks", None),
        "TBZ RULEBOOK FINAL": ("TBZ Rulebook", None),
        "Ironsworn-Starforged-Rules-Summary": ("Ironsworn Starforged Rules Summary", None),
        "100 Fantasy Kingdoms": ("100 Fantasy Kingdoms", None),
        "Cyberpunk 2020 - CP3131 Eurotour": ("Cyberpunk 2020 - CP3131 Eurotour", None),
        "MUH052213 STA BRIEFS TOS Mysteries v1.0": ("STA BRIEFS TOS Mysteries", "Modiphius Entertainment"),
    }
    for raw, want in cases.items():
        assert clean_name(raw) == want, raw
    for raw in ("startrekadventures discoverycrewpackseason3", "Acr1326", "1417108632783", "M5"):
        assert needs_ai(clean_name(raw)[0]), raw
    assert clean_name("warhammerfantasyroleplaywindsofmagic")[0] == "warhammerfantasyroleplaywindsofmagic"


def _local(lib, opid, name, publisher=None):
    rec = {"order_product_id": opid, "product_id": None, "name": name, "publisher": publisher,
           "date_purchased": None, "file_last_modified": None, "archived": 0, "cover_url": None,
           "thumb_url": None, "description": "Local file", "slug": None, "filters": [],
           "files": [{"idx": 0, "filename": name + ".pdf", "checksum": None, "checksum_date": None,
                      "size": 1, "last_modified": None}]}
    lib.upsert_product(rec, {}, source="local")


def test_name_suggestions_alongside_rules():
    lib = Library(":memory:")
    _local(lib, "local-a", "TSR 9068 - X4 - Master of the Desert Nomads")
    _local(lib, "local-b", "Good Name", publisher="Someone")
    lib.upsert_product(normalize_product(_prod(7, "TSR 1234 - DTRPG Product")), {})
    res = enrich.run_rules(lib, enrich.DEFAULT_CONFIG)
    got = {(s["order_product_id"], s["kind"], s["name"]) for s in lib.suggestions({})["items"]}
    assert ("local-a", "name", "X4 - Master of the Desert Nomads") in got
    assert ("local-a", "publisher", "TSR") in got
    assert not any(o in ("local-b", "7") for o, _, _ in got)    # tidy name / DTRPG product untouched
    assert res["matches"]["names"] == 1
    lib.decide_suggestions({"source": "rule:names", "kind": "name"}, accept=True)
    assert lib.product("local-a")["name"] == "X4 - Master of the Desert Nomads"
    enrich.run_rules(lib, enrich.DEFAULT_CONFIG)                 # nothing new once tidy
    assert lib.suggestions({"kind": "name"})["total"] == 0


def test_llm_suggests_titles_for_local_only():
    lib = Library(":memory:")
    _local(lib, "local-x", "startrekadventures discoverycrewpackseason3")
    lib.upsert_product(normalize_product(_prod(5, "A DTRPG Book")), {})
    reply = {"tags": [], "product_type": None, "min_players": None, "max_players": None,
             "title": "Star Trek Adventures: Discovery Crew Pack Season 3", "publisher": "Modiphius Entertainment"}
    fake = NS(messages=NS(batches=FakeBatches(reply)))
    llm.submit(lib, fake)
    msgs = {r["custom_id"]: r["params"]["messages"][0]["content"] for r in fake.messages.batches.created}
    assert "Source: local file" in msgs["plocal-x"] and "Source: local file" not in msgs["p5"]
    llm.collect(lib, fake, "msgbatch_1")
    got = {(s["order_product_id"], s["kind"], s["name"]) for s in lib.suggestions({"source": "llm"})["items"]}
    assert ("local-x", "name", "Star Trek Adventures: Discovery Crew Pack Season 3") in got
    assert ("local-x", "publisher", "Modiphius Entertainment") in got
    assert not any(o == "5" for o, _, _ in got)                  # never renames DTRPG products


def test_cover_fallback_renders_page(tmp_path):
    import shutil
    from dtrpg_manager.local import pdf_info
    if not shutil.which("pdftoppm"):
        pytest.skip("pdftoppm not installed")
    pdf = _make_pdf(tmp_path / "vector.pdf")                     # blank page, no embedded image
    info = pdf_info(pdf, tmp_path / "covers", "local-0123456789abcdef")
    assert info["cover"] == "/covers/local-0123456789abcdef.jpg"
    assert (tmp_path / "covers" / "local-0123456789abcdef.jpg").exists()
