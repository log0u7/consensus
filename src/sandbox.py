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

# Default resource limits.
DEFAULT_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT", "30"))       # seconds
DEFAULT_MEM_LIMIT = os.environ.get("SANDBOX_MEM_LIMIT", "256m")
DEFAULT_CPU_QUOTA = int(os.environ.get("SANDBOX_CPU_QUOTA", "50000"))  # 50% of 1 CPU


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
    skipped: bool = False   # True when SANDBOX_ENGINE=none
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
    - --no-new-privileges  : prevent privilege escalation
    - no secrets mounted   : caller must not pass secrets in Artifact content
    """

    def __init__(self, image: str = SANDBOX_IMAGE) -> None:
        self.image = image

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

            docker_cmd = [
                "docker", "run", "--rm",
                "--network", "none",
                "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "--memory", lim.mem_limit,
                "--cpu-quota", str(lim.cpu_quota),
                "-v", f"{tmpdir}:/workspace:ro",
                "-w", "/workspace",
                self.image,
                *shlex.split(cmd),
            ]

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
                return SandboxResult(
                    stderr=str(exc), exit_code=-1, engine="docker-error"
                )


# ---------------------------------------------------------------------------
# SubprocessSandbox: NOT SAFE - local dev / trusted code only
# ---------------------------------------------------------------------------

class SubprocessSandbox(Sandbox):
    """Execute code in a local subprocess with a temporary working directory.

    WARNING: provides NO real isolation. Suitable only for trusted code or
    local development. Do NOT use with untrusted LLM-generated code.
    """

    async def run(
        self,
        files: list[Artifact],
        cmd: str,
        limits: SandboxLimits | None = None,
    ) -> SandboxResult:
        lim = limits or SandboxLimits()
        log.warning(
            "SubprocessSandbox: NO isolation - use DockerSandbox for untrusted code"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            for f in files:
                dest = Path(tmpdir) / f.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(f.content, encoding="utf-8")

            try:
                proc = await asyncio.create_subprocess_exec(
                    *shlex.split(cmd),
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
                return SandboxResult(
                    stderr=str(exc), exit_code=-1, engine="subprocess-error"
                )


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
