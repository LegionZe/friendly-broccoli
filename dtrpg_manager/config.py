"""Configuration: JSON file in the user's config dir, overridable by env vars."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def default_config_dir() -> Path:
    if env := os.environ.get("DTRPG_MANAGER_HOME"):
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "dtrpg-manager"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "dtrpg-manager"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dtrpg-manager"


@dataclass
class Config:
    api_key: str = ""
    library_path: str = field(default_factory=lambda: str(Path.home() / "DriveThruRPG"))
    db_path: str = ""
    omit_publisher: bool = False
    # Reject downloads whose MD5 differs from DriveThruRPG's newest recorded checksum.
    # Off by default: those checksums describe earlier watermarked copies and often
    # don't match a fresh download (observed 2026-11-03: 2 of 8 files).
    validate_checksums: bool = False
    include_archived: bool = False
    threads: int = 4  # max parallel downloads (1-8); reduced automatically when the server pushes back
    download_delay: float = 0.0  # optional pause after each download, per worker
    hold_failed_hours: float = 24.0  # skip files that failed a checksum/content check for this long
    verify_existing: bool = False  # hash every existing file when checking what's up to date (slow)
    page_size: int = 50
    fetch_filters: bool = True  # getFilters=1: DTRPG categories; slower server-side
    request_timeout: int = 120  # seconds to wait for an API response
    anthropic_api_key: str = ""  # pass 4 (Claude Haiku tag suggestions); or ANTHROPIC_API_KEY

    _path: Path | None = field(default=None, repr=False, compare=False)
    _file_key: str = field(default="", repr=False, compare=False)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or default_config_dir() / "config.json"
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and not k.startswith("_")}
        cfg = cls(**known)
        cfg._path = path
        cfg._file_key = cfg.api_key
        if env := os.environ.get("DTRPG_API_KEY") or os.environ.get("DRPG_TOKEN"):
            cfg.api_key = env
        if env := os.environ.get("DTRPG_LIBRARY_PATH"):
            cfg.library_path = env
        if not cfg.db_path:
            cfg.db_path = str(path.parent / "library.sqlite3")
        return cfg

    def save(self) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        if os.environ.get("DTRPG_API_KEY") or os.environ.get("DRPG_TOKEN"):
            data["api_key"] = self._file_key  # never persist a key that came from the environment
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # API key is a credential
        except OSError:
            pass
        tmp.replace(self._path)

    def public(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        k = self.api_key
        d["api_key"] = ("••••" + (k[-4:] if len(k) >= 16 else "")) if k else ""
        d["api_key_set"] = bool(self.api_key)
        a = self.anthropic_api_key
        d["anthropic_api_key"] = ("••••" + (a[-4:] if len(a) >= 16 else "")) if a else ""
        d["anthropic_api_key_set"] = bool(a or os.environ.get("ANTHROPIC_API_KEY"))
        return d
