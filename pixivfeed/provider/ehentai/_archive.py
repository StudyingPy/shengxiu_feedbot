"""e-hentai/exhentai 的 archive 下载流水线。

流程：
1. GET 画廊主页，提取 Archive Download 对应的 archiver.php URL。
   新旧页面可能带 or= 参数，也可能只有 gid/token；两种都兼容。
2. GET archiver.php 拿到选择页；优先提交普通 Archive Download 表单：
   dltype=res|org + dlcheck=Download ... Archive。
3. 从返回页中提取临时 H@H archive URL。不同页面可能把链接放在：
   - JS document.location = "..."
   - 页面中的 .hath.network/archive 直链
   - “Click Here To Start Downloading” 链接
4. GET 这个链接（带 ?start=1）拿到 zip，流式落盘。
5. 解压到目标目录，删 zip。
6. 按字典序排序图片为 page_index。
"""

from __future__ import annotations

import asyncio
import re
import zipfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ...utils import logger
from ._modes import BASE_HEADERS, EHMode


# 画廊页里的 Archive Download URL。新版页面常见形式只有 gid/token，旧页面可能带 or=。
_ARCHIVER_URL_RE = re.compile(
    r"https?://[^'\"<>\s]+/archiver\.php\?gid=\d+(?:&amp;|&)token=[A-Za-z0-9]+(?:[^'\"<>\s]*)?",
    re.IGNORECASE,
)
_ARCHIVER_TOKEN_RE = re.compile(
    r"archiver\.php\?gid=\d+(?:&amp;|&)token=\w+(?:&amp;|&)or=([^'\"&\s]+)",
    re.IGNORECASE,
)

_JS_LOCATION_RE = re.compile(
    r"document\.location\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_HATH_ARCHIVE_RE = re.compile(
    r"['\"](https?://[^'\"]+?\.hath\.network/archive[^'\"]*)['\"]",
    re.IGNORECASE,
)
_ARCHIVE_ERROR_RE = re.compile(
    r"(You do not have enough .*?credits|insufficient.*?funds|not (allowed|available)|"
    r"Hath archive download.*?available|This gallery is not available for archive download)",
    re.IGNORECASE,
)
# 同 archive 链接被多个 IP 反复使用后，eh/ex 会锁定该 session。提示文案稳定。
_ARCHIVE_LOCKED_RE = re.compile(
    r"This archive session has been used from too many different locations",
    re.IGNORECASE,
)
# 解析 chooser 页面里的 "Estimated Size: <strong>1.77 GiB</strong>"，用来动态算超时。
# 同时存在 org 与 res 两块，按 mode 取对应那块。
_ESTIMATED_SIZE_RE = re.compile(
    r'name="dltype"\s+value="(org|res)"[\s\S]*?Estimated Size:.*?<strong>\s*([\d.]+)\s*([KMGT])iB\s*</strong>',
    re.IGNORECASE,
)


class ArchiveError(Exception):
    """archive 下载流程中的可识别失败。"""


class ArchiveLockedError(ArchiveError):
    """archive session 因多 IP 滥用被锁。

    handler 拿到这个异常时应该：
    1) 提示用户"该 session 已锁定"
    2) 不要自动重试 —— session 已废，重试同样会失败
    上层在抛出此异常前会自动 POST invalidate_sessions=1 清掉旧 session，
    用户下次发同一画廊时会拿到全新的 archive 链接。
    """


@dataclass
class ArchiveResult:
    """解压完成后的图片列表（按 page 顺序）。"""

    image_paths: list[Path]


def parse_estimated_size_bytes(html: str, mode: EHMode) -> int:
    """从 archiver 页面提取所选 mode 对应的预估文件大小（字节）。

    返回 0 表示没解析到。
    """
    target = "org" if mode == EHMode.ARCHIVE_ORG else "res"
    for m in _ESTIMATED_SIZE_RE.finditer(html):
        if m.group(1).lower() != target:
            continue
        try:
            n = float(m.group(2))
        except ValueError:
            continue
        unit = m.group(3).upper()
        mult = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(unit, 1)
        return int(n * mult)
    return 0


# 解析 chooser 页里 "Download Cost: <strong>NN GP</strong>" 或 "<strong>Free!</strong>"
_GP_COST_RE = re.compile(
    r'name="dltype"\s+value="(org|res)"[\s\S]*?Download Cost:.*?<strong>\s*(?:(\d+)\s*GP|(Free!?))\s*</strong>',
    re.IGNORECASE,
)


