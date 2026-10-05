from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from pixivfeed.channel.telegram import handlers, retry
from pixivfeed.channel.telegram.progress import Progress
from pixivfeed.provider import ParsedRef
from pixivfeed.provider.ehentai import EHMode


@pytest.fixture
def sleep(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(retry.asyncio, "sleep", mock)
    return mock


@pytest.mark.asyncio
async def test_repeated_limits_honor_each_wait_and_keep_payload(sleep, monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    operation = AsyncMock(side_effect=[RetryAfter(30), RetryAfter(timedelta(seconds=42.5)), "sent"])
    markup = object()

    result = await retry.retry_on_rate_limit(operation, "final URL", reply_markup=markup)

    assert result == "sent"
    assert [call.args for call in sleep.await_args_list] == [(31.0,), (43.5,)]
    assert operation.await_count == 3
    for call in operation.await_args_list:
        assert call.args == ("final URL",)
        assert call.kwargs == {"reply_markup": markup}


@pytest.mark.asyncio
async def test_retry_exhaustion_is_bounded_and_propagates(sleep):
    error = RetryAfter(30)
    operation = AsyncMock(side_effect=error)

    with pytest.raises(RetryAfter) as caught:
        await retry.retry_on_rate_limit(operation, "final URL")

    assert caught.value is error
    assert operation.await_count == 11
    assert sleep.await_count == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [Forbidden("blocked"), BadRequest("invalid"), TimedOut()])
async def test_other_errors_are_not_retried(sleep, error):
    operation = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await retry.retry_on_rate_limit(operation, "final URL")
    assert operation.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_wait_stops_delivery(sleep):
    sleep.side_effect = asyncio.CancelledError
    operation = AsyncMock(side_effect=RetryAfter(30))
    with pytest.raises(asyncio.CancelledError):
        await retry.retry_on_rate_limit(operation, "final URL")
    assert operation.await_count == 1


def _ctx(monkeypatch, *, cached=None):
    config = SimpleNamespace(
        storage=SimpleNamespace(r2=SimpleNamespace(enabled=False)),
        templates=SimpleNamespace(gallery=SimpleNamespace(page_title="", page_header="", page_footer="")),
    )
    provider = SimpleNamespace()
    registry = SimpleNamespace(find_by_name=lambda name: provider)
    cache = SimpleNamespace(get=AsyncMock(return_value=cached), put=AsyncMock())
    publisher = SimpleNamespace(publish_gallery=AsyncMock())
    monkeypatch.setattr(handlers, "_ctx", lambda context: (config, registry, publisher, cache, None))
    monkeypatch.setattr(handlers, "_pixiv_provider", lambda registry: provider)
    monkeypatch.setattr(handlers, "_eh_provider", lambda registry, name: provider)
    monkeypatch.setattr(handlers, "_effective_force_r2", lambda context, force: False)
    return provider, publisher, cache


async def _run_sender(kind, update, context, placeholder):
    if kind == "eh":
        await handlers._eh_run_with_mode(
            update, context, ParsedRef("e-hentai.org", "gallery", "10/token", ""),
            mode=EHMode.PAGE_SAMPLE, placeholder=placeholder,
        )
    elif kind == "pixiv":
        await handlers._send_pixiv_illust_via_telegraph(update, context, "10", placeholder=placeholder)
    elif kind == "novel":
        await handlers._send_pixiv_novel(update, context, "10", placeholder=placeholder)
    else:
        await handlers._send_via_telegraph_generic(
            update, context, ParsedRef("nhentai", "gallery", "10", ""), placeholder=placeholder,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["eh", "pixiv", "novel", "generic"])
@pytest.mark.parametrize("delivery", ["edit", "reply", "guest"])
async def test_cached_link_paths_retry_without_losing_preview(sleep, monkeypatch, kind, delivery):
    url = "https://telegra.ph/cached-page"
    _, _, cache = _ctx(monkeypatch, cached=SimpleNamespace(url=url, durable=False))
    operation = AsyncMock(side_effect=[RetryAfter(30), True])
    message = SimpleNamespace(reply_text=operation, edit_text=operation)
    update = SimpleNamespace(message=message, effective_message=message)
    placeholder = None if delivery == "reply" else message
    if delivery == "guest":
        placeholder = handlers.GuestReply(
            SimpleNamespace(edit_message_text=operation), "inline-result-id", SimpleNamespace(chat=None),
        )

    await _run_sender(kind, update, SimpleNamespace(), placeholder)

    assert operation.await_count == 2
    sleep.assert_awaited_once_with(31.0)
    assert operation.await_args_list[0] == operation.await_args_list[1]
    options = operation.await_args.kwargs["link_preview_options"]
    assert options.url == url
    assert options.prefer_small_media is True
    if delivery == "guest":
        assert operation.await_args.kwargs["inline_message_id"] == "inline-result-id"
    cache.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_page_retries_only_delivery_and_logs_after_success(sleep, monkeypatch):
    provider, publisher, cache = _ctx(monkeypatch)
    provider.fetch_and_download = AsyncMock(return_value=SimpleNamespace(images=[]))
    url = "https://telegra.ph/new-page"
    publisher.publish_gallery.return_value = SimpleNamespace(
        primary_url=url, page_count=1, durable=False, r2_image_count=0,
        fallback_image_count=0, fallback_reason="",
    )
    monkeypatch.setattr(handlers, "_attach_progress_markup", lambda *args: None)
    monkeypatch.setattr(handlers, "_inject_eh_tags_block", lambda *args: None)
    monkeypatch.setattr(handlers, "_drop_cancel_button", AsyncMock())
    monkeypatch.setattr(handlers, "_r2_skipped_suffix", lambda *args, **kwargs: "")
    events = []

    async def edit(*args, **kwargs):
        events.append("edit")
        if len(events) <= 2:
            raise RetryAfter(30)

    async def log(*args, **kwargs):
        events.append("usage")

    monkeypatch.setattr(handlers, "_log_usage", log)
    await _run_sender("generic", SimpleNamespace(), SimpleNamespace(), SimpleNamespace(edit_text=edit))

    assert events == ["edit", "edit", "edit", "usage"]
    provider.fetch_and_download.assert_awaited_once()
    publisher.publish_gallery.assert_awaited_once()
    cache.put.assert_awaited_once()


@pytest.mark.asyncio
async def test_progress_finish_retries_and_propagates_exhaustion(sleep):
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=[RetryAfter(30), True]))
    progress = Progress(message)
    await progress.finish("done")
    assert progress._last_text == "done"
    sleep.assert_awaited_once_with(31.0)

    message.edit_text.side_effect = RetryAfter(30)
    with pytest.raises(RetryAfter):
        await progress.finish("another final result")
    assert progress._last_text == "done"


@pytest.mark.asyncio
async def test_progress_final_missing_message_fails_but_unchanged_is_success(sleep):
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=BadRequest("Message to edit not found")))
    progress = Progress(message)
    with pytest.raises(BadRequest):
        await progress.finish("done")
    message.edit_text.side_effect = BadRequest("Message is not modified")
    await progress.finish("done")
    assert progress._last_text == "done"
    sleep.assert_not_awaited()
