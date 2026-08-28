# exomem — instructions for Claude

## Concurrent sessions share ONE checkout — isolate new work in a worktree

This repo is often worked on by more than one Claude Code session at once, all
sharing the primary working tree. The hazard is **not "touching the primary"** —
it's **destroying or colliding with another session's in-flight (uncommitted)
work**. So judge an operation by its *effect*, not by a memorized command list.

The primary checkout is `<projects-dir>/exomem`; keep it on
`main` and treat it as coordination space, not a feature branch parking lot.
Feature branches belong in sibling worktrees such as
`<projects-dir>/exomem-<topic>`. Do not leave `main` checked
out in a stale sibling worktree, because that blocks switching the primary back
to `main`.

**Rule: never run a git operation that discards/overwrites uncommitted changes or
rewrites the working tree in the shared primary checkout — unless the user
explicitly approves that specific operation.** That covers `git checkout
<branch>` / `git switch` (swaps files), `git reset --hard`,
`git checkout -- <file>` / `git restore <file>` / `git clean` (discard a file's
uncommitted state), and any rebase/merge that rewrites the tree. These have
already caused a mid-edit collision. `git stash` is worse than these and is
covered separately below, because it is unsafe from *any* worktree, not just
this one.

**`git stash` is never safe here, in any checkout — including your own
worktree.** The stash stack lives in the repository, not the worktree: every
worktree shares one `refs/stash`. A `pop` takes whatever is on top, which may be
another session's entry, and applies it into your tree. Worse, `git stash push
-- <paths>` **exits zero having created nothing** when those paths are already
clean, so the paired `pop` silently targets a stranger's work. This has already
half-applied another session's `uv.lock` change and produced a phantom test
failure. To compare against a committed baseline, create a separate disposable
worktree at that ref. If uncommitted task work must be parked, make a temporary
commit in the task worktree; never use checkout/restore or stash to hide it.

**Always fine on the primary — no worktree, no approval:** read-only git
(`status`, `log`, `diff`, `fetch`); a clean `git pull --ff-only` on the branch
it's already on (it only advances, and *refuses* rather than clobber if
uncommitted work would conflict); and anything off the git tree — building/syncing
venvs (`uv sync`), running or restarting the service, editing a file you yourself
just created. Don't hand the user a command you can safely run yourself.

**Mitigation:** before editing for any *new change* — feature, fix, docs,
OpenSpec artifact, or release prep — first check whether the current checkout is
an isolated worktree. If it is the shared primary checkout, create a dedicated
worktree from `origin/main` and do the edits there. Do not ask the user to repeat
this preference; state the worktree path in your first progress update. The
worktree is the default for new work; the rule above is the guardrail for when
you must operate on the primary.

- Native (Claude Code): `EnterWorktree` — branches off `origin/main`; edit,
  commit, `git push origin HEAD:main` (or open a PR), then `ExitWorktree`.
- Manual: `git worktree add ../exomem-<topic> -b <branch>`; work, commit, push;
  then `git worktree remove ../exomem-<topic>`.

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

## Connector triage ("MCP not working" / slow first call / forced reconnect)

claude.ai connector problems are almost always **connection-side, not the service**.
The public ingress is a **Cloudflare Tunnel** (`exomem.substratesystems.io`, cloudflared
Windows service; migrated FROM Tailscale Funnel 2026-06-21 — the funnel throttled
connector bursts, KB note `kb-mcp-ingress-migrated-to-cloudflare-tunnel-…`). Known
connection-side patterns: (1) a long-lived claude.ai session's **first MCP call
after an exomem service restart** can stall minutes in the gateway's MCP-session
re-establishment while fresh sessions connect instantly — the server log shows
`Created new transport with session ID` when the delayed call finally lands, and
the request then executes in normal time; (2) Cloudflare's edge caps a single
request at ~100 s. **Diagnose from the access log before touching the server**
(claude.ai gateway IPs `160.79.104.0/21` still appear through the tunnel); don't
restart the service reflexively — restarts CAUSE pattern (1) for live sessions.

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

## Live-cell guardrails (2026-08 incident; standing until `bound-graph-recovery-funnel` lands in code)

- **Never run an out-of-process drain (`exomem index --scope vault`) while the
  service is running.** The CLI takes the graph claim, blocks at 0 CPU on the
  live boundary, and the service mints full-index receipts on every write
  meanwhile — a measured soft-deadlock (~2,100 receipts in 40 minutes).
  Stop-window only: `Stop-Service exomem` → drain → `Start-Service exomem`.
  Note: `exomem maintain --reconcile` does NOT drain the deferred queue;
  `exomem index` does.
- **Test kill-switch env INSIDE the venv python, never in the shell.** Unowned
  site-packages `.pth` files inject `EXOMEM_*` flags at interpreter startup, so
  shell/user/machine/NSSM scopes all show them unset while every venv process
  has them. The 5-second test:
  `<venv>\Scripts\python.exe -c "import os; print(os.environ.get('EXOMEM_DISABLE_GRAPH_SCHEDULING'))"`.
  A cell whose graph work is disabled while a durable recovery checkpoint
  exists can never converge, and every write mints a full-index receipt.
- **Chained builds + "index upsert incomplete" warnings while `graph_sync` is
  `recovery_required` are the graph accounting funnel** (openspec change
  `bound-graph-recovery-funnel`), not a regression of the 0.63.x fixes. Read
  `.deferred-index.sqlite` `full_upserts` and the graph state before
  diagnosing anything else; the fixed-era signature is full receipts at 0 and
  the graph converging within minutes of each write.
