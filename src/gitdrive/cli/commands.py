"""GitDrive CLI — user-facing commands."""

import click

from gitdrive import __version__


@click.group()
@click.version_option(version=__version__, prog_name="gitdrive")
def cli() -> None:
    """Turn a Google Drive folder into a Git remote."""
