#!/usr/bin/env bash
# Mutation testing harness (make mutate).
#
# mutmut 3 rejects this repo's `src.*`-prefixed test imports (hard assertion
# in mutmut.stats), so the run happens on a flattened copy in .mutmut-work/:
# src/ modules flattened to top-level modules with relative imports rewritten,
# tests copied with their imports rewritten, data dirs symlinked in.
# .mutmut-work persists so mutmut's cache makes re-runs incremental.
#
# Usage: make mutate   (runs the full suite per mutant; ~13 min for src/)
set -euo pipefail
cd "$(dirname "$0")/.."

WORK=.mutmut-work
MUTBIN="$(pwd)/.venv-dev/bin/mutmut"
PYBIN="$(pwd)/.venv-dev/bin/python"
[ -x "$MUTBIN" ] || {
	echo "mutmut not installed: run 'make dev-setup'"
	exit 1
}

rm -rf "$WORK"
mkdir -p "$WORK/src" "$WORK/tests"

cp src/*.py "$WORK/src/"

# Flatten relative imports in the copied modules: `from .x import` -> `from x import`,
# `from . import` -> `import`.
"$PYBIN" - <<'EOF'
import re
from pathlib import Path
for p in Path(".mutmut-work/src").glob("*.py"):
    s = p.read_text()
    s = re.sub(r"^from \. import ", "import ", s, flags=re.M)
    s = re.sub(r"from \. import ", "import ", s)
    s = re.sub(r"from \.([a-z_]+) import ", r"from \1 import ", s)
    p.write_text(s)
EOF

# Rewrite test imports and module-name string refs to the flat layout.
"$PYBIN" - <<'EOF'
import re
from pathlib import Path
for p in Path("tests").glob("*.py"):
    s = p.read_text()
    s = s.replace("from src import ", "import ")
    s = s.replace("from src.", "from ")
    s = s.replace('"src.', '"').replace("'src.", "'")
    s = re.sub(r"\bimport src\.([a-z_]+) as ", r"import \1 as ", s)
    s = re.sub(r"\bimport src\.([a-z_]+)\b", r"import \1", s)
    Path(".mutmut-work/tests", p.name).write_text(s)

# The flat copy renames modules (src.topologies -> topologies): the log-capture
# test pins the logger by module name.
obs = Path(".mutmut-work/tests/test_observability.py")
if obs.exists():
    s = obs.read_text()
    s = s.replace('caplog.at_level(logging.INFO, logger="src")', "caplog.at_level(logging.INFO)")
    obs.write_text(s)
EOF

cat >"$WORK/pyproject.toml" <<'EOF'
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-q"
pythonpath = ["src", "."]

[tool.mutmut]
source_paths = ["src/"]
pytest_add_cli_args_test_selection = ["tests/"]
EOF

ln -sfn "$(pwd)/teams" "$WORK/teams"
ln -sfn "$(pwd)/skills" "$WORK/skills"
ln -sfn "$(pwd)/docs-projet" "$WORK/docs-projet"

# mutmut runs from mutants/: give it the data dirs its imports expect.
mkdir -p "$WORK/mutants/src"
ln -sfn "$(pwd)/src/static" "$WORK/mutants/src/static"
ln -sfn "$(pwd)/teams" "$WORK/mutants/teams"
ln -sfn "$(pwd)/skills" "$WORK/mutants/skills"
ln -sfn "$(pwd)/docs-projet" "$WORK/mutants/docs-projet"

cd "$WORK"
export ZEN_API_KEY=dummy
"$MUTBIN" run
echo
echo "=== results summary ==="
"$MUTBIN" results | grep -Ev ': no tests' | head -40 || true
