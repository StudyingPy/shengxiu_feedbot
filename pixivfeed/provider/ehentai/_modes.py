"""eh/ex 共用常量与模式枚举。"""

from __future__ import annotations

from enum import Enum


class EHMode(str, Enum):
    """eh/ex 的四种抓取模式。

    - PAGE_SAMPLE   : 子页 <img id="img"> 的 sample 图。免 GP/Credits，分辨率被限制
    - PAGE_ORIGINAL : 子页"Download original"链接。会扣 GP/Credits
    - ARCHIVE_RES   : POST archiver.php dltype=res，1280x 重采样的 zip。免费配额
    - ARCHIVE_ORG   : POST archiver.php dltype=org，原始分辨率 zip。免费配额
    """

    PAGE_SAMPLE = "page_sample"
    PAGE_ORIGINAL = "page_original"
    ARCHIVE_RES = "archive_resample"
    ARCHIVE_ORG = "archive_original"

    @classmethod
    def all(cls) -> list[EHMode]:
        return [cls.PAGE_SAMPLE, cls.PAGE_ORIGINAL, cls.ARCHIVE_RES, cls.ARCHIVE_ORG]

    @property
    def label_zh(self) -> str:
        return {
            EHMode.PAGE_SAMPLE: "网页 · 显示图",
            EHMode.PAGE_ORIGINAL: "网页 · 原图",
            EHMode.ARCHIVE_RES: "归档 · 1280x",
            EHMode.ARCHIVE_ORG: "归档 · 原图",
        }[self]

    @property
    def is_archive(self) -> bool:
        return self in (EHMode.ARCHIVE_RES, EHMode.ARCHIVE_ORG)


_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": _DESKTOP_UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
}


__all__ = ["EHMode", "BASE_HEADERS"]
