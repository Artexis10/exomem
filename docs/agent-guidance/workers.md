<!-- authority:non-specification -->

# Task-specific agent guidance

Read the relevant sections before the work named by the root guidance. OpenSpec remains the specification authority.

## Codex worker protocol (GPT-5.6 fan-out)

Codex CLI agents are first-class implementation workers; Claude Code stays the
orchestrator and merge gate. If you are a **Codex worker**: your task is
`.task/TASK.md` in this worktree — implement it exactly, do not redesign or
expand scope, commit to the current branch, never push, and write
`.task/RESULT.md` when done.

Routing (orchestrator applies):

| Task class | Route |
|---|---|
| Adversarial review / architecture critique | Sol xhigh, read-only (`omc ask codex --agent-prompt critic\|architect`) |
| Branch/PR review | `codex review` in the lane worktree |
| Standard implementation with tests | `scripts/codex_task.sh start <lane> <brief>` (Terra high) |
| Design-sensitive / hard lanes | Sol xhigh, or a Claude executor |
| Mechanical sweeps, docs | `--profile luna-sweep` (Luna medium) |
| Shared-primary ops, merges, releases | Whoever is orchestrating — one owner at a time |

That last row is about serialization, not about which tool. Whoever is holding
the orchestrator role owns the shared checkout and the merge button for as long
as they hold it; a second actor doing the same thing concurrently races on
uncommitted work and pushes to `main` at the same time. That is equally true of
two Claude sessions, two Codex sessions, or one of each — and it is why this
repo's own CLAUDE.md opens with the shared-checkout rule.

Codex CLI is a peer, not a lesser tool. It has its own MCP servers configured,
including exomem itself, and it drives merges and releases perfectly well. An
earlier version of this table read "Claude only — never Codex", which encoded a
capability claim that was never true and is not what the constraint is.

Lane mechanics: one lane = one sibling worktree (`../exomem-<lane>`, branch
`codex/<lane>`, from `origin/main`) = one self-contained `.task/TASK.md` brief
(`codex_task.sh template`) naming the OpenSpec artifacts as source of truth,
a scope allowlist, and exact acceptance commands. `codex exec` runs
`danger-full-access`, scoped to the worktree by `-C` and by
`require_linked_worktree` — never on the primary checkout (the runner enforces
this). Full access is deliberate, not laziness: on Windows, Codex's
`sandbox = "unelevated"` restricted token cannot touch the private DACL exomem
puts on its own state directories, so pytest dies clearing its tmpdir and the
worker cannot run a single test. Widening `writable_roots` moves the path but
not the ACL. A lane under `workspace-write` once burned an hour and 18M tokens
producing zero commits. The runner now proves a worker can run one test before
handing it a brief, and refuses to launch the lane otherwise; containment comes
from the worktree, the `.task/` allowlist and `codex_task.sh verify`, not from
a sandbox mode that also removes the ability to work. `CODEX_SANDBOX=` overrides
it if a lane genuinely needs less. Results come back as commits on the lane branch
plus `.task/RESULT.md`; briefs live under `.task/` (git-excluded, never
committed). Before merging, `scripts/codex_task.sh verify <worktree>` must
pass: clean tree, diff within the brief's allowlist, guarded files untouched
(`tests/golden/`, gate tests, `.github/`), lean pytest + latency gate green.
On failure: write `.task/FEEDBACK.md`, retry once, escalate Terra→Sol, then
reassign to a Claude executor. Cap concurrent workers at 4–6; run benchmarks
only on a quiesced machine.

