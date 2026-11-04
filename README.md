# DTRPG Manager

A local library manager for a DriveThruRPG account. It provides:

- **Library sync**: pulls the product list, file lists, checksums, and (where the API supplies them) DriveThruRPG's own category filters into a local SQLite database.
- **Classification**: user-defined **genres, categories, systems, settings and free tags**, plus status (unread → played), 0–5 rating, favourite, product type, player count, and notes. Supports bulk tagging and bulk status changes.
- **Downloads**: one file, one product, a selection, or "everything new/updated". Downloads run in parallel, are streamed to disk, and are checked against DriveThruRPG's MD5 checksum (`.part` file plus atomic rename). Files are laid out as `Publisher/Product/file`.
- **Random game suggestion**: filter by any mix of tags (AND), DTRPG category, status, type, player count, minimum rating, or favourites. Weighted mode favours favourites, "want-to-play" and unread items, and skips the last 10 picks. Products can be excluded from random picks individually.
- **Metadata backup**: export to JSON or CSV, and re-import from JSON.

The app has a browser UI served on `127.0.0.1` only, and a CLI.

## Install

Requires Python 3.10+.

Ubuntu 23.04+ and Debian 12+ block `pip install` into the system Python ([PEP 668](https://peps.python.org/pep-0668/), "externally-managed-environment"). Install into a virtual environment instead.

**Option A — virtual environment (recommended for a git checkout):**

```bash
sudo apt install python3-venv python3-full
cd friendly-broccoli
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/dtrpg-manager serve
```

Or run `source .venv/bin/activate` once per terminal, then use `dtrpg-manager` directly.

**Option B — pipx (puts `dtrpg-manager` on PATH globally):**

```bash
sudo apt install pipx
pipx ensurepath          # then open a new terminal
pipx install ./friendly-broccoli
dtrpg-manager serve
```

On Windows and macOS, `python -m venv .venv` works the same way.

## Get an API key

Create a key in the **"Library App Keys"** section of https://www.drivethrurpg.com/en/account/settings. The same instructions appear in the [glujan/drpg README](https://github.com/glujan/drpg#usage) and the [JamesSkemp/DriveThruRpg README](https://github.com/JamesSkemp/DriveThruRpg).

## Use

```bash
dtrpg-manager serve
```

This opens http://127.0.0.1:8642/. Go to **Settings & sync**, paste the key, choose a download folder, then click **Sync library**.

### CLI

```bash
dtrpg-manager setup                    # store key + library folder
dtrpg-manager sync [--archived]        # refresh catalogue
dtrpg-manager download [ID ...] [--dry-run] [--force]
dtrpg-manager suggest -t genre:horror -t system:"Call of Cthulhu" -s unread -n 3
dtrpg-manager export meta.json         # / import meta.json
dtrpg-manager diagnose -o diagnostic.json   # dump raw API JSON
dtrpg-manager reparse                  # re-normalise stored raw JSON
```

Configuration lives in `~/.config/dtrpg-manager/config.json` (`%APPDATA%\dtrpg-manager` on Windows, `~/Library/Application Support/dtrpg-manager` on macOS). The file is written with mode 600. The environment variables `DTRPG_API_KEY` (or `DRPG_TOKEN`, for compatibility with drpg), `DTRPG_LIBRARY_PATH`, and `DTRPG_MANAGER_HOME` override it.

## Local books

Books bought elsewhere or scanned can sit in the same library. List their folders under **Settings › Local books** and press **Scan local folders**, or from the command line:

```bash
dtrpg-manager scan "/home/you/RPG PDFs" --save
```

- **What's scanned:** subfolders are included, and PDF, EPUB, CBZ/CBR, MOBI/AZW3 and DjVu files are picked up. Files are only read; they are never copied, moved or changed. The DriveThruRPG download folder is skipped.
- **One file = one product.** The name comes from the PDF title when sensible, otherwise from the file name. The folder path is used as the description, which the keyword rules and the AI pass can use. The cover comes from the first image on the first page.
- **Identity follows the file's content:** renaming or moving a file keeps its tags and notes, and identical copies count once.
- **Rescans:** a file that has disappeared is flagged as removed. A folder that is configured but unavailable, such as an unplugged drive, is left alone.
- **Editing:** name, publisher and description can be edited in the product panel. Rescans keep your edits.
- **Separate from DriveThruRPG:** sync and downloads never touch local books. **Source** in the library sidebar filters by origin.

## Automatic tagging

The **Auto-tag** page runs four passes. Each tag records where it came from (`manual`, `dtrpg`, `rule`, `llm`) and, for automatic tags, a confidence. Tags you add are never changed by a pass. Anything you remove or reject is remembered and not suggested again.

| Pass | What it does | Applied how |
|---|---|---|
| 1 · DriveThruRPG categories | Genre → `genre` tags, Rule System → `system` tags, Product Type → product type; parent categories included; umbrella labels such as "Other systems" skipped | directly |
| 2 · Keyword rules | Case-insensitive regular expressions over title, publisher, description and PDF text (D&D 5e, PbtA, Forged in the Dark, OSR, solo, GM-less, one-shot, zine, player counts, …) | suggestions to review |
| 3 · Read PDFs | Page count, PDF metadata and the first 3 pages' text of each product's largest downloaded PDF (`pypdf`, local) | feeds passes 2 and 4 |
| 4 · AI (Claude Haiku 5.5) | Classifies thinly tagged products against your existing vocabulary, via the Message Batches API (50% price) with a JSON-schema response. Shows an estimated cost and asks for confirmation before submitting. | suggestions to review |

The mapping and rules are editable on the same page and are stored in `enrich.json` next to `config.json`. Pass 4 needs the `anthropic` package in the app's virtual environment (`.venv/bin/pip install -e '.[llm]'`, then restart the app) and an Anthropic API key (Settings, or `ANTHROPIC_API_KEY`).

```bash
dtrpg-manager enrich dtrpg
dtrpg-manager enrich rules --preview
dtrpg-manager enrich pdf
dtrpg-manager enrich llm-estimate
dtrpg-manager enrich llm-submit --limit 50
dtrpg-manager enrich llm-collect
```

## Related products

Each product's detail panel lists:

- **Linked** — relationships you add (`related`, `same-line`, `supplement-of`, `requires`, `sequel-of`).
- **Same line** — same publisher with a shared title prefix (e.g. "Dungeon Crawl Classics …", "Fiasco: …").
- **Similar** — cosine similarity over TF-IDF-weighted DriveThruRPG categories, your tags, publisher, and title and description words. Each match lists the features it shares, for example `title: blades · publisher: Evil Hat Productions`.

**🎲 Something like this** runs the random picker restricted to similar products, weighted by closeness. From the CLI, use `dtrpg-manager related <orderProductId>`.

## API notes and known unknowns

DriveThruRPG has no public API documentation. This app uses the `https://api.drivethrurpg.com/api/vBeta/` endpoints as implemented in:

| Call | Source |
|---|---|
| `POST auth_key?applicationKey=…` → `token` | [glujan/drpg `drpg/api.py`](https://github.com/glujan/drpg/blob/main/drpg/api.py) `token()`; [JamesSkemp/DriveThruRpg `ApiClient.cs`](https://github.com/JamesSkemp/DriveThruRpg/blob/main/DriveThruRpgApi/ApiClient.cs) `GetToken()` |
| `GET order_products?page&pageSize&library=1&archived&getChecksum&getFilters` | drpg `_product_page()`; JamesSkemp `GetProducts()` |
| `GET order_products/{id}/prepare` then `/check` until `status` is not "Preparing…" | drpg `prepare_download_url()` |

The older `/api/v1/` endpoints documented in [jramboz/DTRPG_API](https://github.com/jramboz/DTRPG_API) (`Products.md`, `File_Tasks.md`) were used only as a source of fallback field names, for example `filters_name`, `filters_id`, `parent_id`, `cover_url`, and `date_purchased`.

**Not verified against a real account:** the structure of the category data returned with `getFilters=1`, and the names of the cover-image and purchase-date fields. drpg sends `getFilters: 0` with the comment *"Official clients defaults to 1"*, so the filter field exists, but its layout is not documented. The parser tries several layouts and stores the full raw JSON for every product, so nothing is lost. After the first sync:

1. Run `dtrpg-manager diagnose -o diagnostic.json` and inspect the product objects. Each product's raw JSON is also visible under **Raw API data** in its detail panel.
2. If categories or covers are missing, adjust the key lists in `dtrpg_manager/normalize.py` and run `dtrpg-manager reparse`. No re-download is needed.

**Slow product list.** The authenticated `order_products` call can take longer than 30 seconds to respond (a `ReadTimeout` at 30 s was reported on 2026-11-03). The client now waits up to 120 s (`request_timeout`) and uses HTTP/2, as drpg does. If a page still times out, it retries without `getFilters` and then with smaller pages. Category fetching can be switched off in Settings or with `dtrpg-manager sync --no-filters`.

**HTTP 503 from the download server.** File links point to `watermark.drivethrurpg.com`, which sits behind Cloudflare and answers with `503 Service Temporarily Unavailable` when it is overloaded or rate-limiting. drpg users report the same thing in [glujan/drpg#143](https://github.com/glujan/drpg/issues/143), where the maintainer suggests fewer threads and sequential downloads, and in [glujan/drpg#155](https://github.com/glujan/drpg/issues/155) (`503 Backend fetch failed`, May 2026). To cope with this, the app:

- downloads one file at a time by default (`threads = 1`), with `download_delay` seconds between files;
- on 429/5xx, honours `Retry-After` (otherwise backs off 15 s → 30 s) and prepares a fresh link before each retry;
- pauses for 5 minutes after 5 consecutive server errors, and stops the job if the errors continue;
- runs only one download job at a time, and has a **Stop** button.

Files that are already downloaded are skipped on the next run.

An invalid key returns HTTP 496 `{"error":{"message":"Invalid application key."}}` (observed 2026-11-03).

## Development

```bash
pip install -e .[dev]
pytest
```

The tests use a mocked API (`httpx.MockTransport`). They cover sync, filter hierarchy, tagging, suggestion weighting and exclusion, checksum rejection, path-traversal protection, CSRF guarding, and export/import round-trips.

## Credits

API knowledge from glujan/drpg (MIT), JamesSkemp/DriveThruRpg, and jramboz/DTRPG_API. No code was copied verbatim. The path-sanitising approach follows drpg's `PathNormalizer.normalize`.
