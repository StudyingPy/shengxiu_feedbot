from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from pixivfeed.provider.ehentai._archive import download_archive_with_timeout


def test_shutdown_stops_job_queue_then_cancels_and_waits_background_tasks() -> None:
    from pixivfeed.lifecycle import (
        shutdown_bot_background_tasks,
        start_background_task,
    )

    async def run() -> None:
        events: list[str] = []
        task_started = asyncio.Event()

        class FakeJobQueue:
            async def stop_all(self) -> None:
                events.append("job-queue-stopped")

        async def background() -> None:
            task_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("background-stopped")

        app = SimpleNamespace(bot_data={"job_queue": FakeJobQueue()})
        task = start_background_task(app, background(), name="test-background")
        await task_started.wait()

        await shutdown_bot_background_tasks(app)

        assert task.cancelled()
        assert events == ["job-queue-stopped", "background-stopped"]
        assert app.bot_data["_pixivfeed_background_tasks"] == set()

    asyncio.run(run())


def test_completed_background_task_is_removed_and_exception_retrieved() -> None:
    from pixivfeed.lifecycle import start_background_task

    async def run() -> None:
        app = SimpleNamespace(bot_data={})

        async def completed() -> None:
            return None

        task = start_background_task(app, completed(), name="completed-background")
        await task
        # done callback 在下一轮 event-loop tick 执行。
        await asyncio.sleep(0)

        assert app.bot_data["_pixivfeed_background_tasks"] == set()

    asyncio.run(run())


def _range_response(
    request: httpx.Request,
    payload: bytes,
    *,
    status_code: int = 206,
    content_range: str | None = None,
) -> httpx.Response:
    range_header = request.headers["Range"]
    start_raw, end_raw = range_header.removeprefix("bytes=").split("-", 1)
    start, end = int(start_raw), int(end_raw)
    headers = {}
    if content_range is not None:
        headers["Content-Range"] = content_range
    elif status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{len(payload)}"
    return httpx.Response(
        status_code,
        headers=headers,
        content=payload[start : end + 1],
    )


@pytest.mark.parametrize("invalid_kind", ["status", "content-range", "length"])
def test_invalid_parallel_range_response_falls_back_to_single_stream(
    tmp_path,
    invalid_kind: str,
) -> None:
    payload = b"PK" + (bytes(range(256)) * 16)[:4094]
    assert len(payload) == 4096
    dest = tmp_path / "archive.zip"

    async def run() -> tuple[int, int]:
        probe_seen = False
        invalid_sent = False
        ranged_calls = 0
        single_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal probe_seen, invalid_sent, ranged_calls, single_calls
            range_header = request.headers.get("Range")
            if range_header and not probe_seen:
                probe_seen = True
                return httpx.Response(
                    206,
                    headers={"Content-Range": "bytes 0-2047/4096"},
                    content=payload[:2048],
                )

            if range_header:
                ranged_calls += 1
                start_raw, end_raw = range_header.removeprefix("bytes=").split("-", 1)
                start, end = int(start_raw), int(end_raw)
                if not invalid_sent:
                    invalid_sent = True
                    if invalid_kind == "status":
                        return httpx.Response(200, content=payload[start : end + 1])
                    if invalid_kind == "content-range":
                        return httpx.Response(
                            206,
                            headers={"Content-Range": f"bytes {start + 1}-{end}/4096"},
                            content=payload[start : end + 1],
                        )
                    return httpx.Response(
                        206,
                        headers={"Content-Range": f"bytes {start}-{end}/4096"},
                        content=payload[start:end],
                    )
                return httpx.Response(
                    206,
                    headers={"Content-Range": f"bytes {start}-{end}/4096"},
                    content=payload[start : end + 1],
                )

            single_calls += 1
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(payload))},
                content=payload,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await download_archive_with_timeout(
                client,
                "https://download.example/archive.zip",
                dest,
                10,
                parallel=2,
                min_parallel_size=1,
            )
        return ranged_calls, single_calls

    ranged_calls, single_calls = asyncio.run(run())

    assert ranged_calls >= 1
    assert single_calls == 1
    assert dest.read_bytes() == payload
    assert not dest.with_suffix(".zip.part").exists()


def test_valid_parallel_ranges_are_assembled_without_single_stream(tmp_path) -> None:
    payload = b"PK" + (bytes(range(256)) * 16)[:4094]
    dest = tmp_path / "archive.zip"

    async def run() -> int:
        probe_seen = False
        single_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal probe_seen, single_calls
            if request.headers.get("Range") and not probe_seen:
                probe_seen = True
                return httpx.Response(
                    206,
                    headers={"Content-Range": "bytes 0-2047/4096"},
                    content=payload[:2048],
                )
            if request.headers.get("Range"):
                return _range_response(request, payload)
            single_calls += 1
            return httpx.Response(200, content=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await download_archive_with_timeout(
                client,
                "https://download.example/archive.zip",
                dest,
                10,
                parallel=2,
                min_parallel_size=1,
            )
        return single_calls

    assert asyncio.run(run()) == 0
    assert dest.read_bytes() == payload
