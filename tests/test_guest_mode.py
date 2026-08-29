from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import filters

from pixivfeed.channel.telegram import handlers
from pixivfeed.channel.telegram.handlers import GuestReply, _guest_input_text
from pixivfeed.provider import ParsedRef
from pixivfeed.provider.ehentai import EHMode


def _message(*, text=None, caption=None, reply_to_message=None):
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=-1001, type="supergroup"),
        from_user=User(id=7, first_name="tester", is_bot=False),
        text=text,
        caption=caption,
        reply_to_message=reply_to_message,
    )


def test_guest_input_text_reads_summon_and_replied_message():
    source = _message(text="https://www.pixiv.net/artworks/123")
    summon = _message(text="@feed_bot", reply_to_message=source)

    assert _guest_input_text(summon) == "@feed_bot\nhttps://www.pixiv.net/artworks/123"


def test_guest_input_text_deduplicates_same_caption():
    source = _message(caption="https://e-hentai.org/g/1/token")
    summon = _message(caption="https://e-hentai.org/g/1/token", reply_to_message=source)

    assert _guest_input_text(summon) == "https://e-hentai.org/g/1/token"


def test_guest_update_filter_matches_guest_message_only():
    message = _message(text="@feed_bot")
    guest_update = Update(update_id=1, guest_message=message)
    regular_update = Update(update_id=2, message=message)

    assert filters.UpdateType.GUEST_MESSAGE.check_update(guest_update)
    assert not filters.UpdateType.GUEST_MESSAGE.check_update(regular_update)


@pytest.mark.asyncio
async def test_guest_reply_answers_once_then_edits_inline_message():
    source = _message(text="@feed_bot https://www.pixiv.net/artworks/123")
    guest = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=source.chat,
        from_user=source.from_user,
        text=source.text,
        guest_query_id="guest-q-1",
    )
    update = Update(update_id=1, guest_message=guest)

    class FakeBot:
        def __init__(self):
            self.answers = []
            self.edits = []

        async def answer_guest_query(self, query_id, result):
            self.answers.append((query_id, result))
            return SimpleNamespace(inline_message_id="inline-1")

        async def edit_message_text(self, **kwargs):
            self.edits.append(kwargs)
            return True

    bot = FakeBot()
    context = SimpleNamespace(bot=bot)
    reply = await GuestReply.create(update, context, "⏳ processing")
    await reply.edit_text("done", disable_web_page_preview=False)

    assert len(bot.answers) == 1
    assert bot.answers[0][0] == "guest-q-1"
    assert bot.answers[0][1].input_message_content.message_text == "⏳ processing"
    assert bot.edits == [
        {
            "inline_message_id": "inline-1",
            "text": "done",
            "disable_web_page_preview": False,
        }
    ]


@pytest.mark.asyncio
async def test_guest_eh_uses_archive_resample_and_non_cancellable_queue(monkeypatch):
    source = _message(text="https://e-hentai.org/g/123/token @feed_bot")
    guest = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=source.chat,
        from_user=source.from_user,
        text=source.text,
        guest_query_id="guest-q-2",
    )
    update = Update(update_id=2, guest_message=guest)
    ref = ParsedRef(provider="e-hentai.org", kind="gallery", id="123/token", raw=source.text)
    registry = SimpleNamespace(extract_all_refs=lambda text: [ref])
    config = SimpleNamespace()
    placeholder = SimpleNamespace(message_id=0)
    queued = {}

    async def fake_create(update, context, text):
        return placeholder

    async def fake_gate(context, placeholder):
        return True

    async def fake_enqueue(context, **kwargs):
        queued.update(kwargs)
        return True

    called = {}

    async def fake_run(update, context, ref, *, mode, placeholder):
        called["mode"] = mode

    monkeypatch.setattr(handlers, "_ctx", lambda context: (config, registry, None, None, None))
    monkeypatch.setattr(handlers, "is_authorized", lambda update, allowlist: _true())
    monkeypatch.setattr(handlers, "_track_user", lambda update, context: _done())
    monkeypatch.setattr(handlers.GuestReply, "create", fake_create)
    monkeypatch.setattr(handlers, "_gate_disk_space", fake_gate)
    monkeypatch.setattr(handlers, "_enqueue", fake_enqueue)
    monkeypatch.setattr(handlers, "_eh_run_with_mode", fake_run)

    context = SimpleNamespace(bot_data={"allowlist": object()})
    await handlers.handle_guest_message(update, context)
    await queued["coro_factory"]()

    assert called["mode"] is EHMode.ARCHIVE_RES
    assert queued["category"] == "archive_zip"
    assert queued["cancellable"] is False


async def _true():
    return True


async def _done():
    return None
