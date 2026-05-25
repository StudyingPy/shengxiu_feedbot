"""图片缓存清理脚本。

用法（cron 或 systemd timer）：
    python -m deploy.cleanup /etc/pixiv-feed-bot/config.yaml

读取 storage.cache_dir 和 storage.cache_days，删除超期文件，再删除空目录。
当一个 provider 的工作目录被完整清空（顶层目录 rmdir 成功）时，
顺便把对应 telegraph_cache 行也失效——避免用户重提相同链接命中坏链接。

注意：本脚本是独立进程，bot 进程同时可能在读写 SQLite。SQLite WAL 模式下
不同进程的写入会自动序列化，DELETE 不会冲突。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

# 允许从 deploy/ 目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pixivfeed.config import Config, apply_runtime_overrides
from pixivfeed.storage.cache_keymap import cache_keys_for_path_segment
from pixivfeed.utils import logger, setup_logging


def _invalidate_cache_rows_sync(
    db_path: str, kind_pattern: str, pixiv_id: str,
) -> int:
    """同步版 cache 行失效。返回删除行数。

    cleanup.py 是同步脚本，不引 aiosqlite。bot 进程同时可能在读写，
    用短连接 + WAL 模式（schema 已默认开 WAL）下 DELETE 是原子安全的。
    """
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        cur = conn.execute(
            "DELETE FROM telegraph_cache WHERE kind LIKE ? AND pixiv_id = ?",
            (kind_pattern, pixiv_id),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def cleanup(config_path: str) -> None:
    cfg = Config.load(config_path)
    # PR-4：让 cleanup timer 也感知 /setting set storage.cache_days 这类运行时覆盖。
    # DB 不存在 / 表不存在 → 静默回 YAML；脏值 → log warning + 回该 key 的 YAML 值。
    cfg = apply_runtime_overrides(cfg, cfg.storage.db_path)
    setup_logging(level=cfg.logging.level, to_file=False)
    cache_dir = Path(cfg.storage.cache_dir)
    if not cache_dir.exists():
        logger.info(f"cache dir does not exist: {cache_dir}")
        return

    db_path = cfg.storage.db_path
    db_exists = Path(db_path).exists()

    cutoff = time.time() - cfg.storage.cache_days * 86400
    removed_files = 0
    removed_bytes = 0

    # 删超期文件
    for f in cache_dir.rglob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                size = f.stat().st_size
                f.unlink()
                removed_files += 1
                removed_bytes += size
            except OSError as e:
                logger.warning(f"failed to remove {f}: {e}")

    # 删空目录 + 联动失效 telegraph_cache
    # 顺序：倒序遍历——子目录先于父目录被检查；一个 provider 工作目录顶层
    # 被 rmdir 时表示这个画廊已经被完整逐出，是 invalidate cache 的精确信号。
    removed_dirs = 0
    invalidated_cache_rows = 0
    for d in sorted([p for p in cache_dir.rglob("*") if p.is_dir()], reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
                removed_dirs += 1
                # 只有 cache_dir 顶层目录的 rmdir 表示"某 provider 工作目录被完整逐出"。
                # 内层目录（如 pixiv/{pid}/original/）被回收只意味着某 mode 没文件了，
                # 不代表整个画廊都没了——跳过避免误失效兄弟 mode 的 cache。
                if d.parent.resolve() == cache_dir.resolve() and db_exists:
                    pairs = cache_keys_for_path_segment(d.name)
                    if not pairs:
                        logger.debug(
                            f"cleanup: no cache mapping for dir {d.name!r}; "
                            "no cache invalidation"
                        )
                    for kind_pattern, pixiv_id in pairs:
                        try:
                            n = _invalidate_cache_rows_sync(
                                db_path, kind_pattern, pixiv_id,
                            )
                        except Exception as e:
                            logger.warning(
                                f"cleanup: invalidate {kind_pattern!r}/{pixiv_id!r} "
                                f"failed: {e!r}"
                            )
                            continue
                        invalidated_cache_rows += n
        except OSError:
            pass

    logger.info(
        f"cleanup done: removed {removed_files} files "
        f"({removed_bytes / 1_000_000:.1f} MB), {removed_dirs} empty dirs, "
        f"invalidated {invalidated_cache_rows} telegraph_cache row(s)"
    )


if __name__ == "__main__":
    cleanup(sys.argv[1] if len(sys.argv) >= 2 else "config.yaml")
