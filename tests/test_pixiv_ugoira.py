"""ugoira 解析、有界转换、缓存与 Telegram 降级测试。

不访问真实 Pixiv、不依赖系统 ffmpeg：ffmpeg 调用用 fake subprocess.run
代替，HTTP 边界用 FakePixivAPI 代替。
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pixivfeed.channel.telegram import handlers
from pixivfeed.provider.pixiv import ugoira as ug
from pixivfeed.provider.pixiv.model import IllustWork
from pixivfeed.provider.pixiv.ugoira import (
    UgoiraError,
    UgoiraFFmpegMissingError,
    UgoiraValidationError,
    convert_ugoira,
    ffmpeg_available,
    parse_ugoira_meta,
)


def _make_zip(tmp_path: Path, members: dict[str, bytes]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "frames.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _body(frames: list[dict], src: str = "https://i.pximg.net/x/ugoira.zip") -> dict:
    return {"src": src, "originalSrc": src, "frames": frames}


def _install_fake_ffmpeg(
    monkeypatch, *, fail: bool = False, timeout_kill: bool = False,
) -> list[list[str]]:
    """伪 ffmpeg：写一个最小 GIF 头文件，记录命令与 timeout。"""
    calls: list[list[str]] = []

    def fake_run(cmd, check=False, timeout=None, **kwargs):  # noqa: ANN001
        calls.append(list(cmd))
        assert timeout == ug.FFMPEG_TIMEOUT_SECONDS
        if timeout_kill:
            raise subprocess.TimeoutExpired(cmd, timeout)
        if fail:
            raise subprocess.CalledProcessError(1, cmd)
        Path(cmd[-1]).write_bytes(b"GIF89a" + b"\x00" * 32)
        return SimpleNamespace(returncode=0)

    import pixivfeed.provider.pixiv as pkg

    monkeypatch.setattr(ug, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(pkg, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(ug.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# metadata：解析、顺序、逐帧 delay、非法输入
# ---------------------------------------------------------------------------


def test_parse_ugoira_meta_keeps_order_and_variable_delays() -> None:
    meta = parse_ugoira_meta(_body([
        {"file": "000001.jpg", "delay": 70},
        {"file": "000000.jpg", "delay": 230},
        {"file": "000002.png", "delay": 15},
    ]))
    assert [f.file for f in meta.frames] == ["000001.jpg", "000000.jpg", "000002.png"]
    assert [f.delay_ms for f in meta.frames] == [70, 230, 15]
    assert meta.zip_url.endswith("ugoira.zip")


@pytest.mark.parametrize(
    "body",
    [
        _body([]),
        {"frames": [{"file": "000000.jpg", "delay": 100}]},          # 缺 zip url
        _body([{"file": "000000.jpg", "delay": 0}]),
        _body([{"file": "000000.jpg", "delay": "100"}]),
        _body([{"file": "000000.jpg"}]),
        _body([{"file": "000000.jpg", "delay": True}]),
        _body([{"file": "../evil.png", "delay": 100}]),
        _body([{"file": "a/b.png", "delay": 100}]),
        _body([{"file": "000000.gif", "delay": 100}]),
    ],
)
def test_parse_ugoira_meta_rejects_invalid(body: dict) -> None:
    with pytest.raises(UgoiraValidationError):
        parse_ugoira_meta(body)


def test_parse_ugoira_meta_rejects_frame_count_and_duration_limits(monkeypatch) -> None:
    monkeypatch.setattr(ug, "MAX_FRAMES", 2)
    with pytest.raises(UgoiraValidationError):
        parse_ugoira_meta(_body([{"file": f"{i:06d}.jpg", "delay": 100} for i in range(3)]))
    # 11 帧 × 60s = 660s > 600s
    with pytest.raises(UgoiraValidationError):
        parse_ugoira_meta(_body([{"file": f"{i:06d}.jpg", "delay": 60_000} for i in range(11)]))


# ---------------------------------------------------------------------------
# ZIP / 转换：顺序、时序、安全边界、原子发布
# ---------------------------------------------------------------------------


def test_convert_concat_manifest_uses_metadata_order_and_per_frame_delays(
    monkeypatch, tmp_path: Path,
) -> None:
    # ZIP 内字典序与 metadata 顺序故意不同
    zip_path = _make_zip(tmp_path, {"000000.jpg": b"x", "000001.jpg": b"yy"})
    meta = parse_ugoira_meta(_body([
        {"file": "000001.jpg", "delay": 70},
        {"file": "000000.jpg", "delay": 230},
    ]))
    manifests: list[str] = []

    def spy_run(cmd, check=False, timeout=None, **kwargs):  # noqa: ANN001
        manifests.append(Path(cmd[cmd.index("-i") + 1]).read_text(encoding="utf-8"))
        Path(cmd[-1]).write_bytes(b"GIF89a" + b"\x00" * 16)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ug, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(ug.subprocess, "run", spy_run)
    output = tmp_path / "animation.gif"
    convert_ugoira(zip_path, meta, output)

    manifest = manifests[0]
    assert manifest.index("frame000000") < manifest.index("frame000001")
    # 第一帧对应 metadata 的 000001.jpg（70ms → 0.07s）
    first = manifest.split("file frame000000.jpg")[1]
    assert "duration 0.07" in first
    assert "duration 0.23" in manifest
    assert output.read_bytes().startswith(b"GIF89a")
    assert not list(tmp_path.glob(".ugoira-*"))


def test_convert_fails_when_metadata_frame_missing_from_zip(monkeypatch, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path, {"000000.jpg": b"x"})
    meta = parse_ugoira_meta(_body([
        {"file": "000000.jpg", "delay": 100},
        {"file": "999999.jpg", "delay": 100},
    ]))
    output = tmp_path / "animation.gif"
    calls = _install_fake_ffmpeg(monkeypatch)

    with pytest.raises(UgoiraValidationError, match="missing from zip"):
        convert_ugoira(zip_path, meta, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".ugoira-*"))
    assert calls == []


def test_convert_only_reads_frames_listed_in_metadata(monkeypatch, tmp_path: Path) -> None:
    # 不使用 extractall：ZIP 里多余的恶意 entry 不会落盘，只拷贝 metadata 列出的帧
    zf_bytes = io.BytesIO()
    with zipfile.ZipFile(zf_bytes, "w") as zf:
        zf.writestr("../escape.png", b"evil")
        zf.writestr("000000.jpg", b"ok")
    zip_path = tmp_path / "frames.zip"
    zip_path.write_bytes(zf_bytes.getvalue())
    meta = parse_ugoira_meta(_body([{"file": "000000.jpg", "delay": 100}]))
    extracted: list[str] = []

    def spy_run(cmd, check=False, timeout=None, **kwargs):  # noqa: ANN001
        work = Path(cmd[cmd.index("-i") + 1]).parent
        extracted[:] = sorted(p.name for p in work.iterdir())
        Path(cmd[-1]).write_bytes(b"GIF89a" + b"\x00" * 8)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ug, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(ug.subprocess, "run", spy_run)
    convert_ugoira(zip_path, meta, tmp_path / "animation.gif")
    assert not any("escape" in n for n in extracted)
    assert not (tmp_path.parent / "escape.png").exists()


def test_convert_rejects_oversized_frame_before_extraction(monkeypatch, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path, {"000000.jpg": b"x"})
    meta = parse_ugoira_meta(_body([{"file": "000000.jpg", "delay": 100}]))
    calls = _install_fake_ffmpeg(monkeypatch)
    real_getinfo = zipfile.ZipFile.getinfo

    def fake_getinfo(self, name):  # noqa: ANN001
        info = real_getinfo(self, name)
        if name == "000000.jpg":
            info.file_size = ug.MAX_FRAME_BYTES + 1
        return info

    monkeypatch.setattr(zipfile.ZipFile, "getinfo", fake_getinfo)
    with pytest.raises(UgoiraValidationError, match="oversized"):
        convert_ugoira(zip_path, meta, tmp_path / "animation.gif")
    assert calls == []  # 超限后绝不启动 ffmpeg


def test_convert_rejects_oversized_total_expansion(monkeypatch, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path, {f"{i:06d}.jpg": b"x" for i in range(3)})
    meta = parse_ugoira_meta(_body([{"file": f"{i:06d}.jpg", "delay": 100} for i in range(3)]))
    monkeypatch.setattr(ug, "MAX_TOTAL_FRAME_BYTES", 2)
    _install_fake_ffmpeg(monkeypatch)
    with pytest.raises(UgoiraValidationError, match="exceed"):
        convert_ugoira(zip_path, meta, tmp_path / "animation.gif")


def test_convert_bad_zip_raises_validation_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ug, "ffmpeg_available", lambda: True)
    zip_path = tmp_path / "frames.zip"
    zip_path.write_bytes(b"not a zip")
    meta = parse_ugoira_meta(_body([{"file": "000000.jpg", "delay": 100}]))
    with pytest.raises(UgoiraValidationError):
        convert_ugoira(zip_path, meta, tmp_path / "animation.gif")


def test_convert_raises_when_ffmpeg_missing(monkeypatch, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path, {"000000.jpg": b"x"})
    meta = parse_ugoira_meta(_body([{"file": "000000.jpg", "delay": 100}]))
    output = tmp_path / "animation.gif"
    monkeypatch.setattr(ug, "ffmpeg_available", lambda: False)
    with pytest.raises(UgoiraFFmpegMissingError):
        convert_ugoira(zip_path, meta, output)
    assert not output.exists()


def test_ffmpeg_failure_or_timeout_leaves_no_output_and_is_retriable(
    monkeypatch, tmp_path: Path,
) -> None:
    zip_path = _make_zip(tmp_path, {"000000.jpg": b"x"})
    meta = parse_ugoira_meta(_body([{"file": "000000.jpg", "delay": 100}]))
    output = tmp_path / "animation.gif"

    _install_fake_ffmpeg(monkeypatch, timeout_kill=True)
    with pytest.raises(UgoiraError, match="timed out"):
        convert_ugoira(zip_path, meta, output)
    assert not output.exists()

    _install_fake_ffmpeg(monkeypatch, fail=True)
    with pytest.raises(UgoiraError):
        convert_ugoira(zip_path, meta, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".ugoira-*"))

    # 失败后同一路径可立即重试成功，不会被半成品误判
    _install_fake_ffmpeg(monkeypatch)
    convert_ugoira(zip_path, meta, output)
    assert output.exists()


# ---------------------------------------------------------------------------
# Provider：缓存命中、失败不脏缓存、缺 ffmpeg 降级、阻塞工作移出事件循环
# ---------------------------------------------------------------------------


class _FakeAPI:
    def __init__(self, zip_bytes: bytes) -> None:
        self._zip = zip_bytes
        self.meta_calls = 0
        self.download_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def fetch_ugoira_meta(self, pid: str) -> dict:
        self.meta_calls += 1
        return _body([{"file": "000000.jpg", "delay": 100}])

    async def download_image(self, url: str) -> bytes:
        self.download_calls += 1
        return self._zip


def _work(pid: str = "111") -> IllustWork:
    return IllustWork(
        pid=pid, title="t", author="a", user_id="9", description="",
        create_date="2026-01-01", tags=[], page_count=1, illust_type=2,
    )


def _provider(tmp_path: Path, fake_api: _FakeAPI, monkeypatch):
    from pixivfeed.provider.pixiv import PixivProvider

    config = SimpleNamespace(
        pixiv=SimpleNamespace(phpsessid="", timeout=5, download_concurrency=1),
    )
    provider = PixivProvider(config, cache_dir=tmp_path, public_base_url="https://example.com/p")
    monkeypatch.setattr("pixivfeed.provider.pixiv.PixivAPI", lambda *a, **k: fake_api)
    return provider


def test_provider_cache_hit_skips_reconvert(monkeypatch, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path / "build", {"000000.jpg": b"x"})
    fake = _FakeAPI(zip_path.read_bytes())
    cache = tmp_path / "cache"
    provider = _provider(cache, fake, monkeypatch)
    _install_fake_ffmpeg(monkeypatch)
    work = _work()

    async def run() -> None:
        r1 = await provider.fetch_and_convert_ugoira(work)
        r2 = await provider.fetch_and_convert_ugoira(work)
        assert r1.gif_path == r2.gif_path
        assert r1.public_url == "https://example.com/p/111/ugoira/animation.gif"

    asyncio.run(run())
    assert (fake.meta_calls, fake.download_calls) == (1, 1)
    assert not (cache / "111" / "ugoira" / ".frames.zip.tmp").exists()


def test_provider_failure_leaves_no_cache_artifacts(monkeypatch, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path / "build", {"000000.jpg": b"x"})
    fake = _FakeAPI(zip_path.read_bytes())
    cache = tmp_path / "cache"
    provider = _provider(cache, fake, monkeypatch)
    work = _work()

    _install_fake_ffmpeg(monkeypatch, fail=True)

    async def run() -> None:
        with pytest.raises(UgoiraError):
            await provider.fetch_and_convert_ugoira(work)
        ugoira_dir = cache / "111" / "ugoira"
        assert not (ugoira_dir / "animation.gif").exists()
        assert not (ugoira_dir / ".frames.zip.tmp").exists()

        _install_fake_ffmpeg(monkeypatch)
        result = await provider.fetch_and_convert_ugoira(work)
        assert result.gif_path.exists()

    asyncio.run(run())


def test_provider_missing_ffmpeg_fails_before_http(monkeypatch, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path / "build", {"000000.jpg": b"x"})
    fake = _FakeAPI(zip_path.read_bytes())
    cache = tmp_path / "cache"
    provider = _provider(cache, fake, monkeypatch)
    import pixivfeed.provider.pixiv as pkg

    monkeypatch.setattr(ug, "ffmpeg_available", lambda: False)
    monkeypatch.setattr(pkg, "ffmpeg_available", lambda: False)

    async def run() -> None:
        # 已有缓存时即使没 ffmpeg 也照常发送
        ugoira_dir = cache / "111" / "ugoira"
        ugoira_dir.mkdir(parents=True)
        (ugoira_dir / "animation.gif").write_bytes(b"GIF89a")
        result = await provider.fetch_and_convert_ugoira(_work())
        assert result.gif_path.exists()
        assert (fake.meta_calls, fake.download_calls) == (0, 0)

        # 无缓存则在 HTTP 之前抛 typed error
        (ugoira_dir / "animation.gif").unlink()
        with pytest.raises(UgoiraFFmpegMissingError):
            await provider.fetch_and_convert_ugoira(_work())
        assert (fake.meta_calls, fake.download_calls) == (0, 0)

    asyncio.run(run())


def test_provider_runs_conversion_off_event_loop(monkeypatch, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path / "build", {"000000.jpg": b"x"})
    fake = _FakeAPI(zip_path.read_bytes())
    provider = _provider(tmp_path / "cache", fake, monkeypatch)
    _install_fake_ffmpeg(monkeypatch)

    import pixivfeed.provider.pixiv as pkg

    offloaded: list[bool] = []

    async def fake_to_thread(func, *args, **kwargs):
        offloaded.append(True)
        return func(*args, **kwargs)

    monkeypatch.setattr(pkg.asyncio, "to_thread", fake_to_thread)
    asyncio.run(provider.fetch_and_convert_ugoira(_work()))
    assert offloaded


# ---------------------------------------------------------------------------
# Telegram：ffmpeg 缺失降级、发送失败不记成功、成功语义
# ---------------------------------------------------------------------------


class _Bot:
    def __init__(self, *, fail: bool = False) -> None:
        self.animations: list[dict] = []
        self.fail = fail

    async def send_animation(self, **kwargs):
        if self.fail:
            raise RuntimeError("telegram upload failed")
        self.animations.append(kwargs)
        return SimpleNamespace(message_id=1)


class _Message:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.deleted = False
        self.message_id = 555
        self.chat = SimpleNamespace(id=7, type="private")

    async def reply_text(self, text, **kwargs):
        self.edits.append(text)
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)

    async def delete(self) -> None:
        self.deleted = True


class _UsageStore:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def log(self, **kwargs) -> None:
        self.records.append(kwargs)


def _tg_context(bot: _Bot, usage: _UsageStore) -> SimpleNamespace:
    config = SimpleNamespace(
        templates=SimpleNamespace(illust=SimpleNamespace(direct_caption="")),
    )
    return SimpleNamespace(
        bot=bot,
        bot_data={
            "config": config,
            "registry": SimpleNamespace(),
            "publisher": None,
            "telegraph_cache": None,
            "allowlist": None,
            "usage_store": usage,
        },
    )


def _tg_update(message: _Message) -> SimpleNamespace:
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_chat=message.chat,
        effective_user=SimpleNamespace(id=42),
    )


def test_send_ugoira_without_ffmpeg_degrades_and_keeps_link(monkeypatch) -> None:
    msg, bot, usage = _Message(), _Bot(), _UsageStore()
    context, update = _tg_context(bot, usage), _tg_update(msg)

    class _Provider:
        async def fetch_and_convert_ugoira(self, work):
            raise UgoiraFFmpegMissingError("ffmpeg not found")

    monkeypatch.setattr(handlers, "_pixiv_provider", lambda registry: _Provider())
    asyncio.run(handlers._send_pixiv_ugoira(update, context, _work(), placeholder=msg))

    assert bot.animations == []
    assert any("ffmpeg" in e and "artworks/111" in e for e in msg.edits)
    assert usage.records == []  # 没有假成功记录


def test_send_ugoira_failure_is_logged_failed(monkeypatch, tmp_path: Path) -> None:
    msg, bot, usage = _Message(), _Bot(fail=True), _UsageStore()
    context, update = _tg_context(bot, usage), _tg_update(msg)
    gif = tmp_path / "animation.gif"
    gif.write_bytes(b"GIF89a")

    class _Provider:
        async def fetch_and_convert_ugoira(self, work):
            return SimpleNamespace(work=work, gif_path=gif, public_url="https://x/g.gif")

    monkeypatch.setattr(handlers, "_pixiv_provider", lambda registry: _Provider())
    asyncio.run(handlers._send_pixiv_ugoira(update, context, _work(), placeholder=msg))

    assert [r["status"] for r in usage.records] == ["failed"]
    assert msg.deleted is False
    assert any("发送失败" in e for e in msg.edits)


def test_send_ugoira_success_replies_original_logs_ok_deletes_placeholder(
    monkeypatch, tmp_path: Path,
) -> None:
    msg, bot, usage = _Message(), _Bot(), _UsageStore()
    context, update = _tg_context(bot, usage), _tg_update(msg)
    gif = tmp_path / "animation.gif"
    gif.write_bytes(b"GIF89a" + b"\x00" * 10)

    class _Provider:
        async def fetch_and_convert_ugoira(self, work):
            return SimpleNamespace(work=work, gif_path=gif, public_url="https://x/g.gif")

    monkeypatch.setattr(handlers, "_pixiv_provider", lambda registry: _Provider())
    asyncio.run(handlers._send_pixiv_ugoira(update, context, _work(), placeholder=msg))

    assert len(bot.animations) == 1
    assert bot.animations[0]["reply_to_message_id"] == msg.message_id
    assert msg.deleted is True
    assert len(usage.records) == 1 and usage.records[0].get("status", "ok") == "ok"
    assert usage.records[0]["bytes_out"] == gif.stat().st_size


# ---------------------------------------------------------------------------
# 普通插画无回归
# ---------------------------------------------------------------------------


def test_normal_illustration_parse_unchanged() -> None:
    from pixivfeed.provider.pixiv.parser import parse_illust_meta

    work = parse_illust_meta({
        "id": "222", "illustTitle": "普通图", "userName": "author", "userId": "9",
        "illustType": 0, "pageCount": 1,
        "urls": {"original": "https://i.pximg.net/222_p0.png", "regular": "r",
                 "small": "s", "thumb": "t"},
        "tags": {"tags": []},
    })
    assert work.is_ugoira is False
    assert work.illust_type == 0
    assert work.images[0].original.endswith("222_p0.png")


def test_ffmpeg_available_reflects_which(monkeypatch) -> None:
    monkeypatch.setattr(ug.shutil, "which", lambda _name: None)
    assert ffmpeg_available() is False
    monkeypatch.setattr(ug.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    assert ffmpeg_available() is True
