# Tasks — Install Readiness

## 1. Spec and diagnostics
- [x] 1.1 Add OpenSpec change for install-readiness.
- [x] 1.2 Add a read-only `doctor` module with profiles `lean`, `hybrid`, `media`,
      and `remote`.
- [x] 1.3 Wire `python -m kb_mcp doctor` with `--vault`, `--profile`, and `--json`.

## 2. Documentation
- [x] 2.1 Rewrite README quickstart to use `uv sync` and `uv run`.
- [x] 2.2 Rewrite SETUP-LOCAL install/setup commands to be `uv`-first.
- [x] 2.3 Keep pip fallback documented as secondary.

## 3. Tests and validation
- [x] 3.1 Add torch-free tests for `doctor` report logic and CLI exit codes.
- [x] 3.2 Run focused tests, then full pytest.
- [x] 3.3 Run ruff and `openspec validate improve-install-readiness --strict`.
