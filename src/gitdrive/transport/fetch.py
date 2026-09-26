"""Fetch handler — downloads and unbundles from Google Drive."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

from gitdrive.config import GitDriveConfig
from gitdrive.drive.client import DriveClient
from gitdrive.exceptions import (
    BundleError,
    BundleVerifyError,
    ChecksumMismatchError,
    DriveApiError,
)
from gitdrive.store.manifest import BundleEntry, Manifest


class FetchHandler:
    """Handles fetch operations: downloading bundles and unbundling."""

    def __init__(
        self,
        config: GitDriveConfig,
        client: DriveClient,
        manifest: Manifest,
        repo_name: str,
    ) -> None:
        self._config = config
        self._client = client
        self._manifest = manifest
        self._repo_name = repo_name

    # ── public API ───────────────────────────────────────────────────

    def fetch(self, fetch_specs: list[tuple[str, str]]) -> None:
        """Fetch a batch of ``(sha, ref)`` specs.

        Downloads unapplied bundles in order, verifies checksums, runs
        ``git bundle verify`` + ``unbundle``, and marks each as applied.

        The record of applied bundles only saves downloads; it is not
        trusted blindly.  When it made the fetch skip bundles, the fetched
        objects are checked, and if the record proves wrong — e.g. an older
        ``gitdrive gc`` reissued IDs it lists — every bundle is applied
        again.  Records of bundles no longer on Drive are then dropped.
        """
        bundles = self._manifest.bundles
        recorded = set(self._config.get_applied_bundles(self._repo_name))
        unapplied = [b for b in bundles if b.id not in recorded]
        skipped_recorded = len(unapplied) < len(bundles)
        wanted = [sha for sha, _ref in fetch_specs]
        applied_now: set[str] = set()

        try:
            self._apply_bundles(unapplied, applied_now)
            complete = not skipped_recorded or self._has_objects(wanted)
        except BundleVerifyError:
            if not skipped_recorded:
                raise
            complete = False  # A bundle it skipped is missing here after all.

        if not complete:
            self._msg(
                "  Bundles recorded as applied are missing locally — "
                "re-applying all bundles..."
            )
            self._apply_bundles(
                [b for b in bundles if b.id not in applied_now], applied_now
            )
            if not self._has_objects(wanted):
                raise BundleError(
                    "Objects of the fetched refs are missing even after "
                    "applying every bundle on Drive"
                )

        self._config.forget_applied_bundles(self._repo_name, {b.id for b in bundles})

    # ── bundle application ───────────────────────────────────────────

    def _apply_bundles(self, entries: list[BundleEntry], applied_now: set[str]) -> None:
        """Download and apply *entries* in order, adding each ID to *applied_now*."""
        if not entries:
            self._msg("  Everything up to date.")
            return

        total = len(entries)
        self._msg(f"  Fetching {total} bundle{'s' if total != 1 else ''}...")
        cache_dir = self._config.get_repo_cache_dir(self._repo_name)

        for idx, entry in enumerate(entries, 1):
            bundle_path = cache_dir / f"{entry.id}.bundle"

            # 1. Download the bundle from Drive.
            self._msg(f"  Downloading bundle {idx}/{total}...")
            t0 = time.monotonic()
            try:
                self._client.download_file_to_path(entry.file_id, bundle_path)
            except DriveApiError as exc:
                self._msg(f"  Failed to download bundle {entry.id}: {exc}")
                raise
            elapsed = time.monotonic() - t0
            size = bundle_path.stat().st_size
            self._msg(f"  Downloaded ({self._fmt_size(size)}) — {self._fmt_speed(size, elapsed)}")

            # 2. Verify checksum.
            if entry.checksum:
                self._verify_checksum(entry.id, entry.checksum, bundle_path)

            # 3. Verify bundle integrity.
            result = subprocess.run(
                ["git", "bundle", "verify", str(bundle_path)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                bundle_path.unlink(missing_ok=True)
                raise BundleVerifyError(
                    f"Bundle {entry.id} failed verification: "
                    f"{result.stderr.strip()}"
                )

            # 4. Unbundle into the local repository.
            self._msg(f"  Applying bundle {idx}/{total}...")
            result = subprocess.run(
                ["git", "bundle", "unbundle", str(bundle_path)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                bundle_path.unlink(missing_ok=True)
                raise BundleVerifyError(
                    f"Failed to unbundle {entry.id}: {result.stderr.strip()}"
                )

            # 5. Mark as applied and clean up the cached bundle.
            self._config.mark_bundle_applied(self._repo_name, entry.id)
            applied_now.add(entry.id)
            bundle_path.unlink(missing_ok=True)

    @staticmethod
    def _has_objects(shas: list[str]) -> bool:
        """Return ``True`` if *shas* and every object they reach exist locally.

        The same connectivity check git runs after a fetch; objects reachable
        from local refs are taken as present.
        """
        if not shas:
            return True
        result = subprocess.run(
            ["git", "rev-list", "--objects", "--quiet", *shas, "--not", "--all"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    # ── checksum verification ────────────────────────────────────────

    @staticmethod
    def _verify_checksum(
        bundle_id: str, expected: str, path: Path
    ) -> None:
        """Verify the checksum of a downloaded bundle.

        *expected* is in the format ``algorithm:hexdigest``.
        """
        algo, expected_hash = expected.split(":", 1)
        actual_hash = _compute_checksum(path, algo)
        if actual_hash != expected_hash:
            path.unlink(missing_ok=True)
            raise ChecksumMismatchError(
                f"Bundle {bundle_id}: expected {expected}, "
                f"got {algo}:{actual_hash}"
            )

    # ── output ───────────────────────────────────────────────────────

    @staticmethod
    def _msg(text: str) -> None:
        """Write a user-facing message to stderr."""
        print(text, file=sys.stderr)

    @staticmethod
    def _fmt_size(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.1f} MB"

    @staticmethod
    def _fmt_speed(nbytes: int, elapsed: float) -> str:
        if elapsed <= 0:
            return ""
        bps = nbytes / elapsed
        if bps < 1024:
            return f"{bps:.0f} B/s"
        if bps < 1024 * 1024:
            return f"{bps / 1024:.1f} KB/s"
        return f"{bps / (1024 * 1024):.1f} MB/s"


# ── Module-private helpers ───────────────────────────────────────────

def _compute_checksum(path: Path, algorithm: str = "sha256") -> str:
    """Compute the hex digest of the file at *path*."""
    h = hashlib.new(algorithm)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
