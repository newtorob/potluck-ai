"""Download recovery through HTTPX's real streaming interface, using tiny files."""

import asyncio
import hashlib
import json

import httpx
import pytest

from potluck_local.services import model_downloader as downloads

URL = "https://models.example.test/model.gguf"
DATA = b"abcdef"
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    downloader = downloads.ModelDownloader(tmp_path)
    folder = tmp_path / "test-model"
    folder.mkdir()
    partial = folder / "model.gguf.partial"
    meta = folder / "model.gguf.partial.meta"
    meta.write_text(json.dumps({"url": URL, "sha256": SHA}))
    client_class = httpx.AsyncClient

    def transport(handler):
        monkeypatch.setattr(downloads.httpx, "AsyncClient", lambda **kwargs: client_class(
            transport=httpx.MockTransport(handler), **kwargs,
        ))

    monkeypatch.setattr(downloads, "_CHUNK_SIZE", 3)
    return downloader, partial, transport


def run(downloader, sha=SHA, size=len(DATA)):
    return asyncio.run(downloader.download_model("test-model", URL, sha, size))


def test_ignored_range_restarts_instead_of_appending(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])

    def respond(request):
        assert request.headers["range"] == "bytes=3-"
        return httpx.Response(200, content=DATA)

    transport(respond)
    result = run(downloader)
    assert result.read_bytes() == DATA
    assert not partial.exists()
    assert result.with_suffix(".gguf.sha256").read_text() == SHA
    progress = downloader.get_progress("test-model")
    assert (progress.status, progress.bytes_downloaded, progress.bytes_total) == ("complete", 6, 6)


def test_wrong_content_range_leaves_existing_partial_untouched(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])
    transport(lambda request: httpx.Response(206, content=DATA[3:], headers={
        "Content-Range": "bytes 2-4/6",
    }))
    with pytest.raises(ValueError, match="Content-Range"):
        run(downloader)
    assert partial.read_bytes() == DATA[:3]
    assert downloader.get_progress("test-model").status == "error"
    assert not partial.with_suffix("").exists()


def test_complete_partial_416_is_hash_verified_and_promoted(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA)
    transport(lambda request: httpx.Response(416, headers={"Content-Range": "bytes */6"}))
    result = run(downloader)
    assert result.read_bytes() == DATA
    assert result.with_suffix(".gguf.sha256").read_text() == SHA
    assert downloader.get_progress("test-model").status == "complete"


class Stream(httpx.AsyncByteStream):
    def __init__(self, *events):
        self.events = events

    async def __aiter__(self):
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            if callable(event):
                event()
            else:
                yield event


def test_valid_resume_appends_only_missing_bytes(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])

    def respond(request):
        assert request.headers["range"] == "bytes=3-"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(206, content=DATA[3:], headers={"Content-Range": "bytes 3-5/6"})

    transport(respond)
    assert run(downloader).read_bytes() == DATA
    assert not partial.with_suffix(".partial.meta").exists()


@pytest.mark.parametrize("headers", [
    {}, {"Content-Range": "items 3-5/6"}, {"Content-Range": "bytes 0-2/6"},
    {"Content-Range": "bytes 3-2/6"}, {"Content-Range": "bytes 3-6/6"},
    {"Content-Range": "bytes 3-5/5"},
    {"Content-Range": "bytes 3-5/6", "Content-Length": "2"},
    {"Content-Range": "bytes 3-5/6", "Content-Length": "-1"},
])
def test_invalid_resume_headers_do_not_modify_partial(setup, headers):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])
    transport(lambda request: httpx.Response(206, stream=Stream(DATA[3:]), headers=headers))
    with pytest.raises(ValueError):
        run(downloader)
    assert partial.read_bytes() == DATA[:3]
    assert not partial.with_suffix("").exists()


@pytest.mark.parametrize("hash_present", [True, False])
def test_truncated_body_keeps_received_bytes_and_retry_resumes(setup, hash_present):
    downloader, partial, transport = setup
    sha = SHA if hash_present else ""
    partial.write_bytes(b"")
    transport(lambda request: httpx.Response(200, headers={"Content-Length": "6"}, stream=Stream(DATA[:3])))
    with pytest.raises(ValueError, match="ended before"):
        run(downloader, sha=sha)
    assert partial.read_bytes() == DATA[:3]
    assert not partial.with_suffix("").exists()
    transport(lambda request: httpx.Response(206, content=DATA[3:], headers={"Content-Range": "bytes 3-5/6"}))
    assert run(downloader, sha=sha).read_bytes() == DATA


def test_connection_failure_keeps_partial_for_retry(setup):
    downloader, partial, transport = setup
    transport(lambda request: httpx.Response(200, headers={"Content-Length": "6"}, stream=Stream(
        DATA[:3], httpx.ReadError("connection dropped"),
    )))
    with pytest.raises(httpx.ReadError):
        run(downloader)
    assert partial.read_bytes() == DATA[:3]
    progress = downloader.get_progress("test-model")
    assert (progress.status, progress.bytes_downloaded) == ("error", 3)
    transport(lambda request: httpx.Response(206, content=DATA[3:], headers={"Content-Range": "bytes 3-5/6"}))
    assert run(downloader).read_bytes() == DATA


