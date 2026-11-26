from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pixivfeed.channel.telegram.setting import (
    parse_exhentai_cookie,
    parse_pixiv_cookie,
    render_group,
    render_item,
    render_main_menu,
)
from pixivfeed.config import RUNTIME_SETTING_GROUPS, RUNTIME_SETTINGS, Config
from pixivfeed.storage import Database, RuntimeSettings


class MemoryRuntime:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.many_calls: list[dict[str, str]] = []

    def all(self) -> dict[str, str]:
        return dict(self.values)

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, updated_by: int | None = None) -> None:
        self.values[key] = value

    async def set_many(self, values: dict[str, str], updated_by: int | None = None) -> None:
        self.many_calls.append(dict(values))
        self.values.update(values)

    async def unset(self, key: str) -> bool:
        return self.values.pop(key, None) is not None


def test_parse_exhentai_cookie_header() -> None:
    values = parse_exhentai_cookie("Cookie: ipb_member_id=123; ipb_pass_hash=abc; igneous=def; sk=x")

    assert values == {
        "collectors.exhentai.ipb_member_id": "123",
        "collectors.exhentai.ipb_pass_hash": "abc",
        "collectors.exhentai.igneous": "def",
    }


def test_parse_exhentai_cookie_extension_json_and_devtools_table() -> None:
    exported = json.dumps([
        {"name": "ipb_member_id", "value": "123"},
        {"name": "ipb_pass_hash", "value": "abc"},
    ])
    table = "ipb_member_id\t456\t.exhentai.org\t/\nigneous\tghi\t.exhentai.org\t/"

    assert parse_exhentai_cookie(exported)["collectors.exhentai.ipb_pass_hash"] == "abc"
    assert parse_exhentai_cookie(table) == {
        "collectors.exhentai.ipb_member_id": "456",
        "collectors.exhentai.igneous": "ghi",
    }


@pytest.mark.parametrize("text", ["hello", "ipb_member_id=abc; ipb_pass_hash=x"])
def test_parse_exhentai_cookie_rejects_invalid_input(text: str) -> None:
    with pytest.raises(ValueError):
        parse_exhentai_cookie(text)


def test_parse_pixiv_cookie_accepts_header_or_bare_value() -> None:
    assert parse_pixiv_cookie("a=1; PHPSESSID=123_abc; b=2") == {"pixiv.phpsessid": "123_abc"}
    assert parse_pixiv_cookie("123_abc") == {"pixiv.phpsessid": "123_abc"}
    with pytest.raises(ValueError):
        parse_pixiv_cookie("a=1; b=2")


def _callback_data(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_all_setting_panel_buttons_fit_telegram_limit() -> None:
    config = Config()
    runtime = MemoryRuntime({spec.key: "1" for spec in RUNTIME_SETTINGS})
    rendered = [render_main_menu(config, runtime)]
    rendered.extend(render_group(group, config, runtime) for group, _ in RUNTIME_SETTING_GROUPS)
    rendered.extend(render_item(spec, config, runtime) for spec in RUNTIME_SETTINGS)

    payloads = [data for _, markup in rendered for data in _callback_data(markup)]
    assert payloads
    assert max(len(data.encode()) for data in payloads) <= 64
    assert len({spec.sid for spec in RUNTIME_SETTINGS}) == len(RUNTIME_SETTINGS)


def test_unset_runtime_restores_baseline_immediately() -> None:
    config = Config()
    runtime = MemoryRuntime({"storage.cache_days": "30"})
    config.bind_runtime(runtime)
    assert config.storage.cache_days == 30

    assert asyncio.run(config.unset_runtime("storage.cache_days")) is True

    assert config.storage.cache_days == 7
    assert runtime.get("storage.cache_days") is None


def test_set_runtime_many_validates_before_writing() -> None:
    config = Config()
    runtime = MemoryRuntime()
    config.bind_runtime(runtime)

    with pytest.raises(ValueError):
        asyncio.run(config.set_runtime_many({
            "collectors.exhentai.ipb_pass_hash": "new",
            "storage.cache_days": "0",
        }))

    assert runtime.many_calls == []
    assert config.collectors.exhentai.ipb_pass_hash == ""


def test_runtime_settings_set_many_rolls_back_failed_batch(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict[str, str], list[tuple[str, str]]]:
        db = Database(str(tmp_path / "settings.db"))
        await db.connect()
        try:
            runtime = RuntimeSettings(db)
            await runtime.load()
            await runtime.set("collectors.exhentai.igneous", "old")
            await db.conn.execute(
                """
                CREATE TRIGGER reject_bad_cookie BEFORE INSERT ON runtime_settings
                WHEN NEW.value = 'bad' BEGIN SELECT RAISE(ABORT, 'bad cookie'); END
                """
            )
            await db.conn.commit()
            with pytest.raises(Exception, match="bad cookie"):
                await runtime.set_many({
                    "collectors.exhentai.igneous": "new",
                    "collectors.exhentai.ipb_pass_hash": "bad",
                })
            async with db.conn.execute("SELECT key, value FROM runtime_settings ORDER BY key") as cur:
                rows = await cur.fetchall()
            return runtime.all(), [tuple(row) for row in rows]
        finally:
            await db.close()

    cached, rows = asyncio.run(scenario())

    assert cached == {"collectors.exhentai.igneous": "old"}
    assert rows == [("collectors.exhentai.igneous", "old")]
