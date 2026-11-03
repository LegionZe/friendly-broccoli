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
