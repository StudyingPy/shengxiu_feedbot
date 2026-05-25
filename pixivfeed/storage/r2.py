"""S3 v4 签名实现（无新依赖，仅 hashlib + hmac + httpx）。

只实现这个项目用得到的 5 个 op：PUT / HEAD / GET / DELETE / ListObjectsV2。
不追求通用 S3 客户端——只覆盖 R2 + 我们的访问模式。

为什么不用 boto3：
- 我们只用 5 个简单 op，boto3 + botocore 装下来 ~15MB，依赖图复杂
- aiobotocore 异步支持额外引一个包
- 自己写 sigv4 ~150 行可控，跟 httpx 异步契合无缝

参考：AWS Signature V4 spec
https://docs.aws.amazon.com/general/latest/gr/sigv4_signing.html
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import urllib.parse as _up
import xml.etree.ElementTree as _ET
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..utils import logger


# ---------------------------------------------------------------------------
# sigv4
# ---------------------------------------------------------------------------


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def _canonical_uri(path: str) -> str:
    """对 URI path 做 sigv4 要求的二次 URL-encode（除了 /）。

    例如 key 含空格："my key.jpg" → "my%2520key.jpg"（空格 → %20 → %2520）
    我们的 key 都是 [a-zA-Z0-9_./-]，实际很少触发，但行为要正确。
    """
    if not path.startswith("/"):
        path = "/" + path
    return _up.quote(path, safe="/-_.~")


def _canonical_query(params: dict[str, str] | None) -> str:
    if not params:
        return ""
    items = sorted(params.items())
    return "&".join(f"{_up.quote(k, safe='-_.~')}={_up.quote(v, safe='-_.~')}" for k, v in items)


@dataclass
class _R2Cred:
    endpoint: str       # https://<acct>.r2.cloudflarestorage.com（无尾斜杠）
    region: str
    access_key: str
    secret_key: str
    bucket: str


def _sign_request(
    cred: _R2Cred, *,
    method: str,
    key: str,                           # 不带 bucket 前缀的 object key
    payload: bytes,
    content_type: str | None = None,
    query: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """构造 sigv4 签名后的 URL + headers。"""
    host = cred.endpoint.split("//", 1)[1]   # "acct.r2.cloudflarestorage.com"
    canonical_path = _canonical_uri(f"/{cred.bucket}/{key}" if key else f"/{cred.bucket}")

    now = _dt.datetime.now(tz=_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload_hash = _sha256_hex(payload)

    headers: dict[str, str] = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if content_type:
        headers["content-type"] = content_type

    signed_headers_list = sorted(headers.keys())
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in signed_headers_list)
    signed_headers = ";".join(signed_headers_list)

    canonical_request = "\n".join([
        method,
        canonical_path,
        _canonical_query(query),
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{date_stamp}/{cred.region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])

    signing_key = _signing_key(cred.secret_key, date_stamp, cred.region, "s3")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={cred.access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    qs = _canonical_query(query)
    url = f"{cred.endpoint}{canonical_path}" + (f"?{qs}" if qs else "")
    return url, headers


# ---------------------------------------------------------------------------
# R2Client
# ---------------------------------------------------------------------------


@dataclass
class R2Object:
    """ListObjectsV2 一条 entry。"""
    key: str
    size: int
    last_modified: _dt.datetime    # tz-aware UTC


_CONTENT_TYPE_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def _guess_content_type(key: str) -> str:
    suffix = Path(key).suffix.lower()
    return _CONTENT_TYPE_BY_EXT.get(suffix, "application/octet-stream")


class R2ListIncomplete(Exception):
    """list_all 中途因为网络/HTTP 失败终止——已扫到的 keys 不能当全集用。

    携带 partial_keys 供未来 best-effort 场景调用，但当前所有 caller 都
    应当忽略 partial 数据：LRU 跳过本轮 evict，stats 沿用旧 snapshot。
    """

    def __init__(
        self, *, partial_keys: list[R2Object], scanned_pages: int, cause: object,
    ):
        self.partial_keys = partial_keys
        self.scanned_pages = scanned_pages
        self.cause = cause
        super().__init__(
            f"R2 list_all incomplete after {scanned_pages} page(s): {cause}"
        )


class R2Client:
    """瘦壳 S3-compat 客户端，仅覆盖本项目用到的 op。

    所有 method 是 async；底层 httpx.AsyncClient 复用连接池。
    用于 storage 子系统，不暴露给 publisher 之外的层。
    """

    def __init__(
        self, *,
        endpoint: str,
        region: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        custom_domain: str,
        prefix: str = "",
        timeout: float = 60.0,
    ):
        self._cred = _R2Cred(
            endpoint=endpoint.rstrip("/"),
            region=region,
            access_key=access_key_id,
            secret_key=secret_access_key,
            bucket=bucket,
        )
        self._public_base = custom_domain.rstrip("/")
        # prefix 规范化：去首尾空格 + 去首斜杠 + 若非空则强制以 "/" 结尾。
        # 所有出入 R2 的 key 都走 _normalize_key/_strip_prefix，调用方传"相对 key"即可。
        self.prefix = self._normalize_prefix(prefix)
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        p = (prefix or "").strip().lstrip("/")
        if p and not p.endswith("/"):
            p += "/"
        return p

    def _normalize_key(self, relative_key: str) -> str:
        """把相对 key 拼上 prefix，得到 absolute key。

        relative_key 形如 "pixiv/12345/0.jpg"；prefix 形如 "proj/" 或 ""。
        调用方永远不传 absolute key——避免双重拼接。
        """
        return f"{self.prefix}{relative_key.lstrip('/')}"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, http2=True, follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def public_url(self, relative_key: str) -> str:
        """relative_key 对应的公开访问 URL（CF 自定义域名，含 prefix）。"""
        absolute = self._normalize_key(relative_key)
        return f"{self._public_base}/{absolute}"

    async def head_object(self, relative_key: str) -> dict[str, str] | None:
        """HEAD 检查对象是否存在；返回 response headers 或 None。"""
        absolute = self._normalize_key(relative_key)
        url, headers = _sign_request(self._cred, method="HEAD", key=absolute, payload=b"")
        client = await self._get_client()
        try:
            resp = await client.request("HEAD", url, headers=headers)
        except Exception as e:
            logger.warning(f"R2 HEAD {absolute} failed: {e}")
            return None
        if resp.status_code == 200:
            return dict(resp.headers)
        if resp.status_code == 404:
            return None
        logger.warning(f"R2 HEAD {absolute} → HTTP {resp.status_code}")
        return None

    async def put_file(self, relative_key: str, local_path: Path) -> bool:
        """把本地文件 PUT 到 R2。返回是否成功。"""
        try:
            data = local_path.read_bytes()
        except OSError as e:
            logger.warning(f"R2 put_file: cannot read {local_path}: {e}")
            return False
        absolute = self._normalize_key(relative_key)
        return await self._put_bytes_absolute(absolute, data, _guess_content_type(absolute))

    async def put_bytes(
        self, relative_key: str, data: bytes, content_type: str | None = None,
    ) -> bool:
        absolute = self._normalize_key(relative_key)
        return await self._put_bytes_absolute(
            absolute, data, content_type or _guess_content_type(absolute),
        )

    async def _put_bytes_absolute(
        self, absolute_key: str, data: bytes, content_type: str,
    ) -> bool:
        url, headers = _sign_request(
            self._cred, method="PUT", key=absolute_key, payload=data, content_type=content_type,
        )
        client = await self._get_client()
        try:
            resp = await client.put(url, headers=headers, content=data)
        except Exception as e:
            logger.warning(f"R2 PUT {absolute_key} failed: {e}")
            return False
        if 200 <= resp.status_code < 300:
            return True
        logger.warning(
            f"R2 PUT {absolute_key} → HTTP {resp.status_code}: {resp.text[:200]}"
        )
        return False

    async def delete_object(self, absolute_key: str) -> bool:
        """删除一个 absolute key（list_all 返回的 R2Object.key 即 absolute）。

        不做 prefix 拼接——避免对已含 prefix 的 key 二次拼接成 "proj/proj/..."。
        """
        url, headers = _sign_request(self._cred, method="DELETE", key=absolute_key, payload=b"")
        client = await self._get_client()
        try:
            resp = await client.delete(url, headers=headers)
        except Exception as e:
            logger.warning(f"R2 DELETE {absolute_key} failed: {e}")
            return False
        if resp.status_code in (200, 204, 404):
            return True
        logger.warning(f"R2 DELETE {absolute_key} → HTTP {resp.status_code}")
        return False

    async def list_all(self) -> list[R2Object]:
        """ListObjectsV2 全分页扫，返回所有 (key, size, last_modified)。

        扫描范围由 self.prefix 决定（构造时配置，调用方不再传 prefix——避免与配置漂移）。
        返回的 R2Object.key 是 absolute key（含 prefix），可直接喂给 delete_object。

        失败处理：任一页网络/HTTP 异常 → 抛 R2ListIncomplete，**不返回部分结果**。
        调用方必须显式 catch；LRU 跳过本轮 evict，stats 沿用旧 snapshot。
        """
        results: list[R2Object] = []
        continuation: str | None = None
        scanned_pages = 0
        client = await self._get_client()
        while True:
            query: dict[str, str] = {"list-type": "2", "max-keys": "1000"}
            if self.prefix:
                query["prefix"] = self.prefix
            if continuation:
                query["continuation-token"] = continuation
            url, headers = _sign_request(
                self._cred, method="GET", key="", payload=b"", query=query,
            )
            try:
                resp = await client.get(url, headers=headers)
            except Exception as e:
                raise R2ListIncomplete(
                    partial_keys=results, scanned_pages=scanned_pages, cause=e,
                ) from e
            if resp.status_code != 200:
                raise R2ListIncomplete(
                    partial_keys=results,
                    scanned_pages=scanned_pages,
                    cause=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )
            scanned_pages += 1
            results.extend(_parse_list_objects_v2(resp.text))
            cont = _extract_continuation(resp.text)
            if cont is None:
                break
            continuation = cont
        return results


# ---------------------------------------------------------------------------
# XML helpers (ListObjectsV2)
# ---------------------------------------------------------------------------


_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _parse_list_objects_v2(xml_text: str) -> list[R2Object]:
    out: list[R2Object] = []
    try:
        root = _ET.fromstring(xml_text)
    except _ET.ParseError as e:
        logger.warning(f"R2 LIST XML parse failed: {e}")
        return out
    for content in root.findall(f"{_S3_NS}Contents"):
        key_el = content.find(f"{_S3_NS}Key")
        size_el = content.find(f"{_S3_NS}Size")
        lm_el = content.find(f"{_S3_NS}LastModified")
        if key_el is None or size_el is None or lm_el is None:
            continue
        try:
            size = int(size_el.text or "0")
        except ValueError:
            size = 0
        # LastModified 形如 2026-05-15T03:40:00.000Z
        try:
            lm = _dt.datetime.strptime(lm_el.text or "", "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            try:
                lm = _dt.datetime.strptime(lm_el.text or "", "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
        lm = lm.replace(tzinfo=_dt.timezone.utc)
        out.append(R2Object(key=key_el.text or "", size=size, last_modified=lm))
    return out


def _extract_continuation(xml_text: str) -> str | None:
    try:
        root = _ET.fromstring(xml_text)
    except _ET.ParseError:
        return None
    truncated_el = root.find(f"{_S3_NS}IsTruncated")
    if truncated_el is None or (truncated_el.text or "").lower() != "true":
        return None
    cont = root.find(f"{_S3_NS}NextContinuationToken")
    return cont.text if cont is not None else None


# ---------------------------------------------------------------------------
# 高级辅助：批量上传 + LRU 清理
# ---------------------------------------------------------------------------


async def upload_files_concurrent(
    client: R2Client,
    items: list[tuple[str, Path]],     # (key, local_path)
    *,
    concurrency: int = 8,
    on_progress: callable | None = None,   # async (done, total) -> None
) -> dict[str, bool]:
    """并发批量 PUT。返回 {key: ok}。

    单个失败不抛——调用方按 dict 决定 fallback 策略。
    on_progress(done, total) 每完成一个文件触发一次（done 含失败的，反映总进度）。
    """
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, bool] = {}
    total = len(items)
    done_lock = asyncio.Lock()
    done_count = 0

    async def _one(key: str, path: Path) -> None:
        nonlocal done_count
        async with sem:
            ok = await client.put_file(key, path)
        results[key] = ok
        if on_progress is not None:
            async with done_lock:
                done_count += 1
                try:
                    await on_progress(done_count, total)
                except Exception:
                    logger.exception("upload_files_concurrent on_progress raised; suppressed")

    await asyncio.gather(*(_one(k, p) for k, p in items), return_exceptions=False)
    return results


async def lru_evict_to_target(
    client: R2Client,
    *,
    high_watermark_bytes: int,
    low_watermark_bytes: int,
    objects: list[R2Object] | None = None,
) -> tuple[int, int, list[str]]:
    """如果当前用量 > high_watermark，按 LastModified 升序删到 <= low_watermark。

    返回 `(删除文件数, 释放字节数, 已删 absolute_key 列表)`。
    低于 high_watermark 时 `(0, 0, [])`。
    deleted_keys 用于联动失效 telegraph_cache（见 storage/cache_keymap.py）。

    扫描范围由 client.prefix 决定（构造时配置）；不再接受 prefix 参数避免与配置漂移。
    objects 已知时（调用方刚扫过）跳过 list_all 复用——LRU loop 本来就先扫
    list_all 算 stats 缓存，没必要再扫一次。

    若 objects=None 且 list_all 抛 R2ListIncomplete → 向上传播，调用方决定如何处理
    （正确做法：跳过本轮 evict）。绝不在部分扫描结果上做 LRU 决策——会误删 hot key。
    """
    if low_watermark_bytes >= high_watermark_bytes:
        raise ValueError("low_watermark must be < high_watermark")

    if objects is None:
        objects = await client.list_all()
    total = sum(o.size for o in objects)
    if total <= high_watermark_bytes:
        return (0, 0, [])

    objects = sorted(objects, key=lambda o: o.last_modified)   # 最旧排前面（不破坏入参）
    removed_files = 0
    freed = 0
    deleted_keys: list[str] = []
    for o in objects:
        if total <= low_watermark_bytes:
            break
        # o.key 是 list_all 返回的 absolute key（已含 prefix），直接喂 delete_object
        ok = await client.delete_object(o.key)
        if ok:
            removed_files += 1
            freed += o.size
            total -= o.size
            deleted_keys.append(o.key)
        else:
            logger.warning(f"R2 LRU: failed to delete {o.key}; skipping")
    return (removed_files, freed, deleted_keys)


@dataclass
class R2StatsSnapshot:
    """LRU loop 扫一次 list_all 后存进 bot_data 的快照。

    /stats system 读 bot_data["r2_stats"] 立即返回（毫秒级），不每次都
    重扫——一次 list_all 在 80GB 量级要 200+ 次 API 调用 ~20 秒，吃免费额度。
    """
    scanned_at: _dt.datetime         # tz-aware UTC
    total_bytes: int
    object_count: int
    oldest_at: _dt.datetime | None   # 最早上传的对象时间，None 表示空 bucket
    newest_at: _dt.datetime | None


def stats_from_objects(objects: list[R2Object]) -> R2StatsSnapshot:
    """从一次 list_all 结果聚合 stats 快照。"""
    if not objects:
        return R2StatsSnapshot(
            scanned_at=_dt.datetime.now(tz=_dt.timezone.utc),
            total_bytes=0, object_count=0,
            oldest_at=None, newest_at=None,
        )
    return R2StatsSnapshot(
        scanned_at=_dt.datetime.now(tz=_dt.timezone.utc),
        total_bytes=sum(o.size for o in objects),
        object_count=len(objects),
        oldest_at=min(o.last_modified for o in objects),
        newest_at=max(o.last_modified for o in objects),
    )


__all__ = [
    "R2Client",
    "R2ListIncomplete",
    "R2Object",
    "R2StatsSnapshot",
    "stats_from_objects",
    "upload_files_concurrent",
    "lru_evict_to_target",
]
