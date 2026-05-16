"""共享 R2 上传 + URL 解析 helper（PR-3）。

抽自 telegraph._resolve_image_urls，让 gallery / novel / EH / zip2tph 共用同一份
"本地路径 → R2/fallback URL" 决策逻辑。

不在这里做的：
- node tree 渲染（telegraph publisher / novel publisher 各自负责）
- gallery vs novel 的图片组织差异（来源、缓存目录、命名规则）
- progress hook 包装（caller 提供 on_status / on_progress）

调用方契约：
- 传 ResolveItem(r2_key, local_path, fallback_url) 列表
- r2_key 是"相对 key"——R2Client 会自动拼 prefix（PR-1 引入）
- 返回 ResolveResult.urls 与输入 items 一一对应
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..storage import R2Client, upload_files_concurrent
from ..utils import logger


# ---------------------------------------------------------------------------
# Fallback reason 枚举（与 publisher.telegraph.FallbackReason 保持一致）
# ---------------------------------------------------------------------------


class FallbackReason:
    NONE = ""
    R2_DISABLED = "r2_disabled"
    SIZE_GUARD_SKIPPED = "size_guard_skipped"
    R2_BATCH_FAILED = "r2_batch_failed"
    R2_PARTIAL = "r2_partial"
    LOCAL_FILE_MISSING = "local_file_missing"


@dataclass
class ResolveItem:
    """单张图片的上传输入。

    r2_key: 相对 key（如 'pixiv/12345/0.jpg'），不含 prefix。R2Client 会自动拼接。
    local_path: 本地文件路径。文件不存在时跳过上传、走 fallback。
    fallback_url: R2 不可用时的回退 URL（nginx 反代或 placeholder）。
    """

    r2_key: str
    local_path: Path
    fallback_url: str


@dataclass
class ResolveResult:
    urls: list[str]                # 与输入 items 一一对应
    r2_ok_count: int
    fallback_count: int
    fallback_reason: str           # 见 FallbackReason 枚举


async def resolve_image_urls(
    r2_client: R2Client | None,
    items: list[ResolveItem],
    *,
    r2_enabled: bool,
    force_r2: bool = False,
    size_guard_bytes: int = 0,
    concurrency: int = 8,
    on_status=None,                # async (text) -> None；进度文本回调
) -> ResolveResult:
    """把图片决定上传 R2 还是走 fallback，统一返回最终 URL 列表。

    失败策略（保留 telegraph 原有"R2 故障不阻塞发布"语义）：
      - R2 未启用 / client 缺失 → 全部 fallback；reason=R2_DISABLED（仅 total>0 时）
      - 护栏跳过 → 全部 fallback；reason=SIZE_GUARD_SKIPPED
      - 整批 except → 全部 fallback；reason=R2_BATCH_FAILED
      - 部分图失败 / 文件缺失 → 该图 fallback，其余照常；
        混合 → R2_PARTIAL，纯缺失 → LOCAL_FILE_MISSING

    size_guard_bytes = 0 关闭护栏。force_r2=True 时绕过护栏。
    """
    total = len(items)
    if r2_client is None or not r2_enabled:
        reason = FallbackReason.R2_DISABLED if total > 0 else FallbackReason.NONE
        return ResolveResult(
            urls=[it.fallback_url for it in items],
            r2_ok_count=0,
            fallback_count=total,
            fallback_reason=reason,
        )

    # 算总字节 + 标记缺失文件 / 缺 r2_key 的项
    total_bytes = 0
    skip_idx: set[int] = set()              # 不参与 R2 上传的索引（缺文件 / 缺 key）
    missing_idx: list[int] = []             # 仅"文件缺失"的索引（用于 reason 判定）
    for idx, it in enumerate(items):
        if not it.r2_key:
            # 调用方无法推导 r2_key（如 zip2tph 没显式 set、或本地路径不在 cache_dir）
            # → 永远走 fallback，绝不能把空 key PUT 到 bucket 根 / prefix/ 上覆盖别人。
            skip_idx.add(idx)
            continue
        try:
            total_bytes += it.local_path.stat().st_size
        except OSError:
            missing_idx.append(idx)
            skip_idx.add(idx)

    if not force_r2 and size_guard_bytes > 0 and total_bytes > size_guard_bytes:
        logger.info(
            f"R2 size guard skipped: {total_bytes / 1024**3:.2f} GB > "
            f"{size_guard_bytes / 1024**3:.2f} GB"
        )
        return ResolveResult(
            urls=[it.fallback_url for it in items],
            r2_ok_count=0,
            fallback_count=total,
            fallback_reason=FallbackReason.SIZE_GUARD_SKIPPED,
        )

    # 过滤掉跳过项（缺文件 / 缺 r2_key），剩下的进 upload batch
    upload_items: list[tuple[str, Path]] = []   # (r2_key, local_path)
    for idx, it in enumerate(items):
        if idx in skip_idx:
            continue
        upload_items.append((it.r2_key, it.local_path))

    if not upload_items:
        reason = (
            FallbackReason.LOCAL_FILE_MISSING if missing_idx
            else FallbackReason.R2_BATCH_FAILED
        )
        return ResolveResult(
            urls=[it.fallback_url for it in items],
            r2_ok_count=0,
            fallback_count=total,
            fallback_reason=reason,
        )

    if on_status is not None:
        try:
            await on_status(f"⏳ 上传 R2 (0/{len(upload_items)})")
        except Exception:
            logger.exception("resolve_image_urls on_status raised; suppressed")

    async def _r2_progress(done: int, t: int) -> None:
        if on_status is None:
            return
        try:
            await on_status(f"⏳ 上传 R2 ({done}/{t})")
        except Exception:
            logger.exception("resolve_image_urls on_status raised; suppressed")

    try:
        results = await upload_files_concurrent(
            r2_client, upload_items, concurrency=concurrency, on_progress=_r2_progress,
        )
    except Exception as e:
        logger.warning(f"R2 batch upload raised: {e}; falling back to nginx for all items")
        return ResolveResult(
            urls=[it.fallback_url for it in items],
            r2_ok_count=0,
            fallback_count=total,
            fallback_reason=FallbackReason.R2_BATCH_FAILED,
        )

    # 合成最终 URL：上传成功 → R2 公开 URL；失败/缺失/缺 key → fallback
    urls: list[str] = []
    ok_count = 0
    for idx, it in enumerate(items):
        if idx not in skip_idx and results.get(it.r2_key, False):
            urls.append(r2_client.public_url(it.r2_key))
            ok_count += 1
        else:
            urls.append(it.fallback_url)
    fallback_count = total - ok_count
    if fallback_count == 0:
        reason = FallbackReason.NONE
    elif missing_idx and ok_count + len(missing_idx) >= total:
        # 唯一失败原因是本地文件缺失（缺 r2_key 的也视作 "未尝试上传"，但
        # 它本质是 "调用方没准备好"，不归类为 LOCAL_FILE_MISSING）
        reason = FallbackReason.LOCAL_FILE_MISSING
    else:
        reason = FallbackReason.R2_PARTIAL
    logger.info(
        f"resolve_image_urls: {ok_count}/{total} R2 ok, "
        f"{fallback_count} fallback ({len(missing_idx)} missing); reason={reason!r}"
    )
    return ResolveResult(
        urls=urls,
        r2_ok_count=ok_count,
        fallback_count=fallback_count,
        fallback_reason=reason,
    )


__all__ = ["FallbackReason", "ResolveItem", "ResolveResult", "resolve_image_urls"]
