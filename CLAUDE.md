# exomem — instructions for Claude

For unfamiliar cross-file code paths, optional Graft CLI queries can provide a
small starting map. Read `docs/code-navigation.md` when using it; verify its
results in source and use normal search when it misses.

## Shared-checkout and live-state boundaries

Every new change, including docs and OpenSpec, belongs in its own linked worktree from `origin/main`. Keep the primary on `main`; inspect status and all worktrees before editing. Never discard or overwrite another session's files or processes. Never use `git stash` in any checkout: the stack is repository-global.

Read `docs/agent-guidance/worktrees.md` before Git mutations. Commit/push only the task scope, open a PR, and merge only with authority. Retire the task worktree and branch after merge/closure only when clean, pushed, and free of live task processes.

Before connector diagnosis or service/index operations, read `docs/agent-guidance/live-cell.md`. Identify the actual service manager, interpreter and external state root first. Never run an out-of-process index drain against a running service, restart reflexively, or delete duplicate state by hand.

Read `CONTRIBUTING.md` before committing: use the pinned project-local uv writer and run the public-artifact privacy gate. Keep runtime tests in temporary state.

## Delegated worker tasks

When using `scripts/codex_task.sh`, or receiving a `.task/TASK.md` worker brief, first read `docs/agent-guidance/workers.md`. Its scope, verification and no-push rules apply to those delegated workers. The current orchestrator owns integration; Codex and Claude can both hold that role. Ordinary inline work follows the shared task-routing rules.

## Editing the skill scaffold (hand-authored — keep it generic)

The skill shipped to new users lives at `src/exomem/_scaffold/_Schema/`
(SKILL.md + `references/*.md` + `project-keys.yaml`). It is a **hand-authored,
deliberately-generic starter** and the **single source of the skill** — edit it
directly. It is NOT generated from a private vault, and there is no marker canonical
to keep in sync.

The hard rule: **keep it generic.** `tests/test_scaffold_no_leak.py` fails if any
personal name, product, or vault-structure label appears in the scaffold — or
anywhere under `src/exomem/`. If a test flags a token, genericize it; don't add it
to an allowlist.

(Maintainer-only: the personal claude.ai `.skill` zip is built by
`scripts/rebuild-schema-zip.py` **from this same scaffold**, overlaying only your real
`project-keys.yaml` — no private canonical, no markers. Needs no version bump here.)

## OpenSpec closure is part of delivery

Treat checked task boxes as a claim that requires code, test, and merge evidence,
not as proof by themselves. When an active change is demonstrably shipped and
its non-optional tasks are complete, synchronize its delta into the current
canonical specs and archive it with `openspec archive` in the same delivery.
Never archive by moving the directory. Preserve requirements and scenarios added
by later work when refreshing a stale `MODIFIED` block, and run
`openspec validate --all --strict` before and after the archive. A task-complete
active change is archive debt and CI rejects it.

## OpenSpec is the sole specification system

<!-- spec-system:openspec-only -->

Use `openspec/` for durable change proposals, designs, requirements, and task
plans. Do not create, read as current authority, or revive
`docs/superpowers/` or any parallel specification tree. Before deleting legacy
planning documents, migrate any unique durable contract into the relevant
existing OpenSpec artifact; leave routine implementation history to code,
tests, runbooks, and Git. Routine restorative fixes and operational repair do
not need a new OpenSpec change. New capabilities, contract changes, and
non-trivial repairs do.

## Memory boundary

Treat Claude, ChatGPT, Codex, and other assistants' native memory as short-term
or behavioural memory for preferences, routing, and working context. Exomem is
the long-term governed store for project/domain knowledge, sources, evidence,
decisions, and reusable conclusions.
