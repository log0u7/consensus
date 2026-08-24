"""Tests for src/sandbox.py.

DockerSandbox tests are skipped when Docker is unavailable.
SubprocessSandbox tests run offline (no provider needed).
"""

import sys

import pytest
from src.models import Artifact
from src.sandbox import (
    DockerSandbox,
    NoSandbox,
    SandboxLimits,
    SandboxResult,
    SubprocessSandbox,
)

# Use the running interpreter so tests work in any venv.
_PYTHON = sys.executable

# ---------------------------------------------------------------------------
# SandboxResult helpers
# ---------------------------------------------------------------------------


def test_sandbox_result_success():
    r = SandboxResult(exit_code=0)
    assert r.success is True


def test_sandbox_result_failure():
    r = SandboxResult(exit_code=1)
    assert r.success is False


def test_sandbox_result_timed_out():
    r = SandboxResult(timed_out=True, exit_code=-1)
    assert r.success is False


def test_sandbox_result_skipped():
    r = SandboxResult(skipped=True)
    assert r.success is False


def test_as_context_skipped():
    r = SandboxResult(skipped=True)
    assert "not executed" in r.as_context()


def test_as_context_success():
    r = SandboxResult(stdout="hello\n", exit_code=0, engine="subprocess")
    ctx = r.as_context()
    assert "SUCCESS" in ctx
    assert "hello" in ctx


def test_as_context_failure():
    r = SandboxResult(stderr="SyntaxError\n", exit_code=1, engine="subprocess")
    ctx = r.as_context()
    assert "FAILED" in ctx
    assert "SyntaxError" in ctx


def test_as_context_timeout():
    r = SandboxResult(timed_out=True, exit_code=-1, engine="docker")
    assert "TIMED OUT" in r.as_context()


# ---------------------------------------------------------------------------
# NoSandbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_sandbox_skips():
    s = NoSandbox()
    r = await s.run([], "echo hi")
    assert r.skipped is True
    assert r.engine == "none"


# ---------------------------------------------------------------------------
# SubprocessSandbox (offline, no Docker needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_sandbox_runs_python():
    s = SubprocessSandbox()
    files = [Artifact(path="hello.py", language="python", content='print("hello sandbox")')]
    r = await s.run(files, _PYTHON + " hello.py", SandboxLimits(timeout=10))
    assert r.exit_code == 0
    assert "hello sandbox" in r.stdout
    assert r.engine == "subprocess"


@pytest.mark.asyncio
async def test_subprocess_sandbox_captures_stderr():
    s = SubprocessSandbox()
    files = [Artifact(path="bad.py", language="python", content="raise ValueError('boom')")]
    r = await s.run(files, _PYTHON + " bad.py", SandboxLimits(timeout=10))
    assert r.exit_code != 0
    assert "ValueError" in r.stderr or "boom" in r.stderr


@pytest.mark.asyncio
async def test_subprocess_sandbox_timeout():
    s = SubprocessSandbox()
    files = [Artifact(path="sleep.py", language="python", content="import time; time.sleep(60)")]
    r = await s.run(files, _PYTHON + " sleep.py", SandboxLimits(timeout=1))
    assert r.timed_out is True
    assert r.exit_code == -1


# ---------------------------------------------------------------------------
# SubprocessSandbox resource limits (shell ulimit guards)
# ---------------------------------------------------------------------------


def test_mem_limit_bytes_parses_suffixes():
    from src.sandbox import _mem_limit_bytes

    assert _mem_limit_bytes("256m") == 256 * 1024**2
    assert _mem_limit_bytes("1g") == 1024**3
    assert _mem_limit_bytes("65536k") == 64 * 1024**2
    assert _mem_limit_bytes("1024") == 1024


def test_wrap_cmd_contains_ulimit_guards():
    s = SubprocessSandbox()
    wrapped = s._wrap_cmd(
        "python3 x.py", SandboxLimits(timeout=30, mem_limit="256m", cpu_quota=50000)
    )
    # AS cap: 2x mem_limit in KiB (512 MiB -> 524288 KiB)
    assert "ulimit -v 524288" in wrapped
    # CPU seconds: timeout x cpu_quota/100000 -> 30 * 0.5 = 15
    assert "ulimit -t 15" in wrapped
    # File size: 64 MB in 512-byte blocks
    assert "ulimit -f 131072" in wrapped
    assert "exec python3 x.py" in wrapped


def test_wrap_cmd_quotes_tokens():
    s = SubprocessSandbox()
    wrapped = s._wrap_cmd('py "my file.py"', SandboxLimits(timeout=10))
    assert "'my file.py'" in wrapped


@pytest.mark.asyncio
async def test_subprocess_sandbox_enforces_memory_cap():
    """A process allocating beyond the address-space cap must fail."""
    s = SubprocessSandbox()
    code = "b = bytearray(600 * 1024 * 1024)\nprint('allocated')"
    files = [Artifact(path="big.py", language="python", content=code)]
    r = await s.run(files, _PYTHON + " big.py", SandboxLimits(timeout=20))
    assert r.exit_code != 0


@pytest.mark.asyncio
async def test_subprocess_sandbox_enforces_file_size_cap():
    """Writing a file larger than SANDBOX_FSIZE_MB must fail (SIGXFSZ)."""
    s = SubprocessSandbox()
    code = (
        "with open('blob.bin', 'wb') as f:\n"
        "    f.write(b'x' * (70 * 1024 * 1024))\n"
        "print('written')"
    )
    files = [Artifact(path="writer.py", language="python", content=code)]
    r = await s.run(files, _PYTHON + " writer.py", SandboxLimits(timeout=30))
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# DockerSandbox (skipped when Docker is absent)
# ---------------------------------------------------------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402

docker_available = shutil.which("docker") is not None


def _docker_image_available(image: str) -> bool:
    """Return True only if the image exists locally (no pull)."""
    if not docker_available:
        return False
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


_SANDBOX_IMAGE = "python:3.12-slim"
docker_image_ready = _docker_image_available(_SANDBOX_IMAGE)


@pytest.mark.asyncio
@pytest.mark.skipif(not docker_image_ready, reason="Docker image not available locally")
async def test_docker_sandbox_runs_python():
    # Use "python" - the interpreter inside the container image, not the host venv.
    s = DockerSandbox()
    files = [Artifact(path="hi.py", language="python", content='print("docker sandbox")')]
    r = await s.run(files, "python hi.py", SandboxLimits(timeout=30))
    assert r.exit_code == 0
    assert "docker sandbox" in r.stdout
    assert r.engine == "docker"


@pytest.mark.asyncio
@pytest.mark.skipif(not docker_image_ready, reason="Docker image not available locally")
async def test_docker_sandbox_no_network():
    """The container must not reach the internet."""
    import json

    s = DockerSandbox()
    code = (
        "import urllib.request, json\n"
        "try:\n"
        "    urllib.request.urlopen('http://example.com', timeout=3)\n"
        "    print(json.dumps({'net': True}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'net': False, 'err': str(e)}))\n"
    )
    files = [Artifact(path="net.py", language="python", content=code)]
    r = await s.run(files, "python net.py", SandboxLimits(timeout=15))
    if r.stdout.strip():
        data = json.loads(r.stdout.strip())
        assert data.get("net") is False
