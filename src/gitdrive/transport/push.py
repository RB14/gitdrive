"""Push handler — creates bundles and uploads to Google Drive."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

from gitdrive.config import GitDriveConfig
from gitdrive.drive.client import DriveClient
from gitdrive.exceptions import BundleError, DriveApiError, ManifestError
from gitdrive.remote.helper import Refspec
from gitdrive.store.manifest import BundleEntry, Manifest, _utcnow_iso


class PushHandler:
    """Handles push operations: bundling, uploading, and manifest management."""

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

        # Populated by _ensure_repo_structure().
        self._repo_folder_id: str | None = None
        self._gitdrive_folder_id: str | None = None
        self._bundles_folder_id: str | None = None

    # ── public API ───────────────────────────────────────────────────

    def push(self, refspecs: list[Refspec]) -> list[tuple[Refspec, str | None]]:
        """Push a batch of refspecs.

        Returns a list of ``(refspec, error_or_none)`` pairs.  A ``None``
        error indicates the push for that refspec succeeded.
        """
        results: list[tuple[Refspec, str | None]] = []

        try:
            self._ensure_repo_structure()
        except DriveApiError as exc:
            return [(rs, str(exc)) for rs in refspecs]

        with tempfile.TemporaryDirectory(prefix="gitdrive-push-") as tmp_dir:
            tmp = Path(tmp_dir)

            for refspec in refspecs:
                try:
                    self._push_one(refspec, tmp)
                    results.append((refspec, None))
                except (
                    BundleError,
                    DriveApiError,
                    subprocess.CalledProcessError,
                ) as exc:
                    results.append((refspec, str(exc)))

        return results

    # ── repo folder structure ────────────────────────────────────────

    def _ensure_repo_structure(self) -> None:
        """Create ``GitDrive/<repo>/.gitdrive/bundles/`` on Drive (idempotent)."""
        settings = self._config.load_settings()
        root_id = settings.get("root_folder_id")
        if not root_id:
            raise DriveApiError("GitDrive not initialized — no root folder ID")

        self._repo_folder_id = self._client.ensure_folder(
            self._repo_name, parent_id=root_id
        )
        self._gitdrive_folder_id = self._client.ensure_folder(
            ".gitdrive", parent_id=self._repo_folder_id
        )
        self._bundles_folder_id = self._client.ensure_folder(
            "bundles", parent_id=self._gitdrive_folder_id
        )

    # ── single-refspec push ──────────────────────────────────────────

    def _push_one(self, refspec: Refspec, tmp: Path) -> None:
        """Push a single refspec: bundle, upload, update manifest."""
        # 1. Resolve source ref to a SHA.
        src_sha = self._resolve_ref(refspec.src)

        # 2. Determine remote SHAs to exclude (incremental bundling).
        exclude_shas = self._get_exclude_shas()

        # 3. Create the git bundle.
        bundle_id = self._manifest.next_bundle_id()
        bundle_path = tmp / f"{bundle_id}.bundle"
        self._create_bundle(bundle_path, refspec.src, exclude_shas)

        # 4. Compute checksum.
        checksum = self._compute_checksum(bundle_path)

        # 5. Upload the bundle to Drive.
        bundle_bytes = bundle_path.read_bytes()
        file_id = self._client.upload_file(
            name=f"{bundle_id}.bundle",
            content=bundle_bytes,
            parent_id=self._bundles_folder_id,
        )

        # 6. Sync browsable files if this push updates the browsable ref.
        #    On first push (no refs yet), adopt the pushed branch as the
        #    browsable ref so files appear on Drive immediately.
        if not self._manifest.refs:
            self._manifest.browsable_ref = refspec.dst

        if refspec.dst == self._manifest.browsable_ref:
            old_sha = self._manifest.refs.get(refspec.dst)
            self._sync_browsable(old_sha, src_sha)

        # 7. Update manifest in memory.
        prerequisite_ids = [b.id for b in self._manifest.bundles]
        entry = BundleEntry(
            id=bundle_id,
            file_id=file_id,
            prerequisites=prerequisite_ids,
            checksum=f"sha256:{checksum}",
        )
        self._manifest.add_bundle(entry)
        self._manifest.update_refs({refspec.dst: src_sha})
        self._manifest.updated_at = _utcnow_iso()

        # 8. Upload manifest to Drive.
        self._upload_manifest()

        self._msg(f"  {refspec.dst} -> {src_sha[:8]}")

    # ── git helpers ──────────────────────────────────────────────────

    def _resolve_ref(self, ref: str) -> str:
        """Resolve *ref* to its SHA via ``git rev-parse``."""
        result = subprocess.run(
            ["git", "rev-parse", ref],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise BundleError(
                f"Failed to resolve ref '{ref}': {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def _get_exclude_shas(self) -> list[str]:
        """Return SHAs the remote already has (for incremental bundling).

        Only SHAs that exist in the local repo are returned, so
        ``git bundle create`` won't fail on unknown objects.
        """
        valid: list[str] = []
        for sha in self._manifest.refs.values():
            result = subprocess.run(
                ["git", "cat-file", "-t", sha],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                valid.append(sha)
        return valid

    def _create_bundle(
        self, path: Path, ref: str, exclude_shas: list[str]
    ) -> None:
        """Create a git bundle file at *path*."""
        cmd: list[str] = ["git", "bundle", "create", str(path)]

        if exclude_shas:
            # Incremental: include this ref, exclude known SHAs.
            cmd.append(ref)
            cmd.extend(f"^{sha}" for sha in exclude_shas)
        else:
            # First push — full bundle with everything.
            cmd.append("--all")

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise BundleError(
                f"git bundle create failed: {result.stderr.strip()}"
            )

    # ── checksum ─────────────────────────────────────────────────────

    @staticmethod
    def _compute_checksum(path: Path) -> str:
        """Compute the SHA-256 hex digest of the file at *path*."""
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    # ── browsable sync ───────────────────────────────────────────────

    def _sync_browsable(self, old_sha: str | None, new_sha: str) -> None:
        """Delegate browsable file sync to ``TreeSyncer`` (if available)."""
        try:
            from gitdrive.sync.tree import TreeSyncer
        except ImportError:
            # TreeSyncer hasn't been implemented yet — skip silently.
            return

        syncer = TreeSyncer(self._client, self._repo_folder_id)
        syncer.sync(old_sha, new_sha)

    # ── manifest upload ──────────────────────────────────────────────

    def _upload_manifest(self) -> None:
        """Upload the manifest to Drive, updating in place if it exists."""
        manifest_json = self._manifest.to_json().encode("utf-8")

        existing_id = self._client.find_file(
            "manifest.json", parent_id=self._gitdrive_folder_id
        )

        if existing_id:
            # Optimistic-locking sanity check: re-download and compare
            # updated_at to detect concurrent pushes.
            try:
                current_raw = self._client.download_file(existing_id)
                current = Manifest.from_json(current_raw.decode("utf-8"))
                if current.updated_at != self._manifest.updated_at:
                    self._msg(
                        "  warning: manifest was modified by another push; "
                        "overwriting"
                    )
            except (DriveApiError, ManifestError):
                pass  # Manifest may be corrupted or absent — proceed anyway.

            self._client.upload_file(
                name="manifest.json",
                content=manifest_json,
                parent_id=self._gitdrive_folder_id,
                existing_file_id=existing_id,
            )
        else:
            self._client.upload_file(
                name="manifest.json",
                content=manifest_json,
                parent_id=self._gitdrive_folder_id,
            )

    # ── output ───────────────────────────────────────────────────────

    @staticmethod
    def _msg(text: str) -> None:
        """Write a user-facing message to stderr."""
        print(text, file=sys.stderr)
