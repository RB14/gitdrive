"""Push — incremental bundles and ref-only updates."""

from __future__ import annotations

from support import REPO_NAME, Remote, commit, git

MAIN = "refs/heads/main:refs/heads/main"
WIFI_LIVE = "refs/heads/wifi-live:refs/heads/wifi-live"


def _push_wifi_live_then_fast_forward_main(remote: Remote) -> str:
    """Push ``main`` and a newer ``wifi-live``, then fast-forward ``main`` locally."""
    assert remote.push(MAIN) == {"refs/heads/main": "ok"}
    git("checkout", "-q", "-b", "wifi-live")
    tip = commit("feature.txt")
    assert remote.push(WIFI_LIVE) == {"refs/heads/wifi-live": "ok"}
    git("checkout", "-q", "main")
    git("merge", "-q", "--ff-only", "wifi-live")
    return tip


def test_push_with_new_commits_uploads_a_bundle(repo, remote):
    assert remote.push(MAIN) == {"refs/heads/main": "ok"}
    tip = commit("a.txt")

    assert remote.push(MAIN) == {"refs/heads/main": "ok"}

    assert remote.bundle_ids() == ["0001", "0002"]
    assert remote.manifest().refs == {"refs/heads/main": tip}


def test_fast_forward_onto_pushed_commit_updates_ref_only(repo, remote):
    tip = _push_wifi_live_then_fast_forward_main(remote)

    assert remote.push(MAIN) == {"refs/heads/main": "ok"}

    assert remote.manifest().refs == {
        "refs/heads/main": tip,
        "refs/heads/wifi-live": tip,
    }
    assert remote.bundle_ids() == ["0001", "0002"]
    assert remote.drive.listing(REPO_NAME, ".gitdrive", "bundles") == [
        "0001.bundle",
        "0002.bundle",
    ]
    # main is the browsable branch: its files follow, and the lock is gone.
    assert remote.drive.read(REPO_NAME, "feature.txt") == b"feature.txt\n"
    assert "sync-lock" not in remote.drive.listing(REPO_NAME, ".gitdrive")


def test_ref_only_push_recovers_an_interrupted_browsable_sync(repo, remote):
    _push_wifi_live_then_fast_forward_main(remote)
    drive = remote.drive
    drive.upload_file("sync-lock", b"{}", drive.path_id(REPO_NAME, ".gitdrive"))
    drive.upload_file("stale.txt", b"stale\n", drive.path_id(REPO_NAME))

    assert remote.push(MAIN) == {"refs/heads/main": "ok"}

    # The leftover lock forces an authoritative full sync, then is removed.
    assert drive.listing(REPO_NAME) == [".gitdrive", "README.md", "feature.txt"]
    assert "sync-lock" not in drive.listing(REPO_NAME, ".gitdrive")
    assert remote.bundle_ids() == ["0001", "0002"]


def test_new_branch_at_pushed_commit_updates_ref_only(repo, remote):
    remote.push(MAIN)
    git("branch", "release")

    assert remote.push("refs/heads/release:refs/heads/release") == {
        "refs/heads/release": "ok"
    }

    assert remote.manifest().refs["refs/heads/release"] == git("rev-parse", "main")
    assert remote.bundle_ids() == ["0001"]


def test_lightweight_tag_at_pushed_commit_updates_ref_only(repo, remote):
    remote.push(MAIN)
    git("tag", "v1")

    assert remote.push("refs/tags/v1:refs/tags/v1") == {"refs/tags/v1": "ok"}

    assert remote.manifest().refs["refs/tags/v1"] == git("rev-parse", "main")
    assert remote.bundle_ids() == ["0001"]


def test_annotated_tag_at_pushed_commit_uploads_the_tag_object(repo, remote):
    remote.push(MAIN)
    git("tag", "-a", "v1", "-m", "Release v1")

    assert remote.push("refs/tags/v1:refs/tags/v1") == {"refs/tags/v1": "ok"}

    # The tag object is new even though the commit it points at is not.
    assert remote.manifest().refs["refs/tags/v1"] == git("rev-parse", "v1")
    assert remote.bundle_ids() == ["0001", "0002"]


def test_refs_at_the_same_new_commit_share_one_bundle(repo, remote):
    remote.push(MAIN)
    tip = commit("a.txt")
    git("branch", "copy")

    assert remote.push(MAIN, "refs/heads/copy:refs/heads/copy") == {
        "refs/heads/main": "ok",
        "refs/heads/copy": "ok",
    }

    assert remote.manifest().refs == {"refs/heads/main": tip, "refs/heads/copy": tip}
    assert remote.bundle_ids() == ["0001", "0002"]
