"""Pydantic schemas shared across the pipeline."""

import posixpath
from typing import Literal

from pydantic import BaseModel, field_validator

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["security", "correctness", "performance", "style", "maintainability"]

_SEVERITIES = ("critical", "high", "medium", "low")
_CATEGORIES = ("security", "correctness", "performance", "style", "maintainability")


def _normalize(value: str, allowed: tuple[str, ...], default: str) -> str:
    """Lower/trim a model-provided enum value, mapping anything unknown to a
    safe default. Keeps ingestion resilient: a sloppy label never aborts a run.
    """
    v = (value or "").strip().lower()
    return v if v in allowed else default


def sanitize_path(raw: str) -> str:
    """Normalize a model-provided file path to a safe relative POSIX path.

    Defends against zip-slip and absolute writes: backslashes become slashes,
    drive letters and leading slashes are stripped, and the path is normalized
    and checked to stay within the archive root. Raises ValueError on a path
    that cannot be made safe (so the caller can skip it).
    """
    p = (raw or "").strip().replace("\\", "/")
    if not p:
        raise ValueError("empty path")
    # Strip a Windows drive prefix like "C:".
    if len(p) >= 2 and p[1] == ":":
        p = p[2:]
    p = p.lstrip("/")
    # Normalize . and .. segments.
    norm = posixpath.normpath(p)
    if norm in (".", "") or norm.startswith("../") or norm == ".." or norm.startswith("/"):
        raise ValueError(f"unsafe path: {raw!r}")
    return norm


class Usage(BaseModel):
    """One LLM call's accounting. cost is None when the transport does not
    report one (Anthropic bills tokens, no per-call cost on this gateway)."""

    step: str = ""  # coder | reviewer:<name> | consensus | lead | chat
    transport: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    latency_ms: int = 0


class CostSummary(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0  # sum of known per-call costs
    cost_known: bool = False  # False when no call reported a cost


class Issue(BaseModel):
    title: str
    severity: Severity = "medium"
    category: Category = "correctness"
    location: str = ""
    description: str = ""

    @field_validator("severity", mode="before")
    @classmethod
    def _norm_severity(cls, v: str) -> str:
        return _normalize(v, _SEVERITIES, "medium")

    @field_validator("category", mode="before")
    @classmethod
    def _norm_category(cls, v: str) -> str:
        return _normalize(v, _CATEGORIES, "correctness")


class Review(BaseModel):
    reviewer: str
    ok: bool = True
    error: str = ""
    issues: list[Issue] = []
    overall: str = ""


class ConsensusIssue(BaseModel):
    title: str
    severity: Severity = "medium"
    category: Category = "correctness"
    description: str = ""
    flagged_by: list[str] = []
    consensus_score: float = 0.0  # flagged_by / panel size

    @field_validator("severity", mode="before")
    @classmethod
    def _norm_severity(cls, v: str) -> str:
        return _normalize(v, _SEVERITIES, "medium")

    @field_validator("category", mode="before")
    @classmethod
    def _norm_category(cls, v: str) -> str:
        return _normalize(v, _CATEGORIES, "correctness")


class ConsensusReport(BaseModel):
    panel: list[str] = []
    issues: list[ConsensusIssue] = []
    summary: str = ""


class Artifact(BaseModel):
    """One file of a multi-file solution. `path` is a safe relative POSIX path
    (validated against zip-slip); `language` is a hint for syntax highlighting.
    """

    path: str
    language: str = ""
    content: str = ""

    @field_validator("path", mode="before")
    @classmethod
    def _safe_path(cls, v: str) -> str:
        return sanitize_path(v)


class SandboxResult(BaseModel):
    """Execution result from the sandbox (optional, populated when sandbox=true)."""
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    skipped: bool = False
    engine: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.skipped


class PipelineResult(BaseModel):
    spec: str
    code: str
    language: str = ""
    reviews: list[Review] = []
    consensus: ConsensusReport
    verdict: str = ""  # APPROVE | APPROVE_WITH_CHANGES | REJECT
    final_code: str = ""
    rationale: str = ""
    files: list[Artifact] = []  # multi-file solution; empty for single-file
    rag_sources: list[dict] = []  # injected RAG chunks: {source, chunk_idx, score}
    execution: SandboxResult | None = None  # populated when sandbox=true
    usages: list[Usage] = []
    cost_summary: CostSummary = CostSummary()
