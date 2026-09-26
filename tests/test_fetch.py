"""Fetch/clone — manifests whose refs do not map one-to-one to bundles."""

from __future__ import annotations

from support import commit, git


def test_clone_gets_refs_that_have_no_bundle_of_their_own(repo, remote, tmp_path):
    remote.push("refs/heads/main:refs/heads/main")
    git("checkout", "-q", "-b", "wifi-live")
    commit("feature.txt")
    remote.push("refs/heads/wifi-live:refs/heads/wifi-live")
    git("checkout", "-q", "main")
    git("merge", "-q", "--ff-only", "wifi-live")
    git("tag", "light")
    git("tag", "-a", "v1", "-m", "Release v1", "main~1")
    remote.push(
        "refs/heads/main:refs/heads/main",  # commit inside wifi-live's bundle
        "refs/tags/light:refs/tags/light",  # no bundle at all
        "refs/tags/v1:refs/tags/v1",  # bundle holding only the tag object
    )
    assert remote.bundle_ids() == ["0001", "0002", "0003"]

    refs = remote.clone(tmp_path / "clone")

    assert refs == remote.manifest().refs
    assert refs == {
        ref: git("rev-parse", ref)
        for ref in ("refs/heads/main", "refs/heads/wifi-live", "refs/tags/light", "refs/tags/v1")
    }
    assert remote.head() == "refs/heads/main"


def test_fetch_after_ref_only_update_downloads_nothing(repo, remote, tmp_path):
    remote.push("refs/heads/main:refs/heads/main")
    git("checkout", "-q", "-b", "wifi-live")
    tip = commit("feature.txt")
    remote.push("refs/heads/wifi-live:refs/heads/wifi-live")
    clone = tmp_path / "clone"
    remote.clone(clone)
    git("checkout", "-q", "main")
    git("merge", "-q", "--ff-only", "wifi-live")
    remote.push("refs/heads/main:refs/heads/main")
    downloads = len(remote.drive.downloads)

    refs = remote.fetch_all(clone)

    assert refs["refs/heads/main"] == tip
    assert git("rev-parse", "refs/heads/main", cwd=clone) == tip
    assert len(remote.drive.downloads) == downloads


def test_clone_after_deleting_a_ref_whose_bundle_others_build_on(repo, remote, tmp_path):
    remote.push("refs/heads/main:refs/heads/main")
    git("checkout", "-q", "-b", "wifi-live")
    commit("feature.txt")
    remote.push("refs/heads/wifi-live:refs/heads/wifi-live")
    git("checkout", "-q", "main")
    git("merge", "-q", "--ff-only", "wifi-live")
    remote.push("refs/heads/main:refs/heads/main")  # main's commit is in wifi-live's bundle
    remote.push(":refs/heads/wifi-live")
    commit("later.txt")
    remote.push("refs/heads/main:refs/heads/main")  # needs that commit as a prerequisite

    refs = remote.clone(tmp_path / "clone")

    assert refs == {"refs/heads/main": git("rev-parse", "main")}
    assert remote.bundle_ids() == ["0001", "0002", "0003"]
