from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")
from PIL import Image  # noqa: E402

from pixivfeed.channel.telegram.handlers import (  # noqa: E402
    _extract_image_archive,
    _inspect_image_archive,
    _safe_download_failure,
)
from pixivfeed.provider.ehentai._archive import ArchiveError  # noqa: E402


@pytest.fixture
def archive_limits():
    return SimpleNamespace(
        max_entries=20,
        max_uncompressed_size_gb=1,
        max_image_size_mb=5,
    )


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize("kind", ["zip", "tar.gz", "tar.bz2", "tar.xz"])
def test_extracts_zip_and_tar_family(tmp_path: Path, archive_limits, kind: str) -> None:
    payload = _png_bytes()
    archive = tmp_path / ("images." + kind)
    if kind == "zip":
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("nested/one.png", payload)
    else:
        mode = {"tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}[kind]
        with tarfile.open(archive, mode) as tf:
            info = tarfile.TarInfo("nested/one.png")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

    assert _inspect_image_archive(archive, archive_limits) == (1, len(payload))
    extracted = _extract_image_archive(archive, tmp_path / "out", archive_limits)
    assert len(extracted) == 1
    with Image.open(extracted[0]) as image:
        assert image.size == (2, 2)


def test_rejects_zip_slip_and_hides_local_path() -> None:
    assert "token" not in _safe_download_failure(PermissionError("/var/lib/bot/secret-token/file"))


def test_rejects_unsafe_member(tmp_path: Path, archive_limits) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escape.png", _png_bytes())
    with pytest.raises(ArchiveError):
        _inspect_image_archive(archive, archive_limits)
