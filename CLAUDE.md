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
