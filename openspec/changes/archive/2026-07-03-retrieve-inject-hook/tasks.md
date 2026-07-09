# Tasks — Retrieve-Inject Hook

## 1. Tests First (pure-logic, seams mocked — no real network/subprocess calls)

- [x] 1.1 Add `tests/test_retrieve_inject.py`. Import `kb_retrieve_nudge.py` as a
      module (it is not a package — use
      `importlib.util.spec_from_file_location` against
      `Path(exomem.__file__).parent / "_hooks" / "kb_retrieve_nudge.py"`, the
      same script path `test_install_hook.py` already resolves for its
      subprocess tests) so seam functions can be monkeypatched in-process,
      matching the `doctor_module._probe_get` monkeypatch precedent in
      `tests/test_doctor_probe.py`.
- [x] 1.2 Add `_env_flag`-equivalent truthy-parse unit tests for
      `KB_RETRIEVE_INJECT` / `KB_RETRIEVE_INJECT_CLI`: unset/`""`/`0`/`false`/
      `no`/`off` (any case) → disabled; anything else → enabled.
- [x] 1.3 Add unit tests for the stub-block formatter (e.g.
      `_format_inject_block(hits) -> str`): 0 hits → `""`; 1-3 hits → one
      `- path (type, updated)` line per hit in input order; a block that would
      exceed ~400 chars is truncated to ~400 chars with a trailing marker;
      output never contains `excerpt` text (guard against a future caller
      accidentally passing full hit dicts instead of compact ones).
- [x] 1.4 Add unit tests for the REST seam (e.g. `_fetch_via_rest(prompt,
      api_key, timeout) -> list[dict] | None`) with the actual HTTP call
      monkeypatched: success (`{"success": true, "data": [...]}` → hit list),
      `{"success": false, ...}` → `None`, non-200 → `None`, connection
      error/timeout → `None` (never raises).
- [x] 1.5 Add unit tests for the CLI seam (e.g. `_fetch_via_cli(prompt, limit,
      timeout) -> list[dict] | None`) with `shutil.which` and the subprocess
      call both monkeypatched: `exomem` resolvable → invoked with
      `find --detail compact --limit 3 --mode keyword --json <prompt>` and its
      JSON stdout parsed; neither `exomem` nor `kb` resolvable → `None` without
      attempting a subprocess call; non-zero exit / malformed JSON / timeout →
      `None` (never raises).
- [x] 1.6 Add ladder-decision unit tests (calling the script's top-level
      decision function directly, with `_fetch_via_rest`/`_fetch_via_cli`
      monkeypatched, not real network/subprocess): REST configured+reachable →
      REST hits used, CLI seam never called; REST configured but failing +
      `KB_RETRIEVE_INJECT_CLI` unset → nudge-only, CLI seam never called; REST
      unconfigured + `KB_RETRIEVE_INJECT_CLI` set → CLI hits used; neither
      configured → nudge-only, neither seam called.
- [x] 1.7 Add a subprocess-level black-box test (matching the existing
      `test_install_hook.py` `_run(RETRIEVE_SCRIPT, event, home)` pattern) that
      `KB_RETRIEVE_INJECT` unset produces byte-identical stdout to the current
      un-augmented behavior for the same fixture inputs already covered by
      `test_retrieve_fires_on_substantial_prompt` /
      `test_retrieve_silent_on_short_prompt` /
      `test_retrieve_cooldown_suppresses_second_fire` in
      `tests/test_install_hook.py` — proving the default-off path is untouched
      end-to-end, not just at the unit level.
- [x] 1.8 Run the new test file (expected to fail/error against the
      not-yet-implemented seams — TDD red) before starting section 2.

## 2. Hook Script Implementation (`src/exomem/_hooks/kb_retrieve_nudge.py`)

- [x] 2.1 Add the `_env_flag`-equivalent truthy parser (mirrors
      `extract.py::_env_flag`'s `_FALSY_ENV = {"", "0", "false", "no", "off"}`
      convention) and gate the whole inject path behind
      `_env_flag("KB_RETRIEVE_INJECT")`; when falsy, fall through to today's
      exact code path unchanged.
- [x] 2.2 Add `_fetch_via_rest(prompt, api_key, limit=3, timeout=2.0) ->
      list[dict] | None` using stdlib `urllib.request`/`urllib.error` only (no
      new dependency): `POST http://127.0.0.1:8765/api/find`,
      `Content-Type: application/json`, `Authorization: Bearer <api_key>`,
      body `{"query": prompt, "detail": "compact", "limit": limit, "mode":
      "keyword"}`; parse the `{"success", "data"}` envelope; return `None` on
      any failure (never raise).
