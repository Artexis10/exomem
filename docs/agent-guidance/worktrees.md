<!-- authority:non-specification -->

# Task-specific agent guidance

Read the relevant sections before the work named by the root guidance. OpenSpec remains the specification authority.

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
  commit, push the task branch and open a PR; merge only with authority, then retire the worktree.
- Manual: `git worktree add ../exomem-<topic> -b <branch>`; work, commit, push;
  then `git worktree remove ../exomem-<topic>`.

