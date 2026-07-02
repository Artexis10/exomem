## 1. Leak-Guard + Sample-Vault Move (tests first)

- [x] 1.1 Extend `tests/test_scaffold_no_leak.py`'s structural scan (currently
      `test_scaffold_ships_no_personal_data` walks only
      `SCAFFOLD_SCHEMA` = `src/exomem/_scaffold/_Schema/`) to also walk
      `src/exomem/_sample_vault/` against the same `LEAK_PATTERNS`. Confirm
      (do not modify) that `test_source_ships_no_personal_tokens` already
      covers the new directory, since it walks all of `src/exomem/**`.
- [x] 1.2 `git mv "examples/sample-vault/Knowledge Base" "src/exomem/_sample_vault/Knowledge Base"`.
      Rewrite `examples/sample-vault/README.md` as a short pointer to
      `exomem demo` and the new in-package location.
- [x] 1.3 Run the extended leak-guard tests against the moved vault; fix any
      flagged line (expected: none — the sample vault was already public and
      committed).

## 2. `tests/test_demo.py` Red, Then `src/exomem/demo.py` Green

- [x] 2.1 Write `tests/test_demo.py` (red first): packaged-vault resolution
      (the vault resolves inside the *installed* `exomem` package and
      contains `_Schema/SKILL.md`); happy-path exit 0 with all four
      named+timed steps via `main(["demo"])` + `capsys`; `--json` envelope
      schema (`success`, `steps: [{name, ok, seconds}]`, `total_seconds`);
      temp isolation (installed-package file mtimes unchanged after a run;
      the temp vault directory is removed unless `--keep`, in which case its
      path is printed and it survives); a corrupted-vault fixture (broken
      wikilink or missing target page) causes exit 1 naming the failing step;
      a regression test asserting the exact env vars set are the
      correctly-spelled `EXOMEM_DISABLE_EMBEDDINGS` /
      `EXOMEM_DISABLE_MEDIA_EXTRACTION` / `EXOMEM_DISABLE_CLIP` /
      `EXOMEM_DISABLE_RELEVANCE_CHECK` / `EXOMEM_DISABLE_QUERY_LOG` /
      `EXOMEM_DISABLE_RANKING_CONFIG` (the regression test for the rename
      corruption this change fixes); `"demo"` and `"warm"` stay absent from
      `_core_op_names()` so future registry growth can never shadow either
      CLI-only verb.
- [x] 2.2 Implement `src/exomem/demo.py`: resolve the packaged vault via
      `Path(exomem.__file__).resolve().parent / "_sample_vault" / "Knowledge Base"`,
      copy it to a fresh temp directory, set the lean env vars in-process, and
      run `doctor(vault=..., profile="lean")` →
      `find(vault, query="retrieval", mode="keyword", limit=3, graph=False)`
      (asserting the known insight page is a hit) →
      `get_page(vault, path=...)` → `audit(vault, categories=[...])` in order,
      timing each step; print the per-step + summary lines; implement
      `--json`/`--keep`/exit-code handling; add the `demo` dispatch line and a
      one-line docstring entry to `src/exomem/__main__.py` (matching the
      `setup`/`doctor`/`warm` dispatch pattern already there).
- [x] 2.3 Run `tests/test_demo.py` to green without weakening any assertion
      from 2.1.

## 3. `_server_command` Durability: Red, Then Green

- [x] 3.1 Add `tests/test_setup_wizard.py` cases for `_server_command`'s new
      order: (a) repo checkout present (unchanged) → `uv --directory` form,
      even when `which_fn("exomem")` also resolves; (b) no repo checkout,
      `which_fn("exomem")` resolves → that console script's path is used, not
      `sys.executable`; (c) no repo checkout, `which_fn` returns `None` for
      everything → `["uvx", "exomem", "--transport", "stdio"]` plus a printed
      note recommending `uv tool install exomem`.
- [x] 3.2 Reorder `_server_command` in `src/exomem/setup_wizard.py:46-55` to
      match, and thread the printed durability note through `run_setup`'s
      existing `report()`/`print_fn` calls for the registration step.
- [x] 3.3 Run the full `tests/test_setup_wizard.py` suite to confirm no
      existing case (fresh vault, rerun-converges, foreign-skill,
      no-claude-cli, doctor-failure-aborts) regresses.

## 4. Delete Corrupted Scripts, Rewire CI

- [x] 4.1 Delete `scripts/demo-sample-vault.py` and
      `scripts/smoke-sample-vault.py`.
- [x] 4.2 In `.github/workflows/ci.yml`, replace the `test` job's "Sample
      vault smoke" step (`uv run --python ${{ matrix.python-version }} python
      scripts/smoke-sample-vault.py`) with
      `uv run --python ${{ matrix.python-version }} exomem demo --json`.
- [x] 4.3 Add a new `onboarding` job to `.github/workflows/ci.yml`: `uv build`
      → create a fresh virtualenv → install only the built wheel (no
      checkout on `PYTHONPATH`, no editable install) → run `exomem demo
      --json` from a temporary working directory → run `exomem setup --vault
      <tmp> --yes --skip-claude-register` → assert both succeed and the
      combined sequence stays under the wall-time budget chosen in 4.4.
- [x] 4.4 Add `scripts/time-to-value.py`: reproduces the `onboarding` job's
      build → fresh-venv → install → `demo` → `setup` sequence locally with a
      per-step timing breakdown; measure a baseline run and use it to set the
      budget asserted in 4.3.

## 5. Codex CLI Connect Path (drift risk — verify before writing)

- [x] 5.1 Re-verify the current `codex mcp add` flag syntax and the
      `~/.codex/config.toml` `[mcp_servers.*]` schema against the installed
      Codex CLI version — this proposal's syntax is a best-effort draft, not
      a verified contract; adjust wording to match whatever is current at
      implementation time.
- [x] 5.2 Write the README "Connect your agent" matrix: Claude Code (via
      `exomem setup`), Codex CLI (`codex mcp add` command + the manual
      `~/.codex/config.toml` block), claude.ai remote (pointer to the remote
      deployment guide), other MCP clients (generic stdio JSON server-config
      example).

## 6. README / Docs Restructure

- [x] 6.1 Reorder `README.md` around: "Prove it in 30 seconds"
      (`uvx exomem demo` + expected output) before Install; "Set it up in 5
      minutes" (`exomem setup`); Install (`pip`/`uv tool install`); the
      connect matrix from Task 5; deeper docs unchanged below that.
- [x] 6.2 Reframe `SETUP-LOCAL.md`'s intro as the full-control/manual path,
      pointing back at `exomem demo`/`exomem setup` as the faster entry
      points, without changing its existing manual-steps content.
- [x] 6.3 Update `docs/release.md`'s pre-release checklist: remove the two
      deleted scripts, add `uv run exomem demo` and
      `uv run python scripts/time-to-value.py`.

## 7. Validation

- [x] 7.1 Run `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m
      pytest -q`; confirm the full suite is green, including every new test
      from Tasks 1-3.
- [x] 7.2 Run `uv run ruff check`.
- [x] 7.3 Run `npm exec --yes @fission-ai/openspec -- validate --changes
      --strict` (and `--specs --strict` once archived) until clean.
- [x] 7.4 Run `uv build`, install only the built wheel into a fresh venv, and
      confirm `exomem demo --json` and `exomem setup --vault <tmp> --yes
      --skip-claude-register` both succeed with no repo checkout present —
      the local reproduction of the `onboarding` CI job.