def parse_gp_cost(html: str, mode: EHMode) -> int:
    """从 archiver 页面提取所选 mode 对应的 GP 消耗。

    Free! 视为 0；解析不到也返回 0。
    页面没暴露免费 archive 配额数字，所以 Free 只能记 0。
    """
    target = "org" if mode == EHMode.ARCHIVE_ORG else "res"
    for m in _GP_COST_RE.finditer(html):
        if m.group(1).lower() != target:
            continue
        if m.group(2):
            try:
                return int(m.group(2))
            except ValueError:
                return 0
        return 0
    return 0


async def invalidate_archive_session(
    client: httpx.AsyncClient,
    host: str,
    gid: str,
    token: str,
) -> bool:
    """触发 archiver.php 上的 cancel/invalidate_sessions=1，让该画廊的 archive
    session 作废。下次重新点 Download 会拿到全新链接。

    返回 True 表示请求已发出（不强行解析结果，能发出就视为成功兜底）。
    """
    url = f"https://{host}/archiver.php?gid={gid}&token={token}"
    try:
        resp = await client.post(
            url,
            data={"invalidate_sessions": "1"},
            headers={**BASE_HEADERS, "Referer": url},
        )
        return resp.status_code in (200, 302)
    except Exception:
        return False


async def refresh_download_link(
    client: httpx.AsyncClient,
    host: str,
    gid: str,
    token: str,
) -> str | None:
    """已经申请过 archive、但拿到的 H@H 节点链接 404 没上线时调用：
    重新 GET archiver.php，eh/ex 通常会切换到 "The file was successfully
    prepared" 页面（download 链接通常已落到主站本地 /archive/...?start=1，更稳定）。

    返回新的下载链接（已 normalize 为完整 URL），找不到时返回 None。不消耗 archive 配额。
    """
    url = f"https://{host}/archiver.php?gid={gid}&token={token}"
    try:
        resp = await client.get(
            url,
            headers={**BASE_HEADERS, "Referer": f"https://{host}/g/{gid}/{token}"},
        )
    except Exception as e:
        logger.warning(f"refresh_download_link GET failed: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"refresh_download_link GET HTTP {resp.status_code}")
        return None
    href = _extract_download_link(resp.text, url, host)
    return href


def _normalize_url(url: str, base_url: str, host: str) -> str:
    """把 HTML/JS 中拿到的 URL 解码并补全。"""
    url = unescape(url).replace("amp;", "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return f"https://{host}{url}"
    return urljoin(base_url, url)


def _extract_archiver_url(gallery_html: str, album_url: str, host: str, gid: str, token: str) -> str:
    """从画廊页提取 archiver.php URL；提取失败时按 gid/token 构造。"""
    m = _ARCHIVER_URL_RE.search(gallery_html)
    if m:
        return _normalize_url(m.group(0), album_url, host)

    # 老式 or= token 兜底。
    tm = _ARCHIVER_TOKEN_RE.search(gallery_html)
    if tm:
        return f"https://{host}/archiver.php?gid={gid}&token={token}&or={tm.group(1)}"

    if "archive" not in gallery_html.lower():
        raise ArchiveError("archiver link not present on gallery page (archive may be disabled)")

    # 当前页面有时不把 or= 暴露出来，archiver.php?gid=...&token=... 本身就是入口。
    return f"https://{host}/archiver.php?gid={gid}&token={token}"


def _archive_forms_from_html(html: str, base_url: str, mode: EHMode) -> list[tuple[str, dict[str, str], str]]:
    """从 archiver 选择页里提取可提交的表单。

    优先普通 Archive Download 表单：dltype=org/res + dlcheck。
    H@H Downloader 的 hathdl_xres 作为兜底，不放在第一位。
    """
    soup = BeautifulSoup(html, "html.parser")
    target_dltype = "org" if mode == EHMode.ARCHIVE_ORG else "res"
    target_xres = "org" if mode == EHMode.ARCHIVE_ORG else "1280"

    forms: list[tuple[str, dict[str, str], str]] = []

    for form in soup.find_all("form"):
        inputs = form.find_all("input")
        data: dict[str, str] = {}
        for inp in inputs:
            name = inp.get("name")
            if not name:
                continue
            data[name] = inp.get("value", "")

        action = _normalize_url(form.get("action") or base_url, base_url, "")

        # 普通 Archive Download：这是公开实现里最常用的流程。
        if data.get("dltype") == target_dltype:
            if "dlcheck" not in data:
                data["dlcheck"] = (
                    "Download Original Archive"
                    if mode == EHMode.ARCHIVE_ORG
                    else "Download Resample Archive"
                )
            forms.insert(0, (action, data, "archive-form"))
            continue

        # H@H Downloader：作为兜底尝试。
        if "hathdl_xres" in data:
            data["hathdl_xres"] = target_xres
            forms.append((action, data, "hathdl-form"))

    if not forms:
        forms.append(
            (
                base_url,
                {
                    "dltype": target_dltype,
                    "dlcheck": (
                        "Download Original Archive"
                        if mode == EHMode.ARCHIVE_ORG
                        else "Download Resample Archive"
                    ),
                },
                "fallback-archive-form",
            )
        )

    return forms


def _extract_download_link(body: str, base_url: str, host: str) -> str | None:
    """从 POST 返回页中提取真正的临时 archive 下载 URL。"""
    text = unescape(body)

    # 老实现常见：<script>document.location = "https://...hath.network/archive..."</script>
    m = _JS_LOCATION_RE.search(text)
    if m:
        return _normalize_url(m.group(1), base_url, host)

    # 用户脚本常见：直接从 HTML 中搜 .hath.network/archive。
    m = _HATH_ARCHIVE_RE.search(text)
    if m:
        return _normalize_url(m.group(1), base_url, host)

    soup = BeautifulSoup(text, "html.parser")

    # 新旧页面可能是 “Click Here To Start Downloading” / “Start Downloading”。
    for a in soup.find_all("a", href=True):
        label = " ".join(a.get_text(" ", strip=True).split()).lower()
        href = a.get("href", "")
        if "hath.network/archive" in href or "start downloading" in label:
            return _normalize_url(href, base_url, host)

    # 旧代码曾经假设 id=db 是下载链接；实际有页面把 id=db 给了外层 div，
    # 所以这里只在它确实是 a[href] 时才用。
    a = soup.find("a", id="db", href=True)
    if a:
        return _normalize_url(a["href"], base_url, host)

    return None


async def fetch_archiver_token(
    client: httpx.AsyncClient,
    album_url: str,
) -> str:
    """从画廊主页取得 archiver.php URL。

    函数名保留为 fetch_archiver_token，是为了兼容现有调用；实际返回值现在优先是
    完整 archiver URL。request_archive 会同时兼容“完整 URL”和旧式 or-token。
    """
    resp = await client.get(album_url)
    if resp.status_code != 200:
        raise ArchiveError(f"GET {album_url} returned HTTP {resp.status_code}")

    # album_url 形如 https://exhentai.org/g/3493719/512efaaf9b
    m = re.search(r"https?://([^/]+)/g/(\d+)/([A-Za-z0-9]+)", album_url)
    if not m:
        raise ArchiveError(f"could not parse gallery id/token from album url: {album_url}")
    host, gid, token = m.group(1), m.group(2), m.group(3)
    return _extract_archiver_url(resp.text, album_url, host, gid, token)


async def request_archive(
    client: httpx.AsyncClient,
    host: str,
    gid: str,
    token: str,
    archiver_token: str,
    mode: EHMode,
) -> tuple[str, int, int]:
    """POST archiver.php，返回 (临时 zip 直链 URL, 预估字节数, GP 消耗)。

    预估字节数从 chooser 页的 "Estimated Size: 1.77 GiB" 解析；解析不到时返回 0。
    GP 消耗从 "Download Cost: NN GP" 解析；Free! 时返回 0。
    """
    if not mode.is_archive:
        raise ValueError(f"request_archive called with non-archive mode {mode}")

    # archiver_token 可能是新逻辑返回的完整 URL，也可能是旧式 or-token。
    if archiver_token.startswith("http://") or archiver_token.startswith("https://"):
        archiver_url = archiver_token
    else:
        archiver_url = f"https://{host}/archiver.php?gid={gid}&token={token}&or={archiver_token}"

    # 先 GET 选择页，以便按真实页面表单提交；也能建立和浏览器一致的流程。
    chooser = await client.get(
        archiver_url,
        headers={**BASE_HEADERS, "Referer": f"https://{host}/g/{gid}/{token}"},
    )
    if chooser.status_code != 200:
        raise ArchiveError(f"GET {archiver_url} returned HTTP {chooser.status_code}")

    # 优先识别 "session locked" —— 一旦命中就尝试 invalidate 旧 session 后抛专门异常
    if _ARCHIVE_LOCKED_RE.search(chooser.text):
        invalidated = await invalidate_archive_session(client, host, gid, token)
        logger.warning(
            f"[{host}/{gid}] archive session locked (too many IPs); "
            f"invalidate_sessions submitted={invalidated}"
        )
        raise ArchiveLockedError(
            "archive session locked (too many IPs); 已自动取消旧 session，"
            "请稍后重新提交本画廊以获取新的下载链接"
        )

    err = _ARCHIVE_ERROR_RE.search(chooser.text)
    if err:
        raise ArchiveError(f"archive denied: {err.group(0)[:120]}")

    estimated = parse_estimated_size_bytes(chooser.text, mode)
    gp_cost = parse_gp_cost(chooser.text, mode)

    forms = _archive_forms_from_html(chooser.text, archiver_url, mode)
    last_preview = ""

    for form_url, form, label in forms:
        resp = await client.post(
            form_url,
            data=form,
            headers={**BASE_HEADERS, "Referer": archiver_url},
        )
        if resp.status_code not in (200, 302):
            raise ArchiveError(f"POST {form_url} returned HTTP {resp.status_code}")

        body = resp.text
        if _ARCHIVE_LOCKED_RE.search(body):
            invalidated = await invalidate_archive_session(client, host, gid, token)
            logger.warning(
                f"[{host}/{gid}] archive session locked at POST stage; "
                f"invalidate_sessions submitted={invalidated}"
            )
            raise ArchiveLockedError(
                "archive session locked (too many IPs); 已自动取消旧 session，"
                "请稍后重新提交本画廊以获取新的下载链接"
            )
        err = _ARCHIVE_ERROR_RE.search(body)
        if err:
            raise ArchiveError(f"archive denied: {err.group(0)[:120]}")

        href = _extract_download_link(body, form_url, host)
        if href:
            logger.info(f"archive link parsed via {label}")
            return href, estimated, gp_cost

        # 保存一点诊断信息，但不要把整页打进异常。
        last_preview = " ".join(BeautifulSoup(body, "html.parser").get_text(" ", strip=True).split())[:300]

    raise ArchiveError(
        "archive page returned but download link not found; "
        f"tried {len(forms)} form(s). page preview: {last_preview!r}"
    )


async def download_archive_with_timeout(
    client: httpx.AsyncClient,
    zip_url: str,
    dest_zip: Path,
    timeout_seconds: int,
) -> None:
    """流式下载 zip，全程不能超 timeout。失败抛 ArchiveError。"""
    # 一些 archive 链接需要 ?start=1 才会真正启动 H@H 节点开始传输
    sep = "&" if "?" in zip_url else "?"
    fetch_url = f"{zip_url}{sep}start=1" if "start=" not in zip_url else zip_url

    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_zip.with_suffix(dest_zip.suffix + ".part")

    async def _do() -> None:
        async with client.stream("GET", fetch_url, headers=BASE_HEADERS) as resp:
            if resp.status_code != 200:
                raise ArchiveError(f"zip GET HTTP {resp.status_code}")
            ct = resp.headers.get("content-type", "")
            if "html" in ct.lower():
                # 服务端可能返回 HTML 错误页而不是 zip
                preview = (await resp.aread())[:200].decode("utf-8", errors="replace")
                raise ArchiveError(f"expected zip but got HTML: {preview!r}")
            with tmp.open("wb") as f:
                async for chunk in resp.aiter_bytes(64 * 1024):
                    f.write(chunk)
        tmp.replace(dest_zip)

    try:
        await asyncio.wait_for(_do(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        if tmp.exists():
            tmp.unlink()
        raise ArchiveError(f"archive download exceeded {timeout_seconds}s timeout")
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def extract_archive(zip_path: Path, dest_dir: Path) -> ArchiveResult:
    """解压 zip 到 dest_dir。"""
    dest_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = sorted(n for n in zf.namelist() if not n.endswith("/"))
        if not names:
            raise ArchiveError(f"archive {zip_path} contains no files")
        for name in names:
            # 防止 zip slip
            target = dest_dir / Path(name).name
            with zf.open(name) as src, target.open("wb") as dst:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)

    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    images = sorted(
        [p for p in dest_dir.iterdir() if p.suffix.lower() in image_exts],
        key=lambda p: p.name,
    )
    if not images:
        raise ArchiveError(f"archive {zip_path} has no recognizable images after extraction")

    logger.info(f"extracted {len(images)} images from {zip_path.name}")
    return ArchiveResult(image_paths=images)


__all__ = [
    "EHMode",
    "ArchiveError",
    "ArchiveLockedError",
    "ArchiveResult",
    "fetch_archiver_token",
    "request_archive",
    "download_archive_with_timeout",
    "extract_archive",
    "invalidate_archive_session",
    "parse_estimated_size_bytes",
    "parse_gp_cost",
    "refresh_download_link",
]