- [x] 2.3 Add `_fetch_via_cli(prompt, limit=3, timeout=5.0) -> list[dict] |
      None`: resolve `shutil.which("exomem") or shutil.which("kb")`; if
      neither resolves, return `None` immediately (no subprocess spawned); else
      run `[resolved, "find", "--detail", "compact", "--limit", str(limit),
      "--mode", "keyword", "--json", prompt]` via `subprocess.run(...,
      capture_output=True, text=True, timeout=timeout)` and parse its JSON
      stdout the same way as 2.2; return `None` on any failure (never raise).
- [x] 2.4 Add `_format_inject_block(hits: list[dict]) -> str`: `""` for empty
      `hits`; else a short header ("KB routing stubs — verify with `get`
      before relying on these:") plus one `- path (type, updated)` line per hit
      (using the compact-dict fields already present:
      `path`/`type`/`updated`), truncated to ~400 chars total.
- [x] 2.5 Wire the ladder into `main()`: after the existing min-chars and
      cooldown gates pass, if inject is enabled, attempt REST (only when
      `EXOMEM_REST_API_KEY` is set) then, only on REST failure/absence, CLI
      (only when `KB_RETRIEVE_INJECT_CLI` is truthy); build
      `additionalContext` as the stub block (if any hits) followed by the
      existing `REMINDER` text; when inject is disabled or every rung is
      unusable/empty, emit exactly today's `REMINDER`-only output.
- [x] 2.6 Update the module docstring's "Tunables (env)" list to document
      `KB_RETRIEVE_INJECT` and `KB_RETRIEVE_INJECT_CLI`, matching the existing
      docstring style.
- [x] 2.7 Confirm no change to `kb-retrieve-nudge.sh` or `install_hook.py` is
      needed (both already generic over "whatever `kb_retrieve_nudge.py`
      prints"); re-read both to double check.
- [x] 2.8 Run `tests/test_retrieve_inject.py` and the retrieval-gate tests in
      `tests/test_install_hook.py` until green (TDD green).

## 3. Docs

- [x] 3.1 Update `SETUP-LOCAL.md` section 7 ("Make the KB automatic — both
      directions") to describe the inject upgrade: what it adds over the
      plain reminder, the `KB_RETRIEVE_INJECT=1` / `KB_RETRIEVE_INJECT_CLI=1`
      opt-ins, the REST-first/CLI-opt-in/nudge-only ladder in one or two
      sentences, and the requirement to export `EXOMEM_REST_API_KEY` in the
      same shell/profile Claude Code inherits from for the REST rung to be
      used.
- [x] 3.2 Update `README.md`'s hook one-liner (the `install-hook` bullet under
      "Read") with a one-line mention of the inject opt-in, pointing to
      SETUP-LOCAL for details — matching how README already defers detail to
      SETUP-LOCAL elsewhere.
- [x] 3.3 Confirm no scaffold/reference doc under `src/exomem/_scaffold/`
      mentions the retrieve hook's exact behavior (it doesn't, per the current
      hooks section living only in SETUP-LOCAL/README); no scaffold edit
      needed, but record that this was checked.

## 4. Validation

- [x] 4.1 Run `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q -k "retrieve_inject or install_hook"`
      focused, then the full suite:
      `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q`.
- [ ] 4.2 Run `uv run ruff check`. NOT run this pass: the task instructions this
      change was implemented under explicitly prohibited `uv sync`/`uv run`
      (lock churn), and this worktree's `.venv` has no standalone `ruff`
      module/executable. Reviewed the two changed/added files
      (`kb_retrieve_nudge.py`, `test_retrieve_inject.py`) by hand instead
      (import ordering, no unused imports, consistent style with neighboring
      hook code). Re-run `uv run ruff check` once lock churn is acceptable.
- [x] 4.3 Confirm `KB_RETRIEVE_INJECT` unset reproduces byte-identical output
      to the pre-change script for every existing `test_install_hook.py`
      retrieval-gate fixture (task 1.7's assertion, re-checked here as a final
      gate).
- [x] 4.4 Run `npm exec --yes @fission-ai/openspec -- validate retrieve-inject-hook --strict`,
      then `npm exec --yes @fission-ai/openspec -- validate --changes --strict`
      until both are clean.
