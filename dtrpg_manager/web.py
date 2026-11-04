"""Local web UI + JSON API (Flask). Bind to 127.0.0.1 only."""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

from . import enrich, llm
from .api import DtrpgApi
from .related import RelatedIndex, related
from .config import Config
from . import local as local_books
from . import ocr as ocr_mod
from .db import LINK_KINDS, PRODUCT_TYPES, STATUSES, TAG_KINDS, Library
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
        "any_tag_ids": args.getlist("any_tag"),
        "any_filter_ids": args.getlist("any_filter"),
        "untagged": args.get("untagged") in ("1", "true"),
        "source": args.get("source"),
        "include_archived": args.get("include_archived") in ("1", "true"),
        "sort": args.get("sort"),
    }


def create_app(cfg: Config, lib: Library | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["cfg"] = cfg
    lib = lib or Library(cfg.db_path)
    jobs = JobManager()
    rel_index = RelatedIndex(lib)
    cfg_dir = Path(cfg._path).parent if cfg._path else Path(cfg.db_path).parent

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
            statuses=STATUSES, tag_kinds=TAG_KINDS, product_types=PRODUCT_TYPES, link_kinds=LINK_KINDS,
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
        like = request.args.get("like")
        picks = suggest(
            lib, f,
            similarity=rel_index.scores_for(like) if like else None,
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

        def run(log, cancel):
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
        if jobs.running("download"):
            return jsonify(error="A download is already running; stop it first or wait"), 409
        force = bool(body.get("force"))
        return jsonify(job=jobs.start("download", lambda log, cancel: download_many(cfg, lib, files, log, force, cancel)))

    @app.post("/api/jobs/<jid>/cancel")
    def cancel_job(jid: str):
        if not jobs.cancel(jid):
            abort(404)
        return jsonify(ok=True)

    @app.get("/api/download-holds")
    def download_holds():
        from .sync import on_hold
        out = []
        for f in lib.held_files():
            until = on_hold(f, cfg.hold_failed_hours)
            out.append({**f, "retry_after": until.isoformat(timespec="minutes") if until else None})
        return jsonify(hours=cfg.hold_failed_hours, items=out)

    @app.post("/api/download-holds/clear")
    def clear_holds():
        body = request.get_json()
        if body.get("all"):
            return jsonify(cleared=lib.clear_failed())
        return jsonify(cleared=lib.clear_failed(str(body["order_product_id"]), int(body["idx"])))

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify(jobs.list())

    @app.get("/files/<opid>/<int:idx>")
    def open_file(opid: str, idx: int):
        row = next((f for f in lib.files_for(opid) if f["idx"] == idx), None)
        if not row or not row["local_path"]:
            abort(404)
        path = Path(row["local_path"]).resolve()
        roots = [Path(r).expanduser().resolve() for r in [cfg.library_path, *cfg.local_folders] if r]
        if not any(r in path.parents for r in roots) or not path.exists():
            abort(404)  # only serve files inside the download folder or a configured local folder
        return send_file(path)

    cover_dir = cfg_dir / "covers"

    @app.get("/covers/<name>")
    def cover(name: str):
        if not re.fullmatch(r"local-[0-9a-f]{16}\.jpg", name):
            abort(404)
        return send_from_directory(cover_dir, name)

    @app.post("/api/local/scan")
    def api_local_scan():
        if jobs.running("local scan"):
            return jsonify(error="A scan is already running"), 409
        if not cfg.local_folders:
            raise ValueError("Add at least one local folder in Settings first")
        return jsonify(job=jobs.start("local scan", lambda log, cancel: local_books.scan(
            lib, cfg.local_folders, exclude=[cfg.library_path], cover_dir=cover_dir, log=log, cancel=cancel)))

    @app.get("/api/enrich/ocr")
    def api_ocr_status():
        return jsonify(ocr_mod.status(lib, cfg))

    @app.post("/api/enrich/ocr")
    def api_ocr_run():
        if jobs.running("ocr"):
            return jsonify(error="OCR is already running"), 409
        if not ocr_mod.command(cfg):
            raise ValueError("OCR needs ocrmypdf: sudo apt install ocrmypdf — then restart the app")
        body = request.get_json()
        mode = "full" if body.get("mode") == "full" else "quick"
        return jsonify(job=jobs.start("ocr", lambda log, cancel: ocr_mod.run(
            lib, cfg, log, mode=mode, force=bool(body.get("force")), cancel=cancel, ocr_dir=cfg_dir / "ocr",
            limit=body.get("limit"))))

    @app.get("/ocr/<name>")
    def ocr_copy(name: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.pdf", name):
            abort(404)
        return send_from_directory(cfg_dir / "ocr", name)

    @app.patch("/api/products/<opid>/info")
    def update_info(opid: str):
        lib.update_info(opid, request.get_json())
        return jsonify(lib.product(opid))

    # -- settings ---------------------------------------------------------
    @app.get("/api/config")
    def get_config():
        return jsonify(cfg.public())

    @app.patch("/api/config")
    def set_config():
        body = request.get_json()
        allowed = {"api_key", "library_path", "omit_publisher", "validate_checksums",
                   "include_archived", "threads", "page_size", "fetch_filters", "request_timeout", "download_delay",
                   "anthropic_api_key", "verify_existing", "hold_failed_hours", "local_folders",
                   "ocr_command", "ocr_language", "ocr_keep_pdf"}
        for k, v in body.items():
            if k not in allowed:
                raise ValueError(f"Unknown setting {k}")
            if k in ("api_key", "anthropic_api_key") and (not v or v.startswith("••••")):
                continue
            if k == "local_folders":
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise ValueError("local_folders must be a list of folder paths")
                v = [x.strip() for x in v if x.strip()]
            if k == "threads" and not 1 <= int(v) <= 8:
                raise ValueError("threads must be between 1 and 8")
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

    # -- relationships ---------------------------------------------------------
    @app.get("/api/products/<opid>/related")
    def api_related(opid: str):
        return jsonify(related(lib, rel_index, opid, int(request.args.get("n", 12))))

    @app.post("/api/products/<opid>/links")
    def add_link(opid: str):
        body = request.get_json()
        lib.add_link(opid, str(body["target"]), body.get("kind", "related"), body.get("note"))
        return jsonify(lib.links(opid))

    @app.delete("/api/products/<opid>/links/<target>/<kind>")
    def remove_link(opid: str, target: str, kind: str):
        lib.remove_link(opid, target, kind)
        return jsonify(lib.links(opid))

    # -- enrichment ---------------------------------------------------------------
    @app.get("/api/enrich/config")
    def get_enrich_config():
        return jsonify(enrich.load_config(cfg_dir))

    @app.put("/api/enrich/config")
    def put_enrich_config():
        enrich.save_config(cfg_dir, request.get_json())
        return jsonify(enrich.load_config(cfg_dir))

    @app.post("/api/enrich/dtrpg")
    def api_enrich_dtrpg():
        return jsonify(enrich.apply_dtrpg(lib, enrich.load_config(cfg_dir)))

    @app.post("/api/enrich/rules")
    def api_enrich_rules():
        preview = bool(request.get_json().get("preview"))
        return jsonify(enrich.run_rules(lib, enrich.load_config(cfg_dir), preview=preview))

    @app.post("/api/enrich/pdf")
    def api_enrich_pdf():
        if jobs.running("pdf"):
            return jsonify(error="PDF reading is already running"), 409
        force = bool(request.get_json().get("force"))
        return jsonify(job=jobs.start("pdf", lambda log, cancel: enrich.extract_pdfs(lib, log, force=force, cancel=cancel)))

    @app.get("/api/enrich/llm/estimate")
    def api_llm_estimate():
        return jsonify(llm.estimate(lib, request.args.get("scope", "thin"), request.args.get("pdf", "1") == "1"))

    @app.post("/api/enrich/llm/submit")
    def api_llm_submit():
        body = request.get_json()
        if not body.get("confirm"):
            raise ValueError("Submitting costs money: send confirm=true after reviewing the estimate")
        client = llm.make_client(cfg.anthropic_api_key)
        return jsonify(llm.submit(lib, client, body.get("scope", "thin"), bool(body.get("pdf", True)), body.get("limit")))

    @app.get("/api/enrich/llm/batches")
    def api_llm_batches():
        return jsonify(llm.batches(lib))

    @app.post("/api/enrich/llm/batches/<bid>/collect")
    def api_llm_collect(bid: str):
        return jsonify(llm.collect(lib, llm.make_client(cfg.anthropic_api_key), bid))

    # -- review queue -------------------------------------------------------------
    def _sugg_filters(src) -> dict:
        return {k: src.get(k) for k in ("status", "source", "kind", "min_confidence", "order_product_id")}

    @app.get("/api/suggestions")
    def api_suggestions():
        f = _sugg_filters(request.args)
        return jsonify(lib.suggestions(f, min(int(request.args.get("limit", 100)), 500), int(request.args.get("offset", 0))))

    @app.post("/api/suggestions/<action>")
    def api_decide(action: str):
        if action not in ("accept", "reject"):
            abort(404)
        body = request.get_json()
        f = _sugg_filters(body)
        if body.get("ids"):
            f["ids"] = body["ids"]
        elif not body.get("all_matching"):
            raise ValueError("Pass ids, or all_matching=true with filters")
        return jsonify(decided=lib.decide_suggestions(f, accept=action == "accept"))

    @app.errorhandler(RuntimeError)
    def runtime_error(e: RuntimeError):
        return jsonify(error=str(e)), 400

    return app
