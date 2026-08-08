from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
import yaml

from pixivfeed.config import Config, apply_runtime_overrides
from pixivfeed.utils import logger


class RecordingRuntime:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.calls: list[tuple[str, str, int | None]] = []

    def all(self) -> dict[str, str]:
        return dict(self.values)

    async def set(
        self,
        key: str,
        value: str,
        updated_by: int | None = None,
    ) -> None:
        self.calls.append((key, value, updated_by))


class FailingRuntime(RecordingRuntime):
    async def set(
        self,
        key: str,
        value: str,
        updated_by: int | None = None,
    ) -> None:
        raise OSError("database is read-only")


@pytest.mark.parametrize(
    ("key", "invalid_value"),
    [
        ("pixiv.timeout", "0"),
        ("pixiv.download_concurrency", "33"),
        ("collectors.timeout", "601"),
        ("collectors.download_concurrency", "0"),
        ("collectors.ehentai.archive_timeout", "3601"),
        ("collectors.exhentai.archive_timeout", "0"),
        ("collectors.jm.timeout", "301"),
        ("publish.direct_threshold", "11"),
        ("publish.max_images_per_page", "301"),
        ("storage.cache_days", "0"),
        ("size_prefetch.sample_count", "21"),
        ("size_prefetch.timeout", "0"),
        ("collectors.ehentai.default_mode", "invalid"),
        ("collectors.exhentai.default_mode", "invalid"),
        ("logging.level", "verbose"),
    ],
)
def test_runtime_rejects_out_of_range_and_unknown_values(
    key: str,
    invalid_value: str,
) -> None:
    config = Config()
    runtime = RecordingRuntime()
    config.bind_runtime(runtime)
    old_value = config.get_field(key)

    with pytest.raises(ValueError):
        asyncio.run(config.set_runtime(key, invalid_value, updated_by=42))

    assert config.get_field(key) == old_value
    assert runtime.calls == []


def test_runtime_database_failure_does_not_change_memory_config() -> None:
    config = Config()
    runtime = FailingRuntime()
    config.bind_runtime(runtime)

    with pytest.raises(OSError, match="read-only"):
        asyncio.run(config.set_runtime("storage.cache_days", "30", updated_by=42))

    assert config.storage.cache_days == 7


def test_yaml_integer_fields_reject_fractional_values() -> None:
    config = Config()
    config.telegram.token = "123:token"
    config.auth.admin_users = [1]
    config.publish.base_url = "https://example.test/p"
    config.pixiv.download_concurrency = 2.5  # type: ignore[assignment]

    with pytest.raises(ValueError, match="pixiv.download_concurrency: 必须是整数"):
        config._validate()


def test_cleanup_overlay_ignores_existing_nonpositive_cache_days(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE runtime_settings (key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO runtime_settings(key, value) VALUES (?, ?)",
            ("storage.cache_days", "0"),
        )

    config = Config()
    apply_runtime_overrides(config, db_path)

    assert config.storage.cache_days == 7


def test_logging_level_reconfigures_for_startup_override_and_live_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_setup_logging(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("pixivfeed.utils.setup_logging", fake_setup_logging)
    config = Config()
    runtime = RecordingRuntime({"logging.level": "debug"})

    config.bind_runtime(runtime)
    asyncio.run(config.set_runtime("logging.level", "warning", updated_by=42))

    assert config.logging.level == "WARNING"
    assert calls == [
        {
            "level": "DEBUG",
            "to_file": False,
            "file_path": "/var/log/pixiv-feed-bot/bot.log",
        },
        {
            "level": "WARNING",
            "to_file": False,
            "file_path": "/var/log/pixiv-feed-bot/bot.log",
        },
    ]


def test_save_telegraph_token_inserts_missing_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'publish:\n  base_url: "https://example.test/p"\nlogging:\n  level: INFO\n',
        encoding="utf-8",
    )
    config = Config()
    config._source_path = config_path

    assert config.save_telegraph_token("secret-token") is True

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["publish"]["telegraph_token"] == "secret-token"


def test_save_telegraph_token_handles_publish_key_with_inline_comment(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'publish:  # Telegra.ph settings\n  base_url: "https://example.test/p"\n',
        encoding="utf-8",
    )
    config = Config()
    config._source_path = config_path

    assert config.save_telegraph_token("secret-token") is True

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["publish"]["telegraph_token"] == "secret-token"


def test_save_telegraph_token_reports_write_failure_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'publish:\n  telegraph_token: ""\n',
        encoding="utf-8",
    )
    config = Config()
    config._source_path = config_path
    secret = "must-not-appear-in-logs"
    original_write_text = Path.write_text

    def fail_write(path: Path, *args: object, **kwargs: object) -> int:
        if path == config_path:
            raise PermissionError("permission denied")
        return original_write_text(path, *args, **kwargs)

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), format="{message}")
    monkeypatch.setattr(Path, "write_text", fail_write)
    try:
        assert config.save_telegraph_token(secret) is False
    finally:
        logger.remove(sink_id)

    assert config.publish.telegraph_token == secret
    combined = "".join(messages)
    assert "not persisted" in combined
    assert secret not in combined


def test_main_service_allows_only_config_file_write_and_docs_set_unix_owner() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy" / "pixiv-feed-bot.service").read_text(encoding="utf-8")
    deploy_docs = (root / "docs" / "DEPLOY.md").read_text(encoding="utf-8")

    read_write_line = next(line for line in service.splitlines() if line.startswith("ReadWritePaths="))
    assert "/etc/pixiv-feed-bot/config.yaml" in read_write_line
    assert "/etc/pixiv-feed-bot " not in read_write_line
    assert "chown pixivbot:pixivbot /etc/pixiv-feed-bot/config.yaml" in deploy_docs
    assert "chmod 0640 /etc/pixiv-feed-bot/config.yaml" in deploy_docs
