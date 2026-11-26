from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("telegram")
from PIL import Image  # noqa: E402

from pixivfeed.channel.telegram import handlers  # noqa: E402
from pixivfeed.channel.telegram.handlers import (  # noqa: E402
    _extract_image_archive,
    _inspect_image_archive,
    _safe_download_failure,
    _sort_archive_members,
)
from pixivfeed.config import Config  # noqa: E402
from pixivfeed.provider.ehentai._archive import ArchiveError  # noqa: E402
from pixivfeed.publisher.telegraph import TelegraphPublisher  # noqa: E402


@pytest.fixture
def archive_limits():
    return SimpleNamespace(
        max_entries=20,
        max_uncompressed_size_gb=1,
        max_image_size_mb=5,
    )


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(("caption", "args", "expected", "force_r2"), [
    (False, ["魔女之旅", "{title}"], "魔女之旅 {title}", False),
    (True, ["--r2", "魔女之旅", "第二卷"], "魔女之旅 第二卷", True),
    (False, [], "伊蕾娜", False),
])
async def test_zip2tph_page_title(tmp_path, monkeypatch, caption, args, expected, force_r2):
    archive = tmp_path / "伊蕾娜.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        payload = _png_bytes()
        image = tarfile.TarInfo("1.png")
        image.size = len(payload)
        output.addfile(image, io.BytesIO(payload))
    document = SimpleNamespace(file_id="file", file_unique_id="id", file_size=archive.stat().st_size,
                               file_name=archive.name, mime_type="application/gzip")
    message = SimpleNamespace(document=document, caption=" ".join(["/zip2tph", *args]),
                              reply_to_message=SimpleNamespace(document=document), reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=1))
    config = Config()
    config.auth.admin_users = [1]
    config.storage.cache_dir = str(tmp_path)
    config.publish.telegraph_token = "test"
    publisher = TelegraphPublisher(config)
    create_page = AsyncMock(return_value={"url": "https://example.org/test"})
    monkeypatch.setattr(publisher, "_tg", SimpleNamespace(create_page=create_page))
    resolve = AsyncMock(return_value=(["https://example.org/1.png"], 0, 1, ""))
    monkeypatch.setattr(publisher, "_resolve_image_urls", resolve)
    bot = SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path=str(archive))))
    context = SimpleNamespace(args=args, bot=bot, bot_data=dict(config=config, publisher=publisher,
                              registry=None, telegraph_cache=None, allowlist=None))

    async def run_job(context, *, coro_factory, **kwargs):
        await coro_factory()

    monkeypatch.setattr(handlers, "_enqueue", run_job)
    monkeypatch.setattr(handlers, "is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers, "check_disk_free", lambda *args, **kwargs: (True, 0, 0))
    handler = handlers.handle_zip_document if caption else handlers.cmd_zip2tph
    await handler(update, context)
    assert create_page.await_args.kwargs["title"] == expected
    assert resolve.await_args.kwargs["force_r2"] is force_r2


@pytest.mark.parametrize("kind", ["zip", "tar.gz", "tar.bz2", "tar.xz"])
def test_extracts_zip_and_tar_family(tmp_path: Path, archive_limits, kind: str) -> None:
    payload = _png_bytes()
    archive = tmp_path / ("images." + kind)
    if kind == "zip":
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("nested/one.png", payload)
    else:
        mode = {"tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}[kind]
        with tarfile.open(archive, mode) as tf:
            info = tarfile.TarInfo("nested/one.png")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

    assert _inspect_image_archive(archive, archive_limits) == (1, len(payload))
    extracted = _extract_image_archive(archive, tmp_path / "out", archive_limits)
    assert len(extracted) == 1
    with Image.open(extracted[0]) as image:
        assert image.size == (2, 2)


def test_rejects_zip_slip_and_hides_local_path() -> None:
    assert "token" not in _safe_download_failure(PermissionError("/var/lib/bot/secret-token/file"))


def test_rejects_unsafe_member(tmp_path: Path, archive_limits) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escape.png", _png_bytes())
    with pytest.raises(ArchiveError):
        _inspect_image_archive(archive, archive_limits)


def test_natural_sort_handles_numbers_paths_and_case() -> None:
    members = [(name, 1) for name in (
        "chapter10/page2.jpg",
        "chapter2/page10.jpg",
        "chapter2/page1.jpg",
        "chapter10/page1.jpg",
        "chapter2/Page01.jpg",
    )]
    assert [name for name, _ in _sort_archive_members(members)] == [
        "chapter2/Page01.jpg",
        "chapter2/page1.jpg",
        "chapter2/page10.jpg",
        "chapter10/page1.jpg",
        "chapter10/page2.jpg",
    ]
