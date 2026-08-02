# Proposal — add-terminal-ui

## Why

Exomem's depth is real but its human surface is thin. The product-flow benchmark
(`docs/product-gap-matrix.md`) rates first-run setup as the matrix's only "Behind":
`setup` works but "still feels like configuration rather than immediate product use",
and the recorded target is to reduce the first-run mental model to *demo, choose
vault, scan, initialize*. The recorded product backlog names a "product UI/admin
surface: pack selection, adoption review, source backlog, evidence/case view,
audit/review queue" as a priority. Meanwhile one-shot CLI invocations pay Python
import startup (~11–12 s on the Windows reference host, ~3.1 s on WSL2 —
`openspec/changes/reduce-memory-and-startup-overhead`), which makes a
command-per-action workflow feel slow even though warm in-process reads measure in
milliseconds.

A persistent, keyboard-first terminal UI (`exomem tui`) closes both gaps at once:
one long-lived process amortizes startup, and a guided surface makes capture, ask,
review, packs, adoption, and diagnostics reachable without memorizing the 26-command
registry. It also gives the measured degradation signals a face: the `find`
`degraded`/`warming` markers and the doctor/readiness checks exist precisely so a
client can show *why* results are partial instead of appearing hung.

The browser Review Studio already owns the deep proposal-first review loop; this
change deliberately does not duplicate it. The TUI's Review screen is a queue-and-
triage surface over the same governed commands, and it points at the Studio for
flows the Studio owns (compile/supersession/relation proposals).

## What Changes

- New optional dependency extra `tui = ["textual>=5.0"]` and dependency group
  `tui-dev = ["pytest-textual-snapshot>=1.0"]`. Default-off and soft-fail: a lean
  or server install never carries the UI stack; `exomem tui` without the extra
  prints a one-line install hint (`uv sync --extra tui` / `pip install
  'exomem[tui]'`) and exits non-zero, no traceback. No model, no reasoning
  surface, no network: the TUI is presentation over existing measurements
  (pure-substrate unaffected).
- New CLI subcommand `exomem tui` in `__main__._dispatch_main` (CLI-only admin
  verb, parallel to `warm`/`doctor`; NOT on the unified command registry — it is a
  client of the registry, not a command). Bare `exomem` (serve) is unchanged.
  Non-TTY stdin/stdout exits 2 with a clear message.
- New package `src/exomem/tui/`: a Textual application that is a thin client over
  the existing unified command registry. Every knowledge read/write goes through
  the same invocation seam the CLI uses — `cli_ops.coerce` → `resolve_vault` →
  optional schema injection → `capabilities.active_surface` + owner principal →
  `writer_lease.invoke_command` — so TUI semantics cannot drift from CLI/MCP/REST.
- Smallest shared boundary: the invocation body currently inlined in
  `__main__._core_op_main` is lifted into a reusable module-level function so the
  CLI and the TUI call literally the same code (isolated in a dedicated commit for
  clean reconciliation with concurrent branches). No second storage path, no
  duplicated retrieval/review/adoption logic, no direct vault file writes from TUI
  code.
- Screens: Home (actionable status), Ask (staged recall with evidence, citations,
  degraded-lane marker, copyable deep-context packet, governed write-back),
  Capture (low-friction thought/source/insight capture with auto-derived title),
  Review (Epistemic Inbox queue + dismiss/snooze/reopen triage + item context),
  Continue (recent memory/continuation context), Adopt (pre-init-safe scan-only
  dry run, explicit confirm before copy-as-sources), Packs (catalog + multi-select
  over the persisted selection), Status (doctor/resource/readiness/hooks with
  remediations), Settings (safe subset: compute mode, appearance; secrets never
  shown).
- Global navigation: command palette, `?` help overlay, consistent back/escape
  semantics that never silently discard typed input, confirmation for destructive
  actions, visible focus, no status conveyed by color alone, `NO_COLOR` respected,
  ASCII fallbacks, responsive layouts from 80x24 up.
- Async discipline: every registry call runs on a worker thread; reads are
  cancellable; mutations are never silently abandoned mid-flight (progress until a
  terminal state). Measured basis: warm reads are ms-scale, compiled writes are
  seconds-scale (`docs/benchmarks.md`).
- Tests: headless Textual pilot tests, view-model unit tests, service-boundary
  tests against a synthetic temp vault, snapshot (SVG golden) tests at 80x24 and
  120x40, non-TTY and cancellation tests. No paid credentials; suite runs lean
  (`EXOMEM_DISABLE_EMBEDDINGS=1`). TUI tests skip cleanly when the `tui` extra is
  absent so the existing lean matrix is unaffected.
- Docs: `docs/tui.md` (install, launch, key map, screens, first-run, limitations,
  troubleshooting, running the TUI test/snapshot suite) + README pointer.

## Capabilities

### New Capabilities

- `terminal-ui`: the `exomem tui` entry point and its soft-fail/non-TTY contract;
  the thin-client rule binding the TUI to the unified registry invocation seam;
  the Home/Ask/Capture/Review/Continue/Adopt/Packs/Status/Settings behavioral
  requirements including honest capability exposure (no dead buttons), degraded
  and warming surfacing, first-run onboarding, keyboard/accessibility rules,
  responsive layout floors, and deterministic headless testing requirements.

### Modified Capabilities

- None. The unified command registry, its schemas, and every existing surface
  (MCP/CLI/REST) are untouched; the TUI is an additional client.

## Impact

- Code: `src/exomem/tui/` (new: app, screens, widgets, backend seam, theme),
  `src/exomem/__main__.py` (subcommand dispatch + `_CLI_ONLY_SUBCOMMANDS` entry),
  one shared-invocation function extracted from `__main__._core_op_main`
  (dedicated commit), `pyproject.toml` (+`tui` extra, +`tui-dev` group),
  `uv.lock` (regenerated).
- Surfaces: one new CLI-only verb `exomem tui`. No registry change; capabilities
  doc (`docs/capabilities.md`) is unaffected (`generate-capabilities.py --check`
  stays green).
- Tests: `tests/tui/` (new suite incl. snapshot goldens), all skipped when
  `textual` is not importable; existing lean matrix, latency gate, golden
  retrieval tests untouched.
- Dependencies: `textual` (pure-Python, builds on the already-present `rich`)
  behind the default-off `tui` extra; `pytest-textual-snapshot` in the opt-in
  `tui-dev` group. Soft-fail contract stated above.
- Docs: `docs/tui.md` (new), README/QUICKSTART pointer.
