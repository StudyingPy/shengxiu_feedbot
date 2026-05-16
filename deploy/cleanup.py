"""图片缓存清理脚本。

用法（cron 或 systemd timer）：
    python -m deploy.cleanup /etc/pixiv-feed-bot/config.yaml

读取 storage.cache_dir 和 storage.cache_days，删除超期文件，再删除空目录。
不会清 telegraph_cache 表（那是永久缓存，删掉会让旧链接失效）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 允许从 deploy/ 目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pixivfeed.config import Config, apply_runtime_overrides
from pixivfeed.utils import logger, setup_logging


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

    # 删空目录
    removed_dirs = 0
    # 倒序遍历：子目录先于父目录被检查
    for d in sorted([p for p in cache_dir.rglob("*") if p.is_dir()], reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
                removed_dirs += 1
        except OSError:
            pass

    logger.info(
        f"cleanup done: removed {removed_files} files "
        f"({removed_bytes / 1_000_000:.1f} MB), {removed_dirs} empty dirs"
    )


if __name__ == "__main__":
    cleanup(sys.argv[1] if len(sys.argv) >= 2 else "config.yaml")
