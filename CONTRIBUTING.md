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

## The privacy gate runs anywhere — run it before you push

`public_artifact_privacy` refuses a drive-absolute path literal in any
repository input or shipped wheel member. Docstrings count: they travel inside
the wheel.

```
uv run python scripts/validate-public-artifacts.py --repository
```

It is pure Python over files and takes a couple of seconds on any platform. It
is worth running deliberately because the CI job that enforces it is
Linux-only, so a Windows-only contributor's first encounter with it is a red
PR. That is not hypothetical: a branch that had passed two independent clean
reviews landed 16 findings and six red checks this way (#574).

### The %PROGRAMDATA% fallback: import it, don't retype it

The Windows machine-wide base genuinely needs `ProgramData`, so the last tier
of the fallback chain is written as a concatenation, which keeps the literal
from ever appearing contiguously. Read quickly that looks like odd formatting,
which is exactly how it gets dropped when the chain is reproduced by hand.

- **Python inside the package:** import `mode.windows_machine_wide_root()`.
  Nothing else may re-derive the chain; `tests/test_windows_machine_wide_root.py`
  fails if a new file does.
- **The standalone hook scripts** under `src/exomem/_hooks/` run as bare files
  under the client's interpreter and cannot import the package, so they carry a
  deliberate mirror. The same test pins their spelling.
- **PowerShell** cannot import a Python helper either. Use the same split --
  `"C:" + "\ProgramData"` -- as `scripts/install-service.ps1` does, or
  `$env:SystemDrive`.
- **Prose and docstrings:** write `%PROGRAMDATA%`. The gate permits the
  environment-variable spelling, and one canonical form is what makes the
  convention imitable.

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
