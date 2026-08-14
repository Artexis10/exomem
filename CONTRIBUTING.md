# Maintainer notes

## One worktree per change (sessions share a checkout)

This repo is often edited by several Claude Code sessions at once. Switching
branches in the shared primary checkout disrupts other sessions, so do every new
change in its own git worktree. Do not use `git stash` from any checkout:
`refs/stash` is repository-global, so a pop can apply another worktree's entry.

```
git worktree add ../exomem-<topic> -b <branch>
# work, commit, push from the worktree
git worktree remove ../exomem-<topic>
```

Commit and push from the worktree; leave the primary checkout on whatever branch
the other session is using.

## uv: a floor here, the exact version in CI

`required-version` in `pyproject.toml` is `>=0.11.28`, not `==`. uv is normally
one shared installation managing many unrelated tools, so an exact pin made a
fresh-checkout `uv sync` fail outright and tell you to run
`uv self update 0.11.28` — a global downgrade to satisfy one repo.

The pin still exists where it matters. Lockfile marker normalization can only
diverge when something *writes* the lock, so CI pins uv to the exact writer
version (`0.11.28`) on the job that runs `uv lock --check`. If you regenerate
`uv.lock` and CI disagrees with your local result, match that version without
touching your global install:

```
UV_INSTALL_DIR=.uvbin curl -LsSf https://astral.sh/uv/0.11.28/install.sh | sh
```

## Installing the service on a CPU-only host

`scripts/install-service.sh --profile hybrid` pulls `torch`, which is pinned to
the CUDA index on Linux and Windows — multi-GB, and unusable without a GPU. Use
`--profile onnx` instead: the same bi-encoder served through ONNX Runtime, with
`EXOMEM_EMBED_BACKEND=onnx` written into the service environment. It is the
right default for any host without a GPU.

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
The maintainer derives a personal claude.ai `.skill` zip via
`scripts/rebuild-schema-zip.py`, which reads the **public scaffold**
(`src/exomem/_scaffold/_Schema/`) and overlays their real `project-keys.yaml` from
their vault. There is no private canonical or marker system — the scaffold is the
single source. That path is maintainer-only and unrelated to the public scaffold
above; contributors can ignore it.
