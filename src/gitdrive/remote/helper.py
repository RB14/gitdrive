"""git-remote-gdrive — Git remote helper for Google Drive.

Implements the Git remote helper protocol so that Git can push to and fetch
from a Google Drive-backed repository via the ``gdrive://`` URL scheme.

The binary is invoked by Git as::

    git-remote-gdrive <remote-name> <url>

Communication happens over stdin/stdout (protocol messages) while stderr is
reserved for user-facing progress output.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from gitdrive.config import GitDriveConfig
from gitdrive.drive.auth import AuthManager
from gitdrive.drive.client import DriveClient
from gitdrive.exceptions import GitDriveError
from gitdrive.store.manifest import Manifest


@dataclass
class Refspec:
    """Parsed push refspec."""

    src: str
    dst: str
    force: bool


class RemoteHelper:
    """Implements the Git remote helper protocol for Google Drive.

    Reads commands from stdin line-by-line, dispatches to the appropriate
    handler, and writes protocol responses to stdout.  All user-facing
    messages go to stderr.
    """

    _KNOWN_OPTIONS = frozenset({"verbosity", "progress", "force"})

    def __init__(self, remote_name: str, url: str) -> None:
        self._remote_name = remote_name
        self._repo_name = self._parse_url(url)
        self._config = GitDriveConfig()

        # Lazy-initialized resources.
        self._client: DriveClient | None = None
        self._manifest: Manifest | None = None
        self._manifest_file_id: str | None = None
        self._manifest_loaded: bool = False

        self._options: dict[str, str] = {}

    # ── Main loop ────────────────────────────────────────────────────

    def run(self) -> None:
        """Main protocol loop — reads commands from stdin, dispatches."""
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line:
                break

            parts = line.split()
            cmd = parts[0] if parts else ""

            match cmd:
                case "capabilities":
                    self._cmd_capabilities()
                case "option":
                    self._cmd_option(line)
                case "list":
                    self._cmd_list()
                case "fetch":
                    self._cmd_fetch(line)
                case "push":
                    self._cmd_push(line)
                case _:
                    self._die(f"Unknown command: {line}")

    # ── Protocol command handlers ────────────────────────────────────

    def _cmd_capabilities(self) -> None:
        """Respond to the ``capabilities`` command."""
        self._respond("push")
        self._respond("fetch")
        self._respond("option")
        self._respond("")

    def _cmd_option(self, line: str) -> None:
        """Handle ``option <name> <value>``."""
        parts = line.split(maxsplit=2)
        name = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else ""

        if name in self._KNOWN_OPTIONS:
            self._options[name] = value
            self._respond("ok")
        else:
            self._respond("unsupported")

    def _cmd_list(self) -> None:
        """Handle ``list`` and ``list for-push``.

        Downloads the manifest from Drive and outputs each ref.  If the
        manifest does not yet exist (first push), outputs only a blank line.
        """
        manifest = self._load_manifest()
        if manifest is not None:
            for refname, sha in manifest.refs.items():
                self._respond(f"{sha} {refname}")
        self._respond("")

    def _cmd_fetch(self, first_line: str) -> None:
        """Handle a fetch batch.

        Reads all ``fetch <sha> <ref>`` lines until a blank line, then
        delegates to the transport fetch handler.
        """
        fetch_specs = [self._parse_fetch_line(first_line)]
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line:
                break
            fetch_specs.append(self._parse_fetch_line(line))

        # Import lazily so the module loads even before Phase 6 exists.
        from gitdrive.transport.fetch import FetchHandler

        handler = FetchHandler(
            config=self._config,
            client=self._get_client(),
            manifest=self._get_manifest(),
            repo_name=self._repo_name,
        )
        handler.fetch(fetch_specs)
        self._respond("")

    def _cmd_push(self, first_line: str) -> None:
        """Handle a push batch.

        Reads all ``push [+]<src>:<dst>`` lines until a blank line, then
        delegates to the transport push handler.
        """
        refspecs = [self._parse_push_refspec(first_line)]
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line:
                break
            refspecs.append(self._parse_push_refspec(line))

        # Import lazily so the module loads even before Phase 6 exists.
        from gitdrive.transport.push import PushHandler

        handler = PushHandler(
            config=self._config,
            client=self._get_client(),
            manifest=self._get_manifest(),
            repo_name=self._repo_name,
        )
        results: list[tuple[Refspec, str | None]] = handler.push(refspecs)

        for refspec, error in results:
            if error is None:
                self._respond(f"ok {refspec.dst}")
            else:
                self._respond(f"error {refspec.dst} {error}")
        self._respond("")

    # ── Parsing helpers ──────────────────────────────────────────────

    @staticmethod
    def _parse_url(url: str) -> str:
        """Extract the repository name from a ``gdrive://`` or ``gdrive::`` URL."""
        if url.startswith("gdrive://"):
            return url[len("gdrive://"):]
        if url.startswith("gdrive::"):
            return url[len("gdrive::"):]
        return url

    @staticmethod
    def _parse_push_refspec(line: str) -> Refspec:
        """Parse ``push [+]<src>:<dst>`` into a :class:`Refspec`."""
        spec = line.split(maxsplit=1)[1] if " " in line else line
        force = spec.startswith("+")
        if force:
            spec = spec[1:]
        src, dst = spec.split(":", 1)
        return Refspec(src=src, dst=dst, force=force)

    @staticmethod
    def _parse_fetch_line(line: str) -> tuple[str, str]:
        """Parse ``fetch <sha> <ref>`` into ``(sha, ref)``."""
        parts = line.split()
        return (parts[1], parts[2])

    # ── I/O helpers ──────────────────────────────────────────────────

    def _respond(self, msg: str) -> None:
        """Write a line to stdout and flush immediately."""
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    def _msg(self, msg: str) -> None:
        """Write a user-facing message to stderr."""
        print(msg, file=sys.stderr)

    def _die(self, msg: str) -> None:
        """Print a fatal error to stderr and exit."""
        print(f"fatal: {msg}", file=sys.stderr)
        sys.exit(1)

    # ── Lazy resource initialization ─────────────────────────────────

    def _get_client(self) -> DriveClient:
        """Lazily authenticate and return a :class:`DriveClient`."""
        if self._client is None:
            auth = AuthManager(self._config)
            creds = auth.get_credentials()
            self._client = DriveClient(creds)
        return self._client

    def _load_manifest(self) -> Manifest | None:
        """Download the manifest from Drive, caching the result.

        Traverses the folder hierarchy:
        ``root_folder / <repo_name> / .gitdrive / manifest.json``

        Returns ``None`` if any part of the hierarchy is missing (repository
        has never been pushed to).  Both the parsed :class:`Manifest` and
        the ``manifest.json`` file ID are cached for later use.
        """
        if self._manifest_loaded:
            return self._manifest

        self._manifest_loaded = True

        settings = self._config.load_settings()
        root_folder_id: str | None = settings.get("root_folder_id")
        if root_folder_id is None:
            return None

        client = self._get_client()

        repo_folder_id = client.find_folder(self._repo_name, parent_id=root_folder_id)
        if repo_folder_id is None:
            return None

        gitdrive_folder_id = client.find_folder(".gitdrive", parent_id=repo_folder_id)
        if gitdrive_folder_id is None:
            return None

        manifest_file_id = client.find_file("manifest.json", parent_id=gitdrive_folder_id)
        if manifest_file_id is None:
            return None

        raw = client.download_file(manifest_file_id)
        manifest = Manifest.from_json(raw.decode("utf-8"))

        self._manifest = manifest
        self._manifest_file_id = manifest_file_id
        return manifest

    def _get_manifest(self) -> Manifest:
        """Return the cached manifest, or a fresh empty one if none exists."""
        loaded = self._load_manifest()
        if loaded is None:
            return Manifest.new(self._repo_name)
        return loaded


def main() -> None:
    """Entry point for the ``git-remote-gdrive`` binary."""
    if len(sys.argv) < 3:
        print("Usage: git-remote-gdrive <remote> <url>", file=sys.stderr)
        sys.exit(1)

    remote_name = sys.argv[1]
    url = sys.argv[2]

    try:
        helper = RemoteHelper(remote_name, url)
        helper.run()
    except GitDriveError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
