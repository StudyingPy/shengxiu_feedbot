"""Pixiv Provider 单独测试 CLI。

用法：
    python -m pixivfeed.provider.pixiv.cli illust <PID> [--config PATH] [--meta-only]
    python -m pixivfeed.provider.pixiv.cli novel <NID> [--config PATH]
    python -m pixivfeed.provider.pixiv.cli publish-illust <PID> [--config PATH]
    python -m pixivfeed.provider.pixiv.cli publish-novel <NID> [--config PATH]
    python -m pixivfeed.provider.pixiv.cli url "<text containing pixiv links>"

不依赖 Bot，纯粹用于验证 Pixiv → Telegra.ph 的链路。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ...config import Config
from ...publisher import TelegraphPublisher
from ...storage import Database, RuntimeSettings
from ...utils import logger, setup_logging
from . import PixivProvider, extract_pixiv_refs
from .api import PixivAPIError
from .novel_publisher import publish_novel


async def build_provider_from_config(config_path: str) -> tuple[Config, PixivProvider, TelegraphPublisher, Database]:
    cfg = Config.load(config_path)
    setup_logging(level=cfg.logging.level, to_file=False)
    db = Database(cfg.storage.db_path)
    await db.connect()
    runtime_settings = RuntimeSettings(db)
    await runtime_settings.load()
    cfg.bind_runtime(runtime_settings)
    provider = PixivProvider(
        config=cfg,
        cache_dir=cfg.storage.cache_dir,
        public_base_url=cfg.publish.base_url,
    )
    publisher = TelegraphPublisher(cfg)
    return cfg, provider, publisher, db


async def cmd_illust(args: argparse.Namespace) -> int:
    _, provider, _, db = await build_provider_from_config(args.config)
    try:
        if args.meta_only:
            work = await provider.fetch_illust(args.pid)
            print(f"PID:        {work.pid}")
            print(f"Title:      {work.title}")
            print(f"Author:     {work.author} (uid={work.user_id})")
            print(f"Pages:      {work.page_count}")
            print(f"Type:       illust_type={work.illust_type} (ugoira={work.is_ugoira})")
            print(f"Restrict:   x_restrict={work.x_restrict} ({work.x_restrict_label or 'all-ages'})")
            print(f"AI:         ai_type={work.ai_type} ({work.ai_type_label or 'human'})")
            print(f"Tags:       {' '.join(work.tags)}")
            print(f"Stats:      {work.bookmark_count} bookmarks, {work.like_count} likes, {work.view_count} views")
            print(f"Created:    {work.create_date}")
            print(f"Description (first 200 chars): {work.description[:200]!r}")
            print()
            print("Image URLs (original):")
            for i, img in enumerate(work.images):
                print(f"  p{i}: {img.original}")
        else:
            result = await provider.fetch_and_download_illust(args.pid)
            print(f"Downloaded {len(result.images)} image(s) for PID {result.work.pid}: {result.work.title}")
            for d, public in zip(result.images, result.public_urls_original):
                size = d.original_path.stat().st_size
                tg_size = d.tgphoto_path.stat().st_size
                print(
                    f"  p{d.page_index}: original={d.original_path} ({size:,}B) "
                    f"-> tgphoto={d.tgphoto_path} ({tg_size:,}B)"
                )
                print(f"        public: {public}")
        return 0
    except PixivAPIError as e:
        logger.error(f"Pixiv API error: {e}")
        return 2
    finally:
        await db.close()


async def cmd_novel(args: argparse.Namespace) -> int:
    _, provider, _, db = await build_provider_from_config(args.config)
    try:
        novel = await provider.fetch_novel(args.nid)
        print(f"NID:        {novel.nid}")
        print(f"Title:      {novel.title}")
        print(f"Author:     {novel.author} (uid={novel.user_id})")
        print(f"Length:     {novel.text_length} chars")
        print(f"Tags:       {' '.join(novel.tags)}")
        if novel.series_title:
            print(f"Series:     {novel.series_title} (id={novel.series_id})")
        print(f"Cover URL:  {novel.cover_url}")
        print(f"Description (first 200 chars): {novel.description[:200]!r}")
        print(f"Content (first 300 chars): {novel.content[:300]!r}")
        return 0
    except PixivAPIError as e:
        logger.error(f"Pixiv API error: {e}")
        return 2
    finally:
        await db.close()


def cmd_url(args: argparse.Namespace) -> int:
    refs = extract_pixiv_refs(args.text)
    if not refs:
        print("No Pixiv references found.")
        return 1
    for r in refs:
        print(f"  kind={r.kind} id={r.id} raw={r.raw!r}")
    return 0


async def cmd_publish_illust(args: argparse.Namespace) -> int:
    cfg, provider, publisher, db = await build_provider_from_config(args.config)
    try:
        from .. import ParsedRef
        ref = ParsedRef(provider="pixiv", kind="illust", id=args.pid, raw=args.pid)
        gallery = await provider.fetch_and_download(ref)
        t = cfg.templates.illust
        pub = await publisher.publish_gallery(
            gallery,
            page_title_template=t.page_title,
            page_header_template=t.page_header,
            page_footer_template=t.page_footer,
        )
        print(f"PID {gallery.work_id}: {gallery.title} ({gallery.author})")
        print(f"Images: {pub.image_count}, pages on Telegra.ph: {pub.page_count}")
        for i, url in enumerate(pub.urls):
            print(f"  page {i + 1}: {url}")
        return 0
    except PixivAPIError as e:
        logger.error(f"Pixiv API error: {e}")
        return 2
    except Exception as e:
        logger.exception(f"Publish failed: {e}")
        return 3
    finally:
        await db.close()


async def cmd_publish_novel(args: argparse.Namespace) -> int:
    cfg, provider, publisher, db = await build_provider_from_config(args.config)
    try:
        novel, pub = await publish_novel(cfg, publisher, provider, args.nid)
        print(f"NID {novel.nid}: {novel.title} ({novel.author})")
        print(f"Length: {novel.text_length} chars; embedded images: {pub.image_count}")
        print(f"  url: {pub.primary_url}")
        return 0
    except PixivAPIError as e:
        logger.error(f"Pixiv API error: {e}")
        return 2
    except Exception as e:
        logger.exception(f"Publish failed: {e}")
        return 3
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="pixivfeed.provider.pixiv.cli")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_illust = sub.add_parser("illust", help="fetch and download an illust")
    p_illust.add_argument("pid")
    p_illust.add_argument("--meta-only", action="store_true", help="don't download images")

    p_novel = sub.add_parser("novel", help="fetch a novel (meta + content, no publish)")
    p_novel.add_argument("nid")

    p_url = sub.add_parser("url", help="extract Pixiv refs from text (no network)")
    p_url.add_argument("text")

    p_pub_illust = sub.add_parser("publish-illust", help="end-to-end: fetch + download + publish to Telegra.ph")
    p_pub_illust.add_argument("pid")

    p_pub_novel = sub.add_parser("publish-novel", help="end-to-end: fetch + download embeds + publish to Telegra.ph")
    p_pub_novel.add_argument("nid")

    args = parser.parse_args()

    if args.cmd == "illust":
        return asyncio.run(cmd_illust(args))
    if args.cmd == "novel":
        return asyncio.run(cmd_novel(args))
    if args.cmd == "url":
        return cmd_url(args)
    if args.cmd == "publish-illust":
        return asyncio.run(cmd_publish_illust(args))
    if args.cmd == "publish-novel":
        return asyncio.run(cmd_publish_novel(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