def test_cancel_keeps_bytes_and_allows_retry(setup):
    downloader, partial, transport = setup
    transport(lambda request: httpx.Response(200, stream=Stream(
        DATA[:3], lambda: downloader.cancel_download("test-model"), DATA[3:],
    )))
    assert run(downloader) == partial
    assert partial.read_bytes() == DATA[:3]
    assert downloader.get_progress("test-model").status == "cancelled"
    transport(lambda request: httpx.Response(206, content=DATA[3:], headers={"Content-Range": "bytes 3-5/6"}))
    assert run(downloader).read_bytes() == DATA


def test_cancel_while_connecting_does_not_truncate_existing_partial(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])

    def respond(request):
        downloader.cancel_download("test-model")
        return httpx.Response(200, content=DATA)

    transport(respond)
    assert run(downloader) == partial
    assert partial.read_bytes() == DATA[:3]
    assert downloader.get_progress("test-model").bytes_downloaded == 3


@pytest.mark.parametrize("content_range", ["bytes */6", "bytes */3", "invalid", ""])
def test_unsatisfied_range_retries_full_download_once(setup, content_range):
    downloader, partial, transport = setup
    partial.write_bytes(b"old-too-long-partial")
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(416, headers={"Content-Range": content_range})
        assert "range" not in request.headers
        return httpx.Response(200, content=DATA)

    transport(respond)
    assert run(downloader).read_bytes() == DATA
    assert len(requests) == 2


def test_416_retry_failure_is_bounded_and_preserves_partial(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(416, headers={"Content-Range": "bytes */6"})

    transport(respond)
    with pytest.raises(httpx.HTTPStatusError):
        run(downloader)
    assert len(requests) == 2
    assert partial.read_bytes() == DATA[:3]


def test_416_size_alone_does_not_bypass_hash_verification(setup):
    downloader, partial, transport = setup
    partial.write_bytes(b"broken")
    transport(lambda request: httpx.Response(416, headers={"Content-Range": "bytes */6"}))
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        run(downloader)
    assert not partial.with_suffix("").exists()
    assert downloader.get_progress("test-model").status == "error"


def test_416_without_hash_restarts_instead_of_trusting_size(setup):
    downloader, partial, transport = setup
    partial.write_bytes(b"broken")
    partial.with_suffix(".partial.meta").write_text(json.dumps({"url": URL, "sha256": ""}))
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(416, headers={"Content-Range": "bytes */6"})
        assert "range" not in request.headers
        return httpx.Response(200, content=DATA)

    transport(respond)
    assert run(downloader, sha="").read_bytes() == DATA
    assert len(requests) == 2


def test_short_valid_range_is_kept_until_remaining_range_arrives(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])
    transport(lambda request: httpx.Response(206, content=DATA[3:5], headers={"Content-Range": "bytes 3-4/6"}))
    with pytest.raises(ValueError, match="incomplete"):
        run(downloader)
    assert partial.read_bytes() == DATA[:5]
    transport(lambda request: httpx.Response(206, content=DATA[5:], headers={"Content-Range": "bytes 5-5/6"}))
    assert run(downloader).read_bytes() == DATA


def test_unknown_total_can_complete_with_valid_hash(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])
    transport(lambda request: httpx.Response(206, content=DATA[3:], headers={"Content-Range": "bytes 3-5/*"}))
    assert run(downloader).read_bytes() == DATA


def test_unknown_total_with_incomplete_body_is_not_discarded(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])
    transport(lambda request: httpx.Response(206, content=DATA[3:5], headers={"Content-Range": "bytes 3-4/*"}))
    with pytest.raises(ValueError):
        run(downloader)
    assert partial.read_bytes() == DATA[:5]


def test_declared_body_overrun_is_not_written(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])
    transport(lambda request: httpx.Response(206, stream=Stream(b"defX"), headers={"Content-Range": "bytes 3-5/6"}))
    with pytest.raises(ValueError, match="exceeds"):
        run(downloader)
    assert partial.read_bytes() == DATA
    assert not partial.with_suffix("").exists()


def test_compressed_resume_is_rejected_without_changing_partial(setup):
    downloader, partial, transport = setup
    partial.write_bytes(DATA[:3])
    transport(lambda request: httpx.Response(206, stream=Stream(b"compressed"), headers={
        "Content-Encoding": "gzip", "Content-Range": "bytes 3-5/6",
    }))
    with pytest.raises(ValueError, match="Compressed"):
        run(downloader)
    assert partial.read_bytes() == DATA[:3]


def test_stale_partial_is_not_appended_after_url_changes(setup):
    downloader, partial, transport = setup
    partial.write_bytes(b"old")
    partial.with_suffix(".partial.meta").write_text(json.dumps({"url": URL+"?old", "sha256": SHA}))

    def respond(request):
        assert "range" not in request.headers
        return httpx.Response(200, content=DATA)

    transport(respond)
    assert run(downloader).read_bytes() == DATA
