"""Push — incremental bundles and ref-only updates."""

from __future__ import annotations

import itertools

from gitdrive.store.manifest import Manifest
from gitdrive.transport import push as push_module
from support import REPO_NAME, Remote, commit, git

MAIN = "refs/heads/main:refs/heads/main"
WIFI_LIVE = "refs/heads/wifi-live:refs/heads/wifi-live"
CONFLICT = "error the manifest on Drive changed since it was read; fetch and push again"


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


def test_delete_removes_the_ref_and_keeps_the_bundles(repo, remote):
    remote.push(MAIN)
    git("checkout", "-q", "-b", "wifi-live")
    commit("feature.txt")
    remote.push(WIFI_LIVE)

    assert remote.push(":refs/heads/wifi-live") == {"refs/heads/wifi-live": "ok"}

    assert remote.refs() == {"refs/heads/main": git("rev-parse", "main")}
    assert remote.bundle_ids() == ["0001", "0002"]
    assert remote.drive.listing(REPO_NAME, ".gitdrive", "bundles") == [
        "0001.bundle",
        "0002.bundle",
    ]


def test_delete_of_the_remote_head_is_refused(repo, remote):
    remote.push(MAIN)
    git("branch", "other")
    remote.push("refs/heads/other:refs/heads/other")
    before = remote.manifest()

    replies = remote.push(":refs/heads/main", ":refs/heads/other")

    assert replies["refs/heads/main"].startswith(
        "error refusing to delete the branch the remote HEAD points to"
    )
    assert replies["refs/heads/other"] == "ok"
    assert remote.manifest().refs == {"refs/heads/main": before.refs["refs/heads/main"]}
    assert remote.head() == "refs/heads/main"


def test_delete_of_a_missing_ref_is_rejected(repo, remote):
    remote.push(MAIN)

    assert remote.push(":refs/heads/nope") == {
        "refs/heads/nope": "error remote ref does not exist"
    }


def test_recreating_a_deleted_ref_uploads_its_objects_again(repo, remote):
    remote.push(MAIN)
    git("checkout", "-q", "-b", "wifi-live")
    commit("feature.txt")
    remote.push(WIFI_LIVE)
    remote.push(":refs/heads/wifi-live")

    assert remote.push(WIFI_LIVE) == {"refs/heads/wifi-live": "ok"}

    # Deleted refs no longer vouch for their objects, so they are re-bundled.
    assert remote.bundle_ids() == ["0001", "0002", "0003"]


def test_refs_of_one_push_do_not_trip_over_each_others_manifest(
    repo, remote, monkeypatch, capsys
):
    # Every manifest update gets a distinct timestamp, as on a real, slow Drive.
    ticks = itertools.count()
    monkeypatch.setattr(
        push_module, "_utcnow_iso", lambda: f"2026-01-01T00:00:{next(ticks):02d}Z"
    )
    remote.push(MAIN)
    commit("a.txt")
    git("branch", "copy")
    git("tag", "v1")

    replies = remote.push(
        MAIN, "refs/heads/copy:refs/heads/copy", "refs/tags/v1:refs/tags/v1"
    )

    assert set(replies.values()) == {"ok"}
    assert "warning" not in capsys.readouterr().err


def test_push_does_not_overwrite_a_manifest_another_push_changed(repo, remote):
    remote.push(MAIN)
    git("branch", "feature")
    remote.push("refs/heads/feature:refs/heads/feature")
    old = git("rev-parse", "main")
    tip = commit("a.txt")
    # Another push lands while this one uploads its bundle.
    remote.drive.before_upload(
        "*.bundle",
        lambda: remote.update_manifest(lambda m: m.update_refs({"refs/heads/other": old})),
    )

    replies = remote.push(MAIN, ":refs/heads/feature")

    assert replies == {"refs/heads/main": CONFLICT, "refs/heads/feature": CONFLICT}
    assert remote.manifest().refs == {
        "refs/heads/main": old,
        "refs/heads/feature": old,
        "refs/heads/other": old,
    }
    # The bundle nothing references was dropped again.
    assert remote.drive.listing(REPO_NAME, ".gitdrive", "bundles") == ["0001.bundle"]

    # A fresh attempt (after fetching) goes through and keeps the other push.
    assert remote.push(MAIN) == {"refs/heads/main": "ok"}
    assert remote.manifest().refs["refs/heads/main"] == tip
    assert remote.manifest().refs["refs/heads/other"] == old


