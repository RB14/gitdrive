"""Google Drive API v3 abstraction layer."""

from __future__ import annotations

import io
import logging
import random
import time
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

from gitdrive.exceptions import DriveApiError, NotFoundError, RateLimitError

logger = logging.getLogger(__name__)


class DriveClient:
    """Thin wrapper around the Google Drive API v3.

    Provides folder and file operations with automatic retry/back-off
    for transient errors and rate-limit responses.
    """

    BUNDLE_MIME = "application/octet-stream"
    FOLDER_MIME = "application/vnd.google-apps.folder"
    RESUMABLE_THRESHOLD = 5 * 1024 * 1024  # 5 MB

    def __init__(self, credentials: Credentials) -> None:
        self._credentials = credentials
        self._service: Any | None = None  # lazily initialised

    # ── Service accessor ─────────────────────────────────────────

    @property
    def service(self) -> Any:
        """Lazily build and return the Drive v3 service resource."""
        if self._service is None:
            import google_auth_httplib2
            import httplib2
            from googleapiclient.discovery import build

            http = google_auth_httplib2.AuthorizedHttp(
                self._credentials, http=httplib2.Http()
            )
            self._service = build("drive", "v3", http=http)
        return self._service

    # ── Folder operations ────────────────────────────────────────

    def create_folder(
        self, name: str, parent_id: str | None = None
    ) -> str:
        """Create a Drive folder and return its file ID."""
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": self.FOLDER_MIME,
        }
        if parent_id is not None:
            metadata["parents"] = [parent_id]

        result = self._execute_with_retry(
            self.service.files().create(
                body=metadata,
                fields="id",
            )
        )
        return result["id"]

    def find_folder(
        self, name: str, parent_id: str | None = None
    ) -> str | None:
        """Find a folder by *name* under *parent_id*.

        Returns the file ID or ``None`` if not found.
        """
        q_parts = [
            f"name = '{_escape_query(name)}'",
            f"mimeType = '{self.FOLDER_MIME}'",
            "trashed = false",
        ]
        if parent_id is not None:
            q_parts.append(f"'{parent_id}' in parents")

        result = self._execute_with_retry(
            self.service.files().list(
                q=" and ".join(q_parts),
                fields="files(id)",
                pageSize=1,
            )
        )
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def ensure_folder(
        self, name: str, parent_id: str | None = None
    ) -> str:
        """Find or create a folder by *name*, returning its file ID."""
        existing = self.find_folder(name, parent_id)
        if existing is not None:
            return existing
        return self.create_folder(name, parent_id)

    def ensure_folder_path(self, path: str, root_id: str) -> str:
        """Create nested folders for *path* under *root_id*.

        For example, ``"src/gitdrive/cli"`` creates (or reuses) three
        nested folders and returns the ID of the innermost one.
        """
        current_id = root_id
        for segment in Path(path).parts:
            current_id = self.ensure_folder(segment, parent_id=current_id)
        return current_id

    # ── File operations ──────────────────────────────────────────

    def upload_file(
        self,
        name: str,
        content: bytes,
        parent_id: str,
        mime_type: str = BUNDLE_MIME,
        existing_file_id: str | None = None,
    ) -> str:
        """Upload or update a file, returning its file ID.

        Files larger than :pyattr:`RESUMABLE_THRESHOLD` use a resumable
        upload; smaller files use a simple media upload.
        """
        resumable = len(content) > self.RESUMABLE_THRESHOLD
        media = MediaInMemoryUpload(
            content,
            mimetype=mime_type,
            resumable=resumable,
        )

        if existing_file_id is not None:
            # Update existing file (content only; metadata unchanged).
            result = self._execute_with_retry(
                self.service.files().update(
                    fileId=existing_file_id,
                    media_body=media,
                    fields="id",
                )
            )
        else:
            metadata: dict[str, Any] = {
                "name": name,
                "parents": [parent_id],
            }
            result = self._execute_with_retry(
                self.service.files().create(
                    body=metadata,
                    media_body=media,
                    fields="id",
                )
            )

        return result["id"]

    def download_file(self, file_id: str) -> bytes:
        """Download file content as bytes."""
        request = self.service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)

        done = False
        while not done:
            _, done = self._execute_download_chunk(downloader)

        return buffer.getvalue()

    def download_file_to_path(self, file_id: str, path: Path) -> None:
        """Download file content directly to a local *path*."""
        request = self.service.files().get_media(fileId=file_id)

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = self._execute_download_chunk(downloader)

    def find_file(self, name: str, parent_id: str) -> str | None:
        """Find a file by *name* under *parent_id*.

        Returns the file ID or ``None`` if not found.
        """
        q = (
            f"name = '{_escape_query(name)}' and "
            f"'{parent_id}' in parents and "
            "trashed = false"
        )
        result = self._execute_with_retry(
            self.service.files().list(
                q=q,
                fields="files(id)",
                pageSize=1,
            )
        )
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def delete_file(self, file_id: str) -> None:
        """Move a file to the trash."""
        self._execute_with_retry(
            self.service.files().update(
                fileId=file_id,
                body={"trashed": True},
            )
        )

    def list_files(
        self, parent_id: str, query_extra: str = ""
    ) -> list[dict[str, Any]]:
        """List all files under *parent_id* with automatic pagination.

        *query_extra* is appended to the query string (must start with
        ``" and ..."`` if non-empty).

        Returns a list of file metadata dicts (id, name, mimeType, size,
        modifiedTime).
        """
        q = f"'{parent_id}' in parents and trashed = false"
        if query_extra:
            q += f" {query_extra}"

        all_files: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            result = self._execute_with_retry(
                self.service.files().list(
                    q=q,
                    fields=(
                        "nextPageToken, "
                        "files(id, name, mimeType, size, modifiedTime)"
                    ),
                    pageSize=1000,
                    pageToken=page_token,
                )
            )
            all_files.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if page_token is None:
                break

        return all_files

    # ── Retry logic ──────────────────────────────────────────────

    def _execute_with_retry(
        self, request: Any, *, max_retries: int = 5
    ) -> Any:
        """Execute a Drive API *request* with exponential back-off.

        Retries on HTTP 429 (rate-limit), 500, and 503 responses.
        Respects the ``Retry-After`` header when present.

        Raises:
            RateLimitError: If the final failure is a 429.
            NotFoundError: Immediately on a 404 (no retry).
            DriveApiError: For all other terminal HTTP errors.
        """
        base_delay = 1.0
        max_delay = 60.0

        for attempt in range(max_retries + 1):
            try:
                return request.execute()
            except HttpError as exc:
                status = exc.resp.status

                # Non-retryable errors — raise immediately.
                if status == 404:
                    raise NotFoundError(
                        f"Resource not found: {exc}"
                    ) from exc

                if status not in (429, 500, 503):
                    raise DriveApiError(
                        f"Drive API error (HTTP {status}): {exc}"
                    ) from exc

                # Last attempt — translate to the appropriate exception.
                if attempt == max_retries:
                    if status == 429:
                        raise RateLimitError(
                            f"Rate limit exceeded after {max_retries} "
                            f"retries: {exc}"
                        ) from exc
                    raise DriveApiError(
                        f"Drive API error (HTTP {status}) after "
                        f"{max_retries} retries: {exc}"
                    ) from exc

                # Compute delay, respecting Retry-After if present.
                delay = min(base_delay * (2 ** attempt), max_delay)
                retry_after = exc.resp.get("retry-after")
                if retry_after is not None:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass

                jitter = random.uniform(0, delay * 0.5)  # noqa: S311
                sleep_time = delay + jitter

                logger.info(
                    "Retryable Drive API error (HTTP %d), attempt %d/%d. "
                    "Sleeping %.1fs.",
                    status,
                    attempt + 1,
                    max_retries,
                    sleep_time,
                )
                time.sleep(sleep_time)

        # Should be unreachable, but satisfy type checkers.
        raise DriveApiError("Unexpected retry loop exit")  # pragma: no cover

    def _execute_download_chunk(
        self, downloader: MediaIoBaseDownload
    ) -> tuple[Any, bool]:
        """Execute a single download chunk with retry on transient errors."""
        max_retries = 5
        base_delay = 1.0
        max_delay = 60.0

        for attempt in range(max_retries + 1):
            try:
                return downloader.next_chunk()
            except HttpError as exc:
                status = exc.resp.status

                if status == 404:
                    raise NotFoundError(
                        f"Download resource not found: {exc}"
                    ) from exc

                if status not in (429, 500, 503) or attempt == max_retries:
                    raise DriveApiError(
                        f"Download failed (HTTP {status}): {exc}"
                    ) from exc

                delay = min(base_delay * (2 ** attempt), max_delay)
                jitter = random.uniform(0, delay * 0.5)  # noqa: S311
                time.sleep(delay + jitter)

        raise DriveApiError("Unexpected download retry loop exit")  # pragma: no cover


# ── Module-private helpers ───────────────────────────────────────

def _escape_query(value: str) -> str:
    """Escape single quotes for Drive API query strings."""
    return value.replace("\\", "\\\\").replace("'", "\\'")
