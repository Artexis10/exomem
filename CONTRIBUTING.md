# Maintainer notes

## One worktree per change (sessions share a checkout)

This repo is often edited by several Claude Code sessions at once over a single
checkout, so switching branches or stashing in the main working tree disrupts the
other session. Do every new change in its own git worktree:

```
git worktree add ../exomem-<topic> -b <branch>
# work, commit, push from the worktree
git worktree remove ../exomem-<topic>
```

Commit and push from the worktree; leave the primary checkout on whatever branch
the other session is using.

## The skill scaffold is hand-authored — keep it generic

`src/exomem/_scaffold/_Schema/` (the skill shipped to new users via `init` /
`install-skill`) is a **hand-authored, deliberately-generic starter** — a lean
example schema, not a copy of any private vault. Edit it directly.

**The one rule: keep it generic.** `tests/test_scaffold_no_leak.py` fails if any
personal name, product, podcast, domain, or vault-structure label appears in the
scaffold — or anywhere under `src/exomem/`. That test is the hard wall against the
leak class that once shipped a maintainer's real names into a friend's clone. If
it flags a token, genericize it (don't add it to an allowlist).

### Maintainer-only: the personal claude.ai skill
The maintainer keeps a *private* canonical skill in their own Obsidian vault and
derives a personal claude.ai `.skill` zip from it via
`scripts/rebuild-schema-zip.py` (using the block-marker / substitution utilities
in `scripts/generic/` and `scripts/genericize-schema.py`, the latter now retired
as a scaffold *generator*). That path is maintainer-only and unrelated to the
public scaffold above — contributors can ignore it.
