from __future__ import annotations

import httpx
import pytest

from pixivfeed.provider.ehentai import EHError, EHGalleryUnavailable, ExHentaiProvider


@pytest.fixture
def fetch_ex(tmp_path, monkeypatch):
    provider = ExHentaiProvider(tmp_path, "", config=None)
    ref, = provider.extract_refs("https://exhentai.org/g/123/abc/")

    async def fetch(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(provider, "_make_client", lambda: httpx.AsyncClient(
            transport=transport, follow_redirects=True,
        ))
        return await provider.fetch_work(ref)

    return fetch


@pytest.mark.asyncio
async def test_gallery_thumbnail_hrefs(fetch_ex):
    html = """<h1 id="gn">Example</h1><div id="gdt">
        <a class='thumb' href='/s/abc/123-1'></a>
        <a href="https://exhentai.org/s/abc/123-1"></a>
        <a href="https://elsewhere.example/s/abc/123-2"></a>
        <a href="/s/abc/456-2"></a>
        <a href="https://exhentai.org/s/def/123-2" title="2"></a></div>"""
    gallery = await fetch_ex(lambda request: httpx.Response(200, text=html))
    assert gallery.title == "Example"
    assert gallery.image_page_urls == [
        "https://exhentai.org/s/abc/123-1", "https://exhentai.org/s/def/123-2",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "reason"), [
    (httpx.Response(200, text=""), "空响应"),
    (httpx.Response(200, content=b"GIF89a", headers={"Content-Type": "image/gif"}), "图片"),
    (httpx.Response(200, text='<h1 id="gn">Example</h1>'), "已收到画廊详情页"),
    (httpx.Response(200, text="Unknown page"), "缺少画廊标题"),
])
async def test_ex_unrecognized_response_has_specific_diagnosis(fetch_ex, response, reason):
    with pytest.raises(EHError, match=reason) as error:
        await fetch_ex(lambda request: response)
    assert not isinstance(error.value, EHGalleryUnavailable)


@pytest.mark.asyncio
async def test_forum_redirect_reports_login_context(fetch_ex):
    def handler(request):
        if request.url.host == "exhentai.org":
            return httpx.Response(302, headers={"Location": "https://forums.e-hentai.org/remoteapi.php"})
        return httpx.Response(200, text="Sign in")

    with pytest.raises(EHGalleryUnavailable, match="论坛"):
        await fetch_ex(handler)
