import io
import tarfile
import zipfile

import pytest
from src import archive
from src.models import Artifact, sanitize_path


def test_sanitize_relative_ok():
    assert sanitize_path("a/b.py") == "a/b.py"
    assert sanitize_path("./x.txt") == "x.txt"
    assert sanitize_path("sub/../keep.py") == "keep.py"


def test_sanitize_backslash_and_drive():
    assert sanitize_path("C:\\win\\f.txt") == "win/f.txt"


def test_sanitize_rejects_traversal():
    for bad in ["../escape", "..", "", "a/../../b"]:
        with pytest.raises(ValueError):
            sanitize_path(bad)


def test_sanitize_relativizes_absolute():
    # Absolute path is defanged to a safe relative one (no escape).
    assert sanitize_path("/etc/passwd") == "etc/passwd"


FILES = [
    Artifact(path="roles/zabbix/tasks/main.yml", content="- name: install\n"),
    Artifact(path="README.md", content="# hi"),
]


def test_zip_roundtrip():
    data = archive.build_archive(FILES, "zip", root="project")
    z = zipfile.ZipFile(io.BytesIO(data))
    assert "project/README.md" in z.namelist()
    assert z.read("project/README.md").decode() == "# hi"


@pytest.mark.parametrize("fmt", ["tar", "tar.gz", "tar.bz2", "tar.xz"])
def test_tar_roundtrip(fmt):
    data = archive.build_archive(FILES, fmt, root="project")
    t = tarfile.open(fileobj=io.BytesIO(data))
    assert "project/README.md" in t.getnames()


def test_7z_builds():
    py7zr = pytest.importorskip("py7zr")
    data = archive.build_archive(FILES, "7z", root="project")
    z = py7zr.SevenZipFile(io.BytesIO(data))
    assert set(z.getnames()) == {"project/roles/zabbix/tasks/main.yml", "project/README.md"}


def test_unknown_format_rejected():
    with pytest.raises(ValueError):
        archive.build_archive(FILES, "rar")
