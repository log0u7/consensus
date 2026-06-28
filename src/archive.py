"""Build a downloadable archive from a list of in-memory files.

The LLM never compresses anything: it emits a file tree (path + content) and
this module packs it server-side. Supported formats use the Python stdlib
(zip, tar, tar.gz, tar.bz2, tar.xz) plus 7z via py7zr.

Paths are re-sanitized here (defence in depth) so a crafted path cannot escape
the archive root.
"""

import io
import tarfile
import time
import zipfile

from . import config
from .models import Artifact, sanitize_path

# format -> (media_type, file extension)
FORMATS: dict[str, tuple[str, str]] = {
    "zip": ("application/zip", "zip"),
    "tar": ("application/x-tar", "tar"),
    "tar.gz": ("application/gzip", "tar.gz"),
    "tar.bz2": ("application/x-bzip2", "tar.bz2"),
    "tar.xz": ("application/x-xz", "tar.xz"),
    "7z": ("application/x-7z-compressed", "7z"),
}

_TAR_MODE = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}


def _validated(files: list[Artifact]) -> list[tuple[str, bytes]]:
    if not files:
        raise ValueError("no files to archive")
    if len(files) > config.MAX_ARCHIVE_FILES:
        raise ValueError(f"too many files ({len(files)} > {config.MAX_ARCHIVE_FILES})")
    out: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        path = sanitize_path(f.path)  # raises on unsafe path
        data = f.content.encode("utf-8")
        total += len(data)
        if total > config.MAX_ARCHIVE_BYTES:
            raise ValueError("archive too large")
        out.append((path, data))
    return out


def build_archive(files: list[Artifact], fmt: str, root: str = "project") -> bytes:
    """Pack files into the requested format, nested under `root/`. Returns the
    archive bytes. Raises ValueError on an unknown format or unsafe input."""
    if fmt not in FORMATS:
        raise ValueError(f"unsupported format: {fmt}")
    entries = _validated(files)
    buf = io.BytesIO()
    mtime = time.time()

    if fmt == "zip":
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, data in entries:
                zf.writestr(f"{root}/{path}", data)
    elif fmt.startswith("tar"):
        # mode is one of w/w:gz/w:bz2/w:xz; cast for the typed overloads.
        with tarfile.open(fileobj=buf, mode=_TAR_MODE[fmt]) as tf:  # type: ignore[call-overload]
            for path, data in entries:
                info = tarfile.TarInfo(name=f"{root}/{path}")
                info.size = len(data)
                info.mtime = int(mtime)
                tf.addfile(info, io.BytesIO(data))
    elif fmt == "7z":
        import py7zr  # imported lazily so the rest works without it

        with py7zr.SevenZipFile(buf, "w") as zf:
            for path, data in entries:
                zf.writef(io.BytesIO(data), f"{root}/{path}")

    return buf.getvalue()
