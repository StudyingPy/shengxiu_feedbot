from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import pixivfeed.provider.ehentai as eh_module
from pixivfeed.provider.ehentai import EHArchiveError, EHentaiProvider, EHGallery
from pixivfeed.provider.ehentai._archive import ArchiveError, parse_gp_cost
from pixivfeed.provider.ehentai._modes import EHMode


def _provider(tmp_path) -> EHentaiProvider:
    config = SimpleNamespace(
        collectors=SimpleNamespace(
            ehentai=SimpleNamespace(archive_timeout=300),
        )
    )
    return EHentaiProvider(tmp_path, "https://cache.example", config=config)


def _gallery() -> EHGallery:
    return EHGallery(
        host="e-hentai.org",
        gallery_id="123",
        token="token",
        title="gallery",
        page_count=1,
        image_page_urls=[],
    )


def test_archive_pipeline_returns_chooser_gp(monkeypatch, tmp_path) -> None:
    provider = _provider(tmp_path)
    image_path = tmp_path / "p0.jpg"

    async def fake_fetch_archiver_token(client, album_url):
        return "https://e-hentai.org/archiver.php"

    async def fake_request_archive(client, host, gid, token, archiver_token, mode):
        return "https://download.example/archive.zip", 1024, 37

    async def fake_download(client, url, path, timeout, **kwargs):
        return None

    monkeypatch.setattr(eh_module, "fetch_archiver_token", fake_fetch_archiver_token)
    monkeypatch.setattr(eh_module, "request_archive", fake_request_archive)
    monkeypatch.setattr(eh_module, "download_archive_with_timeout", fake_download)
    monkeypatch.setattr(
        eh_module,
        "extract_archive",
        lambda zip_path, work_dir: SimpleNamespace(image_paths=[image_path]),
    )

    paths, gp_cost = asyncio.run(
        provider._archive_pipeline(
            object(),
            _gallery(),
            EHMode.ARCHIVE_ORG,
            tmp_path,
        )
    )

    assert paths == [image_path]
    assert gp_cost == 37


@pytest.mark.parametrize("failure_type", [ArchiveError, RuntimeError])
def test_archive_pipeline_keeps_gp_when_download_fails(
    monkeypatch, tmp_path, failure_type
) -> None:
    provider = _provider(tmp_path)

    async def fake_fetch_archiver_token(client, album_url):
        return "https://e-hentai.org/archiver.php"

    async def fake_request_archive(client, host, gid, token, archiver_token, mode):
        return "https://download.example/archive.zip", 1024, 41

    async def fake_download(client, url, path, timeout, **kwargs):
        raise failure_type("download failed")

    monkeypatch.setattr(eh_module, "fetch_archiver_token", fake_fetch_archiver_token)
    monkeypatch.setattr(eh_module, "request_archive", fake_request_archive)
    monkeypatch.setattr(eh_module, "download_archive_with_timeout", fake_download)

    with pytest.raises(EHArchiveError) as exc_info:
        asyncio.run(
            provider._archive_pipeline(
                object(),
                _gallery(),
                EHMode.ARCHIVE_ORG,
                tmp_path,
            )
        )

    assert exc_info.value.gp_cost == 41


def test_fetch_and_download_exposes_archive_gp(monkeypatch, tmp_path) -> None:
    provider = _provider(tmp_path)
    image_path = tmp_path / "eh_123_token_archive_original" / "p0.jpg"

    async def fake_fetch_gallery_meta(gid, token):
        return _gallery()

    async def fake_archive_pipeline(client, gallery, mode, work_dir, **kwargs):
        return [image_path], 47

    class ClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(provider, "_fetch_gallery_meta", fake_fetch_gallery_meta)
    monkeypatch.setattr(provider, "_make_client", lambda mode: ClientContext())
    monkeypatch.setattr(provider, "_archive_pipeline", fake_archive_pipeline)

    ref = SimpleNamespace(id="123/token")
    work = asyncio.run(
        provider.fetch_and_download_with_mode(ref, EHMode.ARCHIVE_ORG)
    )

    assert work.extra_vars["archive_gp_cost"] == 47


def test_parse_gp_cost_keeps_free_archive_at_zero() -> None:
    chooser_html = """
        <form><input name="dltype" value="org">Download Cost: <strong>53 GP</strong></form>
        <form><input name="dltype" value="res">Download Cost: <strong>Free!</strong></form>
    """

    assert parse_gp_cost(chooser_html, EHMode.ARCHIVE_ORG) == 53
    assert parse_gp_cost(chooser_html, EHMode.ARCHIVE_RES) == 0
