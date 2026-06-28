"""Role and Team data model + YAML loader.

A Team is a named collection of Roles plus a topology name.
A Role describes one agent slot: which model, which skills/tools/rag to use,
whether to run code in a sandbox, and which providers to fall back to.

Teams are declared in teams/*.yaml and loaded on demand. The application
never hardcodes team structure; it loads a manifest and passes it to the
matching topology in topologies.py.

YAML schema (teams/<name>.yaml):
  topology: consensus | pipeline | loop
  sandbox: false            # team-level default (overridable per role)
  roles:
    coder:
      model: zen/deepseek-v3-0324
      fallback: [local]     # optional provider fallback list
      max_tokens: 8000      # optional; overrides config default
      skills: []            # skill names loaded from skills/
      tools: []             # MCP tool names (phase 5)
      rag_ns: ""            # RAG namespace (empty = disabled)
      sandbox: false        # run code after generation?
    reviewer:
      model: zen/qwen3-coder
      fanout: 3             # how many reviewer instances to spawn
      members:              # explicit member list (overrides fanout)
        - name: deepseek-coder
          model: zen/deepseek-v3-0324
        - name: qwen3-coder
          model: zen/qwen3-coder
      ...
    consensus:
      model: zen/deepseek-r1-0528
    lead:
      model: zen/deepseek-r1-0528
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Path to the teams directory (relative to this file's parent = src/ -> ../)
_TEAMS_DIR = Path(__file__).parent.parent / "teams"


@dataclass
class Role:
    name: str
    model: str                          # "provider/model-id"
    fallback: list[str] = field(default_factory=list)  # provider names
    max_tokens: int | None = None
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    rag_ns: str = ""
    sandbox: bool = False
    # For the reviewer role: explicit panel members (overrides config.PANEL).
    members: list[dict] | None = None


@dataclass
class Team:
    name: str
    topology: str                        # "consensus" | "pipeline" | "loop"
    roles: dict[str, Role]
    sandbox: bool = False               # team-level sandbox default


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

def _role_from_dict(name: str, d: dict, team_sandbox: bool) -> Role:
    members_raw = d.get("members")
    members: list[dict] | None = None
    if members_raw:
        members = [
            {"name": m["name"], "model": m["model"],
             "fallback": m.get("fallback", []),
             "max_tokens": m.get("max_tokens")}
            for m in members_raw
        ]
    return Role(
        name=name,
        model=d.get("model", ""),
        fallback=d.get("fallback", []),
        max_tokens=d.get("max_tokens"),
        skills=d.get("skills", []),
        tools=d.get("tools", []),
        rag_ns=d.get("rag_ns", ""),
        sandbox=d.get("sandbox", team_sandbox),
        members=members,
    )


def load(team_name: str) -> Team:
    """Load and parse a team manifest from teams/<team_name>.yaml.

    Raises FileNotFoundError if the file does not exist.
    Raises ValueError on a malformed manifest.
    """
    import yaml  # imported lazily: only needed when loading teams

    path = _TEAMS_DIR / f"{team_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Team manifest not found: {path}. "
            f"Available: {[p.stem for p in _TEAMS_DIR.glob('*.yaml')]}"
        )
    with path.open() as f:
        raw = yaml.safe_load(f)

    topology = raw.get("topology", "consensus")
    team_sandbox = bool(raw.get("sandbox", False))
    roles_raw = raw.get("roles", {})
    if not roles_raw:
        raise ValueError(f"Team {team_name!r}: no roles defined in {path}")

    roles = {
        role_name: _role_from_dict(role_name, role_data, team_sandbox)
        for role_name, role_data in roles_raw.items()
    }
    return Team(name=team_name, topology=topology, roles=roles, sandbox=team_sandbox)


def list_teams() -> list[str]:
    """Return the names of all available team manifests."""
    if not _TEAMS_DIR.exists():
        return []
    return sorted(p.stem for p in _TEAMS_DIR.glob("*.yaml"))
