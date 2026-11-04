"""Command-line entry point: ``dtrpg-manager <command>``."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
import webbrowser
from pathlib import Path

from .api import DtrpgApi
from .config import Config
from .db import Library
from .suggest import suggest
from .sync import download_many, sync_library


def _cfg(args: argparse.Namespace) -> Config:
    return Config.load(Path(args.config) if args.config else None)


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print("Create a key under \"Library App Keys\" at https://www.drivethrurpg.com/en/account/settings")
    key = getpass.getpass("DriveThruRPG API key (input hidden, blank keeps current): ").strip()
    if key:
        cfg.api_key = key
    lib = input(f"Library folder [{cfg.library_path}]: ").strip()
    if lib:
        cfg.library_path = lib
    cfg.save()
    print(f"Saved {cfg._path}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if args.archived:
        cfg.include_archived = True
    if args.no_filters:
        cfg.fetch_filters = False
    with DtrpgApi(cfg.api_key, read_timeout=cfg.request_timeout) as api:
        sync_library(api, Library(cfg.db_path), cfg)
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    lib = Library(cfg.db_path)
    files = lib.all_files()
    if args.ids:
        files = [f for f in files if f["order_product_id"] in set(args.ids)]
    if args.dry_run:
        for f in files:
            print(f"{f['product_name']} / {f['filename']}")
        print(f"{len(files)} files")
        return 0
    res = download_many(cfg, lib, files, print, force=args.force)
    return 1 if res["failed"] else 0


def cmd_suggest(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    lib = Library(cfg.db_path)
    tag_ids = []
    for spec in args.tag or []:
        kind, _, name = spec.partition(":") if ":" in spec else ("tag", "", spec)
        match = [t for t in lib.tags() if t["kind"] == kind and t["name"].lower() == name.lower()]
        if not match:
            print(f"No such tag {kind}:{name}", file=sys.stderr)
            return 2
        tag_ids.append(match[0]["id"])
    picks = suggest(
        lib,
        {"q": args.query, "publisher": args.publisher, "status": args.status, "tag_ids": tag_ids,
         "players": args.players},
        count=args.count,
        weighted=not args.uniform,
    )
    if not picks:
        print("Nothing matches those filters.")
        return 1
    for p in picks:
        tags = ", ".join(f"{t['kind']}:{t['name']}" for t in p["tags"])
        print(f"* {p['name']} — {p['publisher'] or '?'}  [{p['status']}]{'  ' + tags if tags else ''}")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Dump raw API JSON so the real field structure (filters etc.) can be inspected."""
    cfg = _cfg(args)
    with DtrpgApi(cfg.api_key, read_timeout=cfg.request_timeout) as api:
        auth = api.authenticate()
        page = api.products_page(1, args.count, archived=False, filters=True)
    out = {
        "auth_response_keys": sorted(auth.keys()),
        "products_page_1": page,
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(text)
    return 0


def cmd_reparse(args: argparse.Namespace) -> int:
    """Re-run normalisation over stored raw JSON (after parser changes)."""
    from .normalize import normalize_product

    cfg = _cfg(args)
    lib = Library(cfg.db_path)
    n = 0
    for row in lib._q("SELECT raw_json FROM products WHERE raw_json IS NOT NULL"):
        raw = json.loads(row["raw_json"])
        lib.upsert_product(normalize_product(raw), raw)
        n += 1
    print(f"Re-parsed {n} products")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    lib = Library(_cfg(args).db_path)
    Path(args.file).write_text(json.dumps(lib.export_metadata(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {args.file}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    lib = Library(_cfg(args).db_path)
    n = lib.import_metadata(json.loads(Path(args.file).read_text(encoding="utf-8")))
    print(f"Imported metadata for {n} products")
    return 0


def cmd_ocr(args: argparse.Namespace) -> int:
    from . import ocr

    cfg = _cfg(args)
    ocr.run(Library(cfg.db_path), cfg, print, mode="full" if args.full else "quick", force=args.force,
            ocr_dir=Path(cfg._path).parent / "ocr", limit=args.limit)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from .local import scan

    cfg = _cfg(args)
    folders = args.folders or cfg.local_folders
    if not folders:
        print("No folders given and none configured (Settings › Local books).")
        return 2
    if args.folders and args.save:
        cfg.local_folders = sorted(set(cfg.local_folders) | set(args.folders))
        cfg.save()
    scan(Library(cfg.db_path), folders, exclude=[cfg.library_path], cover_dir=Path(cfg._path).parent / "covers")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    from . import enrich, llm

    cfg = _cfg(args)
    lib = Library(cfg.db_path)
    conf = enrich.load_config(Path(cfg._path).parent)
    step = args.step
    if step == "dtrpg":
        print(enrich.apply_dtrpg(lib, conf))
    elif step == "rules":
        res = enrich.run_rules(lib, conf, preview=args.preview)
        for rid, n in res["matches"].items():
            print(f"{rid:12} {n:5}  e.g. {'; '.join(res['examples'][rid][:2])}")
        print("preview only" if args.preview else f"queued {res['queued']} suggestions")
    elif step == "pdf":
        enrich.extract_pdfs(lib, print, force=args.force)
    elif step == "classify":
        res = enrich.classify_from_content(lib, preview=args.preview)
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif step == "llm-estimate":
        print(json.dumps(llm.estimate(lib, args.scope, not args.no_pdf), indent=2))
    elif step == "llm-submit":
        est = llm.estimate(lib, args.scope, not args.no_pdf)
        n = min(est["products"], args.limit or est["products"])
        print(f"{n} products, estimated ≈ ${est['est_cost_usd_batch'] * (n / max(1, est['products'])):.2f} (batch pricing)")
        if not args.yes and input("Submit? [y/N] ").strip().lower() != "y":
            return 1
        print(llm.submit(lib, llm.make_client(cfg.anthropic_api_key), args.scope, not args.no_pdf, args.limit))
    elif step == "llm-collect":
        client = llm.make_client(cfg.anthropic_api_key)
        ids = [args.batch] if args.batch else [b["id"] for b in llm.batches(lib) if b["status"] != "imported"]
        for bid in ids:
            print(bid, llm.collect(lib, client, bid))
    elif step == "llm-batches":
        for b in llm.batches(lib):
            print(b["id"], b["status"], b["request_count"], b["created_at"], b["summary"])
    return 0


def cmd_related(args: argparse.Namespace) -> int:
    from .related import RelatedIndex, related

    lib = Library(_cfg(args).db_path)
    res = related(lib, RelatedIndex(lib), args.id, args.n)
    print("Same line:")
    for r in res["same_line"]:
        print(f"  {r['name']}")
    print("Similar:")
    for r in res["similar"]:
        print(f"  {r['score']:.2f}  {r.get('name')}  ({'; '.join(r['why'])})")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import create_app

    cfg = _cfg(args)
    if not cfg._path.exists():
        cfg.save()
    app = create_app(cfg)
    url = f"http://{args.host}:{args.port}/"
    print(f"DTRPG Manager running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dtrpg-manager", description=__doc__)
    p.add_argument("--config", help="path to config.json")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="store API key and library folder").set_defaults(fn=cmd_setup)

    s = sub.add_parser("sync", help="fetch product list + metadata into the local database")
    s.add_argument("--archived", action="store_true", help="also fetch archived products")
    s.add_argument("--no-filters", action="store_true", help="skip DTRPG category data (faster)")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("download", help="download new/updated files")
    s.add_argument("ids", nargs="*", help="orderProductIds (default: everything)")
    s.add_argument("--force", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_download)

    s = sub.add_parser("suggest", help="random game suggestion")
    s.add_argument("-n", "--count", type=int, default=1)
    s.add_argument("-t", "--tag", action="append", help="kind:name, e.g. genre:horror (repeatable, AND)")
    s.add_argument("-s", "--status", action="append")
    s.add_argument("-p", "--publisher")
    s.add_argument("-q", "--query")
    s.add_argument("--players", type=int)
    s.add_argument("--uniform", action="store_true", help="ignore weighting")
    s.set_defaults(fn=cmd_suggest)

    s = sub.add_parser("diagnose", help="dump raw API JSON for inspection")
    s.add_argument("-n", "--count", type=int, default=3)
    s.add_argument("-o", "--output")
    s.set_defaults(fn=cmd_diagnose)

    sub.add_parser("reparse", help="re-normalise stored raw JSON").set_defaults(fn=cmd_reparse)

    s = sub.add_parser("export", help="export user metadata to JSON")
    s.add_argument("file")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("import", help="import user metadata JSON")
    s.add_argument("file")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("ocr", help="OCR scanned PDFs (needs ocrmypdf); originals are never changed")
    s.add_argument("--full", action="store_true", help="every page (slow) instead of the pages the app reads")
    s.add_argument("--force", action="store_true", help="redo books already OCR'd")
    s.add_argument("--limit", type=int, help="at most this many books")
    s.set_defaults(fn=cmd_ocr)

    s = sub.add_parser("scan", help="index local book files (bought elsewhere / scanned)")
    s.add_argument("folders", nargs="*", help="folders to scan (default: the configured local folders)")
    s.add_argument("--save", action="store_true", help="add the given folders to the configuration")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("enrich", help="automatic tagging passes")
    s.add_argument("step", choices=["dtrpg", "rules", "pdf", "classify", "llm-estimate", "llm-submit", "llm-collect", "llm-batches"])
    s.add_argument("--preview", action="store_true", help="rules: count matches without queuing")
    s.add_argument("--force", action="store_true", help="pdf: re-read already processed PDFs")
    s.add_argument("--scope", choices=["thin", "all"], default="thin", help="llm: which products")
    s.add_argument("--no-pdf", action="store_true", help="llm: leave PDF text out of the prompt")
    s.add_argument("--limit", type=int, help="llm-submit: at most this many products")
    s.add_argument("--yes", action="store_true", help="llm-submit: don't ask for confirmation")
    s.add_argument("--batch", help="llm-collect: a specific batch id")
    s.set_defaults(fn=cmd_enrich)

    s = sub.add_parser("related", help="show related products")
    s.add_argument("id", help="orderProductId")
    s.add_argument("-n", type=int, default=12)
    s.set_defaults(fn=cmd_related)

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8642)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    # httpx logs every request URL at INFO; the auth URL contains the API key
    # (?applicationKey=...) and download URLs contain one-time tokens.
    for noisy in ("httpx", "httpcore", "hpack", "h2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
