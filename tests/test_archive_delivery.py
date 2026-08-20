from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from types import SimpleNamespace

from pixivfeed.channel.telegram import handlers


class _Message:
    message_id = 900

    def __init__(self) -> None:
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True

    async def edit_reply_markup(self, **kwargs) -> None:
        return None


class _Progress:
    def __init__(self) -> None:
        self._msg = _Message()
        self.statuses: list[str] = []
        self.finishes: list[str] = []
        self.markup = object()

    def set_markup(self, markup) -> None:
        self.markup = markup

    async def status(self, text: str) -> None:
        self.statuses.append(text)

    async def update(self, text: str) -> None:
        self.statuses.append(text)

    async def finish(self, text: str) -> None:
        self.finishes.append(text)


class _Bot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_document(self, **kwargs) -> None:
        kwargs["document"].read()
        self.calls.append(kwargs)


def _context(bot: _Bot):
    telegram = SimpleNamespace(base_url="", local_mode=False)
    return SimpleNamespace(
        bot=bot,
        bot_data={"config": SimpleNamespace(telegram=telegram)},
    )


def test_send_zip_replies_to_original_and_removes_progress(tmp_path: Path) -> None:
    zip_path = tmp_path / "gallery.zip"
    zip_path.write_bytes(b"PK\x03\x04payload")
    progress = _Progress()
    bot = _Bot()

    delivered = asyncio.run(handlers._send_zip_file(
        _context(bot),
        42,
        zip_path,
        progress,
        caption="caption",
        reply_to=123,
        cleanup_progress=True,
    ))

    assert delivered is True
    assert bot.calls[0]["reply_to_message_id"] == 123
    assert progress._msg.deleted is True
    assert progress.finishes == []


def test_send_zip_limit_failure_is_reported_to_caller(
    monkeypatch,
    tmp_path: Path,
) -> None:
    zip_path = tmp_path / "too-large.zip"
    zip_path.write_bytes(b"12345")
    progress = _Progress()
    bot = _Bot()
    monkeypatch.setattr(handlers, "TG_DOCUMENT_LIMIT", 4)

    delivered = asyncio.run(handlers._send_zip_file(
        _context(bot),
        42,
        zip_path,
        progress,
        caption="",
        reply_to=123,
    ))

    assert delivered is False
    assert bot.calls == []
    assert "超过 Bot 上传上限" in progress.finishes[-1]


def test_zip_build_runs_each_file_off_event_loop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "1.jpg"
    second = tmp_path / "2.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    progress = _Progress()
    offloaded: list[str] = []
    archived: dict[str, bytes] = {}
    real_to_thread = asyncio.to_thread

    async def tracked_to_thread(func, *args, **kwargs):
        offloaded.append(Path(args[0]).name)
        return await real_to_thread(func, *args, **kwargs)

    async def fake_send_zip_file(context, chat_id, zip_path, progress, **kwargs):
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                archived[name] = zf.read(name)
        return True

    monkeypatch.setattr(handlers.asyncio, "to_thread", tracked_to_thread)
    monkeypatch.setattr(handlers, "_send_zip_file", fake_send_zip_file)

    delivered = asyncio.run(handlers._zip_and_send_to_chat(
        SimpleNamespace(),
        42,
        progress,
        files=[first, second],
        stem="gallery",
        caption="",
        reply_to=123,
    ))

    assert delivered is True
    assert offloaded == ["1.jpg", "2.png"]
    assert archived == {"1.jpg": b"first", "2.png": b"second"}
