"""Sandbox: isolated code execution for LLM-generated code.

SECURITY WARNING: executing LLM-generated code is inherently risky.
- DockerSandbox (default) is the only safe option: network disabled,
  read-only filesystem, CPU/memory/time limits, no secrets mounted.
- SubprocessSandbox provides NO real isolation. Use only for trusted
  code or local development. Never expose it to untrusted input.

Interface:
    result = await sandbox.run(files, cmd, limits)

where files is a list of Artifact (path + content), cmd is the command
to execute inside the sandbox, and limits controls resources.

SANDBOX_ENGINE env var selects the engine: "docker" | "subprocess" | "none".
"none" skips execution and returns a SandboxResult with skipped=True.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .models import Artifact

log = logging.getLogger(__name__)

SANDBOX_ENGINE = os.environ.get("SANDBOX_ENGINE", "docker").lower()

# Docker image used for sandboxed execution. Override with SANDBOX_IMAGE.
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "python:3.12-slim")

# Interpreter used to run generated code inside the sandbox. Must name an
# interpreter that exists IN the sandbox environment, not on the host
# (sys.executable points at the host venv and usually does not exist in the
# container). Override with SANDBOX_PYTHON.
SANDBOX_PYTHON = os.environ.get("SANDBOX_PYTHON", "python3")

# Max bytes a sandboxed process may write to any single file (ulimit -f).
SANDBOX_FSIZE_MB = int(os.environ.get("SANDBOX_FSIZE_MB", "64"))

# Default resource limits.
DEFAULT_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT", "30"))  # seconds
DEFAULT_MEM_LIMIT = os.environ.get("SANDBOX_MEM_LIMIT", "256m")
DEFAULT_CPU_QUOTA = int(os.environ.get("SANDBOX_CPU_QUOTA", "50000"))  # 50% of 1 CPU

_MEM_SUFFIX = {"k": 1024, "m": 1024**2, "g": 1024**3}


def _mem_limit_bytes(value: str) -> int:
    """Parse a docker-style memory limit ("256m") into bytes."""
    v = value.strip().lower()
    if v and v[-1] in _MEM_SUFFIX:
        return int(v[:-1]) * _MEM_SUFFIX[v[-1]]
    return int(v)


@dataclass
class SandboxLimits:
    timeout: int = DEFAULT_TIMEOUT
    mem_limit: str = DEFAULT_MEM_LIMIT
    cpu_quota: int = DEFAULT_CPU_QUOTA


@dataclass
class SandboxResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    skipped: bool = False  # True when SANDBOX_ENGINE=none
    engine: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.skipped

    def as_context(self) -> str:
        """Format the execution result for injection into a reviewer prompt."""
        if self.skipped:
            return "(sandbox disabled - code was not executed)"
        status = "SUCCESS" if self.success else f"FAILED (exit {self.exit_code})"
        if self.timed_out:
            status = "TIMED OUT"
        parts = [f"=== Execution result: {status} ==="]
        if self.stdout.strip():
            parts.append(f"stdout:\n{self.stdout[:2000]}")
        if self.stderr.strip():
            parts.append(f"stderr:\n{self.stderr[:1000]}")
        return "\n".join(parts)


class Sandbox(ABC):
    """Abstract sandbox interface (Strategy pattern, OCP-compliant)."""

    @abstractmethod
    async def run(
        self,
        files: list[Artifact],
        cmd: str,
        limits: SandboxLimits | None = None,
    ) -> SandboxResult: ...


# ---------------------------------------------------------------------------
# DockerSandbox: the safe default
# ---------------------------------------------------------------------------


class DockerSandbox(Sandbox):
    """Execute code in a throwaway Docker container.

    Security invariants:
    - --network none       : no outbound network
    - --read-only          : root filesystem read-only
    - --tmpfs /tmp         : writable tmpfs for the workdir
    - --memory             : hard memory cap
    - --cpu-quota          : CPU cap
    - --cap-drop ALL       : no Linux capabilities
    - --no-new-privileges  : prevent privilege escalation
    - --pids-limit         : fork-bomb guard
    - --user               : unprivileged (host uid) inside the container
    - no secrets mounted   : caller must not pass secrets in Artifact content
    """

    def __init__(self, image: str = SANDBOX_IMAGE) -> None:
        self.image = image

    def _docker_args(self, tmpdir: str, lim: SandboxLimits, cmd: str) -> list[str]:
        """Build the docker run argv. Pure: unit-testable without Docker.

        Hardening (keep in sync with the class docstring):
        - --network none            : no outbound network
        - --read-only               : root filesystem read-only
        - --cap-drop ALL            : drop every Linux capability
        - --security-opt no-new-privileges : block privilege escalation
        - --pids-limit              : fork-bomb guard
        - --user <host-uid:gid>     : unprivileged container user (also lets it
          traverse the 0700 temp dir once DAC_OVERRIDE is dropped)
        - --tmpfs /tmp              : writable tmpfs for /tmp only
        - --memory / --cpu-quota    : resource caps
        - workspace mounted :ro     : LLM files are inputs, never writable
        - no secrets passed         : caller must not embed secrets in Artifacts
        """
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--memory",
            lim.mem_limit,
            "--cpu-quota",
            str(lim.cpu_quota),
            "-v",
            f"{tmpdir}:/workspace:ro",
            "-w",
            "/workspace",
            self.image,
            *shlex.split(cmd),
        ]

    async def run(
        self,
        files: list[Artifact],
        cmd: str,
        limits: SandboxLimits | None = None,
    ) -> SandboxResult:
        if not shutil.which("docker"):
            log.warning("Docker not found; falling back to skipped sandbox result")
            return SandboxResult(skipped=True, engine="docker-missing")

        lim = limits or SandboxLimits()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write files into the temp dir.
            for f in files:
                dest = Path(tmpdir) / f.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(f.content, encoding="utf-8")

            docker_cmd = self._docker_args(tmpdir, lim, cmd)

            log.debug("DockerSandbox: %s", " ".join(docker_cmd))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *docker_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(), timeout=lim.timeout
                    )
                    return SandboxResult(
                        stdout=stdout_b.decode("utf-8", errors="replace"),
                        stderr=stderr_b.decode("utf-8", errors="replace"),
                        exit_code=proc.returncode or 0,
                        engine="docker",
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    return SandboxResult(timed_out=True, exit_code=-1, engine="docker")
            except Exception as exc:  # noqa: BLE001
                log.warning("DockerSandbox failed: %s", exc)
                return SandboxResult(stderr=str(exc), exit_code=-1, engine="docker-error")


# ---------------------------------------------------------------------------
# SubprocessSandbox: NOT SAFE - local dev / trusted code only
# ---------------------------------------------------------------------------


class SubprocessSandbox(Sandbox):
    """Execute code in a local subprocess with a temporary working directory.

    Resource-bounded but NOT isolated: shell ulimits cap address space,
    CPU seconds and per-file size, and the caller-side timeout kills stuck
    runs. The process can still read host files, open network sockets and
    see inherited environment. Suitable only for trusted code or local
    development. Do NOT use with untrusted LLM-generated code.
    """

    def _wrap_cmd(self, cmd: str, limits: SandboxLimits) -> str:
        """Wrap cmd in `sh -c` with ulimit guards, then exec the payload.

        - ulimit -v : address space in KiB; 2x the docker-style limit because
          RLIMIT_AS counts reserved virtual memory (CPython reserves more
          than resident).
        - ulimit -t : cumulative CPU seconds derived from timeout x cpu_quota.
        - ulimit -f : max file size in 512-byte blocks.
        Tokens are re-quoted with shlex.quote so nothing is interpreted twice.
        """
        as_kib = max(_mem_limit_bytes(limits.mem_limit) * 2 // 1024, 65536)
        cpu_seconds = max(1, int(limits.timeout * limits.cpu_quota / 100000))
        fsize_blocks = SANDBOX_FSIZE_MB * 2048
        inner = " ".join(shlex.quote(t) for t in shlex.split(cmd))
        return (
            f"ulimit -v {as_kib} && "
            f"ulimit -t {cpu_seconds} && "
            f"ulimit -f {fsize_blocks} && "
            f"exec {inner}"
        )

    async def run(
        self,
        files: list[Artifact],
        cmd: str,
        limits: SandboxLimits | None = None,
    ) -> SandboxResult:
        lim = limits or SandboxLimits()
        log.warning(
            "SubprocessSandbox: resource-limited but NO isolation - "
            "use DockerSandbox for untrusted code"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            for f in files:
                dest = Path(tmpdir) / f.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(f.content, encoding="utf-8")

            try:
                proc = await asyncio.create_subprocess_exec(
                    "/bin/sh",
                    "-c",
                    self._wrap_cmd(cmd, lim),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tmpdir,
                )
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(), timeout=lim.timeout
                    )
                    return SandboxResult(
                        stdout=stdout_b.decode("utf-8", errors="replace"),
                        stderr=stderr_b.decode("utf-8", errors="replace"),
                        exit_code=proc.returncode or 0,
                        engine="subprocess",
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    return SandboxResult(timed_out=True, exit_code=-1, engine="subprocess")
            except Exception as exc:  # noqa: BLE001
                log.warning("SubprocessSandbox failed: %s", exc)
                return SandboxResult(stderr=str(exc), exit_code=-1, engine="subprocess-error")


# ---------------------------------------------------------------------------
# NoSandbox: no execution (SANDBOX_ENGINE=none)
# ---------------------------------------------------------------------------


class NoSandbox(Sandbox):
    async def run(
        self,
        files: list[Artifact],
        cmd: str,
        limits: SandboxLimits | None = None,
    ) -> SandboxResult:
        return SandboxResult(skipped=True, engine="none")


# ---------------------------------------------------------------------------
# Factory + module-level instance
# ---------------------------------------------------------------------------


def _make(engine: str) -> Sandbox:
    if engine == "docker":
        return DockerSandbox()
    if engine == "subprocess":
        return SubprocessSandbox()
    if engine == "none":
        return NoSandbox()
    log.warning("Unknown SANDBOX_ENGINE %r; defaulting to 'none'", engine)
    return NoSandbox()


# Module-level instance - can be replaced in tests via monkeypatching.
default: Sandbox = _make(SANDBOX_ENGINE)


async def run(
    files: list[Artifact],
    cmd: str,
    limits: SandboxLimits | None = None,
) -> SandboxResult:
    """Convenience function: run using the module-level default sandbox."""
    return await default.run(files, cmd, limits)
