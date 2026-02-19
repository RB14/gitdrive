"""Browsable file sync — mirrors the Git working tree on Google Drive."""

from __future__ import annotations

import mimetypes
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import PurePosixPath

from gitdrive.drive.client import DriveClient
from gitdrive.exceptions import DriveApiError

__all__ = ["TreeSyncer"]

# Maximum concurrent uploads.  Conservative to stay well within the
# Drive API limit of 20,000 queries / 100 s (~200 req/s).
_MAX_WORKERS = 4


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
        # Cache: (parent_id, file_name) → Drive file ID (or None)
        self._file_listing_cache: dict[str, dict[str, str]] = {}
        # Running stats for speed display (guarded by lock for threads).
        self._sync_bytes: int = 0
        self._sync_done: int = 0
        self._sync_total: int = 0
        self._sync_start: float = 0.0
        self._lock = threading.Lock()

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

        all_files = [
            fp for fp in result.stdout.strip().splitlines()
            if not self._is_excluded(fp)
        ]
        total = len(all_files)
        self._msg(f"  Syncing {total} files to Drive...")

        # Phase 1: ensure all folder paths exist (sequential — creates dirs).
        self._ensure_all_folders(all_files)

        # Phase 2: pre-list existing files per folder (eliminates per-file
        # find_file calls — 1 list_files per folder instead of 1 per file).
        self._prefetch_file_listings(all_files)

        # Phase 3: parallel uploads.
        self._sync_bytes = 0
        self._sync_done = 0
        self._sync_total = total
        self._sync_start = time.monotonic()

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = {
                pool.submit(self._upload_git_file, sha, fp): fp
                for fp in all_files
            }
            for future in as_completed(futures):
                future.result()  # propagate exceptions
                with self._lock:
                    self._sync_done += 1
                    self._progress(
                        f"  Syncing files ({self._sync_done}/{total})"
                        f" — {self._current_speed()}"
                    )

        self._progress_done(total)

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

        lines = [ln for ln in result.stdout.strip().splitlines() if ln]
        if not lines:
            return

        # Parse diff entries to determine file paths that need uploading.
        entries = [self._parse_diff_entry(ln) for ln in lines]

        # Collect files that need uploading for folder pre-creation.
        upload_paths = []
        for status, paths in entries:
            if status in ("A", "M"):
                if not self._is_excluded(paths[0]):
                    upload_paths.append(paths[0])
            elif status.startswith("R"):
                new_path = paths[1] if len(paths) > 1 else paths[0]
                if not self._is_excluded(new_path):
                    upload_paths.append(new_path)

        total = len(lines)
        self._msg(f"  Syncing {total} changed file{'s' if total != 1 else ''} to Drive...")

        # Pre-create folders and pre-list existing files.
        if upload_paths:
            self._ensure_all_folders(upload_paths)
            self._prefetch_file_listings(upload_paths)

        self._sync_bytes = 0
        self._sync_done = 0
        self._sync_total = total
        self._sync_start = time.monotonic()

        # Incremental sync stays sequential — mixed add/delete/rename
        # operations have ordering dependencies.
        for i, (status, paths) in enumerate(entries, 1):
            self._progress(
                f"  Syncing changes ({i}/{total})"
                f" — {self._current_speed()}"
            )
            self._apply_diff_entry(status, paths, new_sha)

        self._progress_done(total)

    @staticmethod
    def _parse_diff_entry(line: str) -> tuple[str, list[str]]:
        """Parse a diff-tree line into ``(status, [paths])``."""
        meta, *paths = line.split("\t")
        status = meta.split()[-1]
        return status, paths

    def _apply_diff_entry(
        self, status: str, paths: list[str], new_sha: str
    ) -> None:
        """Apply a single parsed diff entry."""
        file_path = paths[0]

        if status in ("A", "M"):
            if not self._is_excluded(file_path):
                self._upload_git_file(new_sha, file_path)

        elif status == "D":
            if not self._is_excluded(file_path):
                self._delete_drive_file(file_path)

        elif status.startswith("R"):
            old_path = paths[0]
            new_path = paths[1] if len(paths) > 1 else paths[0]
            if not self._is_excluded(old_path):
                self._delete_drive_file(old_path)
            if not self._is_excluded(new_path):
                self._upload_git_file(new_sha, new_path)

    # ── Pre-fetching ─────────────────────────────────────────────

    def _ensure_all_folders(self, file_paths: list[str]) -> None:
        """Create all required folder paths on Drive (sequential)."""
        # Collect unique folder paths first.
        unique_folders: list[str] = []
        seen: set[str] = set()
        for fp in file_paths:
            parent = PurePosixPath(fp).parent
            if parent == PurePosixPath("."):
                continue
            folder_path = str(parent)
            if folder_path not in seen:
                seen.add(folder_path)
                unique_folders.append(folder_path)

        total = len(unique_folders)
        for i, folder_path in enumerate(unique_folders, 1):
            self._progress(f"  Preparing folders ({i}/{total})")
            self._ensure_folder_path(folder_path)

    def _prefetch_file_listings(self, file_paths: list[str]) -> None:
        """Pre-list existing files per folder (1 API call per folder).

        Populates ``_file_listing_cache`` so that ``_find_existing_file``
        can resolve file IDs locally without per-file API calls.
        """
        # Collect unique parent folder IDs.
        folder_ids: set[str] = set()
        for fp in file_paths:
            parent = PurePosixPath(fp).parent
            if parent == PurePosixPath("."):
                folder_ids.add(self._repo_folder_id)
            else:
                fid = self._folder_cache.get(str(parent))
                if fid:
                    folder_ids.add(fid)

        to_list = [fid for fid in folder_ids if fid not in self._file_listing_cache]
        for i, fid in enumerate(to_list, 1):
            self._progress(f"  Indexing existing files ({i}/{len(to_list)} folders)")
            try:
                files = self._client.list_files(fid)
                self._file_listing_cache[fid] = {
                    f["name"]: f["id"] for f in files
                }
            except DriveApiError:
                self._file_listing_cache[fid] = {}

    def _find_existing_file(self, name: str, parent_id: str) -> str | None:
        """Look up an existing file ID from the pre-fetched listing cache."""
        listing = self._file_listing_cache.get(parent_id)
        if listing is not None:
            return listing.get(name)
        # Fallback to individual API call if not pre-fetched.
        return self._client.find_file(name, parent_id=parent_id)

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
        with self._lock:
            self._sync_bytes += len(content)
        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        parent_id = self._resolve_parent_folder(file_path, create=True)
        file_name = PurePosixPath(file_path).name

        existing_id = self._find_existing_file(file_name, parent_id)

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
        file_id = self._find_existing_file(file_name, parent_id)
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

    @staticmethod
    def _progress(text: str) -> None:
        """Overwrite the current stderr line with *text* (inline progress)."""
        print(f"\r{text:<72s}", end="", file=sys.stderr, flush=True)

    def _progress_done(self, total: int) -> None:
        """Finish progress with a summary line showing total size and speed."""
        elapsed = time.monotonic() - self._sync_start
        size = self._fmt_size(self._sync_bytes)
        speed = self._fmt_speed(self._sync_bytes, elapsed)
        print(f"\r  Synced {total} files ({size}) — {speed:<72s}", file=sys.stderr)

    def _current_speed(self) -> str:
        """Return the running average speed as a formatted string."""
        elapsed = time.monotonic() - self._sync_start
        if elapsed < 0.5:
            return "..."
        return self._fmt_speed(self._sync_bytes, elapsed)

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
