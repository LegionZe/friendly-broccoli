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
    validate_checksums: bool = True
    include_archived: bool = False
    threads: int = 1  # parallel downloads; >1 makes HTTP 503s from DTRPG more likely
    download_delay: float = 2.0  # seconds between downloads
    page_size: int = 50
    fetch_filters: bool = True  # getFilters=1: DTRPG categories; slower server-side
    request_timeout: int = 120  # seconds to wait for an API response

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
        return d
