"""Local web UI + JSON API (Flask). Bind to 127.0.0.1 only."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

from .api import DtrpgApi
from .config import Config
from .db import PRODUCT_TYPES, STATUSES, TAG_KINDS, Library
from .suggest import suggest
from .sync import JobManager, download_many, sync_library

STATIC = Path(__file__).parent / "static"


def _filters_from_args(args: Any) -> dict[str, Any]:
    return {
        "q": args.get("q"),
        "publisher": args.get("publisher"),
        "status": args.getlist("status") or None,
        "product_type": args.get("product_type"),
        "favourite": args.get("favourite") in ("1", "true"),
        "min_rating": args.get("min_rating"),
        "players": args.get("players"),
        "downloaded": args.get("downloaded"),
        "tag_ids": args.getlist("tag"),
        "filter_ids": args.getlist("filter"),
        "untagged": args.get("untagged") in ("1", "true"),
        "include_archived": args.get("include_archived") in ("1", "true"),
        "sort": args.get("sort"),
    }


def create_app(cfg: Config, lib: Library | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["cfg"] = cfg
    lib = lib or Library(cfg.db_path)
    jobs = JobManager()

    @app.before_request
    def csrf_guard() -> None:
        # Mutating calls must be JSON (forces a CORS preflight from foreign
        # origins) and, when the browser sends Origin, it must be this host.
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            if not request.is_json:
                abort(415)
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                abort(403)

    @app.errorhandler(ValueError)
    def bad_value(e: ValueError):
        return jsonify(error=str(e)), 400

    # -- static -----------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_file(name: str):
        return send_from_directory(STATIC, name)

    # -- reference data ---------------------------------------------------
    @app.get("/api/meta")
    def meta():
        return jsonify(
            statuses=STATUSES, tag_kinds=TAG_KINDS, product_types=PRODUCT_TYPES,
            tags=lib.tags(), dtrpg_filters=lib.dtrpg_filters(), publishers=lib.publishers(),
            stats=lib.stats(), config=cfg.public(),
        )

    # -- products ---------------------------------------------------------
    @app.get("/api/products")
    def products():
        limit = min(int(request.args.get("limit", 60)), 500)
        offset = int(request.args.get("offset", 0))
        return jsonify(lib.search(_filters_from_args(request.args), limit, offset))

    @app.get("/api/products/<opid>")
    def product(opid: str):
        p = lib.product(opid)
        if not p:
            abort(404)
        return jsonify(p)

    @app.patch("/api/products/<opid>")
    def update_product(opid: str):
        if not lib.product(opid):
            abort(404)
        lib.update_meta(opid, request.get_json())
        return jsonify(lib.product(opid))

    @app.post("/api/products/<opid>/tags")
    def add_tag(opid: str):
        body = request.get_json()
        lib.add_tag(opid, body.get("kind", "tag"), body.get("name", ""))
        return jsonify(lib.product(opid)["tags"])

    @app.delete("/api/products/<opid>/tags/<int:tag_id>")
    def remove_tag(opid: str, tag_id: int):
        lib.remove_tag(opid, tag_id)
        return jsonify(lib.product(opid)["tags"])

    @app.post("/api/bulk/tag")
    def bulk_tag():
        body = request.get_json()
        ids = [str(i) for i in body.get("ids", [])]
        lib.bulk_tag(ids, body.get("kind", "tag"), body.get("name", ""))
        return jsonify(ok=True, count=len(ids))

    @app.post("/api/bulk/meta")
    def bulk_meta():
        body = request.get_json()
        for i in body.get("ids", []):
            lib.update_meta(str(i), body.get("changes", {}))
        return jsonify(ok=True)

    # -- tags -------------------------------------------------------------
    @app.patch("/api/tags/<int:tag_id>")
    def rename_tag(tag_id: int):
        lib.rename_tag(tag_id, request.get_json().get("name", ""))
        return jsonify(lib.tags())

    @app.delete("/api/tags/<int:tag_id>")
    def delete_tag(tag_id: int):
        lib.delete_tag(tag_id)
        return jsonify(lib.tags())

    # -- suggestion -------------------------------------------------------
    @app.get("/api/suggest")
    def api_suggest():
        f = _filters_from_args(request.args)
        picks = suggest(
            lib, f,
            count=min(int(request.args.get("count", 1)), 20),
            weighted=request.args.get("weighted", "1") != "0",
            avoid_recent=int(request.args.get("avoid_recent", 10)),
        )
        return jsonify(picks)

    # -- sync & downloads ---------------------------------------------------
    def _need_key():
        if not cfg.api_key:
            abort(Response(json.dumps({"error": "Set your API key in Settings first"}), 400, mimetype="application/json"))

    @app.post("/api/sync")
    def api_sync():
        _need_key()
        if jobs.running("sync"):
            return jsonify(error="A sync is already running"), 409

        def run(log):
            with DtrpgApi(cfg.api_key, read_timeout=cfg.request_timeout) as api:
                return sync_library(api, lib, cfg, log)

        return jsonify(job=jobs.start("sync", run))

    @app.post("/api/download")
    def api_download():
        _need_key()
        body = request.get_json()
        ids = {str(i) for i in body.get("ids", [])}
        files = [f for f in lib.all_files() if body.get("all") or f["order_product_id"] in ids]
        if idx := body.get("idx"):
            files = [f for f in files if f["idx"] in idx]
        if not files:
            return jsonify(error="Nothing to download"), 400
        force = bool(body.get("force"))
        return jsonify(job=jobs.start("download", lambda log: download_many(cfg, lib, files, log, force)))

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify(jobs.list())

    @app.get("/files/<opid>/<int:idx>")
    def open_file(opid: str, idx: int):
        row = next((f for f in lib.files_for(opid) if f["idx"] == idx), None)
        if not row or not row["local_path"]:
            abort(404)
        path = Path(row["local_path"]).resolve()
        root = Path(cfg.library_path).expanduser().resolve()
        if root not in path.parents or not path.exists():
            abort(404)
        return send_file(path)

    # -- settings ---------------------------------------------------------
    @app.get("/api/config")
    def get_config():
        return jsonify(cfg.public())

    @app.patch("/api/config")
    def set_config():
        body = request.get_json()
        allowed = {"api_key", "library_path", "omit_publisher", "validate_checksums",
                   "include_archived", "threads", "page_size", "fetch_filters", "request_timeout"}
        for k, v in body.items():
            if k not in allowed:
                raise ValueError(f"Unknown setting {k}")
            if k == "api_key" and (not v or v.startswith("••••")):
                continue
            setattr(app.config["cfg"], k, type(getattr(cfg, k))(v))
        cfg.save()
        return jsonify(cfg.public())

    # -- export / import ----------------------------------------------------
    @app.get("/api/export.json")
    def export_json():
        return Response(
            json.dumps(lib.export_metadata(), indent=2, ensure_ascii=False),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=dtrpg-metadata.json"},
        )

    @app.get("/api/export.csv")
    def export_csv():
        rows = lib.export_metadata()
        buf = io.StringIO()
        fields = [k for k in rows[0] if k != "tags"] + list(TAG_KINDS) if rows else ["name"]
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        for r in rows:
            out = {k: v for k, v in r.items() if k != "tags"}
            for kind in TAG_KINDS:
                out[kind] = "; ".join(t["name"] for t in r["tags"] if t["kind"] == kind)
            w.writerow(out)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=dtrpg-library.csv"})

    @app.post("/api/import")
    def import_json():
        return jsonify(imported=lib.import_metadata(request.get_json()))

    return app
