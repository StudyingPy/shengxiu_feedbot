from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import filters

from pixivfeed.channel.telegram import handlers
from pixivfeed.channel.telegram.handlers import (
    GuestReply,
    _guest_extract_refs,
    _guest_input_text,
    _make_guest_eh_keyboard,
    _make_guest_pixiv_keyboard,
    _make_guest_ref_picker,
)
from pixivfeed.provider import ParsedRef
from pixivfeed.provider.ehentai import EHMode


def _message(*, text=None, caption=None, reply_to_message=None, api_kwargs=None):
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=-1001, type="supergroup"),
        from_user=User(id=7, first_name="tester", is_bot=False),
        text=text,
        caption=caption,
        reply_to_message=reply_to_message,
        api_kwargs=api_kwargs,
    )


def test_guest_input_text_reads_only_explicit_summon_message():
    source = _message(text="https://www.pixiv.net/artworks/123")
    summon = _message(text="@feed_bot", reply_to_message=source)

    assert _guest_input_text(summon) == "@feed_bot"


def test_guest_input_text_uses_caption_when_no_text():
    source = _message(caption="https://e-hentai.org/g/1/token")
    summon = _message(caption="https://e-hentai.org/g/1/token", reply_to_message=source)

    assert _guest_input_text(summon) == "https://e-hentai.org/g/1/token"


def test_guest_reference_message_is_not_processed():
    summon = _message(text="@feed_bot", api_kwargs={"reference_messages": [{"message_id": 99}]})
    assert _guest_input_text(summon) == "@feed_bot"


def test_guest_user_reference_link_is_processed():
    source = _message(text="https://www.pixiv.net/artworks/123")
    summon = _message(text="@feed_bot", reply_to_message=source)
    ref = ParsedRef(provider="pixiv", kind="illust", id="123", raw=source.text)
    registry = SimpleNamespace(extract_all_refs=lambda text: [ref] if "pixiv" in text else [])

    assert _guest_extract_refs(summon, registry) == [ref]


def test_guest_bot_reference_link_is_ignored():
    summon = _message(
        text="@feed_bot",
        api_kwargs={
            "reference_messages": [{
                "message": "https://www.pixiv.net/artworks/123",
                "via_bot_id": 99,
            }],
        },
    )
    registry = SimpleNamespace(extract_all_refs=lambda text: [object()] if "pixiv" in text else [])

    assert _guest_extract_refs(summon, registry) == []


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
async def test_guest_reply_without_new_link_is_silent(monkeypatch):
    guest = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=-1001, type="supergroup"),
        from_user=User(id=7, first_name="tester", is_bot=False),
        text="这个图片好棒",
        guest_query_id="guest-q-noop",
    )
    update = Update(update_id=10, guest_message=guest)

    class FakeBot:
        def __init__(self):
            self.answers = []

        async def answer_guest_query(self, *args, **kwargs):
            self.answers.append((args, kwargs))

    bot = FakeBot()
    registry = SimpleNamespace(extract_all_refs=lambda text: [])
    monkeypatch.setattr(handlers, "_ctx", lambda context: (SimpleNamespace(), registry, None, None, None))
    monkeypatch.setattr(handlers, "is_authorized", lambda update, allowlist: _true())
    monkeypatch.setattr(handlers, "_track_user", lambda update, context: _done())

    context = SimpleNamespace(bot=bot, bot_data={"allowlist": object()})
    await handlers.handle_guest_message(update, context)

    assert bot.answers == []


def test_guest_keyboards_match_regular_layout_without_extra_emoji():
    eh = _make_guest_eh_keyboard("abc123")
    assert eh.inline_keyboard[0][0].callback_data == "g:abc123:page_sample"
    assert eh.inline_keyboard[0][0].text == EHMode.PAGE_SAMPLE.label_zh
    assert eh.inline_keyboard[1][0].callback_data == "g:abc123:archive_resample"
    assert eh.inline_keyboard[-1][0].callback_data == "g:abc123:cancel"

    single = SimpleNamespace(page_count=1)
    multi = SimpleNamespace(page_count=2)
    assert single.page_count == 1
    assert _make_guest_pixiv_keyboard("p1", single).inline_keyboard[0][0].callback_data == "g:p1:direct"
    assert _make_guest_pixiv_keyboard("p2", multi).inline_keyboard[0][0].callback_data == "g:p2:ph"