def test_first_push_does_not_overwrite_a_manifest_created_meanwhile(repo, remote):
    sha = git("rev-parse", "main")

    def other_first_push() -> None:
        manifest = Manifest.new(REPO_NAME)
        manifest.update_refs({"refs/heads/other": sha})
        remote.drive.upload_file(
            "manifest.json",
            manifest.to_json().encode("utf-8"),
            remote.drive.path_id(REPO_NAME, ".gitdrive"),
        )

    remote.drive.before_upload("*.bundle", other_first_push)

    assert remote.push(MAIN) == {"refs/heads/main": CONFLICT}
    assert remote.manifest().refs == {"refs/heads/other": sha}
    assert remote.drive.listing(REPO_NAME, ".gitdrive", "bundles") == []


def _push_two_commits_then_rewrite(remote: Remote) -> tuple[str, str]:
    """Push main twice, then replace its last commit locally (a rewrite that
    needs a forced push).  Return ``(first pushed commit, main on Drive)``."""
    first = git("rev-parse", "main")
    remote.push(MAIN)
    on_drive = commit("a.txt")
    remote.push(MAIN)
    git("reset", "-q", "--hard", "HEAD~1")
    commit("rewritten.txt")
    return first, on_drive


def test_lease_option_is_accepted(repo, remote):
    assert remote.option(f"cas refs/heads/main:{'0' * 40}") == "ok"
    assert remote.option("cas refs/heads/main") == "ok"


def test_forced_push_with_a_holding_lease_goes_through(repo, remote):
    _, on_drive = _push_two_commits_then_rewrite(remote)

    replies = remote.push("+" + MAIN, options=(f"cas refs/heads/main:{on_drive}",))

    assert replies == {"refs/heads/main": "ok"}
    assert remote.manifest().refs["refs/heads/main"] == git("rev-parse", "main")


def test_forced_push_with_a_stale_lease_is_rejected(repo, remote):
    first, on_drive = _push_two_commits_then_rewrite(remote)

    replies = remote.push("+" + MAIN, options=(f"cas refs/heads/main:{first}",))

    assert replies == {"refs/heads/main": "error stale info"}
    assert remote.manifest().refs["refs/heads/main"] == on_drive


def test_lease_is_checked_against_drive_at_push_time(repo, remote):
    first, on_drive = _push_two_commits_then_rewrite(remote)

    # The lease held when git listed the refs; then another push moved main.
    replies = remote.push(
        "+" + MAIN,
        options=(f"cas refs/heads/main:{on_drive}",),
        meanwhile=lambda: remote.update_manifest(
            lambda m: m.update_refs({"refs/heads/main": first})
        ),
    )

    assert replies == {"refs/heads/main": "error stale info"}
    assert remote.manifest().refs["refs/heads/main"] == first


def test_lease_that_the_ref_must_not_exist(repo, remote):
    remote.push(MAIN)
    git("branch", "new")
    absent = f"cas refs/heads/new:{'0' * 40}"

    assert remote.push("refs/heads/new:refs/heads/new", options=(absent,)) == {
        "refs/heads/new": "ok"
    }
    assert remote.push("+refs/heads/new:refs/heads/new", options=(absent,)) == {
        "refs/heads/new": "error stale info"
    }


def test_bare_lease_expects_the_remote_tracking_ref(repo, remote):
    git("remote", "add", "gdrive", f"gdrive://{REPO_NAME}")
    first, on_drive = _push_two_commits_then_rewrite(remote)

    git("update-ref", "refs/remotes/gdrive/main", first)  # an outdated view
    assert remote.push("+" + MAIN, options=("cas refs/heads/main",)) == {
        "refs/heads/main": "error stale info"
    }

    git("update-ref", "refs/remotes/gdrive/main", on_drive)
    assert remote.push("+" + MAIN, options=("cas refs/heads/main",)) == {
        "refs/heads/main": "ok"
    }


def test_forced_push_without_a_lease_overwrites(repo, remote):
    _push_two_commits_then_rewrite(remote)

    assert remote.push("+" + MAIN) == {"refs/heads/main": "ok"}
    assert remote.manifest().refs["refs/heads/main"] == git("rev-parse", "main")
