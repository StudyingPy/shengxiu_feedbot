from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pixivfeed.channel.telegram import handlers
from pixivfeed.provider.ehentai import SearchResultItem, SearchResultPage
from pixivfeed.provider.ehentai import _search as eh_search
from pixivfeed.provider.jm import (
    clean_jm_title,
    jm_creator_hints,
    jm_search_candidates,
)


def test_jm_title_candidates_split_example_into_stable_anchor() -> None:
    title = (
        "[NEO AXIS (そうたつ)] 轉變墮落 "
        "～最強武道家 TS○リオナホに墮つ～[中國翻譯] [DL版]"
    )

    candidates = jm_search_candidates(title)

    assert candidates == [
        "轉變墮落 ～最強武道家 TS○リオナホに墮つ",
        "最強武道家 TS○リオナホに墮つ",
        "最強武道家 TS",
        "轉變墮落",
    ]


def test_jm_title_cleaning_removes_truncated_metadata_bracket() -> None:
    title = "轉變墮落 ～最強武道家 TS○リオナホに墮つ～[中國翻譯"

    assert clean_jm_title(title) == "轉變墮落 ～最強武道家 TS○リオナホに墮つ～"


def test_jm_title_candidates_reject_numeric_range_fragment() -> None:
    title = "[伴カズヤス] ゾンビっ娘の救濟は中出しSEXで 1~10 [中國翻譯]"

    candidates = jm_search_candidates(title)

    assert candidates == [
        "ゾンビっ娘の救濟は中出しSEXで 1~10",
        "ゾンビっ娘の救濟は中出しSEXで 1",
    ]
    assert "10" not in candidates


def test_jm_creator_hints_merge_authors_and_title_group() -> None:
    title = "[NEO AXIS (そうたつ)] 転変堕落"

    assert jm_creator_hints(title, ("そうたつ", "Second Artist")) == [
        "そうたつ",
        "soutatsu",
        "Second Artist",
        "NEO AXIS",
    ]


def test_jm_creator_hints_follow_eh_romanization_for_real_samples() -> None:
    expected = {
        "なかじまゆか": "nakajima yuka",
        "雪國おまる": "yukiguni omaru",
        "ぞんだ": "zonda",
        "そうたつ": "soutatsu",
        "さいとう": "saitou",
    }

    for original, eh_tag in expected.items():
        hints = jm_creator_hints("", (original,))
        assert any(handlers._creator_names_match(hint, eh_tag) for hint in hints)


def test_eh_search_slot_enforces_shared_per_provider_interval(monkeypatch) -> None:
    clock = [100.0]
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(eh_search, "_SEARCH_MIN_INTERVAL_SECONDS", 3.0)
    monkeypatch.setattr(eh_search.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(eh_search.asyncio, "sleep", fake_sleep)

    async def run() -> None:
        await eh_search._wait_for_search_slot()
        await eh_search._wait_for_search_slot()

    asyncio.run(run())

    assert sleeps == [3.0]


def test_ehsearch_landing_uses_fallback_only_after_empty_result(monkeypatch) -> None:
    calls: list[str] = []
    registry = object()
    item = SearchResultItem(
        gid=1,
        token="abcdef",
        url="https://e-hentai.org/g/1/abcdef/",
        title="matched",
        category="Manga",
        tags=["group:neo axis", "artist:soutatsu"],
    )

    async def fake_dispatch(_registry, keyword):
        calls.append(keyword)
        items = [item] if keyword == "最強武道家 TS" else []
        return SearchResultPage(
            items=items,
            total_count=len(items),
            next_url=None,
            prev_url=None,
            host="e-hentai.org",
            keyword=keyword,
        )

    async def fake_log_usage(*args, **kwargs):
        return None

    class Placeholder:
        chat = SimpleNamespace(id=10)
        message_id = 20

        def __init__(self) -> None:
            self.edits: list[str] = []

        async def edit_text(self, text, **kwargs):
            self.edits.append(text)

    placeholder = Placeholder()
    monkeypatch.setattr(
        handlers,
        "_ctx",
        lambda context: (None, registry, None, None, None),
    )
    monkeypatch.setattr(handlers, "_ehsearch_dispatch", fake_dispatch)
    monkeypatch.setattr(handlers, "_gc_pending", lambda: None)
    monkeypatch.setattr(handlers, "_get_ehtagdb", lambda context: None)
    monkeypatch.setattr(handlers, "_log_usage", fake_log_usage)

    asyncio.run(handlers._run_ehsearch_landing(
        SimpleNamespace(effective_user=SimpleNamespace(id=30)),
        SimpleNamespace(),
        placeholder=placeholder,
        keyword="完整标题",
        force_r2=False,
        fallback_keywords=["副标题", "最強武道家 TS", "不会再尝试"],
        creator_hints=["NEO AXIS", "そうたつ"],
    ))

    assert calls == ["完整标题", "副标题", "最強武道家 TS"]
    seid, state = next(
        (seid, state) for seid, state in handlers._SEARCH_STATES.items()
        if state.chat_id == 10 and state.msg_id == 20
    )
    assert state.keyword == "最強武道家 TS"
    assert state.creator_match == "NEO AXIS"
    assert "完整标题未命中" in placeholder.edits[0]
    handlers._SEARCH_STATES.pop(seid)


def test_ehsearch_landing_keeps_first_unverified_results(monkeypatch) -> None:
    calls: list[str] = []
    item = SearchResultItem(
        gid=2,
        token="123abc",
        url="https://e-hentai.org/g/2/123abc/",
        title="possible match",
        category="Manga",
        tags=["artist:unknown"],
    )

    async def fake_dispatch(_registry, keyword):
        calls.append(keyword)
        items = [item] if keyword == "完整标题" else []
        return SearchResultPage(
            items=items,
            total_count=len(items),
            next_url=None,
            prev_url=None,
            host="e-hentai.org",
            keyword=keyword,
        )

    async def fake_log_usage(*args, **kwargs):
        return None

    class Placeholder:
        chat = SimpleNamespace(id=11)
        message_id = 21

        async def edit_text(self, text, **kwargs):
            return None

    monkeypatch.setattr(
        handlers,
        "_ctx",
        lambda context: (None, object(), None, None, None),
    )
    monkeypatch.setattr(handlers, "_ehsearch_dispatch", fake_dispatch)
    monkeypatch.setattr(handlers, "_gc_pending", lambda: None)
    monkeypatch.setattr(handlers, "_get_ehtagdb", lambda context: None)
    monkeypatch.setattr(handlers, "_log_usage", fake_log_usage)

    asyncio.run(handlers._run_ehsearch_landing(
        SimpleNamespace(effective_user=SimpleNamespace(id=31)),
        SimpleNamespace(),
        placeholder=Placeholder(),
        keyword="完整标题",
        force_r2=False,
        fallback_keywords=["副标题", "稳定锚点"],
        creator_hints=["NEO AXIS"],
    ))

    assert calls == ["完整标题", "副标题", "稳定锚点"]
    seid, state = next(
        (seid, state) for seid, state in handlers._SEARCH_STATES.items()
        if state.chat_id == 11 and state.msg_id == 21
    )
    assert state.keyword == "完整标题"
    assert state.creator_match is None
    assert state.page.items == [item]
    handlers._SEARCH_STATES.pop(seid)
