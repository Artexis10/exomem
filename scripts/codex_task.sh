#!/usr/bin/env bash
# Codex worker lane runner — see "Codex worker protocol" in CLAUDE.md.
#
#   codex_task.sh template                          print a TASK.md brief template
#   codex_task.sh start <lane> <brief-file> [--profile <name>]
#                                                   create ../exomem-<lane> worktree from
#                                                   origin/main, install the brief, uv sync,
#                                                   run `codex exec` in it (foreground —
#                                                   background the whole command yourself)
#   codex_task.sh verify <worktree-dir>             run the merge gate in a lane worktree
#
# Safety: start/verify refuse to operate on anything that is not a *linked*
# worktree — the shared primary checkout can never be where a Codex lane starts.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(dirname "$SCRIPT_DIR")
PARENT=$(dirname "$REPO")

WORKER_PROMPT='You are an autonomous worker in an isolated git worktree. Read .task/TASK.md and implement it exactly — do not redesign, simplify the governed surface, or expand scope. Run the acceptance commands from the brief. Commit your work to the current branch; never push. Write .task/RESULT.md summarizing what changed, the test/benchmark output, and any deviations from the brief.'

die() { echo "codex_task: $*" >&2; exit 1; }

require_linked_worktree() {
  local dir=$1 gitdir
  gitdir=$(git -C "$dir" rev-parse --git-dir 2>/dev/null) || die "$dir is not a git checkout"
  case "$gitdir" in
    */worktrees/*) ;;
    *) die "$dir is the primary checkout (or not a linked worktree) — refusing" ;;
  esac
}

cmd_template() {
  cat <<'EOF'
# Task: <one-line objective>

## Source of truth
<OpenSpec change path (openspec/changes/<name>/) or exact spec inline.>
Implement exactly this — do NOT redesign or simplify the governed surface.

## Scope (allowlist)
Expected files to touch:
- <path>
Out of scope: everything else. Touching tests/golden/, gate thresholds, or CI
config is a gate failure unless listed above.

## Acceptance criteria (exact commands)
- `uv run python -m pytest -q` green
- <benchmark command> meets <threshold>

On Windows and macOS, append `--ignore=tests/test_latency_gate.py`. CI runs that
file on ubuntu only (see cross-platform.yml); it builds an 8k-page fixture and
times out elsewhere. If it is your only failure, you are green -- say so in
RESULT.md and do not spend the lane chasing it.

## Deliverable
Commit to the current branch (do not push). Write .task/RESULT.md with a
summary, acceptance-command output, and any new dependency you believe is
needed (do not add it yourself).
EOF
}

# Every brief in this repo ends in "run the tests", so a sandbox that cannot run
# them is not a safer worker -- it is a worker that cannot do the job, and it
# does not find that out until it has spent an hour trying. That is exactly what
# happened on 2026-08-16: one lane burned roughly an hour and 18M tokens under
# `-s workspace-write` and produced zero commits.
#
# The failure is not a permission that can be granted. On Windows, Codex's
# `sandbox = "unelevated"` runs the worker under a restricted token, and exomem
# deliberately hardens its state directories to a private DACL naming the real
# user SID. The restricted token is then denied by design -- pytest's own
# `shutil.rmtree` of its tmpdir dies with WinError 5. Widening `writable_roots`
# moves the path but not the ACL, so it cannot be configured away.
#
# Two more deliverables were structurally impossible under that sandbox, which
# is why a compliant worker read afterwards like a disobedient one. A linked
# worktree's gitdir lives under the primary's `.git/worktrees/`, outside the
# sandbox, so a worker could not create `index.lock` and could not commit the
# work the brief asked it to commit. And `~/.cache/uv` and every PyPI route
# were blocked, so an acceptance command naming `uvx` could never pass.
#
# The cost, stated rather than implied: full access does NOT confine a worker
# to its worktree. `require_linked_worktree` decides where a lane may *start*;
# nothing stops a worker reaching the primary checkout mid-run. The brief's
# scope allowlist and `cmd_verify` are what catch that, and both run after the
# fact -- so read the diff before you trust it.
CODEX_SANDBOX=${CODEX_SANDBOX:-danger-full-access}

# Prove the worker's environment can run one test BEFORE handing it a brief.
# A lane costs an hour; this costs seconds, and it converts an unrunnable
# sandbox from a silent hour of thrashing into a loud failure on line one.
preflight_sandbox() {
  local wt=${1:?} profile=${2:?}

  # Under danger-full-access the worker runs with the same token as this
  # script, so running the test here IS the worker's environment -- and it is
  # deterministic, which three attempts at asking the worker to report back
  # were not. Each of those blocked a healthy lane: one grepped the reply for
  # pytest's summary and got its progress dots; one appended a POSIX `$?` that
  # pwsh evaluates as a boolean; one redirected into a file that pwsh left
  # empty. A gate whose false positives outnumber its true ones protects
  # nothing, and each false one cost a launch.
  if [ "$CODEX_SANDBOX" != "danger-full-access" ]; then
    echo "codex_task: preflight SKIPPED -- CODEX_SANDBOX=$CODEX_SANDBOX." >&2
    echo "  A direct run here would use this shell's token, not the worker's," >&2
    echo "  so it cannot tell you whether a narrowed sandbox can run the tests." >&2
    echo "  Watch the first lane closely; that is the configuration that broke." >&2
    return 0
  fi

  echo "codex_task: preflight -- can this environment run one test?"
  local out
  # The live Exomem service legitimately updates the machine-default state
  # root while this probe runs.  Point both the platform default and the
  # explicit Exomem root into the lane so pytest's cross-process guard tests
  # the worker rather than observing unrelated service activity.
  local preflight_xdg="$wt/.task/preflight-xdg"
  local preflight_localappdata="$wt/.task/preflight-localappdata"
  local preflight_state="$wt/.task/preflight-state"
  local preflight_pytest="$wt/.task/preflight-pytest"
  if out=$(cd "$wt" && \
    XDG_STATE_HOME="$preflight_xdg" \
    LOCALAPPDATA="$preflight_localappdata" \
    EXOMEM_STATE_ROOT="$preflight_state" \
    uv run --frozen python -m pytest tests/test_scaffold_no_leak.py -q \
      --basetemp="$preflight_pytest" 2>&1); then
    echo "codex_task: preflight OK -- $(grep -oE '[0-9]+ (passed|skipped).*' <<<"$out" | tail -1)"
    return 0
  fi

  echo "GATE FAIL: this environment cannot run the test suite." >&2
  echo "  sandbox=$CODEX_SANDBOX profile=$profile worktree=$wt" >&2
  echo "  Every brief ends in 'run the tests'; a worker that cannot is worse" >&2
  echo "  than no worker. Do not launch the lane until this passes." >&2
  echo "--- last 20 lines:" >&2
  tail -20 <<<"$out" >&2
  exit 1
}

cmd_start() {
  local lane=${1:?usage: codex_task.sh start <lane> <brief-file> [--profile <name>]}
  local brief=${2:?brief file required}
  shift 2
  local profile="terra-worker"
  while [ $# -gt 0 ]; do
    case "$1" in
      --profile) profile=${2:?}; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  [[ "$lane" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "lane must be kebab-case: $lane"
  [ -f "$brief" ] || die "brief file not found: $brief"
  local wt="$PARENT/exomem-$lane" branch="codex/$lane"
  [ -e "$wt" ] && die "$wt already exists"

  git -C "$REPO" fetch origin --quiet
  git -C "$REPO" worktree add "$wt" -b "$branch" origin/main
  require_linked_worktree "$wt"

  mkdir -p "$wt/.task"
  cp "$brief" "$wt/.task/TASK.md"
  local excl
  # Linked worktrees read ignore rules from the COMMON git dir's info/exclude,
  # not the per-worktree gitdir — write there (harmlessly repo-wide).
  excl=$(git -C "$wt" rev-parse --path-format=absolute --git-common-dir)/info/exclude
  mkdir -p "$(dirname "$excl")"
  grep -qxF ".task/" "$excl" 2>/dev/null || echo ".task/" >> "$excl"

  (cd "$wt" && uv sync)

  preflight_sandbox "$wt" "$profile"

  echo "codex_task: launching codex exec (profile=$profile, sandbox=$CODEX_SANDBOX) in $wt"
  codex exec --profile "$profile" -s "$CODEX_SANDBOX" -C "$wt" \
    --json -o "$wt/.task/codex-run.jsonl" "$WORKER_PROMPT"
  echo "codex_task: worker finished — inspect $wt/.task/RESULT.md then run: codex_task.sh verify $wt"
}

# CI runs tests/test_latency_gate.py on ubuntu ONLY -- cross-platform.yml
# passes `--ignore=tests/test_latency_gate.py` for windows-latest and
# macos-latest. It generates an 8k-page fixture and reproducibly exceeds the
# per-item timeout on a Windows host, so demanding it here fails a lane for a
# test its own CI would not have run. Mirror the platform policy instead of
# inventing a stricter one.
case "$(uname -s 2>/dev/null || echo unknown)" in
  Linux) PLATFORM_PYTEST_IGNORES=() ;;
  *) PLATFORM_PYTEST_IGNORES=(--ignore=tests/test_latency_gate.py) ;;
esac

cmd_verify() {
  local wt=${1:?usage: codex_task.sh verify <worktree-dir>}
  require_linked_worktree "$wt"
  local fail=0

  if [ -n "$(git -C "$wt" status --porcelain)" ]; then
    echo "GATE FAIL: uncommitted changes in $wt" >&2
    git -C "$wt" status --short >&2
    fail=1
  fi

  echo "--- files changed vs origin/main:"
  git -C "$wt" diff --name-only origin/main...HEAD

  local guarded
  guarded=$(git -C "$wt" diff --name-only origin/main...HEAD -- \
    tests/golden/ tests/test_latency_gate.py tests/test_retrieval_golden.py .github/)
  if [ -n "$guarded" ]; then
    echo "GATE WARNING: guarded files changed (allowed only if the brief says so):" >&2
    echo "$guarded" >&2
  fi

  (cd "$wt" && uv run python -m pytest -q "${PLATFORM_PYTEST_IGNORES[@]}") || fail=1
  if [ ${#PLATFORM_PYTEST_IGNORES[@]} -eq 0 ]; then
    (cd "$wt" && uv run python -m pytest tests/test_latency_gate.py -q) || fail=1
  else
    echo "GATE NOTE: skipping tests/test_latency_gate.py (CI runs it on ubuntu only)"
  fi
  (cd "$wt" && uvx ruff check .) || echo "GATE WARNING: ruff findings (advisory)" >&2

  [ "$fail" -eq 0 ] && echo "GATE PASS (benchmark before/after still your job)" || die "gate failed"
}

case "${1:-}" in
  template) cmd_template ;;
  start) shift; cmd_start "$@" ;;
  verify) shift; cmd_verify "$@" ;;
  *) die "usage: codex_task.sh {template|start|verify} ..." ;;
esac
