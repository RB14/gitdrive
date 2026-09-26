"""Test support — git helpers, an in-memory Drive, and a remote-helper driver."""

from __future__ import annotations

import contextlib
import io
import itertools
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gitdrive.drive.client import DriveClient
from gitdrive.remote.helper import RemoteHelper
from gitdrive.store.manifest import Manifest

REPO_NAME = "demo"


# ── git ──────────────────────────────────────────────────────────────


def git(*args: str, cwd: Path | None = None) -> str:
    """Run ``git`` and return its stripped stdout, failing on a non-zero exit."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


def commit(name: str) -> str:
    """Commit a new file *name* in the current repo and return the commit SHA."""
    Path(name).write_text(f"{name}\n")
    git("add", name)
    git("commit", "-q", "-m", f"Add {name}")
    return git("rev-parse", "HEAD")


# ── Drive ────────────────────────────────────────────────────────────


class FakeDriveClient:
    """In-memory stand-in for :class:`DriveClient` — never touches the network.

    Mirrors the subset of the Drive API that gitdrive uses, including
    trashing (not erasing) deleted files.
    """

    def __init__(self) -> None:
        self._files: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()  # TreeSyncer uploads from worker threads.
        self.downloads: list[str] = []
        self.root_id = self.create_folder("GitDrive")

    # ── DriveClient API ──────────────────────────────────────────────

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        return self._create(name, parent_id, DriveClient.FOLDER_MIME, b"")

    def find_folder(self, name: str, parent_id: str | None = None) -> str | None:
        return self._find(name, parent_id, folders_only=True)

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
        return self.find_folder(name, parent_id) or self.create_folder(name, parent_id)

    def upload_file(
        self,
        name: str,
        content: bytes,
        parent_id: str,
        mime_type: str = DriveClient.BUNDLE_MIME,
        existing_file_id: str | None = None,
    ) -> str:
        if existing_file_id is not None:
            with self._lock:
                self._files[existing_file_id]["content"] = content
            return existing_file_id
        return self._create(name, parent_id, mime_type, content)

    def download_file(self, file_id: str) -> bytes:
        with self._lock:
            return self._files[file_id]["content"]

    def download_file_to_path(self, file_id: str, path: Path) -> None:
        self.downloads.append(file_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.download_file(file_id))

    def find_file(self, name: str, parent_id: str) -> str | None:
        return self._find(name, parent_id, folders_only=False)

    def delete_file(self, file_id: str) -> None:
        with self._lock:
            self._files[file_id]["trashed"] = True

    def list_files(self, parent_id: str, query_extra: str = "") -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"id": fid, "name": f["name"], "mimeType": f["mimeType"]}
                for fid, f in self._files.items()
                if f["parent"] == parent_id and not f["trashed"]
            ]

    # ── test helpers ─────────────────────────────────────────────────

    def path_id(self, *names: str) -> str | None:
        """Resolve *names* as a path below the root folder (``None`` if absent)."""
        current: str | None = self.root_id
        for name in names:
            if current is None:
                return None
            current = self._find(name, current, folders_only=False)
        return current

    def read(self, *names: str) -> bytes:
        """Return the content of the file at path *names* below the root."""
        file_id = self.path_id(*names)
        assert file_id is not None, f"{'/'.join(names)} not found on Drive"
        return self.download_file(file_id)

    def listing(self, *names: str) -> list[str]:
        """Return the sorted names inside the folder at path *names*."""
        folder_id = self.path_id(*names)
        assert folder_id is not None, f"{'/'.join(names)} not found on Drive"
        return sorted(f["name"] for f in self.list_files(folder_id))

    # ── internals ────────────────────────────────────────────────────

    def _create(
        self, name: str, parent_id: str | None, mime_type: str, content: bytes
    ) -> str:
        with self._lock:
            file_id = f"id{next(self._ids)}"
            self._files[file_id] = {
                "name": name,
                "parent": parent_id,
                "mimeType": mime_type,
                "content": content,
                "trashed": False,
            }
        return file_id

    def _find(
        self, name: str, parent_id: str | None, *, folders_only: bool
    ) -> str | None:
        with self._lock:
            for fid, f in self._files.items():
                if (
                    f["name"] == name
                    and f["parent"] == parent_id
                    and not f["trashed"]
                    and (not folders_only or f["mimeType"] == DriveClient.FOLDER_MIME)
                ):
                    return fid
        return None


# ── remote helper ────────────────────────────────────────────────────


class Remote:
    """Drives ``git-remote-gdrive`` for ``gdrive://demo`` the way git does.

    Each call runs a fresh :class:`RemoteHelper` (as git spawns a new
    process per command), so state only carries over through Drive.
    """

    def __init__(self, drive: FakeDriveClient) -> None:
        self.drive = drive

    def refs(self) -> dict[str, str]:
        """Return the refs advertised by ``list`` (without the HEAD symref)."""
        refs: dict[str, str] = {}
        for line in self._run("list"):
            if line and not line.startswith("@"):
                sha, ref = line.split()
                refs[ref] = sha
        return refs

    def head(self) -> str | None:
        """Return the ref advertised as the remote HEAD, if any."""
        for line in self._run("list"):
            if line.startswith("@") and line.endswith(" HEAD"):
                return line[1:].removesuffix(" HEAD")
        return None

    def push(self, *refspecs: str) -> dict[str, str]:
        """Push *refspecs* in one batch; map each destination to its reply.

        A reply is ``"ok"`` or ``"error <why>"``.
        """
        replies = self._run("list for-push", *(f"push {s}" for s in refspecs), "")
        statuses: dict[str, str] = {}
        for line in replies:
            status, _, rest = line.partition(" ")
            if status in ("ok", "error"):
                dst, _, why = rest.partition(" ")
                statuses[dst] = f"{status} {why}".rstrip()
        return statuses

    def clone(self, path: Path) -> dict[str, str]:
        """Like ``git clone``: init *path*, then :meth:`fetch_all` into it."""
        git("init", "-q", str(path))
        return self.fetch_all(path)

    def fetch_all(self, path: Path) -> dict[str, str]:
        """Fetch every advertised ref into the repo at *path* and write the refs.

        Fails if an advertised object, or anything it reaches, is missing
        afterwards.  Returns the advertised refs.
        """
        with contextlib.chdir(path):
            refs = self.refs()
            self._run(*(f"fetch {sha} {ref}" for ref, sha in refs.items()), "")
            for ref, sha in refs.items():
                git("update-ref", ref, sha)
            git("fsck", "--connectivity-only", "--no-dangling")
        return refs

    def manifest(self) -> Manifest:
        """Return the manifest currently stored on Drive."""
        raw = self.drive.read(REPO_NAME, ".gitdrive", "manifest.json")
        return Manifest.from_json(raw.decode("utf-8"))

    def bundle_ids(self) -> list[str]:
        """Return the IDs of the bundles listed in the manifest on Drive."""
        return [b.id for b in self.manifest().bundles]

    @staticmethod
    def _run(*commands: str) -> list[str]:
        """Feed *commands* to a fresh helper and return its stdout lines."""
        stdin = io.StringIO("".join(f"{c}\n" for c in commands))
        stdout = io.StringIO()
        with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
            RemoteHelper("gdrive", f"gdrive://{REPO_NAME}").run()
        return stdout.getvalue().splitlines()
