"""GitDrive manifest — tracks bundles and refs for a Drive-backed repo."""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from typing import Any

from gitdrive.exceptions import ManifestError


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass
class BundleEntry:
    """A single git-bundle record inside a manifest."""

    id: str
    file_id: str
    prerequisites: list[str] = field(default_factory=list)
    checksum: str = ""
    created_at: str = field(default_factory=_utcnow_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file_id": self.file_id,
            "prerequisites": list(self.prerequisites),
            "checksum": self.checksum,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BundleEntry:
        try:
            return cls(
                id=data["id"],
                file_id=data["file_id"],
                prerequisites=list(data.get("prerequisites", [])),
                checksum=data.get("checksum", ""),
                created_at=data.get("created_at", _utcnow_iso()),
            )
        except KeyError as exc:
            raise ManifestError(f"BundleEntry missing required field: {exc}") from exc


@dataclass
class Manifest:
    """Top-level manifest for a Drive-backed Git repository.

    The manifest is stored as a single JSON file in Google Drive alongside the
    bundle objects.  It records every bundle ever pushed, the current refs, and
    the canonical *browsable_ref* used for Drive-side preview.
    """

    version: int = 1
    repo_name: str = ""
    refs: dict[str, str] = field(default_factory=dict)
    bundles: list[BundleEntry] = field(default_factory=list)
    browsable_ref: str = "refs/heads/main"
    updated_at: str = field(default_factory=_utcnow_iso)

    # ── bundle helpers ──────────────────────────────────────────────

    def next_bundle_id(self) -> str:
        """Return the next sequential zero-padded bundle ID."""
        if not self.bundles:
            return "0001"
        last = max(int(b.id) for b in self.bundles)
        return f"{last + 1:04d}"

    def add_bundle(self, entry: BundleEntry) -> None:
        """Append *entry* to the bundle list."""
        self.bundles.append(entry)

    # ── refs helpers ────────────────────────────────────────────────

    def update_refs(self, new_refs: dict[str, str]) -> None:
        """Merge *new_refs* into the current ref map."""
        self.refs.update(new_refs)

    def resolve_browsable_ref(self) -> str | None:
        """Return the best browsable ref from available refs.

        Resolution order: configured browsable_ref (if it exists in refs),
        then ``refs/heads/main``, then ``refs/heads/master``, then the first
        ``refs/heads/*`` entry alphabetically.
        """
        if self.browsable_ref in self.refs:
            return self.browsable_ref
        if "refs/heads/main" in self.refs:
            return "refs/heads/main"
        if "refs/heads/master" in self.refs:
            return "refs/heads/master"
        for ref in sorted(self.refs):
            if ref.startswith("refs/heads/"):
                return ref
        return next(iter(self.refs), None) if self.refs else None

    # ── serialization ───────────────────────────────────────────────

    def to_json(self) -> str:
        """Serialize the manifest to an indented JSON string."""
        payload: dict[str, Any] = {
            "version": self.version,
            "repo_name": self.repo_name,
            "refs": dict(self.refs),
            "bundles": [b.to_dict() for b in self.bundles],
            "browsable_ref": self.browsable_ref,
            "updated_at": self.updated_at,
        }
        return json.dumps(payload, indent=2) + "\n"

    @classmethod
    def from_json(cls, data: str) -> Manifest:
        """Deserialize a manifest from a JSON string."""
        try:
            raw = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"Invalid manifest JSON: {exc}") from exc

        try:
            return cls(
                version=raw["version"],
                repo_name=raw["repo_name"],
                refs=dict(raw.get("refs", {})),
                bundles=[BundleEntry.from_dict(b) for b in raw.get("bundles", [])],
                browsable_ref=raw.get("browsable_ref", "refs/heads/main"),
                updated_at=raw.get("updated_at", _utcnow_iso()),
            )
        except KeyError as exc:
            raise ManifestError(
                f"Manifest missing required field: {exc}"
            ) from exc

    # ── factory ─────────────────────────────────────────────────────

    @classmethod
    def new(cls, repo_name: str) -> Manifest:
        """Create a fresh, empty manifest for *repo_name*."""
        now = _utcnow_iso()
        return cls(repo_name=repo_name, updated_at=now)
