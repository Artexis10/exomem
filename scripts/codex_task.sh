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
# worktree — the shared primary checkout can never be a Codex workspace.
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
# The workers run in linked worktrees under $PARENT, against a repo whose own
# guardrail is `require_linked_worktree`, and `cmd_verify` gates every diff
# before it can merge. Full access is the mode that matches that containment.
CODEX_SANDBOX=${CODEX_SANDBOX:-danger-full-access}

# Prove the worker's environment can run one test BEFORE handing it a brief.
# A lane costs an hour; this costs seconds, and it converts an unrunnable
# sandbox from a silent hour of thrashing into a loud failure on line one.
preflight_sandbox() {
  local wt=${1:?} profile=${2:?}
  echo "codex_task: preflight (sandbox=$CODEX_SANDBOX) -- can a worker run one test?"
  # The worker leaves evidence on disk and the runner reads it. Two earlier
  # versions inspected the worker's REPLY instead and both blocked a healthy
  # lane: the first grepped for pytest's "N passed" summary and got its progress
  # dots; the second appended `echo PREFLIGHT_EXIT=$?`, which is a POSIX idiom
  # the worker then ran through pwsh, where `$?` is a boolean and the exit code
  # lives in $LASTEXITCODE.
  #
  # Both failures share one cause: the gate depended on how something else chose
  # to phrase an answer. A redirect into a file does not, and it still exercises
  # the thing that actually broke -- writing inside the worktree and running
  # pytest under the worker's own token. `.task/` is git-excluded, so the log is
  # never committed.
  local log="$wt/.task/preflight.log"
  rm -f "$log"
  local out
  out=$(codex exec --profile "$profile" -s "$CODEX_SANDBOX" -C "$wt" \
      "Run exactly this one command and nothing else, then reply with the word done: \
uv run --frozen python -m pytest tests/test_scaffold_no_leak.py -q > .task/preflight.log 2>&1" 2>&1)
  if [ -f "$log" ] && grep -qE "[0-9]+ (passed|skipped)" "$log"; then
    echo "codex_task: preflight OK -- $(grep -oE "[0-9]+ (passed|skipped).*" "$log" | tail -1)"
    rm -f "$log"
    return 0
  fi
  if [ -f "$log" ]; then
    echo "--- worker's pytest output:" >&2
    tail -15 "$log" >&2
  else
    echo "--- the worker produced no log at all (could not write inside the worktree)" >&2
  fi
  echo "GATE FAIL: the worker sandbox cannot run the test suite." >&2
  echo "  sandbox=$CODEX_SANDBOX profile=$profile worktree=$wt" >&2
  echo "  Every brief ends in 'run the tests'; a worker that cannot is worse" >&2
  echo "  than no worker. Do not launch the lane until this passes." >&2
  echo "--- last 20 lines of preflight output:" >&2
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
