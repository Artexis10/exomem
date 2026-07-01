# exomem — instructions for Claude

## Concurrent sessions share ONE checkout — isolate new work in a worktree

This repo is often worked on by more than one Claude Code session at once, all
sharing the primary working tree. The hazard is **not "touching the primary"** —
it's **destroying or colliding with another session's in-flight (uncommitted)
work**. So judge an operation by its *effect*, not by a memorized command list.

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

**Mitigation:** isolate any *new change* — feature, fix, or docs — in a dedicated
git worktree (its own branch + tree) and commit/push from there, leaving the
primary untouched for the other session. The worktree is the default for new work;
the rule above is the guardrail for when you must operate on the primary.

- Native (Claude Code): `EnterWorktree` — branches off `origin/main`; edit,
  commit, `git push origin HEAD:main` (or open a PR), then `ExitWorktree`.
- Manual: `git worktree add ../exomem-<topic> -b <branch>`; work, commit, push;
  then `git worktree remove ../exomem-<topic>`.

## Editing the skill scaffold (hand-authored — keep it generic)

The skill shipped to new users lives at `src/kb_mcp/_scaffold/_Schema/`
(SKILL.md + `references/*.md` + `project-keys.yaml`). It is a **hand-authored,
deliberately-generic starter** — edit it directly. It is NOT generated from a
private vault; `scripts/genericize-schema.py` is retired as a generator (running
it would clobber the hand-authored scaffold).

The hard rule: **keep it generic.** `tests/test_scaffold_no_leak.py` fails if any
personal name, product, or vault-structure label appears in the scaffold — or
anywhere under `src/kb_mcp/`. If a test flags a token, genericize it; don't add it
to an allowlist.

(Maintainer-only: a private claude.ai `.skill` zip is still derived from a private
canonical via `scripts/rebuild-schema-zip.py`; that's separate from the public
scaffold and needs no version bump here.)

## Connector triage ("MCP not working" / forced reconnect)

claude.ai connector problems are almost always **connection-side, not the service**.
A healthy service returns a fast `401` at the funnel. The most common cause is the
**Tailscale Funnel relay throttling the connector's request burst** — the connector
looks disconnected but the exomem service is RUNNING and fine. **Diagnose from the
access log before touching the server** (look for the claude.ai gateway IPs); don't
restart the service reflexively.
