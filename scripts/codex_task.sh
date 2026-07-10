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

## Deliverable
Commit to the current branch (do not push). Write .task/RESULT.md with a
summary, acceptance-command output, and any new dependency you believe is
needed (do not add it yourself).
EOF
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
  excl=$(git -C "$wt" rev-parse --absolute-git-dir)/info/exclude
  mkdir -p "$(dirname "$excl")"
  echo ".task/" >> "$excl"

  (cd "$wt" && uv sync)

  echo "codex_task: launching codex exec (profile=$profile) in $wt"
  codex exec --profile "$profile" -s workspace-write -C "$wt" \
    --json -o "$wt/.task/codex-run.jsonl" "$WORKER_PROMPT"
  echo "codex_task: worker finished — inspect $wt/.task/RESULT.md then run: codex_task.sh verify $wt"
}

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

  (cd "$wt" && uv run python -m pytest -q) || fail=1
  (cd "$wt" && uv run python -m pytest tests/test_latency_gate.py -q) || fail=1
  (cd "$wt" && uvx ruff check .) || echo "GATE WARNING: ruff findings (advisory)" >&2

  [ "$fail" -eq 0 ] && echo "GATE PASS (benchmark before/after still your job)" || die "gate failed"
}

case "${1:-}" in
  template) cmd_template ;;
  start) shift; cmd_start "$@" ;;
  verify) shift; cmd_verify "$@" ;;
  *) die "usage: codex_task.sh {template|start|verify} ..." ;;
esac
