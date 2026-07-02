# add-setup-wizard — tasks

## 1. Wizard core (test-first)

- [ ] 1.1 Write `tests/test_setup_wizard.py`: scripted `input_fn`, recording
      `run_fn`, fake `which_fn`, tmp `home` — fresh-vault happy path,
      full re-run → all `[skipped]` exit 0, KB-exists skip, foreign-skill
      preserve, no-CLI snippet path, doctor-fail `--yes` → exit 1,
      registration argv shape (scope/env/`--` separator), `--yes` without
      `--vault` → exit 2.
- [ ] 1.2 Implement `src/kb_mcp/setup_wizard.py`: `run_setup(...)` with the
      injected seams, converger steps per design, utf-8 subprocess handling.

## 2. CLI dispatch + docs

- [ ] 2.1 Wire `setup` into `src/kb_mcp/__main__.py` (dispatch + module
      docstring subcommand list) with `_setup_main` argparse (`exomem setup`).
- [ ] 2.2 README "Local quickstart": three-line path ending in
      `uv run python -m kb_mcp setup`.
- [ ] 2.3 SETUP-LOCAL.md: "One command" section on top; retitle existing steps
      2–7 as the manual path ("what `setup` does under the hood").

## 3. Verification

- [ ] 3.1 `uv run pytest -q` + `ruff check` green; run `exomem setup` end-to-end
      against a scratch vault shaped like a daily-notes vault (pre-init digest,
      re-run idempotency, registration argv on Windows).
