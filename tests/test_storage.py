from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

import app.storage as storage_module
from app.storage import ScoreStorage, StorageUnavailable


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(storage_module, "SUPABASE_URL", "https://proj.supabase.test")
    monkeypatch.setattr(storage_module, "SUPABASE_SECRET_KEY", "secret-key")
    monkeypatch.setattr(storage_module, "SUPABASE_SCORE_BUCKET", "scores")


@pytest.fixture
def requests(monkeypatch, configured):
    """Record outgoing requests and reply with the queued response body."""
    sent: list = []
    replies: list[bytes] = []

    def fake_urlopen(request, timeout):
        sent.append(request)
        return _Response(replies.pop(0) if replies else b"")

    monkeypatch.setattr(storage_module, "urlopen", fake_urlopen)
    return sent, replies


def test_unconfigured_storage_raises(monkeypatch):
    monkeypatch.setattr(storage_module, "SUPABASE_URL", "")
    with pytest.raises(StorageUnavailable, match="not configured"):
        ScoreStorage().download("key")


def test_signed_upload_url(requests):
    sent, replies = requests
    replies.append(json.dumps({"url": "/object/upload/sign/scores/a/b.xml?token=t"}).encode())
    result = ScoreStorage().create_signed_upload_url("user-scores/u 1/b.xml", "application/xml")
    assert result == {
        "uploadUrl": "https://proj.supabase.test/storage/v1/object/upload/sign/scores/a/b.xml?token=t",
        "uploadMethod": "PUT",
        "uploadHeaders": {"Content-Type": "application/xml"},
        "expiresInSeconds": 600,
    }
    request = sent[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://proj.supabase.test/storage/v1/object/upload/sign/scores/user-scores/u%201/b.xml"
    assert request.get_header("Apikey") == "secret-key"
    assert json.loads(request.data) == {"expiresIn": 600}


@pytest.mark.parametrize("field", ["signedURL", "signedUrl"])
def test_signed_download_url_accepts_alternate_fields(requests, field):
    _, replies = requests
    replies.append(json.dumps({field: "/object/sign/scores/k?token=t"}).encode())
    result = ScoreStorage().create_signed_download_url("k")
    assert result == {"sourceUrl": "https://proj.supabase.test/storage/v1/object/sign/scores/k?token=t", "expiresInSeconds": 600}


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"url": ""}'])
def test_signed_url_bad_response(requests, body):
    _, replies = requests
    replies.append(body)
    with pytest.raises(StorageUnavailable):
        ScoreStorage().create_signed_download_url("k")


def test_download_and_remove(requests):
    sent, replies = requests
    replies.append(b"file-bytes")
    assert ScoreStorage().download("a/b.txt") == b"file-bytes"
    ScoreStorage().remove("a/b.txt")
    assert [(r.get_method(), r.full_url) for r in sent] == [
        ("GET", "https://proj.supabase.test/storage/v1/object/scores/a/b.txt"),
        ("DELETE", "https://proj.supabase.test/storage/v1/object/scores/a/b.txt"),
    ]


def test_http_error_becomes_storage_unavailable(monkeypatch, configured):
    def fail(request, timeout):
        raise HTTPError(request.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(storage_module, "urlopen", fail)
    with pytest.raises(StorageUnavailable, match="request failed"):
        ScoreStorage().download("k")
