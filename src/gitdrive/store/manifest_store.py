"""Manifest storage on Drive with optimistic locking."""

from __future__ import annotations

from gitdrive.drive.client import DriveClient
from gitdrive.exceptions import ManifestConflictError
from gitdrive.store.manifest import Manifest


class ManifestStore:
    """Reads and writes ``.gitdrive/manifest.json`` without losing updates.

    The store remembers the manifest revision it last read or wrote (its
    *base*).  :meth:`save` refuses to overwrite the manifest when Drive's
    copy is no longer that base — someone else wrote it in the meantime —
    so read-modify-write cycles never silently drop another writer's refs
    or bundles.  After each of its own saves the base moves to the revision
    just written, so a series of saves from one process never conflicts
    with itself.

    Drive has no conditional update, so a writer landing between the check
    and the upload (a sub-second window) still goes undetected.
    """

    FILE_NAME = "manifest.json"

    def __init__(self, client: DriveClient, gitdrive_folder_id: str) -> None:
        self._client = client
        self._folder_id = gitdrive_folder_id
        # Base: the manifest file and revision last read or written.  A
        # ``None`` file ID means the manifest did not exist.
        self._file_id: str | None = None
        self._revision: str | None = None

    def load(self) -> Manifest | None:
        """Download the manifest and make it the base (``None`` if absent)."""
        file_id = self._client.find_file(self.FILE_NAME, parent_id=self._folder_id)
        if file_id is None:
            self._file_id = self._revision = None
            return None

        # Read the revision before the content: if another write lands in
        # between, the content is newer than the base, and the next save
        # reports a conflict instead of overwriting it.
        revision = self._client.get_revision(file_id)
        raw = self._client.download_file(file_id)
        manifest = Manifest.from_json(raw.decode("utf-8"))

        self._file_id, self._revision = file_id, revision
        return manifest

    def read_current(self) -> Manifest | None:
        """Download Drive's manifest as it is now, leaving the base alone."""
        file_id = self._client.find_file(self.FILE_NAME, parent_id=self._folder_id)
        if file_id is None:
            return None
        return Manifest.from_json(self._client.download_file(file_id).decode("utf-8"))

    def check(self) -> None:
        """Raise :class:`ManifestConflictError` if Drive's manifest left the base."""
        file_id = self._client.find_file(self.FILE_NAME, parent_id=self._folder_id)
        if file_id != self._file_id or (
            file_id is not None and self._client.get_revision(file_id) != self._revision
        ):
            raise ManifestConflictError(
                "the manifest on Drive changed since it was read"
            )

    def save(self, manifest: Manifest) -> None:
        """Upload *manifest* if Drive's copy is still the base, then rebase."""
        self.check()
        self._file_id, self._revision = self._client.upload_file_with_revision(
            name=self.FILE_NAME,
            content=manifest.to_json().encode("utf-8"),
            parent_id=self._folder_id,
            existing_file_id=self._file_id,
        )
