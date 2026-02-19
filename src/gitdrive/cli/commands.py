"""GitDrive CLI — user-facing commands."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import click

from gitdrive import __version__
from gitdrive.config import GitDriveConfig
from gitdrive.drive.auth import AuthManager
from gitdrive.drive.client import DriveClient
from gitdrive.exceptions import AuthenticationError, DriveApiError, ManifestError
from gitdrive.store.manifest import BundleEntry, Manifest

# ── Helpers ───────────────────────────────────────────────────


def _get_auth_and_client(
    ctx: click.Context,
) -> tuple[AuthManager, DriveClient]:
    """Build an AuthManager and an authenticated DriveClient from context."""
    config: GitDriveConfig = ctx.obj["config"]
    auth = AuthManager(config)
    try:
        creds = auth.get_credentials()
    except AuthenticationError as exc:
        raise click.ClickException(str(exc)) from exc
    return auth, DriveClient(creds)


def _require_root_folder(config: GitDriveConfig) -> str:
    """Load settings and return the root folder ID, or abort."""
    settings = config.load_settings()
    root_id = settings.get("root_folder_id")
    if not root_id:
        raise click.ClickException(
            "GitDrive is not initialized. Run 'gitdrive init' first."
        )
    return root_id


def _short_sha(sha: str) -> str:
    """Return the first 8 characters of a SHA hex string."""
    return sha[:8] if len(sha) >= 8 else sha


def _detect_repo_from_remote(remote_name: str = "gdrive") -> str:
    """Auto-detect the repo name from the gdrive git remote URL.

    Must be called from inside a git working tree that has a remote
    with a ``gdrive://`` or ``gdrive::`` URL.
    """
    result = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"Could not auto-detect repo: no '{remote_name}' remote found. "
            f"Use --repo to specify the repository name."
        )
    url = result.stdout.strip()
    if url.startswith("gdrive://"):
        return url.removeprefix("gdrive://")
    if url.startswith("gdrive::"):
        return url.removeprefix("gdrive::")
    raise click.ClickException(
        f"Remote '{remote_name}' is not a gdrive remote (URL: {url}). "
        f"Use --repo to specify the repository name."
    )


# ── Root group ────────────────────────────────────────────────


@click.group()
@click.version_option(version=__version__, prog_name="gitdrive")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Turn a Google Drive folder into a Git remote."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = GitDriveConfig()


# ── Auth command group ────────────────────────────────────────


@cli.group()
def auth() -> None:
    """Manage Google Drive authentication."""


@auth.command("setup")
@click.pass_context
def auth_setup(ctx: click.Context) -> None:
    """Guided setup for Google OAuth2 credentials."""
    config: GitDriveConfig = ctx.obj["config"]
    manager = AuthManager(config)
    try:
        manager.setup_credentials()
    except AuthenticationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(click.style("Credentials configured successfully.", fg="green"))


@auth.command("login")
@click.pass_context
def auth_login(ctx: click.Context) -> None:
    """Authenticate with Google Drive (opens browser)."""
    config: GitDriveConfig = ctx.obj["config"]
    manager = AuthManager(config)
    try:
        manager.login()
    except AuthenticationError as exc:
        raise click.ClickException(str(exc)) from exc


@auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Show authentication status."""
    config: GitDriveConfig = ctx.obj["config"]
    manager = AuthManager(config)
    status = manager.get_status()

    creds_ok = status["credentials_configured"]
    token_ok = status["token_valid"]
    expiry = status["token_expiry"]

    click.echo("Authentication status:")
    click.echo(
        f"  Credentials configured: "
        f"{click.style('yes', fg='green') if creds_ok else click.style('no', fg='red')}"
    )
    click.echo(
        f"  Token valid:            "
        f"{click.style('yes', fg='green') if token_ok else click.style('no', fg='red')}"
    )
    if expiry:
        from datetime import datetime, timezone
        utc_dt = datetime.fromisoformat(expiry).replace(tzinfo=timezone.utc)
        local_dt = utc_dt.astimezone()
        expiry_display = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    else:
        expiry_display = "N/A"
    click.echo(f"  Token expiry:           {expiry_display}")


# ── Init command ──────────────────────────────────────────────


