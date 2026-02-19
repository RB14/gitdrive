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
        # Cache: folder_id → {name: (file_id, mimeType)}
        self._file_listing_cache: dict[str, dict[str, tuple[str, str]]] = {}
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
        """Upload every tracked file and delete stale files from Drive.

        This is an **authoritative** sync: after it completes, the Drive
        folder tree matches the git tree exactly (modulo ``.gitdrive``).
        """
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

        # Phase 2: discover ALL folders on Drive (replaces _prefetch_file_listings).
        # This finds folders left by previous syncs / branch switches too.
        self._walk_drive_folders()

        # Phase 3: build the expected-files set for authoritative cleanup.
        expected_files: dict[str, set[str]] = {}
        for fp in all_files:
            parent = PurePosixPath(fp).parent
            if parent == PurePosixPath("."):
                folder_id = self._repo_folder_id
            else:
                folder_id = self._folder_cache.get(str(parent))
            if folder_id:
                expected_files.setdefault(folder_id, set()).add(
                    PurePosixPath(fp).name
                )

        # Phase 4: parallel uploads.
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

        # Phase 5: authoritative cleanup — delete stale files.
        self._cleanup_stale_files(expected_files)

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
                    f["name"]: (f["id"], f.get("mimeType", ""))
                    for f in files
                }
            except DriveApiError:
                self._file_listing_cache[fid] = {}

    def _find_existing_file(self, name: str, parent_id: str) -> str | None:
        """Look up an existing file ID from the pre-fetched listing cache."""
        listing = self._file_listing_cache.get(parent_id)
        if listing is not None:
            entry = listing.get(name)
            return entry[0] if entry else None
        # Fallback to individual API call if not pre-fetched.
        return self._client.find_file(name, parent_id=parent_id)

    # ── Drive discovery & cleanup ────────────────────────────────

    def _walk_drive_folders(self) -> None:
        """Recursively list all folders under the repo root into caches.

        Discovers ALL Drive folders (not just those in the git tree) so
        that ``_cleanup_stale_files`` can find and remove stale files left
        by previous syncs or branch switches.  Populates both
        ``_folder_cache`` and ``_file_listing_cache``.
        """
        queue: list[tuple[str, str]] = [(self._repo_folder_id, "")]
        visited = 0

        while queue:
            folder_id, rel_path = queue.pop(0)
            if folder_id in self._file_listing_cache:
                continue

            visited += 1
            self._progress(f"  Indexing Drive folders ({visited})")

            try:
                files = self._client.list_files(folder_id)
            except DriveApiError:
                self._file_listing_cache[folder_id] = {}
                continue

            listing: dict[str, tuple[str, str]] = {}
            for f in files:
                name = f["name"]
                fid = f["id"]
                mime = f.get("mimeType", "")
                listing[name] = (fid, mime)

                # Recurse into subfolders, skipping excluded dirs.
                if mime == DriveClient.FOLDER_MIME:
                    if name not in self._EXCLUDE_DIRS:
                        sub_path = (
                            f"{rel_path}/{name}" if rel_path else name
                        )
                        self._folder_cache.setdefault(sub_path, fid)
                        queue.append((fid, sub_path))

            self._file_listing_cache[folder_id] = listing

    def _cleanup_stale_files(
        self, expected_files: dict[str, set[str]]
    ) -> None:
        """Delete files on Drive not in the expected set, then remove
        any folders left empty as a result.

        Skips the ``.gitdrive`` metadata folder and the repo root.
        """
        # ── Phase 1: identify and delete stale files ──────────────
        stale: list[tuple[str, str]] = []  # (file_id, name)

        for folder_id, listing in self._file_listing_cache.items():
            expected = expected_files.get(folder_id, set())
            for name, (file_id, mime_type) in listing.items():
                # Never delete subfolders (handled in phase 2).
                if mime_type == DriveClient.FOLDER_MIME:
                    continue
                # Belt-and-suspenders: never touch .gitdrive by name.
                if name == ".gitdrive":
                    continue
                if name not in expected:
                    stale.append((file_id, name))

        deleted_ids: set[str] = set()

        if stale:
            self._msg(
                f"  Cleaning up {len(stale)} stale"
                f" file{'s' if len(stale) != 1 else ''}..."
            )
            for file_id, name in stale:
                try:
                    self._client.delete_file(file_id)
                    deleted_ids.add(file_id)
                except DriveApiError as exc:
                    self._msg(
                        f"Warning: failed to delete stale file"
                        f" {name}: {exc}"
                    )

        # ── Phase 2: remove folders left empty ────────────────────
        # Reverse map so we can sort by depth.
        id_to_path: dict[str, str] = {
            fid: path for path, fid in self._folder_cache.items()
        }

        # Only consider non-root folders that aren't expected to hold
        # files (expected_files entries may point to folders whose
        # listing cache is stale because uploads happened after the
        # walk).
        candidates = [
            fid
            for fid in self._file_listing_cache
            if fid != self._repo_folder_id
            and fid in id_to_path
            and fid not in expected_files
        ]
        # Deepest first so children are resolved before parents.
        candidates.sort(
            key=lambda fid: id_to_path[fid].count("/"), reverse=True
        )

        removed_folders = 0
        for folder_id in candidates:
            listing = self._file_listing_cache.get(folder_id, {})
            # Folder is empty when every item in its listing was
            # deleted (stale file or empty subfolder from this pass).
            if all(
                fid in deleted_ids
                for _name, (fid, _mime) in listing.items()
            ):
                try:
                    self._client.delete_file(folder_id)
                    deleted_ids.add(folder_id)
                    removed_folders += 1
                except DriveApiError:
                    pass

        if removed_folders:
            self._msg(
                f"  Removed {removed_folders} empty"
                f" folder{'s' if removed_folders != 1 else ''}"
            )

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
