"""启动入口。

用法：
    python -m pixivfeed [config.yaml]

配置文件路径解析顺序：
    1. 命令行第一个参数
    2. 环境变量 PIXIVFEED_CONFIG
    3. ./config.yaml

启动顺序很关键：
    1. Config.load 读 yaml + env
    2. db.connect
    3. RuntimeSettings.load 把 SQLite 里的覆盖项加载进内存
    4. config.bind_runtime 把覆盖项应用到 dataclass
    5. build_registry：所有 Provider 已经能读到合并后的最终配置
    6. publisher.ensure_account（可能写回 yaml）
    7. build_application、polling
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from telegram import Update

from .channel.telegram.bot import init_bot_async, install_commands
from .config import Config
from .provider import ProviderRegistry
from .provider.ehentai import EHentaiProvider, ExHentaiProvider
from .provider.nhentai import NHentaiProvider
from .provider.pixiv import PixivProvider
from .publisher import TelegraphPublisher
from .storage import Database, RuntimeSettings
from .utils import logger, setup_logging


def resolve_config_path(argv: list[str]) -> Path:
    if len(argv) >= 2:
        return Path(argv[1])
    if env_path := os.environ.get("PIXIVFEED_CONFIG"):
        return Path(env_path)
    return Path("config.yaml")


def build_registry(config: Config) -> ProviderRegistry:
    """注册所有 Provider 实例。

    设计：永远把所有 Provider 都注册，由 Provider 自身的 can_handle()
    根据 config.collectors.{name}.enabled 实时决定是否接管 URL。
    这样运行时 /setting set collectors.xxx.enabled true 立刻生效，
    不需要重启。
    """
    registry = ProviderRegistry()

    # pixiv 永远启用（没 enabled 开关）
    registry.register(
        PixivProvider(
            config=config,
            cache_dir=config.storage.cache_dir,
            public_base_url=config.publish.base_url,
        )
    )

    # exhentai 必须在 e-hentai 之前（虽然 URL 不会冲突，保持有序便于调试）
    registry.register(
        ExHentaiProvider(
            cache_dir=config.storage.cache_dir,
            public_base_url=config.publish.base_url,
            config=config,
        )
    )
    registry.register(
        EHentaiProvider(
            cache_dir=config.storage.cache_dir,
            public_base_url=config.publish.base_url,
            config=config,
        )
    )
    registry.register(
        NHentaiProvider(
            cache_dir=config.storage.cache_dir,
            public_base_url=config.publish.base_url,
            config=config,
        )
    )

    return registry


async def async_main(config_path: Path) -> None:
    try:
        config = Config.load(config_path)
    except FileNotFoundError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        print("Hint: copy config.example.yaml to config.yaml and edit it.", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"[FATAL] Configuration error:\n{e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(
        level=config.logging.level,
        to_file=config.logging.to_file,
        file_path=config.logging.file_path,
    )
    logger.info(f"Loaded config from {config_path}")

    # DB
    db = Database(config.storage.db_path)
    await db.connect()

    # RuntimeSettings：从 SQLite 加载并覆盖 config
    runtime_settings = RuntimeSettings(db)
    await runtime_settings.load()
    config.bind_runtime(runtime_settings)
    if rt := runtime_settings.all():
        logger.info(f"Loaded {len(rt)} runtime setting overrides from SQLite")

    # 缓存目录
    Path(config.storage.cache_dir).mkdir(parents=True, exist_ok=True)
    logger.info(f"Image cache dir: {config.storage.cache_dir}")

    # Provider Registry
    registry = build_registry(config)

    # Telegraph
    publisher = TelegraphPublisher(config)
    await publisher.ensure_account()

    # Bot
    app = await init_bot_async(config, db, registry, publisher, runtime_settings)

    logger.success("Starting bot polling...")

    try:
        await app.initialize()
        await app.start()
        await install_commands(app, config)
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        stop_event = asyncio.Event()

        def _on_signal():
            logger.info("Received stop signal.")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            try:
                import signal as _sig
                loop.add_signal_handler(getattr(_sig, sig_name), _on_signal)
            except (NotImplementedError, AttributeError):
                pass

        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception:
            logger.exception("error during shutdown")
        await db.close()
        logger.info("Bye.")


def main() -> None:
    config_path = resolve_config_path(sys.argv)
    try:
        asyncio.run(async_main(config_path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
