"""DriveClient — revision tokens behind the manifest's optimistic locking."""

from __future__ import annotations

import json
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from gitdrive.drive.client import DriveClient


def _client(*responses: dict[str, Any]) -> tuple[DriveClient, HttpMockSequence]:
    """A DriveClient whose HTTP layer replays *responses* (no network)."""
    http = HttpMockSequence([({"status": "200"}, json.dumps(r)) for r in responses])
    client = DriveClient(credentials=None)
    client._local.service = build("drive", "v3", http=http, static_discovery=True)
    return client, http


def test_get_revision_prefers_the_head_revision():
    client, http = _client({"headRevisionId": "0Babc", "version": "7"})

    assert client.get_revision("f1") == "rev:0Babc"
    assert "fields=headRevisionId%2Cversion" in http.request_sequence[0][0]


def test_get_revision_falls_back_to_the_file_version():
    client, _ = _client({"version": "7"})

    assert client.get_revision("f1") == "version:7"


def test_upload_with_revision_returns_the_revision_it_created():
    client, http = _client({"id": "f1", "headRevisionId": "0Bnew", "version": "8"})

    assert client.upload_file_with_revision(
        "manifest.json", b"{}", "parent", existing_file_id="f1"
    ) == ("f1", "rev:0Bnew")
    assert "fields=id%2CheadRevisionId%2Cversion" in http.request_sequence[0][0]
