"""Skills loader: inject domain-specific expertise into agent prompts.

A skill is a Markdown file under skills/<name>/SKILL.md. It is loaded
only when a Role references it by name (YAGNI: never loaded otherwise).

Skills are prepended to the prompt as stable, cacheable content so they
contribute to prefix-cache hits on providers that support it.

Directory layout:
  skills/
    coding/SKILL.md      # general coding best practices
    review/SKILL.md      # security / code review heuristics
    sre/SKILL.md         # SRE runbooks and infra patterns
    pentest/SKILL.md     # pentest / CTF methodology

Usage:
    from src.skills import load_skills
    content = load_skills(["sre", "coding"])  # -> concatenated Markdown
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).parent.parent / "skills"


def load(name: str) -> str:
    """Load a single skill by name. Returns empty string if not found."""
    path = _SKILLS_DIR / name / "SKILL.md"
    if not path.exists():
        log.warning("skill %r not found at %s", name, path)
        return ""
    return path.read_text(encoding="utf-8")


def load_skills(names: list[str]) -> str:
    """Load and concatenate multiple skills, skipping missing ones."""
    parts = []
    for name in names:
        content = load(name)
        if content:
            parts.append(f"## Skill: {name}\n\n{content.strip()}")
    return "\n\n".join(parts)


def list_available() -> list[str]:
    """Return names of all available skills."""
    if not _SKILLS_DIR.exists():
        return []
    return sorted(p.parent.name for p in _SKILLS_DIR.glob("*/SKILL.md"))
