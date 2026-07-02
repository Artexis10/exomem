## Why

exomem already ships a guided onboarding wizard (`exomem setup` —
`src/exomem/setup_wizard.py`: scan → init → profile → doctor → `claude mcp add`
registration → skill → optional hooks; fully tested in
`tests/test_setup_wizard.py`, and the README already leads with it). What is
missing is a pre-commitment proof a prospective user can run **before**
deciding to install anything, plus two real defects that undermine the little
proof surface that exists today:

1. **The "Five-minute proof" is a repo-only, corrupted script.** README's
   "Five-minute proof" section (`README.md:65-90`) points at
   `scripts/demo-sample-vault.py`. That script (a) only exists in a git
   checkout — it is not shipped in the wheel, so `pip install exomem` /
   `uvx exomem` gives a user no way to run it — and (b) is corrupted: the
   2026-07-01 rename commit's bad case-sensitive replace left
   `TAuGET_PATH` (`scripts/demo-sample-vault.py:19`),
   `EXOMEM_DISABLE_MEDIA_EXTuACTION` (`:29`),
   `EXOMEM_DISABLE_uELEVANCE_CHECK` (`:31`), `EXOMEM_DISABLE_QUEuY_LOG` (`:32`),
   and `EXOMEM_DISABLE_uANKING_CONFIG` (`:33`) in it. Its sibling
   `scripts/smoke-sample-vault.py` — the one CI actually runs on every PR
   (`.github/workflows/ci.yml`'s "Sample vault smoke" step) and the one
   `docs/release.md`'s pre-release checklist tells a maintainer to run — is
   corrupted the same way: `EXOMEM_DImABLE_EMBEDDINGm`,
   `EXOMEM_DImABLE_MEDIA_EXTRACTION`, `EXOMEM_DImABLE_CLIP`,
   `EXOMEM_DImABLE_RELEVANCE_CHECK`, `EXOMEM_DImABLE_QUERY_LOG`,
   `EXOMEM_DImABLE_RANKING_CONFIG` (`scripts/smoke-sample-vault.py:25-30`).
   None of these match a real environment variable the server reads (verified:
   `src/exomem/**` reads only the correctly-spelled `EXOMEM_DISABLE_*` names).
   Every `os.environ[...] = "1"` assignment in both scripts is therefore a
   silent no-op — the "lean env" both scripts claim to set never actually
   disables embeddings/media/CLIP/relevance-check/query-log/ranking-config.
   CI has been smoking a script whose env pins are typos, on every PR, since
   the rename landed.
2. **`exomem setup`'s registered command can point into an ephemeral cache.**
   `_server_command` (`src/exomem/setup_wizard.py:46-55`) returns
   `[sys.executable, "-m", "exomem", "--transport", "stdio"]` whenever the
   process isn't running from a repo checkout with `uv` on `PATH`. Under
   `uvx exomem setup`, `sys.executable` resolves inside `uvx`'s ephemeral
   per-invocation cache environment. The registration `claude mcp add` writes
   therefore breaks the moment that cache entry is pruned — a real user
   running the officially-documented zero-install path (`uvx exomem setup`)
   ends up with a server registration that silently stops working later, with
   no signal at registration time that this would happen.
3. **There is no Codex CLI connect path documented anywhere.** README, docs,
   and the CLI reference exactly one MCP client wiring (Claude Code via
   `exomem setup`) and a remote claude.ai path; Codex CLI users have no
   documented `codex mcp add` command or manual config block.

## What Changes

- New `exomem demo` subcommand (`src/exomem/demo.py` + a dispatch line and
  docstring entry in `src/exomem/__main__.py`): the packaged 30-second proof,
  runnable from a bare `uvx exomem demo` with no clone, no config, no
  user-supplied vault. The sample vault moves into the wheel:
  `git mv "examples/sample-vault/Knowledge Base" "src/exomem/_sample_vault/Knowledge Base"`
  (hatchling's `packages = ["src/exomem", ...]` wheel target already ships
  non-Python files living under `src/exomem/` — the shipped `_scaffold/`
  scaffold, with its `.md`/`.yaml` files, is the existing proof of this);
  `examples/sample-vault/README.md` becomes a short pointer to `exomem demo`
  and the new in-package location. `exomem demo` copies the packaged
  `_sample_vault` into a temporary directory (an installed package's
  `site-packages` may be read-only; the command must never mutate the
  install), sets the **correctly-spelled** lean environment variables in that
  process, then runs four timed steps: `doctor` (lean profile) → `find`
  "retrieval" (keyword mode; asserts the known insight page is a hit) →
  `get` that page → `audit` (read-only). Output is one line per step naming
  the step and its duration, then `demo PASS — total <N>s` and a pointer to
  `exomem setup`. Flags: `--json` (a stable envelope
  `{success, steps: [{name, ok, seconds}], total_seconds}` for CI), `--keep`
  (retain the temp vault and print its path so a user can open it directly in
  Obsidian). Exit 1 on any failed step.
- Delete `scripts/demo-sample-vault.py` and `scripts/smoke-sample-vault.py` —
  both corrupted, both superseded by the packaged command. CI's per-Python
  "Sample vault smoke" step becomes `uv run exomem demo --json`, so CI now
  exercises the exact command a real user runs, rather than a repo-only
  script with silently no-op env pins.
- Durability fix in `_server_command` (`src/exomem/setup_wizard.py:46-55`):
  keep the existing repo-checkout branch first (`pyproject.toml` present and
  `uv` on `PATH` → `uv --directory <repo> run python -m exomem --transport
  stdio`, unchanged); otherwise prefer the durable `exomem` console script
  (`shutil.which("exomem")`, the result of a `pip install`/`uv tool install`)
  over `sys.executable -m exomem`; when neither resolves (a transient
  `uvx exomem setup` invocation with no durable install anywhere), fall back
  to `["uvx", "exomem", "--transport", "stdio"]` and print a note recommending
  `uv tool install exomem` for a registration that survives cache pruning.
- Codex CLI connect path: a new README "Connect your agent" matrix — Claude
  Code (via `exomem setup`), Codex CLI (`codex mcp add exomem --env
  EXOMEM_VAULT_PATH=... -- exomem --transport stdio`, plus the equivalent
  manual `~/.codex/config.toml` `[mcp_servers.exomem]` block), claude.ai
  remote (a pointer to the remote deployment guide), other MCP clients (a
  generic stdio JSON server-config example). The exact current Codex CLI flag
  syntax is re-verified at implementation time (drift risk — tracked as an
  explicit task) rather than trusted from this proposal.
- README restructure around two one-liners: "Prove it in 30 seconds"
  (`uvx exomem demo` + expected output) placed **before** Install; "Set it up
  in 5 minutes" (`exomem setup`); an Install section covering `pip`/`uv tool
  install`; the connect matrix; deeper docs unchanged. `SETUP-LOCAL.md`'s
  intro is reframed as the full-control/manual path, pointing back at
  `exomem demo`/`exomem setup` as the faster entry points.
- New CI job `onboarding` (the wheel-path gate): `uv build` → fresh venv →
  install **only** the built wheel (no repo checkout in that venv) →
  `exomem demo --json` from a temporary working directory (proves
  `_sample_vault` ships inside the wheel and the demo has no repo
  dependency) → `exomem setup --vault <tmp> --yes --skip-claude-register`
  (proves the wizard works from a wheel-only install) → assert both succeed
  and the sequence completes within a documented wall-time budget. New
  `scripts/time-to-value.py` runs the same build → install → demo → setup
  sequence locally with a per-step timing breakdown — the measurable
  artifact behind the "prove it, set it up" time claims.
- Leak-guard: extend `tests/test_scaffold_no_leak.py`'s structural
  `LEAK_PATTERNS` scan (today scoped to `SCAFFOLD_SCHEMA` —
  `src/exomem/_scaffold/_Schema/` — only) to also cover
  `src/exomem/_sample_vault/`. The separate token-denylist scan
  (`test_source_ships_no_personal_tokens`) already covers it automatically
  since it walks all of `src/exomem/**`.
- Tests: `tests/test_demo.py` (packaged-vault resolution inside the installed
  package, containing `_Schema/SKILL.md`; happy-path exit 0 with all four
  named+timed steps via `main(["demo"])` + `capsys`; `--json` schema
  validation; temp isolation — installed-package file mtimes untouched, temp
  dir removed unless `--keep`; a corrupted-vault fixture exits 1; a regression
  test asserting the exact corrected env-var spellings are the ones set;
  `demo` and `warm` stay absent from `_core_op_names()` so registry growth
  can never shadow them). `tests/test_setup_wizard.py` additions for the
  `_server_command` preference order (console script present → used; absent
  + repo checkout → unchanged `uv --directory` path; neither → `uvx` fallback
  + durability note).

**Pure-substrate note**: `exomem demo` is deterministic CLI plumbing over
existing leaf functions (`doctor`/`find`/`get_page`/`audit`) that already run
today; it adds no reasoning surface, no new model, and no server-side judgment
— it only sequences and times calls a user could already make by hand.

## Capabilities

### Modified Capabilities

- `install-readiness`: the packaged zero-install demo command, the CI/release
  smoke gate now running that command instead of the two corrupted repo
  scripts, a new wheel-path CI gate proving the demo and the setup wizard both
  work from a wheel-only install with no checkout, the setup wizard's durable
  server-registration preference order, and the documented agent-connect
  matrix are all install-readiness concerns — this change touches no other
  capability.

## Impact

- Code: new `src/exomem/demo.py`; dispatch line + docstring entry in
  `src/exomem/__main__.py`; `_server_command` reorder in
  `src/exomem/setup_wizard.py:46-55`.
- Moved: `examples/sample-vault/Knowledge Base/` →
  `src/exomem/_sample_vault/Knowledge Base/` (git mv, preserving history);
  `examples/sample-vault/README.md` rewritten as a pointer.
- Deleted: `scripts/demo-sample-vault.py`, `scripts/smoke-sample-vault.py`.
- New: `scripts/time-to-value.py`.
- CI/docs: `.github/workflows/ci.yml` (rewired smoke step + new `onboarding`
  job), `README.md` (proof-first restructure, connect matrix), `SETUP-LOCAL.md`
  (intro reframed), `docs/release.md` (checklist commands updated).
- Tests: new `tests/test_demo.py`; additions to `tests/test_setup_wizard.py`
  and `tests/test_scaffold_no_leak.py`.
- Dependencies: none new. Reuses `doctor`, `find`, `get_page`, `audit`,
  `shutil`, and the existing `subprocess`/`which` seams `setup_wizard.py`
  already injects for testability.
- Release: this adds new public CLI surface (`exomem demo`), so per
  `docs/release.md`'s pre-1.0 policy it lands as a `feat:` (minor) commit.
