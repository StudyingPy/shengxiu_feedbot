from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from pixivfeed.config import Config
from pixivfeed.provider.ehentai import EHTagDB


def test_config_loads_jm_collector_values() -> None:
    config = Config._from_dict({"collectors": {"jm": {"enabled": True, "timeout": 99}}})

    assert config.collectors.jm.enabled is True
    assert config.collectors.jm.timeout == 99


def test_ehtagdb_moves_blocking_load_and_parse_off_event_loop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tagdb = EHTagDB(tmp_path / "ehtagdb.json")
    event_loop_thread = threading.get_ident()
    worker_threads: dict[str, int] = {}

    def fake_fetch() -> dict:
        worker_threads["fetch"] = threading.get_ident()
        return {"data": []}

    def fake_parse(payload: dict) -> None:
        assert payload == {"data": []}
        worker_threads["parse"] = threading.get_ident()

    monkeypatch.setattr(tagdb, "_read_cached_or_fetch", fake_fetch)
    monkeypatch.setattr(tagdb, "_parse_into_dict", fake_parse)

    asyncio.run(tagdb.load())

    assert tagdb.loaded is True
    assert worker_threads["fetch"] != event_loop_thread
    assert worker_threads["parse"] != event_loop_thread


def test_cleanup_service_can_write_cache_and_database_directories() -> None:
    service_path = Path(__file__).resolve().parents[1] / "deploy" / "pixiv-feed-bot-cleanup.service"
    service = service_path.read_text(encoding="utf-8")
    read_write_line = next(line for line in service.splitlines() if line.startswith("ReadWritePaths="))

    assert "/var/cache/pixiv-feed-bot" in read_write_line
    assert "/var/lib/pixiv-feed-bot" in read_write_line
