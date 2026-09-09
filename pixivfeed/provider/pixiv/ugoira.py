"""Pixiv ugoira（动图）元数据解析与 ffmpeg 转换。

ugoira 是一个帧 ZIP + 每帧独立 delay（毫秒），不是 GIF。转换用 ffmpeg
concat demuxer 按帧顺序写 duration，保留原始逐帧时长，不能按固定 FPS 重排。

ffmpeg 是可选能力：检测不到时抛 UgoiraFFmpegMissingError，由 channel 层
降级。转换在临时目录进行，成功后 os.replace 原子发布。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_FRAMES = 10_000
MAX_TOTAL_DURATION_MS = 600_000        # 600s，挡住异常数据；Pixiv 作品通常在几十秒内
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_TOTAL_FRAME_BYTES = 512 * 1024 * 1024   # 未压缩展开上限，防 zip bomb
MAX_ANIMATION_BYTES = 49 * 1024 * 1024      # sendAnimation 上限 50 MiB，留 1 MiB
FFMPEG_TIMEOUT_SECONDS = 240

# 帧文件名形如 000000.jpg / 000000.png；只接受 basename，挡住任何路径分隔。
_FRAME_FILE_RE = re.compile(r"[A-Za-z0-9_-]+\.(?:jpg|jpeg|png)", re.IGNORECASE)


class UgoiraError(Exception):
    """帧包校验或转换失败。"""


class UgoiraValidationError(UgoiraError):
    """metadata 或帧包不合法。"""


class UgoiraFFmpegMissingError(UgoiraError):
    """找不到 ffmpeg；ugoira 不可用，其余功能不受影响。"""


@dataclass(frozen=True)
class UgoiraFrame:
    file: str
    delay_ms: int


@dataclass(frozen=True)
class UgoiraMeta:
    zip_url: str
    frames: list[UgoiraFrame]


def parse_ugoira_meta(body: dict[str, Any]) -> UgoiraMeta:
    """解析 /ajax/illust/{pid}/ugoira_meta 的 body：
    {"originalSrc"/"src": "...zip", "frames": [{"file", "delay"}, ...]}
    """
    zip_url = str(body.get("originalSrc") or body.get("src") or "")
    raw_frames = body.get("frames")
    if not zip_url:
        raise UgoiraValidationError("ugoira meta missing frame zip url")
    if not isinstance(raw_frames, list) or not raw_frames:
        raise UgoiraValidationError("ugoira meta has no frames")
    if len(raw_frames) > MAX_FRAMES:
        raise UgoiraValidationError(f"ugoira frame count {len(raw_frames)} exceeds {MAX_FRAMES}")

    frames: list[UgoiraFrame] = []
    for item in raw_frames:
        if not isinstance(item, dict):
            raise UgoiraValidationError("ugoira frame entry is not an object")
        name = str(item.get("file") or "")
        if not _FRAME_FILE_RE.fullmatch(name):
            raise UgoiraValidationError(f"invalid ugoira frame filename: {name!r}")
        delay = item.get("delay")
        # bool 是 int 子类，单独挡掉。
        if isinstance(delay, bool) or not isinstance(delay, int) or not 1 <= delay <= 60_000:
            raise UgoiraValidationError(f"invalid ugoira frame delay for {name!r}: {delay!r}")
        frames.append(UgoiraFrame(file=name, delay_ms=delay))

    total_ms = sum(f.delay_ms for f in frames)
    if total_ms > MAX_TOTAL_DURATION_MS:
        raise UgoiraValidationError(f"ugoira duration {total_ms}ms exceeds {MAX_TOTAL_DURATION_MS}ms")
    return UgoiraMeta(zip_url=zip_url, frames=frames)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _delays_centiseconds(frames: list[UgoiraFrame]) -> list[int]:
    # GIF 时间精度只有 1/100s；四舍五入到厘秒，每帧至少 1 厘秒。
    # Pixiv ugoira stores a delay per frame; replacing it with a fixed FPS
    # changes playback timing.
    # Prior art: gallery-dl ugoira metadata handling; concat+per-file duration
    # is the standard variable-delay-to-GIF approach.
    return [max(1, (f.delay_ms + 5) // 10) for f in frames]


def convert_ugoira(zip_path: Path, meta: UgoiraMeta, output_path: Path) -> None:
    """帧 ZIP + metadata → 循环 GIF。阻塞函数，调用方需包 asyncio.to_thread。"""
    delays_cs = _delays_centiseconds(meta.frames)

    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        raise UgoiraValidationError(f"invalid ugoira zip: {e}") from e

    # 先确认帧包可打开再检查可选能力，畸形数据不应被笼统报成缺 ffmpeg。
    if not ffmpeg_available():
        archive.close()
        raise UgoiraFFmpegMissingError("ffmpeg not found; ugoira conversion unavailable")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".ugoira-", dir=output.parent) as tmp:
        work = Path(tmp)
        with archive:
            infos: list[zipfile.ZipInfo] = []
            total = 0
            for frame in meta.frames:
                try:
                    info = archive.getinfo(frame.file)
                except KeyError:
                    raise UgoiraValidationError(
                        f"frame listed in metadata missing from zip: {frame.file}"
                    ) from None
                if info.is_dir() or info.file_size > MAX_FRAME_BYTES:
                    raise UgoiraValidationError(f"invalid or oversized ugoira frame: {frame.file}")
                total += info.file_size
                if total > MAX_TOTAL_FRAME_BYTES:
                    raise UgoiraValidationError("ugoira expanded frames exceed 512 MiB")
                infos.append(info)

            # 不用 extractall：只按 metadata 顺序取帧，落盘改成生成名，
            # concat 清单不引用 ZIP 内原始名字。
            lines = ["ffconcat version 1.0"]
            for index, (info, delay) in enumerate(zip(infos, delays_cs, strict=True)):
                name = f"frame{index:06d}{Path(info.filename).suffix.lower()}"
                with archive.open(info) as src, (work / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                lines.extend([
                    f"file {name}",
                    "option framerate 100",
                    f"duration {delay / 100:.2f}",
                ])

        (work / "frames.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        # 限到 640px + 单帧调色板，控制 ffmpeg 内存与 GIF 体积；不放大。
        filters = (
            "scale=w='min(640,iw)':h='min(640,ih)':force_original_aspect_ratio=decrease,"
            "split[a][b];[a]palettegen=stats_mode=single[p];[b][p]paletteuse=new=1"
        )
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-max_alloc", "67108864",
            "-f", "concat", "-safe", "0", "-protocol_whitelist", "file",
            "-i", str(work / "frames.txt"),
            "-vf", filters, "-fps_mode", "vfr", "-loop", "0",
            "-final_delay", str(delays_cs[-1]),
            str(work / "animation.gif"),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as e:
            raise UgoiraError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_SECONDS}s") from e
        except subprocess.CalledProcessError as e:
            raise UgoiraError(f"ffmpeg exited with code {e.returncode}") from e
        except OSError as e:
            raise UgoiraError(f"failed to run ffmpeg: {e}") from e

        result = work / "animation.gif"
        try:
            size = result.stat().st_size
        except OSError:
            raise UgoiraError("ffmpeg produced no output file") from None
        if not 0 < size <= MAX_ANIMATION_BYTES:
            raise UgoiraError(f"converted GIF is {size} bytes (limit {MAX_ANIMATION_BYTES})")

        # 转换成功后才替换，半成品永远不会成为缓存命中。
        os.replace(result, output)


__all__ = [
    "UgoiraError",
    "UgoiraValidationError",
    "UgoiraFFmpegMissingError",
    "UgoiraFrame",
    "UgoiraMeta",
    "parse_ugoira_meta",
    "convert_ugoira",
    "ffmpeg_available",
    "MAX_FRAMES",
    "MAX_TOTAL_DURATION_MS",
    "MAX_FRAME_BYTES",
    "MAX_TOTAL_FRAME_BYTES",
    "MAX_ANIMATION_BYTES",
    "FFMPEG_TIMEOUT_SECONDS",
]
