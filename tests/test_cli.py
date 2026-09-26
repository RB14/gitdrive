"""gitdrive CLI — commands that rewrite the manifest on Drive."""

from __future__ import annotations

from support import REPO_NAME, commit, git, gitdrive

MAIN = "refs/heads/main:refs/heads/main"


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
