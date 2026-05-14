"""EhTagTranslation 数据库——eh tag 中文翻译。

来源：https://github.com/EhTagTranslation/Database 的 GitHub Release
（每日自动构建），下载 db.text.json，约 1MB，包含所有 namespace × value 的
"name"（中文显示名）。

设计：
- 启动时由 bot.py fire-and-forget 调 load()，不阻塞 bot 上线。
- 本地缓存到指定文件路径，<30 天的缓存视作有效，超期或缺失就重新拉。
- 加载前/失败时所有 translate*() 都直接返回原文，整套调用 100% safe fallback。
- 单进程内存常驻，无需持久化反查；JSON 1MB 解析 + 字典化大概 ~50ms 一次。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from ...utils import logger


class EHTagDB:
    URL = "https://github.com/EhTagTranslation/Database/releases/latest/download/db.text.json"
    REFRESH_DAYS = 30
    FETCH_TIMEOUT = 60.0

    def __init__(self, cache_path: Path):
        self.cache_path = Path(cache_path)
        # (namespace, value_normalized) -> 中文 name。value 用全小写+空格 normalize
        # 后做 key，匹配 eh HTML 抠出来的原始 value（HTML 里就是 lowercase + space）。
        self._db: dict[tuple[str, str], str] = {}
        # namespace -> namespace 中文名（如 "language" → "语言"）
        self._ns_zh: dict[str, str] = {}
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        """从本地缓存或远端 GitHub Release 拉取数据库。

        本地缓存有效（存在且 <30 天）→ 直接解析；否则下载新版本覆盖。
        任何异常都吞掉只 log——加载失败不影响 bot 正常运行，只是没翻译而已。
        """
        try:
            payload = self._read_cached_or_fetch()
            if payload is None:
                return
            self._parse_into_dict(payload)
            self._loaded = True
            logger.success(
                f"ehtagdb loaded: {len(self._db)} tag entries, "
                f"{len(self._ns_zh)} namespaces"
            )
        except Exception:
            logger.exception("ehtagdb load failed; tags will display untranslated")

    def _read_cached_or_fetch(self) -> dict | None:
        """同步入口（内部跑 async http）。返回解析后的 JSON dict，失败 None。"""
        # 优先用本地缓存
        if self.cache_path.exists():
            age_days = (time.time() - self.cache_path.stat().st_mtime) / 86400.0
            if age_days < self.REFRESH_DAYS:
                try:
                    return json.loads(self.cache_path.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"ehtagdb local cache parse failed: {e}; refetching")

        # 没缓存或超期 → 拉新
        try:
            with httpx.Client(timeout=self.FETCH_TIMEOUT, follow_redirects=True) as client:
                resp = client.get(self.URL)
                resp.raise_for_status()
                content = resp.content
        except Exception as e:
            logger.warning(f"ehtagdb fetch failed: {e}")
            # 远端拉不到但本地有旧缓存（>30 天）也算能用，比完全没翻译强
            if self.cache_path.exists():
                try:
                    logger.info("ehtagdb falling back to stale local cache")
                    return json.loads(self.cache_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return None

        # 写入缓存
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_bytes(content)
        except Exception as e:
            logger.warning(f"ehtagdb cache write failed (non-fatal): {e}")

        return json.loads(content.decode("utf-8"))

    def _parse_into_dict(self, payload: dict) -> None:
        """填充 self._db 和 self._ns_zh。

        EhTagTranslation 数据库结构（db.text.json）：
            {
              "data": [
                {
                  "namespace": "language",
                  "data": {
                    "chinese": {"name": "中文", "intro": "...", "links": "..."},
                    ...
                  }
                },
                {
                  "namespace": "rows",            # 元 namespace，存 namespace 自身的中文名
                  "data": {
                    "language": {"name": "语言", ...},
                    "parody":   {"name": "原作", ...},
                    ...
                  }
                },
                ...
              ]
            }
        """
        self._db.clear()
        self._ns_zh.clear()
        for group in payload.get("data") or []:
            ns = (group.get("namespace") or "").strip()
            data = group.get("data") or {}
            if ns == "rows":
                for value, entry in data.items():
                    name = self._extract_name(entry, value)
                    self._ns_zh[value] = name
                continue
            for value, entry in data.items():
                name = self._extract_name(entry, value)
                # eh HTML 里 multi-word value 用空格分隔（"big breasts"），数据库
                # key 通常用空格（部分老条目用下划线，规范化都按空格）
                key = value.replace("_", " ").lower()
                self._db[(ns, key)] = name

    @staticmethod
    def _extract_name(entry: dict | str, fallback: str) -> str:
        if isinstance(entry, str):
            return entry or fallback
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return fallback

    def translate(self, namespace: str, value: str) -> str:
        """单条 tag value → 中文。找不到返回 value 原文。"""
        if not self._loaded:
            return value
        return self._db.get((namespace, value.replace("_", " ").lower()), value)

    def translate_tag(self, raw: str) -> str:
        """raw 形如 'language:chinese' / 'female:big breasts' → 翻译。

        没冒号或翻译不到时按规则降级：
        - 没冒号：原样返回
        - 翻译不到：返回 value 原文（不含 namespace）
        """
        if ":" not in raw:
            return raw
        ns, v = raw.split(":", 1)
        return self.translate(ns, v)

    def translate_namespace(self, ns: str) -> str:
        """namespace → 中文（"language" → "语言"）。找不到返回原文。"""
        if not self._loaded:
            return ns
        return self._ns_zh.get(ns, ns)


__all__ = ["EHTagDB"]
