"""Browsable file sync — mirrors the Git working tree on Google Drive."""

from __future__ import annotations

import mimetypes
import subprocess
import sys
from pathlib import PurePosixPath

from gitdrive.drive.client import DriveClient
from gitdrive.exceptions import DriveApiError

__all__ = ["TreeSyncer"]


class TreeSyncer:
    """Syncs a snapshot of the Git working tree as browsable files on Drive.

    Files are read from Git objects (``git show <sha>:<path>``), **not** from
    the working directory, ensuring we always sync exactly what was committed.
    """

    _EXCLUDE_DIRS: frozenset[str] = frozenset({".git", ".gitdrive"})

    def __init__(self, client: DriveClient, repo_folder_id: str) -> None:
        self._client = client
        self._repo_folder_id = repo_folder_id
        # Cache: repo-relative folder path → Drive folder ID
        self._folder_cache: dict[str, str] = {}

    # ── Public API ────────────────────────────────────────────────

    def sync(self, old_sha: str | None, new_sha: str) -> None:
        """Sync browsable files to Drive.

        If *old_sha* is ``None`` a full sync is performed (first push).
        Otherwise only the diff between the two commits is applied.
        """
        if old_sha is None:
            self._full_sync(new_sha)
        else:
            self._incremental_sync(old_sha, new_sha)

    # ── Full sync ─────────────────────────────────────────────────

    def _full_sync(self, sha: str) -> None:
        """Upload every tracked file for the given commit."""
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", sha],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self._msg(f"Warning: could not list tree for {sha[:8]}")
            return

        for file_path in result.stdout.strip().splitlines():
            if self._is_excluded(file_path):
                continue
            self._upload_git_file(sha, file_path)

    # ── Incremental sync ──────────────────────────────────────────

    def _incremental_sync(self, old_sha: str, new_sha: str) -> None:
        """Apply only the diff between *old_sha* and *new_sha*."""
        result = subprocess.run(
            ["git", "diff-tree", "-r", "--no-commit-id", old_sha, new_sha],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self._msg("Warning: diff-tree failed, falling back to full sync")
            self._full_sync(new_sha)
            return

        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            self._process_diff_entry(line, new_sha)

    def _process_diff_entry(self, line: str, new_sha: str) -> None:
        """Parse a single ``git diff-tree`` output line and apply the change.

        Format: ``:old_mode new_mode old_blob new_blob status\\tpath[\\tpath2]``
        """
        meta, *paths = line.split("\t")
        status = meta.split()[-1]  # last token before the first tab
        file_path = paths[0]

        if status in ("A", "M"):
            if not self._is_excluded(file_path):
                self._upload_git_file(new_sha, file_path)

        elif status == "D":
            if not self._is_excluded(file_path):
                self._delete_drive_file(file_path)

        elif status.startswith("R"):
            # Rename: paths[0] = old name, paths[1] = new name
            old_path = paths[0]
            new_path = paths[1] if len(paths) > 1 else paths[0]
            if not self._is_excluded(old_path):
                self._delete_drive_file(old_path)
            if not self._is_excluded(new_path):
                self._upload_git_file(new_sha, new_path)

    # ── File operations ───────────────────────────────────────────

    def _upload_git_file(self, sha: str, file_path: str) -> None:
        """Read *file_path* from Git objects and upload it to Drive."""
        result = subprocess.run(
            ["git", "show", f"{sha}:{file_path}"],
            capture_output=True,
        )
        if result.returncode != 0:
            self._msg(f"Warning: could not read {file_path} from {sha[:8]}")
            return

        content: bytes = result.stdout
        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        parent_id = self._resolve_parent_folder(file_path, create=True)
        file_name = PurePosixPath(file_path).name

        existing_id = self._client.find_file(file_name, parent_id=parent_id)

        try:
            self._client.upload_file(
                name=file_name,
                content=content,
                parent_id=parent_id,
                mime_type=mime_type,
                existing_file_id=existing_id,
            )
        except DriveApiError as exc:
            self._msg(f"Warning: failed to upload {file_path}: {exc}")

    def _delete_drive_file(self, file_path: str) -> None:
        """Remove *file_path* from Drive (trash)."""
        parent_id = self._resolve_parent_folder(file_path, create=False)
        if parent_id is None:
            return  # parent folder doesn't exist, so the file is already gone

        file_name = PurePosixPath(file_path).name
        file_id = self._client.find_file(file_name, parent_id=parent_id)
        if file_id is None:
            return

        try:
            self._client.delete_file(file_id)
        except DriveApiError as exc:
            self._msg(f"Warning: failed to delete {file_path}: {exc}")

    # ── Folder helpers ────────────────────────────────────────────

    def _resolve_parent_folder(
        self, file_path: str, *, create: bool
    ) -> str | None:
        """Return the Drive folder ID for the parent of *file_path*.

        When *create* is ``True``, missing folders are created (upload path).
        When ``False``, returns ``None`` if any ancestor is missing (delete path).
        """
        parent = PurePosixPath(file_path).parent
        if parent == PurePosixPath("."):
            return self._repo_folder_id

        folder_path = str(parent)
        if folder_path in self._folder_cache:
            return self._folder_cache[folder_path]

        if create:
            return self._ensure_folder_path(folder_path)
        return self._find_folder_path(folder_path)

    def _ensure_folder_path(self, folder_path: str) -> str:
        """Ensure nested folders exist on Drive, caching every segment."""
        if folder_path in self._folder_cache:
            return self._folder_cache[folder_path]

        parts = PurePosixPath(folder_path).parts
        current_id = self._repo_folder_id

        for i, part in enumerate(parts):
            sub_path = str(PurePosixPath(*parts[: i + 1]))
            if sub_path in self._folder_cache:
                current_id = self._folder_cache[sub_path]
                continue
            current_id = self._client.ensure_folder(part, parent_id=current_id)
            self._folder_cache[sub_path] = current_id

        return current_id

    def _find_folder_path(self, folder_path: str) -> str | None:
        """Walk *folder_path* segments, returning ``None`` if any is missing."""
        if folder_path in self._folder_cache:
            return self._folder_cache[folder_path]

        parts = PurePosixPath(folder_path).parts
        current_id = self._repo_folder_id

        for i, part in enumerate(parts):
            sub_path = str(PurePosixPath(*parts[: i + 1]))
            if sub_path in self._folder_cache:
                current_id = self._folder_cache[sub_path]
                continue
            found = self._client.find_folder(part, parent_id=current_id)
            if found is None:
                return None
            current_id = found
            self._folder_cache[sub_path] = current_id

        return current_id

    # ── Utilities ─────────────────────────────────────────────────

    def _is_excluded(self, file_path: str) -> bool:
        """Return ``True`` if *file_path* lives under an excluded directory."""
        return any(
            part in self._EXCLUDE_DIRS for part in PurePosixPath(file_path).parts
        )

    @staticmethod
    def _msg(text: str) -> None:
        """Write a user-facing message to stderr."""
        print(text, file=sys.stderr)
