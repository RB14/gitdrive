"""gitdrive CLI — commands that rewrite the manifest on Drive."""

from __future__ import annotations

import subprocess
from pathlib import Path

from support import REPO_NAME, Remote, commit, git, gitdrive

MAIN = "refs/heads/main:refs/heads/main"


def _commit_secret() -> str:
    """Commit a file holding a secret; return the secret's blob ID."""
    Path("secret.txt").write_text("hunter2\n")
    git("add", "secret.txt")
    git("commit", "-q", "-m", "Add secret")
    return git("rev-parse", "HEAD:secret.txt")


def _fresh_clone_has(remote: Remote, clone: Path, blob: str) -> bool:
    """Clone into *clone* and report whether it holds object *blob* at all."""
    remote.clone(clone)
    result = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", blob])
    return result.returncode == 0


def _push_three_bundles(remote) -> str:
    """Push main three times, one new commit each; return main's SHA."""
    remote.push(MAIN)
    commit("a.txt")
    remote.push(MAIN)
    commit("b.txt")
    remote.push(MAIN)
    return git("rev-parse", "main")


def test_gc_compacts_the_bundles_into_one(repo, remote, tmp_path):
    main = _push_three_bundles(remote)

    result = gitdrive("gc", REPO_NAME)

    assert result.exit_code == 0, result.output
    assert len(remote.bundle_ids()) == 1
    assert len(remote.drive.listing(REPO_NAME, ".gitdrive", "bundles")) == 1
    assert remote.clone(tmp_path / "clone") == {"refs/heads/main": main}


def test_gc_never_reuses_a_bundle_id_an_existing_clone_applied(repo, remote, tmp_path):
    _push_three_bundles(remote)
    clone = tmp_path / "clone"
    remote.clone(clone)  # records 0001-0003 as applied
    assert gitdrive("gc", REPO_NAME).exit_code == 0
    tip = commit("c.txt")
    remote.push(MAIN)

    refs = remote.fetch_all(clone)

    assert refs == {"refs/heads/main": tip}
    # The compacted bundle and the push after it got IDs never issued before,
    # and the clone forgot the records of bundles that are gone.
    assert remote.bundle_ids() == ["0004", "0005"]
    assert remote.applied_bundles(clone) == ["0004", "0005"]


def test_gc_does_not_overwrite_a_manifest_changed_meanwhile(repo, remote):
    main = _push_three_bundles(remote)
    # A push lands while gc uploads the compacted bundle.
    remote.drive.before_upload(
        "*.bundle",
        lambda: remote.update_manifest(lambda m: m.update_refs({"refs/heads/other": main})),
    )

    result = gitdrive("gc", REPO_NAME)

    assert result.exit_code == 1
    assert "changed during gc" in result.output
    assert remote.manifest().refs == {"refs/heads/main": main, "refs/heads/other": main}
    # Nothing was deleted: the manifest still lists, and Drive still holds,
    # the original bundles.
    assert remote.bundle_ids() == ["0001", "0002", "0003"]
    assert remote.drive.listing(REPO_NAME, ".gitdrive", "bundles") == [
        "0001.bundle",
        "0002.bundle",
        "0003.bundle",
    ]


def test_browse_does_not_overwrite_a_manifest_changed_meanwhile(repo, remote):
    remote.push(MAIN)
    git("branch", "release")
    remote.push("refs/heads/release:refs/heads/release")
    main = git("rev-parse", "main")
    # A push lands while browse re-syncs the files.
    remote.drive.before_upload(
        "README.md",
        lambda: remote.update_manifest(lambda m: m.update_refs({"refs/heads/other": main})),
    )

    result = gitdrive("browse", "release", "--repo", REPO_NAME)

    assert result.exit_code == 1
    assert "changed meanwhile" in result.output
    manifest = remote.manifest()
    assert manifest.browsable_ref == "refs/heads/main"
    assert manifest.refs["refs/heads/other"] == main


def test_gc_purges_history_a_force_push_rewrote_away(repo, remote, tmp_path):
    remote.push(MAIN)
    secret = _commit_secret()
    remote.push(MAIN)
    git("reset", "-q", "--hard", "HEAD~1")  # rewrite history without the secret
    commit("clean.txt")
    remote.push("+" + MAIN)
    assert _fresh_clone_has(remote, tmp_path / "before", secret)  # still on Drive

    assert gitdrive("gc", REPO_NAME).exit_code == 0

    assert not _fresh_clone_has(remote, tmp_path / "after", secret)


def test_gc_purges_a_rewind_onto_an_already_pushed_commit(repo, remote, tmp_path):
    secret = _commit_secret()
    remote.push(MAIN)  # one bundle, holding the secret
    git("reset", "-q", "--hard", "HEAD~1")
    remote.push("+" + MAIN)  # ref-only: the parent commit is already on Drive
    assert remote.bundle_ids() == ["0001"]

    result = gitdrive("gc", REPO_NAME)

    assert result.exit_code == 0, result.output
    assert not _fresh_clone_has(remote, tmp_path / "clone", secret)


def test_gc_leaves_an_already_compact_repository_alone(repo, remote):
    _push_three_bundles(remote)
    assert gitdrive("gc", REPO_NAME).exit_code == 0
    compacted = remote.bundle_ids()

    result = gitdrive("gc", REPO_NAME)

    assert result.exit_code == 0
    assert "Nothing to garbage-collect" in result.output
    assert remote.bundle_ids() == compacted  # no new bundle for clones to fetch