@cli.command()
@click.option(
    "--folder",
    default="GitDrive",
    show_default=True,
    help="Root folder name on Google Drive.",
)
@click.pass_context
def init(ctx: click.Context, folder: str) -> None:
    """Initialize GitDrive root folder on Google Drive."""
    config: GitDriveConfig = ctx.obj["config"]
    _auth, client = _get_auth_and_client(ctx)

    try:
        root_id = client.ensure_folder(folder)
    except DriveApiError as exc:
        raise click.ClickException(f"Failed to create root folder: {exc}") from exc

    settings = config.load_settings()
    settings["root_folder_id"] = root_id
    config.save_settings(settings)

    click.echo(
        f"Initialized GitDrive root folder "
        f"{click.style(folder, fg='green')} (id: {root_id})"
    )


# ── Add command ───────────────────────────────────────────────


@cli.command()
@click.option(
    "--name",
    "remote_name",
    default="gdrive",
    show_default=True,
    help="Git remote name.",
)
@click.option(
    "--repo",
    "repo_name",
    default=None,
    help="Repository name on Drive (defaults to current directory name).",
)
@click.pass_context
def add(ctx: click.Context, remote_name: str, repo_name: str | None) -> None:
    """Add a gdrive remote to the current Git repository."""
    # Validate that CWD is a git repository.
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(
            "Not a git repository. Run this command from inside a git repo."
        )

    if repo_name is None:
        repo_name = Path.cwd().name

    # Check if the remote already exists.
    result = subprocess.run(
        ["git", "remote"],
        capture_output=True,
        text=True,
    )
    existing_remotes = result.stdout.strip().splitlines()
    if remote_name in existing_remotes:
        raise click.ClickException(
            f"Remote '{remote_name}' already exists. "
            f"Remove it first with: git remote remove {remote_name}"
        )

    remote_url = f"gdrive://{repo_name}"
    result = subprocess.run(
        ["git", "remote", "add", remote_name, remote_url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"Failed to add remote: {result.stderr.strip()}"
        )

    click.echo(
        f"Added remote {click.style(remote_name, fg='green')} → "
        f"{click.style(remote_url, bold=True)}"
    )


# ── List command ──────────────────────────────────────────────


@cli.command("list")
@click.pass_context
def list_repos(ctx: click.Context) -> None:
    """List all repositories on Google Drive."""
    config: GitDriveConfig = ctx.obj["config"]
    _auth, client = _get_auth_and_client(ctx)
    root_id = _require_root_folder(config)

    try:
        folders = client.list_files(
            root_id,
            query_extra=f"and mimeType = '{DriveClient.FOLDER_MIME}'",
        )
    except DriveApiError as exc:
        raise click.ClickException(f"Failed to list Drive folders: {exc}") from exc

    if not folders:
        click.echo("No repositories found. Push a repo with 'git push gdrive main'.")
        return

    rows: list[tuple[str, str, str]] = []

    for folder in folders:
        folder_name = folder["name"]
        folder_id = folder["id"]

        # Check for a .gitdrive metadata subfolder.
        gitdrive_id = client.find_folder(".gitdrive", parent_id=folder_id)
        if gitdrive_id is None:
            continue

        manifest_file_id = client.find_file("manifest.json", parent_id=gitdrive_id)
        if manifest_file_id is None:
            rows.append((folder_name, "-", "-"))
            continue

        try:
            raw = client.download_file(manifest_file_id)
            manifest = Manifest.from_json(raw.decode("utf-8"))
            branches = ", ".join(
                ref.removeprefix("refs/heads/") for ref in manifest.refs
            )
            rows.append((folder_name, branches or "-", manifest.updated_at or "-"))
        except (DriveApiError, ManifestError):
            rows.append((folder_name, "?", "?"))

    if not rows:
        click.echo("No repositories found. Push a repo with 'git push gdrive main'.")
        return

    # Compute column widths for alignment.
    headers = ("REPOSITORY", "BRANCHES", "LAST UPDATED")
    col_widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows))
        for i in range(len(headers))
    ]

    header_line = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    click.echo(click.style(header_line, bold=True))
    for row in rows:
        click.echo("  ".join(val.ljust(col_widths[i]) for i, val in enumerate(row)))


# ── Info command ──────────────────────────────────────────────


