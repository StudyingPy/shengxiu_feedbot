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
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

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

    def public_url(self, key: str) -> str:
        """key 对应的公开访问 URL（CF 自定义域名）。"""
        return f"{self._public_base}/{key.lstrip('/')}"

    async def head_object(self, key: str) -> dict[str, str] | None:
        """HEAD 检查对象是否存在；返回 response headers 或 None。"""
        url, headers = _sign_request(self._cred, method="HEAD", key=key, payload=b"")
        client = await self._get_client()
        try:
            resp = await client.request("HEAD", url, headers=headers)
        except Exception as e:
            logger.warning(f"R2 HEAD {key} failed: {e}")
            return None
        if resp.status_code == 200:
            return dict(resp.headers)
        if resp.status_code == 404:
            return None
        logger.warning(f"R2 HEAD {key} → HTTP {resp.status_code}")
        return None

    async def put_file(self, key: str, local_path: Path) -> bool:
        """把本地文件 PUT 到 R2。返回是否成功。"""
        try:
            data = local_path.read_bytes()
        except OSError as e:
            logger.warning(f"R2 put_file: cannot read {local_path}: {e}")
            return False
        return await self._put_bytes(key, data, _guess_content_type(key))

    async def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> bool:
        return await self._put_bytes(key, data, content_type or _guess_content_type(key))

    async def _put_bytes(self, key: str, data: bytes, content_type: str) -> bool:
        url, headers = _sign_request(
            self._cred, method="PUT", key=key, payload=data, content_type=content_type,
        )
        client = await self._get_client()
        try:
            resp = await client.put(url, headers=headers, content=data)
        except Exception as e:
            logger.warning(f"R2 PUT {key} failed: {e}")
            return False
        if 200 <= resp.status_code < 300:
            return True
        logger.warning(
            f"R2 PUT {key} → HTTP {resp.status_code}: {resp.text[:200]}"
        )
        return False

    async def delete_object(self, key: str) -> bool:
        url, headers = _sign_request(self._cred, method="DELETE", key=key, payload=b"")
        client = await self._get_client()
        try:
            resp = await client.delete(url, headers=headers)
        except Exception as e:
            logger.warning(f"R2 DELETE {key} failed: {e}")
            return False
        if resp.status_code in (200, 204, 404):
            return True
        logger.warning(f"R2 DELETE {key} → HTTP {resp.status_code}")
        return False

    async def list_all(self, prefix: str = "") -> list[R2Object]:
        """ListObjectsV2 全分页扫，返回所有 (key, size, last_modified)。

        bucket 大时这步比较慢（每页 1000 个）。LRU 调用方应该缓存结果，别一直扫。
        """
        results: list[R2Object] = []
        continuation: str | None = None
        client = await self._get_client()
        while True:
            query: dict[str, str] = {"list-type": "2", "max-keys": "1000"}
            if prefix:
                query["prefix"] = prefix
            if continuation:
                query["continuation-token"] = continuation
            url, headers = _sign_request(
                self._cred, method="GET", key="", payload=b"", query=query,
            )
            try:
                resp = await client.get(url, headers=headers)
            except Exception as e:
                logger.warning(f"R2 LIST failed: {e}")
                break
            if resp.status_code != 200:
                logger.warning(f"R2 LIST → HTTP {resp.status_code}: {resp.text[:200]}")
                break
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
) -> dict[str, bool]:
    """并发批量 PUT。返回 {key: ok}。

    单个失败不抛——调用方按 dict 决定 fallback 策略。
    """
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, bool] = {}

    async def _one(key: str, path: Path) -> None:
        async with sem:
            results[key] = await client.put_file(key, path)

    await asyncio.gather(*(_one(k, p) for k, p in items), return_exceptions=False)
    return results


async def lru_evict_to_target(
    client: R2Client,
    *,
    high_watermark_bytes: int,
    low_watermark_bytes: int,
    prefix: str = "",
) -> tuple[int, int]:
    """如果当前用量 > high_watermark，按 LastModified 升序删到 <= low_watermark。

    返回 (删除文件数, 释放字节数)。低于 high_watermark 时 (0, 0)。
    """
    if low_watermark_bytes >= high_watermark_bytes:
        raise ValueError("low_watermark must be < high_watermark")

    objects = await client.list_all(prefix=prefix)
    total = sum(o.size for o in objects)
    if total <= high_watermark_bytes:
        return (0, 0)

    objects.sort(key=lambda o: o.last_modified)   # 最旧排前面
    removed_files = 0
    freed = 0
    for o in objects:
        if total <= low_watermark_bytes:
            break
        ok = await client.delete_object(o.key)
        if ok:
            removed_files += 1
            freed += o.size
            total -= o.size
        else:
            logger.warning(f"R2 LRU: failed to delete {o.key}; skipping")
    return (removed_files, freed)


__all__ = [
    "R2Client",
    "R2Object",
    "upload_files_concurrent",
    "lru_evict_to_target",
]
