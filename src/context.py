"""Context builder: assemble the prompt context for an agent call.

Composes content in a stable prefix order to maximise provider cache hits:
  1. Skills (static, domain expertise - most stable)
  2. Tool definitions (static per session)
  3. RAG chunks (semi-static, keyed to the query)
  4. Task / spec (volatile - always last)

This order ensures the stable prefix is as long as possible, so the provider
can serve the system + skills + tools segment from its prefix cache.

Usage:
    ctx = await build(
        spec="write an Ansible role for nginx",
        role=team.roles["coder"],
        rag_hits=[...],          # optional, from rag.search()
        tool_definitions=[...],  # optional, from mcp_client.list_tools()
    )
    # ctx.system  -> full system prompt (skills + tools injected)
    # ctx.user    -> user message (RAG context + spec)
    # ctx.tokens_estimate -> rough char count
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .roles import Role
from .skills import load_skills

log = logging.getLogger(__name__)


@dataclass
class AgentContext:
    system: str
    user: str
    rag_sources: list[dict] = field(default_factory=list)

    @property
    def tokens_estimate(self) -> int:
        """Rough token estimate: chars / 4 (conservative, model-agnostic)."""
        return (len(self.system) + len(self.user)) // 4


def _format_tools(tool_defs: list[dict]) -> str:
    """Format MCP tool definitions for injection into the system prompt."""
    if not tool_defs:
        return ""
    lines = ["Available tools (call via JSON tool-use format):"]
    for t in tool_defs:
        lines.append(f"- {t['name']}: {t.get('description', '(no description)')}")
    return "\n".join(lines)


def _format_rag(hits: list[dict]) -> str:
    """Format RAG chunks for injection into the user message."""
    if not hits:
        return ""
    parts = ["Relevant context retrieved from the knowledge base:"]
    for h in hits:
        parts.append(f"[{h['source']}#{h.get('chunk_idx', 0)}]\n{h['content']}")
    return "\n\n".join(parts)


async def build(
    spec: str,
    role: Role,
    base_system: str = "",
    rag_hits: list[dict] | None = None,
    tool_definitions: list[dict] | None = None,
) -> AgentContext:
    """Build the agent context for a role + spec combination.

    Stable prefix order (for cache efficiency):
      system = base_system + skills_block + tools_block
      user   = rag_block + spec

    RAG is only fetched when role.rag_ns is set; callers may also pass
    pre-fetched hits via rag_hits.
    """
    hits: list[dict] = []

    # Fetch RAG if a namespace is configured and no pre-fetched hits provided.
    if role.rag_ns and not rag_hits:
        try:
            from . import rag
            hits = await rag.search(spec, k=3)
            log.debug("context builder: RAG retrieved %d chunk(s) for ns=%s", len(hits), role.rag_ns)
        except Exception as exc:  # noqa: BLE001
            log.warning("context builder: RAG skipped (%s)", exc)
    elif rag_hits:
        hits = rag_hits

    # Load skills (stable, cached between calls with the same role).
    skills_block = load_skills(role.skills) if role.skills else ""

    # Format tool definitions.
    tools_block = _format_tools(tool_definitions or [])

    # Assemble system prompt (stable prefix first).
    system_parts = [p for p in [base_system, skills_block, tools_block] if p]
    system = "\n\n".join(system_parts)

    # Assemble user message (volatile content last).
    rag_block = _format_rag(hits)
    user_parts = [p for p in [rag_block, spec] if p]
    user = "\n\n".join(user_parts)

    return AgentContext(system=system, user=user, rag_sources=hits)
