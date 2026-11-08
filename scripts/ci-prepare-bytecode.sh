#!/usr/bin/env bash
# Fill the shared bytecode cache before a sharded pytest run on a CI runner.
#
# Every xdist worker imports, rewrites and compiles the whole tier before
# pytest-split keeps its shard. Without a written cache each of the four
# workers repeats that work, about two minutes on every shard. The cache lives
# in PYTHONPYCACHEPREFIX, outside the checkout, so no test sees __pycache__.
# The test step keeps PYTHONDONTWRITEBYTECODE and only reads this cache.
#
# A module that this step misses is rewritten at collection, as before; the
# cache never decides which tests run.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: ${0##*/} <python-version>" >&2
  exit 2
fi
: "${PYTHONPYCACHEPREFIX:?set PYTHONPYCACHEPREFIX to the shared cache directory}"
unset PYTHONDONTWRITEBYTECODE

run=(uv run --frozen --python "$1")

# Interpreter, dependencies and project sources, in parallel processes. The code
# goes through -c, not stdin: from 3.12 compileall's process pool re-imports
# __main__ from its file, and stdin has none.
"${run[@]}" python -c '
import compileall
import sysconfig

paths = sysconfig.get_paths()
for root in (paths["stdlib"], paths["purelib"], "src", "benchmarks"):
    compileall.compile_dir(root, quiet=2, workers=0)
'

# Test modules with pytest's assertion rewriting: one disjoint share per CPU.
# The tier (EXOMEM_TEST_TIER) and ignores match the test step's collection.
parts=$(nproc)
pids=()
for ((part = 0; part < parts; part++)); do
  EXOMEM_COLLECT_PART="$part/$parts" "${run[@]}" python -m pytest --collect-only -q \
    -p no:cacheprovider -p no:xdist --ignore="$EXOMEM_TUI_TEST_DIRECTORY" >/dev/null 2>&1 &
  pids+=("$!")
done
for pid in "${pids[@]}"; do
  # A collection error here reappears, with its message, in the test step.
  wait "$pid" || true
done
