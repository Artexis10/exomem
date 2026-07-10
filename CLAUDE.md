# exomem — instructions for Claude

## Concurrent sessions share ONE checkout — isolate new work in a worktree

This repo is often worked on by more than one Claude Code session at once, all
sharing the primary working tree. The hazard is **not "touching the primary"** —
it's **destroying or colliding with another session's in-flight (uncommitted)
work**. So judge an operation by its *effect*, not by a memorized command list.

The primary checkout is `C:\Users\hugoa\Desktop\projects\exomem`; keep it on
`main` and treat it as coordination space, not a feature branch parking lot.
Feature branches belong in sibling worktrees such as
`C:\Users\hugoa\Desktop\projects\exomem-<topic>`. Do not leave `main` checked
out in a stale sibling worktree, because that blocks switching the primary back
to `main`.

**Rule: never run a git operation that discards/overwrites uncommitted changes or
rewrites the working tree in the shared primary checkout — unless the user
explicitly approves that specific operation.** That covers `git checkout
<branch>` / `git switch` (swaps files), `git stash`, `git reset --hard`,
`git checkout -- <file>` / `git restore <file>` / `git clean` (discard a file's
uncommitted state), and any rebase/merge that rewrites the tree. These have
already caused a mid-edit collision.

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

## Memory boundary

Treat Claude, ChatGPT, Codex, and other assistants' native memory as short-term
or behavioural memory for preferences, routing, and working context. Exomem is
the long-term governed store for project/domain knowledge, sources, evidence,
decisions, and reusable conclusions.

## Connector triage ("MCP not working" / slow first call / forced reconnect)

claude.ai connector problems are almost always **connection-side, not the service**.
The public ingress is a **Cloudflare Tunnel** (`kb.substratesystems.io`, cloudflared
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
| Shared-primary ops, merges, KB writes, MCP-needing tasks | Claude only — never Codex |

Lane mechanics: one lane = one sibling worktree (`../exomem-<lane>`, branch
`codex/<lane>`, from `origin/main`) = one self-contained `.task/TASK.md` brief
(`codex_task.sh template`) naming the OpenSpec artifacts as source of truth,
a scope allowlist, and exact acceptance commands. `codex exec` runs
`workspace-write`, sandboxed to the worktree — never on the primary checkout
(the runner enforces this). Results come back as commits on the lane branch
plus `.task/RESULT.md`; briefs live under `.task/` (git-excluded, never
committed). Before merging, `scripts/codex_task.sh verify <worktree>` must
pass: clean tree, diff within the brief's allowlist, guarded files untouched
(`tests/golden/`, gate tests, `.github/`), lean pytest + latency gate green.
On failure: write `.task/FEEDBACK.md`, retry once, escalate Terra→Sol, then
reassign to a Claude executor. Cap concurrent workers at 4–6; run benchmarks
only on a quiesced machine.
