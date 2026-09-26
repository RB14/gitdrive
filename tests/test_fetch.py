"""Fetch/clone — manifests whose refs do not map one-to-one to bundles."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitdrive.store.manifest import BundleEntry, Manifest
from support import REPO_NAME, Remote, commit, git

MAIN = "refs/heads/main:refs/heads/main"


def _compact_like_the_old_gc(remote: Remote, tmp_path: Path) -> None:
    """Compact Drive's bundles the way gc did before bundle IDs stayed unique:
    into a single bundle renumbered 0001, reusing an ID clones had applied."""
    drive = remote.drive
    bundle = tmp_path / "compacted.bundle"
    git("bundle", "create", "-q", str(bundle), "--all")
    file_id = drive.upload_file(
        "0001.bundle", bundle.read_bytes(), drive.path_id(REPO_NAME, ".gitdrive", "bundles")
    )
    old_bundles = remote.manifest().bundles

    def compact(manifest: Manifest) -> None:
        manifest.bundles = [BundleEntry(id="0001", file_id=file_id)]

    remote.update_manifest(compact)
    for entry in old_bundles:
        drive.delete_file(entry.file_id)


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


# 1 push: the clone skips every bundle as applied and ends up missing objects.
# 3 pushes: a bundle it does apply lacks the prerequisites it skipped.
@pytest.mark.parametrize("pushes_after_gc", [1, 3])
def test_fetch_repairs_a_clone_after_an_old_gc_reused_bundle_ids(
    repo, remote, tmp_path, pushes_after_gc
):
    remote.push(MAIN)
    commit("a.txt")
    remote.push(MAIN)
    commit("b.txt")
    remote.push(MAIN)
    clone = tmp_path / "clone"
    remote.clone(clone)  # records 0001-0003 as applied
    _compact_like_the_old_gc(remote, tmp_path)
    for i in range(pushes_after_gc):
        commit(f"after-gc-{i}.txt")
        remote.push(MAIN)  # reissues 0002, 0003, ...

    refs = remote.fetch_all(clone)

    assert refs == {"refs/heads/main": git("rev-parse", "main")}
    # The record now matches Drive, so the next fetch takes just the new bundle.
    assert sorted(remote.applied_bundles(clone)) == remote.bundle_ids()
    tip = commit("later.txt")
    remote.push(MAIN)
    downloads = len(remote.drive.downloads)
    assert remote.fetch_all(clone) == {"refs/heads/main": tip}
    assert len(remote.drive.downloads) == downloads + 1
