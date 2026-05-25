"""cache_dir 顶层目录名 / R2 key 首段 → telegraph_cache (kind, pixiv_id) 反向映射。

R2 key 与 cache_dir 相对路径完全同源（v0.8.0 设计约束），因此本模块两边都能用：
- `deploy/cleanup.py` 清完空目录后调用，把对应 telegraph_cache 行也失效
- `pixivfeed/channel/telegram/bot.py:_r2_lru_loop` LRU 删完对象后调用同步失效

为什么要联动失效：
    `telegraph_cache` 表自身没有 TTL；命中后直接返回旧 URL。如果图片底层存储
    （cache_dir 文件 / R2 对象）已被清理，旧 telegraph 页面里的 <img> 就会
    404，用户重提相同链接仍然命中坏链接。联动失效让"图没了"的事实顺手
    invalidate cache 行，下次用户重提自动重新发布。

═══════════════════════════════════════════════════════════════════════════
🚨 协作者警告 —— 加新 provider 时必读 🚨

每加一个把图片存进 `cache_dir / R2` 的 provider，**必须**在本文件的
`cache_keys_for_path_segment` 加一条对应规则，否则：

  R2 LRU / cleanup.py 清掉该 provider 的图后，telegraph_cache 行不会失效
  → 用户重提相同链接命中 cache → 返回旧 URL → 页面里 <img> 404
  → 用户没办法触发重发，只能管理员手动 /cache invalidate 救

加规则时同步检查：
  1. provider 的 work_dir 顶层目录命名规则（看 provider/<name>/__init__.py 的
     `work_dir = self.cache_dir / f"..."` 那一行）
  2. handlers.py 里 `cache_kind = f"{ref.provider}/..."` 的实际拼接结果
     （cache_kind 是 telegraph_cache.kind 字段的实际值）
  3. 写完后跑一遍 cache_keymap 的 doctest / 单测，确认反向映射正确
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations


def cache_keys_for_path_segment(seg: str) -> list[tuple[str, str]]:
    """从 cache_dir 顶层目录名 / R2 key 首段反推可能的 (kind_pattern, pixiv_id)。

    返回的 kind_pattern 直接喂给 `TelegraphCache.invalidate_by_pattern`，
    它走 SQL `LIKE`——含 `%` 是通配，否则等价于精确 `=`。

    返回空 list 表示该路径不在 telegraph_cache 范围内（例如 zip_xxx，
    zip2tph 不写 cache），或路径不匹配任何已知规则（这种情况会有
    `logger.debug` 在调用方那侧打日志便于排查）。

    现有规则（与 provider work_dir 命名 + handlers cache_kind 对账）：

      路径首段                              → (kind_pattern, pixiv_id)
      ─────────────────────────────────────────────────────────────────
      `12345`（纯数字）                     → ("pixiv/illust", "12345")
      `novel_67890`                         → ("pixiv/novel", "67890")
      `eh_3936793_abc_page_sample`          → ("ehentai/gallery/page_sample", "3936793")
      `eh_3936793_abc_archive_original`     → ("ehentai/gallery/archive_original", "3936793")
      `ex_xxx_yyy_zzz`                      → ("exhentai/gallery/zzz", "xxx")
      `nh_654321`                           → ("nhentai/gallery/%", "654321")
      `zip_xxx`                             → []（zip2tph 不入 cache）
      其他                                   → []（未识别，调用方应跳过 + 打 debug 日志）

    nhentai 用 LIKE `nhentai/gallery/%` 是因为路径里看不出 mode（nhentai 工作目录
    只是 `nh_{gid}`，没有 mode 后缀），保险起见删该 gid 下所有 mode 的 cache 行。
    """
    if not seg:
        return []

    # zip2tph：直接发布到 telegra.ph，不写 telegraph_cache，无需联动
    if seg.startswith("zip_"):
        return []

    # pixiv novel: `novel_{nid}/...`，cache_kind == "pixiv/novel"
    if seg.startswith("novel_"):
        nid = seg[len("novel_"):]
        return [("pixiv/novel", nid)] if nid else []

    # e-hentai: `eh_{gid}_{token}_{mode}/...`
    # cache_kind == f"ehentai/gallery/{mode.value}"
    if seg.startswith("eh_"):
        parts = seg.split("_", 3)
        if len(parts) >= 4 and parts[1]:
            gid, _token, mode = parts[1], parts[2], parts[3]
            return [(f"ehentai/gallery/{mode}", gid)]
        return []

    # exhentai: `ex_{gid}_{token}_{mode}/...`
    if seg.startswith("ex_"):
        parts = seg.split("_", 3)
        if len(parts) >= 4 and parts[1]:
            gid, _token, mode = parts[1], parts[2], parts[3]
            return [(f"exhentai/gallery/{mode}", gid)]
        return []

    # nhentai: `nh_{gid}/...`，路径无 mode 后缀，用 LIKE 删该 gid 全 mode
    if seg.startswith("nh_"):
        gid = seg[len("nh_"):]
        return [("nhentai/gallery/%", gid)] if gid else []

    # pixiv illust: `{pid}/...`（pid 是纯数字），cache_kind == "pixiv/illust"
    if seg.isdigit():
        return [("pixiv/illust", seg)]

    # 未识别——加新 provider 后应该来本文件加规则（见文件顶部警告）
    return []


def cache_keys_for_r2_key(absolute_key: str, prefix: str = "") -> list[tuple[str, str]]:
    """从 R2 absolute key 反推 (kind_pattern, pixiv_id)。

    R2 absolute key 形如 `feedbot/eh_xxx_yyy_page_sample/p0.jpg`（含 prefix）。
    剥掉 prefix 后取首段，转给 `cache_keys_for_path_segment`。

    prefix 留空时按"key 第一段就是 provider 目录"处理（兼容无 prefix 部署）。
    """
    key = absolute_key
    if prefix:
        # prefix 形如 "feedbot" 或 "feedbot/"；归一化
        p = prefix.rstrip("/") + "/"
        if key.startswith(p):
            key = key[len(p):]
    seg = key.split("/", 1)[0]
    return cache_keys_for_path_segment(seg)


__all__ = ["cache_keys_for_path_segment", "cache_keys_for_r2_key"]
