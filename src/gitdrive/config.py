"""GitDrive configuration — XDG-compliant paths and persistent settings."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gitdrive.exceptions import ConfigError

_APP_NAME = "gitdrive"


def _resolve_home() -> Path | None:
    """Determine the gitdrive home directory.

    Resolution order:
    1. ``GITDRIVE_HOME`` environment variable (explicit override).
    2. Auto-detect local mode: if the running binary lives inside a
       ``.venv/`` that belongs to the gitdrive project and a
       ``.gitdrive/`` directory exists (or ``settings.json`` would be
       created there), use ``<project>/.gitdrive/``.
    3. ``None`` → fall back to XDG base directories (global mode).
    """
    # 1. Explicit override.
    raw = os.environ.get("GITDRIVE_HOME")
    if raw:
        return Path(raw)

    # 2. Auto-detect: walk up from the *package* source to find the
    #    project root.  In an editable install the source lives at
    #    ``<project>/src/gitdrive/config.py``.
    try:
        pkg_dir = Path(__file__).resolve().parent        # .../src/gitdrive
        project_root = pkg_dir.parent.parent             # .../
        candidate = project_root / ".gitdrive"
        if candidate.is_dir():
            return candidate
    except Exception:
        pass

    return None


def _xdg_config_home() -> Path:
    """Return the XDG config base directory, respecting env overrides."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _xdg_data_home() -> Path:
    """Return the XDG data base directory, respecting env overrides."""
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _default_config_dir() -> Path:
    home = _resolve_home()
    return home if home else _xdg_config_home() / _APP_NAME


def _default_data_dir() -> Path:
    home = _resolve_home()
    return home if home else _xdg_data_home() / _APP_NAME


@dataclass
class GitDriveConfig:
    """Central configuration for gitdrive.

    When the ``GITDRIVE_HOME`` environment variable is set, all state
    (credentials, tokens, settings, bundle cache) is stored under that
    single directory.  Otherwise XDG base directories are used.

    On instantiation every required directory is created with mode 0o700.
    Settings are persisted as JSON and written atomically.
    """

    config_dir: Path = field(default_factory=_default_config_dir)
    data_dir: Path = field(default_factory=_default_data_dir)

    # ── directory-derived properties ────────────────────────────────

    @property
    def credentials_file(self) -> Path:
        return self.config_dir / "credentials.json"

    @property
    def token_file(self) -> Path:
        return self.config_dir / "token.enc"

    @property
    def encryption_key_file(self) -> Path:
        return self.config_dir / "token.key"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def bundles_cache_dir(self) -> Path:
        return self.data_dir / "bundles"

    # ── lifecycle ───────────────────────────────────────────────────

    def __post_init__(self) -> None:
        for directory in (self.config_dir, self.data_dir, self.bundles_cache_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    # ── settings persistence ────────────────────────────────────────

    def load_settings(self) -> dict[str, Any]:
        """Load settings from disk, returning defaults when the file is absent."""
        if not self.settings_file.exists():
            return self._default_settings()
        try:
            text = self.settings_file.read_text(encoding="utf-8")
            data = json.loads(text)
        except (json.JSONDecodeError, OSError) as exc:
            raise ConfigError(f"Failed to load settings: {exc}") from exc

        # Ensure expected keys always exist.
        defaults = self._default_settings()
        for key, value in defaults.items():
            data.setdefault(key, value)
        return data

    def save_settings(self, settings: dict[str, Any]) -> None:
        """Atomically write *settings* to disk as JSON."""
        try:
            payload = json.dumps(settings, indent=2, sort_keys=True) + "\n"
            fd, tmp_path = tempfile.mkstemp(
                dir=self.config_dir,
                prefix=".settings_",
                suffix=".tmp",
            )
            try:
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, self.settings_file)
        except OSError as exc:
            raise ConfigError(f"Failed to save settings: {exc}") from exc

    # ── convenience accessors ───────────────────────────────────────

    def get_applied_bundles(self, repo: str) -> list[str]:
        """Return the list of bundle IDs already applied for *repo*."""
        settings = self.load_settings()
        return list(settings.get("applied_bundles", {}).get(repo, []))

    def mark_bundle_applied(self, repo: str, bundle_id: str) -> None:
        """Record *bundle_id* as applied for *repo* and persist."""
        settings = self.load_settings()
        applied: dict[str, list[str]] = settings.setdefault("applied_bundles", {})
        repo_bundles = applied.setdefault(repo, [])
        if bundle_id not in repo_bundles:
            repo_bundles.append(bundle_id)
        self.save_settings(settings)

    def get_repo_cache_dir(self, repo: str) -> Path:
        """Return the bundle-cache subdirectory for *repo*, creating it if needed."""
        repo_dir = self.bundles_cache_dir / repo
        repo_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return repo_dir

    # ── private helpers ─────────────────────────────────────────────

    @staticmethod
    def _default_settings() -> dict[str, Any]:
        return {
            "root_folder_id": None,
            "applied_bundles": {},
        }
