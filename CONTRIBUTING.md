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

## uv is pinned — install it project-locally, don't downgrade your global one

`uv sync` in a fresh checkout fails on any uv other than the pinned writer
version:

```
error: Required uv version `==0.11.28` does not match the running version `0.11.31`.
Update `uv` by running `uv self update 0.11.28`.
```

**Do not run that suggestion.** uv is normally one shared installation managing
many unrelated tools, and `uv self update` downgrades all of them to satisfy this
repo. Install the pinned version project-locally instead:

```
UV_INSTALL_DIR=.uvbin curl -LsSf https://astral.sh/uv/0.11.28/install.sh | sh
PATH="$PWD/.uvbin:$PATH" uv sync
```

The pin is deliberate and is not just about this file. A single writer version
keeps lockfile marker normalization identical across Windows, WSL, Docker and
CI, and `required-version` is what makes that bind — `Dockerfile`,
`infra/tool-versions.env` and `.github/workflows/hosted-infrastructure.yml` all
declare the same version, and
`tests/test_uv_lock_policy.py::test_uv_writer_version_is_pinned_across_repository_surfaces`
fails if any of the four drifts. Bumping uv means bumping all four together.

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
