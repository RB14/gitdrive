"""Shared fixtures — isolated git/gitdrive state and a fake Google Drive."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitdrive.cli import commands as cli_module
from gitdrive.config import GitDriveConfig
from gitdrive.remote import helper as helper_module
from support import FakeDriveClient, Remote, commit, git


class _NoAuth:
    """Stands in for :class:`AuthManager`; the fake Drive needs no credentials."""

    def __init__(self, config: GitDriveConfig) -> None:
        pass

    def get_credentials(self) -> None:
        return None


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep git and gitdrive away from the user's config, tokens, and settings."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgSign = false\n"
        "[tag]\n\tgpgSign = false\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITDRIVE_HOME", str(tmp_path / "gitdrive-home"))


@pytest.fixture
def drive(isolated_env: None, monkeypatch: pytest.MonkeyPatch) -> FakeDriveClient:
    """A fake Drive, handed to the remote helper and CLI instead of the real one."""
    fake = FakeDriveClient()
    GitDriveConfig().save_settings({"root_folder_id": fake.root_id})
    for module in (helper_module, cli_module):
        monkeypatch.setattr(module, "AuthManager", _NoAuth)
        monkeypatch.setattr(module, "DriveClient", lambda _creds: fake)
    return fake


@pytest.fixture
def remote(drive: FakeDriveClient) -> Remote:
    """The ``gdrive://demo`` remote, backed by the fake Drive."""
    return Remote(drive)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A local repo with one commit on ``main``, used as the working directory."""
    path = tmp_path / "local"
    git("init", "-q", str(path))
    monkeypatch.chdir(path)
    commit("README.md")
    return path