def test_guest_multiple_refs_use_picker_buttons():
    refs = [
        ParsedRef(provider="pixiv", kind="illust", id="123", raw="pixiv"),
        ParsedRef(provider="e-hentai.org", kind="gallery", id="456/token", raw="eh"),
    ]

    keyboard = _make_guest_ref_picker("select1", refs)

    assert keyboard.inline_keyboard[0][0].text == "1. Pixiv 123"
    assert keyboard.inline_keyboard[0][0].callback_data == "g:select1:pick:0"
    assert keyboard.inline_keyboard[1][0].text == "2. EH 456/token"
    assert keyboard.inline_keyboard[1][0].callback_data == "g:select1:pick:1"
    assert keyboard.inline_keyboard[-1][0].callback_data == "g:select1:cancel"


@pytest.mark.asyncio
async def test_guest_picker_callback_starts_selected_ref(monkeypatch):
    refs = [
        ParsedRef(provider="pixiv", kind="illust", id="123", raw="pixiv"),
        ParsedRef(provider="e-hentai.org", kind="gallery", id="456/token", raw="eh"),
    ]
    token = "select2"
    pending = handlers._GuestPending(
        ref=None, refs=refs, reply=SimpleNamespace(), update=SimpleNamespace(),
        user_id=7, created_at=0,
    )
    handlers._GUEST_PENDING[token] = pending
    selected = {}

    async def fake_start(update, context, ref, placeholder, user_id, *, token=None):
        selected.update(ref=ref, user_id=user_id, token=token)

    monkeypatch.setattr(handlers, "_start_guest_ref", fake_start)

    class FakeQuery:
        data = f"g:{token}:pick:1"
        from_user = User(id=7, first_name="tester", is_bot=False)
        message = None

        async def answer(self, *args, **kwargs):
            return None

    try:
        await handlers._handle_guest_callback(
            SimpleNamespace(callback_query=FakeQuery()),
            SimpleNamespace(),
        )
    finally:
        handlers._GUEST_PENDING.pop(token, None)

    assert selected == {"ref": refs[1], "user_id": 7, "token": token}


@pytest.mark.asyncio
async def test_guest_pixiv_direct_edits_inline_media_with_public_url(monkeypatch, tmp_path):
    source = _message(text="@feed_bot https://www.pixiv.net/artworks/123")

    class FakeBot:
        def __init__(self):
            self.media_edits = []

        async def edit_message_media(self, **kwargs):
            self.media_edits.append(kwargs)
            return True

    bot = FakeBot()
    reply = GuestReply(bot, "inline-1", source)
    image_path = tmp_path / "p0.jpg"
    image_path.write_bytes(b"image")
    work = SimpleNamespace(
        page_count=1,
        x_restrict=1,
        template_vars=lambda: {"pid": "123", "title": "demo"},
    )
    result = SimpleNamespace(
        work=work,
        images=[SimpleNamespace(tgphoto_path=image_path)],
        public_urls_tgphoto=["https://cdn.example/p0.jpg"],
        public_urls_original=[],
    )
    provider = SimpleNamespace(fetch_and_download_illust=lambda pid, on_progress=None: _result(result))
    config = SimpleNamespace(templates=SimpleNamespace(illust=SimpleNamespace(direct_caption="{title}")))
    registry = SimpleNamespace(find_by_name=lambda name: provider)
    context = SimpleNamespace(bot_data={"config": config, "registry": registry, "publisher": None, "telegraph_cache": None, "allowlist": None})

    class DummyProgress:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(handlers, "Progress", DummyProgress)
    monkeypatch.setattr(handlers, "_pixiv_provider", lambda registry: provider)
    await handlers._send_pixiv_guest_direct(Update(update_id=3, message=source), context, "123", reply, work=work)

    assert bot.media_edits[0]["inline_message_id"] == "inline-1"
    assert bot.media_edits[0]["media"].media == "https://cdn.example/p0.jpg"
    assert bot.media_edits[0]["media"].has_spoiler is True


@pytest.mark.asyncio
async def test_guest_cancel_delete_uses_optional_guest_delete_api(monkeypatch):
    calls = []

    class FakeBot:
        async def delete_guest_message(self, **kwargs):
            calls.append(kwargs)

    reply = SimpleNamespace(_bot=FakeBot(), inline_message_id="inline-cancel")
    async def no_sleep(_):
        return None

    monkeypatch.setattr(handlers.asyncio, "sleep", no_sleep)
    await handlers._delete_guest_reply_after_cancel(reply)
    assert calls == [{"inline_message_id": "inline-cancel"}]


@pytest.mark.asyncio
async def test_guest_cancel_falls_back_to_cancelled_text(monkeypatch):
    edits = []

    class FakeBot:
        async def edit_message_text(self, **kwargs):
            edits.append(kwargs)

    reply = GuestReply(FakeBot(), "inline-cancel", None)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(handlers.asyncio, "sleep", no_sleep)
    await handlers._delete_guest_reply_after_cancel(reply)

    assert edits == [{"inline_message_id": "inline-cancel", "text": "已取消", "reply_markup": None}]


async def _result(value):
    return value


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
