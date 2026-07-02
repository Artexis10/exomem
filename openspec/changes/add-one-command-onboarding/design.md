# Design - one-command onboarding proof

## Context

`exomem setup` (`src/exomem/setup_wizard.py`) already exists and is fully
tested (`tests/test_setup_wizard.py`): it is an idempotent wizard that scans a
vault, runs `init`, picks a search profile, runs `doctor`, registers the
server with Claude Code via `claude mcp add` (or prints a `.mcp.json`
snippet), installs the skill, and offers the optional hooks. README already
leads with it (`README.md:92-125`). This design does not touch that flow's
logic — it fixes one real defect in it (`_server_command`'s launch-path
durability) and builds the missing **pre-install** proof step that has to
exist before a user is willing to run `setup` at all.

Today that pre-install proof is `README.md`'s "Five-minute proof" section
(`README.md:65-90`), which shells out to `scripts/demo-sample-vault.py`. Two
things are wrong with it, both confirmed by reading the files directly:

- **It is repo-only.** `pyproject.toml`'s `[project.scripts]` entry points
  (`kb`, `exomem`) and `[tool.hatch.build.targets.wheel]`'s `packages =
  ["src/exomem", "src/kb_mcp"]` define what ships in the wheel; `scripts/`
  is not part of it. A user who ran `pip install exomem` or `uvx exomem` (the
  README's own documented Install path, `README.md:47-63`) has no way to run
  this script — it does not exist in their environment.
- **It is corrupted.** `scripts/demo-sample-vault.py:19` defines
  `TAuGET_PATH` (a garbled `TARGET_PATH`) and lines 29-33 set
  `EXOMEM_DISABLE_MEDIA_EXTuACTION`, `EXOMEM_DISABLE_uELEVANCE_CHECK`,
  `EXOMEM_DISABLE_QUEuY_LOG`, and `EXOMEM_DISABLE_uANKING_CONFIG`. Its sibling
  `scripts/smoke-sample-vault.py` — the one actually wired into
  `.github/workflows/ci.yml`'s "Sample vault smoke" step and into
  `docs/release.md`'s pre-release checklist — has a parallel corruption:
  `EXOMEM_DImABLE_EMBEDDINGm`, `EXOMEM_DImABLE_MEDIA_EXTRACTION`,
  `EXOMEM_DImABLE_CLIP`, `EXOMEM_DImABLE_RELEVANCE_CHECK`,
  `EXOMEM_DImABLE_QUERY_LOG`, `EXOMEM_DImABLE_RANKING_CONFIG`
  (`scripts/smoke-sample-vault.py:25-30`). A repo-wide check of every real
  `EXOMEM_DISABLE_*` name the server actually reads (`add.py`, `audit.py`,
  `corpus_aware.py`, `doctor.py`, `embeddings.py`, `extract.py`, `find.py`,
  `query_log.py`, `voice_embed.py`, `warmup.py`, `server.py`) confirms none of
  these garbled names match anything — every lean-env assignment in both
  scripts is a silent no-op. CI has therefore been running its "cheap public
  readiness gate" (`openspec/specs/install-readiness/spec.md`'s "Sample Vault
  Smoke" / "CI Install-Readiness Gates" requirements) against a script that
  never actually disabled embeddings, media extraction, CLIP, the relevance
  check, query logging, or the ranking-config override — it happened to still
  pass only because the sample vault is small enough that the default
  (non-lean) path also succeeds quickly in CI.

Separately, `_server_command` (`src/exomem/setup_wizard.py:46-55`) picks
between exactly two launch forms today: the `uv --directory <repo> run
python -m exomem --transport stdio` form for a source checkout, or
`[sys.executable, "-m", "exomem", "--transport", "stdio"]` otherwise. Under
`uvx exomem setup` (the officially-recommended zero-install path once this
change ships a zero-install proof), `sys.executable` is the interpreter inside
`uvx`'s ephemeral per-run cache environment — a path that `uv cache prune` (or
routine cache eviction) can invalidate, silently breaking a previously-working
Claude Code registration with no error at the time it happens.

`pyproject.toml`'s `[tool.hatch.build.targets.wheel]` already ships
non-Python files that live under `src/exomem/` without any special
configuration — `src/exomem/_scaffold/_Schema/SKILL.md` and
`_Schema/project-keys.yaml` are the existing proof of this (verified by
`tests/test_scaffold_no_leak.py`, which imports `exomem` and locates
`Path(exomem.__file__).resolve().parent / "_scaffold"` at runtime from the
installed package). A new `src/exomem/_sample_vault/` directory ships the
same way, with no `pyproject.toml` change required.

## Goals / Non-Goals

**Goals:**

- A prospective user proves exomem works, from a bare `uvx exomem demo`, with
  no git clone, no manual configuration, and no vault of their own.
- The proof and the CI/release smoke gate become the **same command** — no
  second script that can independently drift or independently rot.
- The installed package is never mutated by running the proof, on any OS
  (Windows/macOS/Linux), including from a read-only `site-packages`.
- The guided wizard's Claude Code registration survives ephemeral `uvx` cache
  eviction whenever a durable alternative is available.
- Every supported MCP client (Claude Code, Codex CLI, claude.ai remote, other
  MCP clients) has a documented connect path in one place.

**Non-Goals:**

- Changing anything about `exomem setup`'s scan/init/profile/doctor/skill/hook
  logic — only its server-command selection.
- Adding a second, richer demo vault, an interactive demo mode, or any new
  reasoning surface. See Decisions below for each rejected direction.
- GPU/media/embeddings coverage in the demo — it is deliberately lean-only,
  matching the existing `doctor --profile lean` contract.

## Decisions

### 1. The sample vault moves into the wheel; it is not regenerated from `_scaffold`

The demo needs vault **content** to search: a real page with a real body, a
real wikilink, and text that a keyword `find` for "retrieval" actually
matches. `src/exomem/_scaffold/` (the skill scaffold installed by
`install-skill`/`init`) is deliberately empty of content — it is index/log
stubs and schema docs, not populated notes. Generating a demo vault from the
scaffold on the fly was considered and rejected: `find` would have nothing to
match, so the "proof" would prove nothing beyond "the process didn't crash";
and it would open a second leak-guard surface (generated content escaping the
same discipline `test_scaffold_no_leak.py` already holds the hand-authored
scaffold to) for no benefit over reusing the vault that already exists and is
already leak-guarded implicitly (it's already public, committed, and referenced
by the current smoke scripts). Instead, `examples/sample-vault/Knowledge Base/`
— the same fixture the corrupted scripts already point at — is moved
(`git mv`, preserving history) to `src/exomem/_sample_vault/Knowledge Base/`,
becoming part of the installed package rather than a repo-only fixture.
`examples/sample-vault/README.md` becomes a short pointer to `exomem demo`
and the new location, rather than being deleted outright, so old links/clones
still find an explanation.

### 2. The repo scripts are deleted outright, not kept alongside the subcommand

Keeping `scripts/demo-sample-vault.py` / `scripts/smoke-sample-vault.py`
alongside the new `exomem demo` subcommand was considered and rejected: two
independent smoke paths is exactly the condition that let both scripts rot
silently for as long as they did (the rename swept `src/exomem/**` but not
`scripts/`, and nothing forced the two to agree). Both are also already
corrupted and provide zero remaining value once `exomem demo` exists and is
wired into CI. Deleting them and pointing every consumer (CI, `docs/release.md`,
`examples/sample-vault/README.md`) at `exomem demo` leaves exactly one smoke
path that can be exercised identically by a user, by CI, and by a maintainer
cutting a release.

### 3. `exomem demo` is a fixed, zero-question sequence — not interactive

An interactive `demo` (prompting for a vault path, a profile, or which checks
to run) was considered and rejected: the entire point of a pre-install proof
is that it requires zero decisions and zero typing beyond the one command
itself. Every parameter (vault, profile, checks, ordering) is fixed; the only
user-facing choices are `--json` (machine-readable output for CI) and
`--keep` (inspect the temp vault afterward instead of discarding it).

### 4. Packaged-vault isolation: copy-to-temp, never operate on the install in place

`site-packages` may be (and on several install paths, such as system Python
or certain container images, routinely is) read-only to the invoking user, and
even where it is writable, a demo command must never be able to leave stray
files inside an installed package. `exomem demo` therefore resolves the
packaged vault via `Path(exomem.__file__).resolve().parent / "_sample_vault" /
"Knowledge Base"` (the same resolution pattern `test_scaffold_no_leak.py`
already uses for `_scaffold`), copies it into a fresh `tempfile.mkdtemp()`-style
directory, and runs every step against that copy. `--keep` skips only the
final cleanup of the temp directory; it never changes what gets copied or
where the source is read from. A dedicated test asserts no file under the
installed package's `_sample_vault` changes mtime across a full `demo` run.

### 5. `_server_command` durability: console script before interpreter path, `uv`-checkout unchanged, `uvx` as last resort

The reordered preference, in the order the code checks them:

1. **Repo checkout** (`pyproject.toml` present next to the running module
   and `uv` resolves on `PATH`) → unchanged:
   `uv --directory <repo> run python -m exomem --transport stdio`. This
   branch stays first so a contributor working in a clone continues to get
   the checkout-aware form even if an unrelated `exomem` console script
   happens to also be on their `PATH` from a prior install.
2. **Durable console script** (`shutil.which("exomem")` resolves) →
   `[<resolved path>, "--transport", "stdio"]`. This is the result of
   `pip install exomem` or `uv tool install exomem` — a stable path that
   survives cache eviction, restarts, and virtualenv changes, unlike
   `sys.executable -m exomem` under an ephemeral runner.
3. **Neither** (a transient `uvx exomem setup` invocation with no durable
   install anywhere on the machine) → fall back to
   `["uvx", "exomem", "--transport", "stdio"]`, and print a note recommending
   `uv tool install exomem` for a registration that survives `uvx` cache
   pruning. This keeps `uvx exomem setup` fully functional today (Claude Code
   re-invokes `uvx exomem ...` fresh each time, so the registration keeps
   working) while being honest that it is the least durable of the three
   options and naming the fix.

This ordering is exercised by three new `tests/test_setup_wizard.py` cases
against the existing `which_fn`-injection seam (`_server_command(which_fn)`
already takes an injectable `which_fn`; no new seam is needed).

### 6. CI gate: rewire the existing smoke step, and add a separate wheel-path gate

Two independent risks existed before this change and get two independent
gates:

- **"Does the documented user command work?"** — today answered by a
  corrupted, repo-only script. Fixed by rewiring the existing per-Python-version
  "Sample vault smoke" step in `.github/workflows/ci.yml` to
  `uv run exomem demo --json`, run from the checkout (fast — no wheel build
  needed for this gate, matching the existing job's per-matrix-version
  repetition).
- **"Does the wheel actually ship what the demo/setup need, with zero repo
  dependency?"** — never answered before. A new `onboarding` CI job: `uv
  build` → create a fresh virtualenv → install *only* the built wheel (no
  editable install, no `src/` on `PYTHONPATH`) → run `exomem demo --json` from
  a temporary working directory outside the checkout → run `exomem setup
  --vault <tmp> --yes --skip-claude-register` → assert both succeed and the
  combined sequence completes inside a documented wall-time budget. This is
  the only gate that would have caught "the sample vault isn't in the wheel"
  or "the wizard's registration doesn't work outside a checkout" before a
  user does.

`scripts/time-to-value.py` runs the same build → fresh-venv → install →
`demo` → `setup` sequence locally, printing a per-step timing breakdown, so a
maintainer can reproduce and tune the CI job's budget without needing CI logs.

### 7. `setup` keeps its name; it is not renamed to `quickstart`

Renaming `exomem setup` to `exomem quickstart` (to pair naming-wise with a new
`exomem demo`) was considered and rejected: `setup` is already shipped,
documented (`README.md`, `SETUP-LOCAL.md`), and covered by
`tests/test_setup_wizard.py`; renaming it would be a breaking CLI change for
zero behavioral benefit, disguised as a naming nicety. `demo` and `setup`
read fine as a pair without renaming either.

## Risks / Trade-offs

- **A future contributor edits `src/exomem/_sample_vault/` and reintroduces
  private content** → mitigated by extending
  `tests/test_scaffold_no_leak.py`'s structural `LEAK_PATTERNS` scan (today
  scoped to `_scaffold/_Schema/` only) to also walk `_sample_vault/`; the
  separate token-denylist scan already covers it via its `src/exomem/**` walk.
- **The wheel-path CI gate adds real CI wall-time** (a full `uv build` plus a
  fresh venv plus a wheel install, on top of the existing per-Python-version
  test matrix) → accepted trade-off; it is the only gate that actually proves
  the documented zero-install path works, and it runs once per PR (not once
  per Python version).
- **Codex CLI flag syntax drifts between when this proposal is written and
  when it is implemented** → explicitly flagged, not guessed around: the
  implementation re-verifies `codex mcp add`'s current flags and the
  `~/.codex/config.toml` schema against the installed Codex CLI at
  implementation time (tracked as its own task) rather than trusting this
  document's syntax.
- **Deleting the two corrupted scripts removes any working reference for
  anyone who had bookmarked them** → both are non-functional today (the env
  pins are no-ops) and superseded one-for-one by `exomem demo`; nothing of
  value is lost.

## Migration Plan

No data migration and no schema change. `git mv` preserves file history for
the moved vault content. `examples/sample-vault/README.md` stays in place as a
pointer rather than disappearing, so existing links/clones still resolve to
an explanation. `_server_command`'s new branch order is additive-safe: any
environment that previously hit the repo-checkout branch is unaffected (that
branch is unchanged and still checked first); any environment that previously
fell through to `sys.executable` now more often resolves a durable console
script instead, which is a strict improvement with no behavior a test needs to
preserve. Deleting the two scripts is a breaking change only for someone
invoking them directly outside of CI/docs, which nothing in this repository
does after the CI/docs rewire in this change.

## Open Questions

- The exact `codex mcp add` flag names and the precise
  `~/.codex/config.toml` `[mcp_servers.*]` schema are re-verified at
  implementation time against the currently-installed Codex CLI rather than
  fixed here — see Risks/Trade-offs and `tasks.md`.
- The exact wall-time budget asserted by the `onboarding` CI job and
  documented by `scripts/time-to-value.py` is set at implementation time from
  a measured baseline run, rather than fixed in this design, so it reflects
  real CI runner performance rather than a guess.