@cli.command()
@click.argument("repo")
@click.pass_context
def info(ctx: click.Context, repo: str) -> None:
    """Show detailed info about a repository."""
    config: GitDriveConfig = ctx.obj["config"]
    _auth, client = _get_auth_and_client(ctx)
    root_id = _require_root_folder(config)

    repo_folder_id = client.find_folder(repo, parent_id=root_id)
    if repo_folder_id is None:
        raise click.ClickException(f"Repository '{repo}' not found on Drive.")

    gitdrive_id = client.find_folder(".gitdrive", parent_id=repo_folder_id)
    if gitdrive_id is None:
        raise click.ClickException(
            f"Repository '{repo}' exists but has no .gitdrive metadata."
        )

    manifest_file_id = client.find_file("manifest.json", parent_id=gitdrive_id)
    if manifest_file_id is None:
        raise click.ClickException(
            f"Repository '{repo}' has no manifest.json."
        )

    try:
        raw = client.download_file(manifest_file_id)
        manifest = Manifest.from_json(raw.decode("utf-8"))
    except (DriveApiError, ManifestError) as exc:
        raise click.ClickException(f"Failed to read manifest: {exc}") from exc

    click.echo(f"Repository: {click.style(manifest.repo_name or repo, bold=True)}")
    click.echo(f"Last push:  {manifest.updated_at or 'N/A'}")
    click.echo(f"Bundles:    {len(manifest.bundles)}")

    click.echo("\nBranches:")
    if manifest.refs:
        for ref, sha in sorted(manifest.refs.items()):
            branch = ref.removeprefix("refs/heads/")
            click.echo(f"  {branch:30s} {_short_sha(sha)}")
    else:
        click.echo("  (none)")


# ── GC command ────────────────────────────────────────────────


