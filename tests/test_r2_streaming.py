from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest

from pixivfeed.storage import r2


class _CallbackTransport(httpx.AsyncBaseTransport):
    """Unlike MockTransport, leave the request stream unbuffered for inspection."""

    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._handler(request)


def _r2_client(transport: httpx.AsyncBaseTransport) -> r2.R2Client:
    client = r2.R2Client(
        endpoint="https://example.invalid",
        region="auto",
        bucket="test-bucket",
        access_key_id="access",
        secret_access_key="secret",
        custom_domain="https://cdn.example.invalid",
        prefix="feedbot",
    )
    client._client = httpx.AsyncClient(transport=transport)
    return client


def test_put_file_streams_with_strict_hash_and_length(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"a" * (r2._UPLOAD_STREAM_CHUNK_BYTES * 2 + 37)
    local_path = tmp_path / "large.jpg"
    local_path.write_bytes(payload)
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        chunks = [chunk async for chunk in request.stream]
        captured["headers"] = dict(request.headers)
        captured["chunks"] = chunks
        captured["body"] = b"".join(chunks)
        return httpx.Response(200)

    def reject_read_bytes(self: Path) -> bytes:
        raise AssertionError(f"put_file must not buffer {self} with read_bytes()")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)
    client = _r2_client(_CallbackTransport(handler))

    async def run() -> bool:
        try:
            return await client.put_file("gallery/large.jpg", local_path)
        finally:
            await client.aclose()

    assert asyncio.run(run()) is True
    headers = captured["headers"]
    chunks = captured["chunks"]
    assert isinstance(headers, dict)
    assert isinstance(chunks, list)
    assert captured["body"] == payload
    assert headers["content-length"] == str(len(payload))
    assert headers["x-amz-content-sha256"] == hashlib.sha256(payload).hexdigest()
    assert "content-length" in headers["authorization"]
    assert "transfer-encoding" not in headers
    assert len(chunks) == 3
    assert max(map(len, chunks)) <= r2._UPLOAD_STREAM_CHUNK_BYTES


def test_put_file_rejects_length_change_between_hash_and_upload(tmp_path: Path) -> None:
    local_path = tmp_path / "changing.png"
    local_path.write_bytes(b"original payload")

    async def handler(request: httpx.Request) -> httpx.Response:
        local_path.write_bytes(b"short")
        async for _ in request.stream:
            pass
        return httpx.Response(200)

    client = _r2_client(_CallbackTransport(handler))

    async def run() -> bool:
        try:
            return await client.put_file("gallery/changing.png", local_path)
        finally:
            await client.aclose()

    assert asyncio.run(run()) is False
    # The stream must close its file even when length validation raises.
    moved_path = tmp_path / "closed.png"
    local_path.replace(moved_path)
    assert moved_path.read_bytes() == b"short"


def test_put_file_rejects_empty_file_without_sending_request(tmp_path: Path) -> None:
    local_path = tmp_path / "empty.jpg"
    local_path.touch()
    request_seen = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_seen
        request_seen = True
        return httpx.Response(200)

    client = _r2_client(_CallbackTransport(handler))

    async def run() -> bool:
        try:
            return await client.put_file("gallery/empty.jpg", local_path)
        finally:
            await client.aclose()

    assert asyncio.run(run()) is False
    assert request_seen is False


def test_existing_byte_payload_signing_path_stays_compatible() -> None:
    cred = r2._R2Cred(
        endpoint="https://example.invalid",
        region="auto",
        access_key="access",
        secret_key="secret",
        bucket="test-bucket",
    )

    for method in ("PUT", "HEAD", "GET", "DELETE"):
        _, headers = r2._sign_request(
            cred,
            method=method,
            key="object.jpg",
            payload=b"",
        )
        assert headers["x-amz-content-sha256"] == hashlib.sha256(b"").hexdigest()
        assert "content-length" not in headers["authorization"]


def test_effective_upload_concurrency_tracks_largest_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(r2, "_UPLOAD_CONCURRENT_SOURCE_BUDGET_BYTES", 100)

    assert r2._effective_upload_concurrency([10, 10], requested=8) == 8
    assert r2._effective_upload_concurrency([10, 30], requested=8) == 3
    assert r2._effective_upload_concurrency([101, 1], requested=8) == 1
    assert r2._effective_upload_concurrency([], requested=4) == 4
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        r2._effective_upload_concurrency([1], requested=0)


def test_upload_batch_applies_size_limited_concurrency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(r2, "_UPLOAD_CONCURRENT_SOURCE_BUDGET_BYTES", 10)
    items: list[tuple[str, Path]] = []
    for index in range(4):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(b"123456")
        items.append((f"{index}.jpg", path))

    class FakeClient:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def put_file(self, key: str, path: Path) -> bool:
            assert key and path.exists()
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return True

    client = FakeClient()
    results = asyncio.run(
        r2.upload_files_concurrent(client, items, concurrency=4)  # type: ignore[arg-type]
    )

    assert all(results.values())
    assert client.max_active == 1
