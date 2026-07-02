# Design — rename internals to exomem

## D1. Package move + import shim

`git mv src/kb_mcp src/exomem`. The package uses relative imports internally, so the move
itself is cheap; remaining `kb_mcp` strings inside `src/` are docstrings/messages and are
swept.

The `kb_mcp` shim is a **meta-path alias**, not a re-export package. A `__path__`-sharing or
`__getattr__` shim would let `import kb_mcp.find` load `find.py` a second time under the old
name — two module instances, two copies of module state (caches, locks, singletons), which
is exactly the failure mode a KB-recorded sqlite/idempotency bug class comes from. The
meta-path finder intercepts any `kb_mcp[.X]` import and returns a spec whose loader hands
back the already-imported `exomem[.X]` module object, so `kb_mcp.find is exomem.find` holds
and there is exactly one module state. `kb_mcp/__init__.py` registers the finder, emits one
`DeprecationWarning`, and re-binds itself to `exomem`; `kb_mcp/__main__.py` delegates to
`exomem.__main__.main()` so `python -m kb_mcp` keeps working.

Wheel: `packages = ["src/exomem", "src/kb_mcp"]` (hatchling).

## D2. Env identity: canonical EXOMEM_*, promoted KB_MCP_*

Internal code reads **only** `EXOMEM_*` after a mechanical `KB_MCP_` → `EXOMEM_` sweep of
`src/` (call sites keep their shape — direct `os.environ` reads stay direct).

Compatibility is a **process-env promotion**, not a per-read fallback:
`exomem.env_compat.promote_legacy()` iterates `os.environ`, and for every `KB_MCP_X` sets
`EXOMEM_X = value` iff `EXOMEM_X` is unset. It runs at `exomem/__init__.py` import — before
any module-level or call-time env read can observe a miss — and is exposed publicly so any
late env loading (service wrappers, dotenv-style setup) can re-run it. One log line fires
when legacy names were promoted, naming the preferred prefix.

Why promotion over a fallback helper at every read: there are ~250 env read sites across
~35 modules in several shapes (`.get`, `[...]`, `in os.environ`); rewriting them all into a
helper multiplies the diff and the review risk for identical behavior. Promotion is one
tested function; the only case it misses is a process that sets `KB_MCP_*` *after* import
without calling `promote_legacy()` again — acceptable for env vars (set-before-start is the
contract) and covered in the compat tests via an explicit re-run.

Precedence: an explicitly-set `EXOMEM_X` always wins over a legacy `KB_MCP_X`.

## D3. Sweep boundaries

- `src/` (including the shipped `_scaffold/_Schema` skill docs — user-facing, so they teach
  the new names), `tests/`, `scripts/`, `docs/`, `README.md`, `SETUP-LOCAL.md`,
  `CONTRIBUTING.md`, repo `CLAUDE.md`, `openspec/config.yaml` (test command).
- NOT swept: `openspec/changes/archive/**` and past spec deltas (historical records);
  `CHANGELOG.md` (history); KB/vault content (out of repo).
- The main `openspec/specs/**` current-truth specs ARE swept (they describe the system as it
  is; `KB_MCP_*` mentions there would be stale after this change) — with the old names noted
  once in the new `exomem-identity` spec as the supported legacy surface.

## D4. Test strategy

The swept suite itself proves the new names end-to-end (imports + env gates). The legacy
surfaces get dedicated `tests/test_rename_compat.py`:
- `import kb_mcp` warns `DeprecationWarning`; `kb_mcp.find is exomem.find` (module identity);
  `from kb_mcp import embeddings` returns the exomem module object.
- `python -m kb_mcp --help` exits 0 (subprocess).
- `promote_legacy()`: legacy-only var becomes visible under the new name; explicit new-name
  value is not clobbered; re-run picks up late-set legacy vars.

## D5. Rollout / box ops (documented follow-ups, not code)

After merge + release: on the service box, re-register the Windows service as `exomem`
(`install-service.ps1`), rename the deploy checkout folder, and optionally migrate its
`.env` to `EXOMEM_*` keys at leisure (legacy keys keep working indefinitely via promotion).

## Risks

- **Merge collisions**: this touches every test file's import line; land it while no other
  PR is in flight (verified: only the release-please PR is open).
- **Double-import hazard** is the core correctness risk of any alias shim — addressed by the
  meta-path design and pinned by the module-identity test.
- **Late env reads**: any env var read at module-import time *before* `exomem/__init__`
  finishes would miss promotion — impossible by construction (promotion is the first
  statement executed in the package `__init__`).
- Ruff/leak-guard: sweep must not introduce findings; leak guard is name-agnostic here
  (exomem/kb-mcp are both project tokens, not personal ones) but is re-run regardless.