@cli.command()
@click.argument("repo")
@click.pass_context
def gc(ctx: click.Context, repo: str) -> None:
    """Garbage-collect bundles for a repository."""
    config: GitDriveConfig = ctx.obj["config"]
    _auth, client = _get_auth_and_client(ctx)
    root_id = _require_root_folder(config)

    repo_folder_id = client.find_folder(repo, parent_id=root_id)
    if repo_folder_id is None:
        raise click.ClickException(f"Repository '{repo}' not found on Drive.")

    gitdrive_id = client.find_folder(".gitdrive", parent_id=repo_folder_id)
    if gitdrive_id is None:
        raise click.ClickException(
            f"Repository '{repo}' exists but has no .gitdrive metadata."
        )

    manifest_file_id = client.find_file("manifest.json", parent_id=gitdrive_id)
    if manifest_file_id is None:
        raise click.ClickException(f"Repository '{repo}' has no manifest.json.")

    try:
        raw = client.download_file(manifest_file_id)
        manifest = Manifest.from_json(raw.decode("utf-8"))
    except (DriveApiError, ManifestError) as exc:
        raise click.ClickException(f"Failed to read manifest: {exc}") from exc

    old_count = len(manifest.bundles)
    if old_count <= 1:
        click.echo("Nothing to garbage-collect (0 or 1 bundles).")
        return

    with tempfile.TemporaryDirectory(prefix="gitdrive-gc-") as tmp_dir:
        tmp = Path(tmp_dir)

        # Download all existing bundles and unbundle them into a bare repo.
        bare_repo = tmp / "repo.git"
        result = subprocess.run(
            ["git", "init", "--bare", str(bare_repo)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise click.ClickException(
                f"Failed to init bare repo: {result.stderr.strip()}"
            )

        for entry in manifest.bundles:
            bundle_path = tmp / f"{entry.id}.bundle"
            try:
                client.download_file_to_path(entry.file_id, bundle_path)
            except DriveApiError as exc:
                raise click.ClickException(
                    f"Failed to download bundle {entry.id}: {exc}"
                ) from exc

            result = subprocess.run(
                ["git", "-C", str(bare_repo), "bundle", "unbundle", str(bundle_path)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise click.ClickException(
                    f"Failed to unbundle {entry.id}: {result.stderr.strip()}"
                )

        # Update refs in the bare repo to match the manifest.
        for ref, sha in manifest.refs.items():
            subprocess.run(
                ["git", "-C", str(bare_repo), "update-ref", ref, sha],
                capture_output=True,
                text=True,
            )

        # Create a single compacted bundle from the bare repo.
        compacted_path = tmp / "compacted.bundle"
        result = subprocess.run(
            [
                "git",
                "-C",
                str(bare_repo),
                "bundle",
                "create",
                str(compacted_path),
                "--all",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise click.ClickException(
                f"Failed to create compacted bundle: {result.stderr.strip()}"
            )

        compacted_bytes = compacted_path.read_bytes()

        # Upload the new compacted bundle.
        bundles_folder_id = client.ensure_folder("bundles", parent_id=gitdrive_id)
        new_bundle_id = "0001"
        try:
            new_file_id = client.upload_file(
                name=f"{new_bundle_id}.bundle",
                content=compacted_bytes,
                parent_id=bundles_folder_id,
            )
        except DriveApiError as exc:
            raise click.ClickException(
                f"Failed to upload compacted bundle: {exc}"
            ) from exc

        # Delete old bundles from Drive.
        for entry in manifest.bundles:
            try:
                client.delete_file(entry.file_id)
            except DriveApiError:
                click.echo(
                    click.style(
                        f"  Warning: could not delete old bundle {entry.id}",
                        fg="yellow",
                    )
                )

        # Update manifest: single bundle, same refs.
        manifest.bundles = [
            BundleEntry(
                id=new_bundle_id,
                file_id=new_file_id,
            )
        ]
        manifest.updated_at = Manifest.new(repo).updated_at

        try:
            client.upload_file(
                name="manifest.json",
                content=manifest.to_json().encode("utf-8"),
                parent_id=gitdrive_id,
                existing_file_id=manifest_file_id,
            )
        except DriveApiError as exc:
            raise click.ClickException(
                f"Failed to update manifest: {exc}"
            ) from exc

    # Clean up orphaned bundles in bundles/ folder.
    try:
        all_bundle_files = client.list_files(bundles_folder_id)
        valid_ids = {new_file_id}
        orphans = [f for f in all_bundle_files if f["id"] not in valid_ids]
        for orphan in orphans:
            try:
                client.delete_file(orphan["id"])
            except DriveApiError:
                click.echo(
                    click.style(
                        f"  Warning: could not delete orphan bundle {orphan['name']}",
                        fg="yellow",
                    )
                )
        if orphans:
            click.echo(f"  Cleaned up {len(orphans)} orphaned bundle(s)")
    except DriveApiError:
        pass  # Best effort.

    # Clean up sync-lock if present.
    try:
        lock_id = client.find_file("sync-lock", parent_id=gitdrive_id)
        if lock_id:
            client.delete_file(lock_id)
            click.echo("  Removed stale sync-lock")
    except DriveApiError:
        pass  # Best effort.

    click.echo(
        f"Garbage collection complete: "
        f"{click.style(str(old_count), fg='yellow')} bundles → "
        f"{click.style('1', fg='green')}"
    )


# ── Browse command ───────────────────────────────────────────


@cli.command()
@click.argument("branch", required=False, default=None)
@click.option(
    "--repo", "-r", "repo_name", default=None,
    help="Repository name (auto-detected from gdrive remote if omitted).",
)
@click.pass_context
def browse(ctx: click.Context, branch: str | None, repo_name: str | None) -> None:
    """View or change the browsable branch for a repository.

    Without BRANCH, shows the current browsable branch.
    With BRANCH, switches the browsable files on Drive to that branch
    and re-syncs all files.

    When run from inside a git repo with a gdrive remote, the repository
    name is auto-detected.  Use --repo to override or when running outside
    a git repo.
    """
    if repo_name is None:
        repo_name = _detect_repo_from_remote()

    config: GitDriveConfig = ctx.obj["config"]
    _auth, client = _get_auth_and_client(ctx)
    root_id = _require_root_folder(config)

    repo_folder_id = client.find_folder(repo_name, parent_id=root_id)
    if repo_folder_id is None:
        raise click.ClickException(f"Repository '{repo_name}' not found on Drive.")

    gitdrive_id = client.find_folder(".gitdrive", parent_id=repo_folder_id)
    if gitdrive_id is None:
        raise click.ClickException(
            f"Repository '{repo_name}' exists but has no .gitdrive metadata."
        )

    manifest_file_id = client.find_file("manifest.json", parent_id=gitdrive_id)
    if manifest_file_id is None:
        raise click.ClickException(f"Repository '{repo_name}' has no manifest.json.")

    try:
        raw = client.download_file(manifest_file_id)
        manifest = Manifest.from_json(raw.decode("utf-8"))
    except (DriveApiError, ManifestError) as exc:
        raise click.ClickException(f"Failed to read manifest: {exc}") from exc

    # Show current browsable ref if no branch specified.
    if branch is None:
        resolved = manifest.resolve_browsable_ref()
        if resolved:
            display = resolved.removeprefix("refs/heads/")
            click.echo(f"Browsable branch: {click.style(display, bold=True)}")
        else:
            click.echo("No branches found — push something first.")
        return

    # Resolve branch to full ref name.
    new_ref = branch if branch.startswith("refs/") else f"refs/heads/{branch}"

    if new_ref not in manifest.refs:
        available = ", ".join(
            r.removeprefix("refs/heads/") for r in sorted(manifest.refs)
        )
        raise click.ClickException(
            f"Branch '{branch}' not found on Drive. "
            f"Available: {available or '(none)'}"
        )

    sha = manifest.refs[new_ref]
    click.echo(f"Switching browsable branch to {click.style(branch, bold=True)}...")

    # Re-sync all files for the new branch (full sync).
    from gitdrive.sync.tree import TreeSyncer

    syncer = TreeSyncer(client, repo_folder_id)
    syncer.sync(old_sha=None, new_sha=sha)

    # Update manifest and upload.
    manifest.browsable_ref = new_ref
    manifest.updated_at = Manifest.new(repo_name).updated_at

    try:
        client.upload_file(
            name="manifest.json",
            content=manifest.to_json().encode("utf-8"),
            parent_id=gitdrive_id,
            existing_file_id=manifest_file_id,
        )
    except DriveApiError as exc:
        raise click.ClickException(f"Failed to update manifest: {exc}") from exc

    click.echo(
        f"Done — files on Drive now show "
        f"{click.style(branch, fg='green')} at {_short_sha(sha)}"
    )


# ── Sync command ─────────────────────────────────────────


@cli.command()
@click.option(
    "--repo", "-r", "repo_name", default=None,
    help="Repository name (auto-detected from gdrive remote if omitted).",
)
@click.pass_context
def sync(ctx: click.Context, repo_name: str | None) -> None:
    """Re-sync browsable files on Drive for the current repository.

    Downloads the manifest and re-uploads all files for the browsable
    branch.  Useful after a push if files didn't sync, or to force a
    full refresh.

    When run from inside a git repo with a gdrive remote, the repository
    name is auto-detected.  Use --repo to override.
    """
    if repo_name is None:
        repo_name = _detect_repo_from_remote()

    config: GitDriveConfig = ctx.obj["config"]
    _auth, client = _get_auth_and_client(ctx)
    root_id = _require_root_folder(config)

    repo_folder_id = client.find_folder(repo_name, parent_id=root_id)
    if repo_folder_id is None:
        raise click.ClickException(f"Repository '{repo_name}' not found on Drive.")

    gitdrive_id = client.find_folder(".gitdrive", parent_id=repo_folder_id)
    if gitdrive_id is None:
        raise click.ClickException(
            f"Repository '{repo_name}' exists but has no .gitdrive metadata."
        )

    manifest_file_id = client.find_file("manifest.json", parent_id=gitdrive_id)
    if manifest_file_id is None:
        raise click.ClickException(f"Repository '{repo_name}' has no manifest.")

    try:
        raw = client.download_file(manifest_file_id)
        manifest = Manifest.from_json(raw.decode("utf-8"))
    except (DriveApiError, ManifestError) as exc:
        raise click.ClickException(f"Failed to read manifest: {exc}") from exc

    browsable = manifest.resolve_browsable_ref()
    if browsable is None:
        raise click.ClickException("No branches found — push something first.")

    sha = manifest.refs[browsable]
    branch_name = browsable.removeprefix("refs/heads/")

    click.echo(
        f"Syncing browsable files for "
        f"{click.style(branch_name, bold=True)} ({_short_sha(sha)})..."
    )

    from gitdrive.sync.tree import TreeSyncer

    syncer = TreeSyncer(client, repo_folder_id)
    syncer.sync(old_sha=None, new_sha=sha)

    # Clean up sync-lock if present (Drive is now consistent).
    try:
        lock_id = client.find_file("sync-lock", parent_id=gitdrive_id)
        if lock_id:
            client.delete_file(lock_id)
            click.echo("  Removed stale sync-lock")
    except DriveApiError:
        pass  # Best effort.

    # Persist browsable_ref if it was resolved via fallback.
    if manifest.browsable_ref != browsable:
        manifest.browsable_ref = browsable
        manifest.updated_at = Manifest.new(repo_name).updated_at
        try:
            client.upload_file(
                name="manifest.json",
                content=manifest.to_json().encode("utf-8"),
                parent_id=gitdrive_id,
                existing_file_id=manifest_file_id,
            )
        except DriveApiError as exc:
            raise click.ClickException(
                f"Failed to update manifest: {exc}"
            ) from exc

    click.echo(
        f"Done — Drive files now show "
        f"{click.style(branch_name, fg='green')}"
    )


# ── Clone command ────────────────────────────────────────────


@cli.command()
@click.argument("repo")
@click.argument("directory", required=False, default=None)
@click.option(
    "--name",
    "remote_name",
    default="gdrive",
    show_default=True,
    help="Git remote name.",
)
@click.pass_context
def clone(ctx: click.Context, repo: str, directory: str | None, remote_name: str) -> None:
    """Clone a repository from Google Drive.

    Creates a new local Git repository from a Drive-backed repo.
    """
    config: GitDriveConfig = ctx.obj["config"]
    _auth, client = _get_auth_and_client(ctx)
    root_id = _require_root_folder(config)

    # Verify the repo exists on Drive.
    repo_folder_id = client.find_folder(repo, parent_id=root_id)
    if repo_folder_id is None:
        raise click.ClickException(f"Repository '{repo}' not found on Drive.")

    gitdrive_id = client.find_folder(".gitdrive", parent_id=repo_folder_id)
    if gitdrive_id is None:
        raise click.ClickException(
            f"Repository '{repo}' has no .gitdrive metadata."
        )

    manifest_file_id = client.find_file("manifest.json", parent_id=gitdrive_id)
    if manifest_file_id is None:
        raise click.ClickException(f"Repository '{repo}' has no manifest — nothing to clone.")

    try:
        raw = client.download_file(manifest_file_id)
        manifest = Manifest.from_json(raw.decode("utf-8"))
    except (DriveApiError, ManifestError) as exc:
        raise click.ClickException(f"Failed to read manifest: {exc}") from exc

    if not manifest.bundles:
        raise click.ClickException(f"Repository '{repo}' has no bundles — nothing to clone.")

    # Determine target directory.
    target = Path(directory) if directory else Path.cwd() / repo
    if target.exists():
        raise click.ClickException(f"Directory '{target}' already exists.")

    click.echo(f"Cloning {click.style(repo, bold=True)} into {target} ...")

    # Initialize a new git repo.
    result = subprocess.run(
        ["git", "init", str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(f"git init failed: {result.stderr.strip()}")

    # Download and apply all bundles in order.
    from gitdrive.transport.fetch import FetchHandler

    handler = FetchHandler(
        config=config,
        client=client,
        manifest=manifest,
        repo_name=repo,
    )
    # Pass a dummy fetch_specs — the handler applies all unapplied bundles.
    fetch_specs = [(sha, ref) for ref, sha in manifest.refs.items()]

    # Run unbundle inside the new repo.
    import os
    original_dir = os.getcwd()
    try:
        os.chdir(target)
        handler.fetch(fetch_specs)
    finally:
        os.chdir(original_dir)

    # Add the remote.
    remote_url = f"gdrive://{repo}"
    subprocess.run(
        ["git", "-C", str(target), "remote", "add", remote_name, remote_url],
        capture_output=True,
        text=True,
    )

    # Create tracking refs from the manifest.
    for ref, sha in manifest.refs.items():
        # Map refs/heads/X → refs/remotes/gdrive/X
        if ref.startswith("refs/heads/"):
            branch = ref.removeprefix("refs/heads/")
            remote_ref = f"refs/remotes/{remote_name}/{branch}"
            subprocess.run(
                ["git", "-C", str(target), "update-ref", remote_ref, sha],
                capture_output=True,
                text=True,
            )

    # Checkout the browsable ref (main > master > first available).
    checkout_ref = manifest.resolve_browsable_ref()

    if checkout_ref:
        branch = checkout_ref.removeprefix("refs/heads/")
        subprocess.run(
            ["git", "-C", str(target), "checkout", "-b", branch, manifest.refs[checkout_ref]],
            capture_output=True,
            text=True,
        )

    click.echo(
        f"Cloned into {click.style(str(target), fg='green')} — "
        f"{len(manifest.refs)} ref(s), {len(manifest.bundles)} bundle(s)"
    )
