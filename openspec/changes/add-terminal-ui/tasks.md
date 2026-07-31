# Tasks — add-terminal-ui

## 1. Shared invocation seam (dedicated commit)

- [ ] 1.1 Tests: invoking a registry command through the extracted seam matches
      `_core_op_main` semantics — coercion, vault resolution, schema injection,
      surface descriptor, owner principal, writer-lease wrapping, and the
      pre-init allowances for `browse_memory`/`adopt_vault` scan-only.
- [ ] 1.2 Extract the invocation body from `__main__._core_op_main` into a
      reusable function; re-wire the CLI through it (behavior-identical; existing
      CLI tests stay green).

## 2. Backend facade + fakes

- [ ] 2.1 Tests: `tui.backend` typed wrappers (ask/read/capture/remember/review/
      triage/adopt/packs/status/mode) call the seam with the right command names
      and kwargs; errors normalize to the shared envelope (code/message/
      remediation); all against a temp synthetic vault.
- [ ] 2.2 Implement `src/exomem/tui/backend.py` (thread-offloaded, cancellable
      reads) plus a deterministic `FakeBackend` for UI tests.

## 3. Entry point

- [ ] 3.1 Tests: `exomem tui` in `_CLI_ONLY_SUBCOMMANDS`; non-TTY exits 2 with
      one-line message; missing-`textual` exits with install hint and no
      traceback; bare `exomem` dispatch unchanged.
- [ ] 3.2 Implement `_tui_main` dispatch with lazy import + soft-fail.

## 4. App shell

- [ ] 4.1 Tests: screen registry/navigation, help overlay toggle, command
      palette lists major actions, escape-with-input confirmation, focus
      visibility, NO_COLOR launch does not crash.
- [ ] 4.2 Implement app, theme (dark/light/terminal-native + ASCII fallback),
      key map, palette commands, help overlay, responsive breakpoints.

## 5. Home

- [ ] 5.1 Tests: Home view-model composes vault identity, mode, attention count,
      packs, hooks, warming state from backend results; warning rows carry next
      actions; empty/first-run variants.
- [ ] 5.2 Implement Home screen + snapshot at 80x24 and 120x40.

## 6. Ask

- [ ] 6.1 Tests: query lifecycle (idle→running→results/error/cancelled), late
      results dropped after cancel, degraded marker rendering, result preview via
      read command, deep-context copy, filter narrowing, write-back draft →
      confirm → remember invocation.
- [ ] 6.2 Implement Ask screen (input, staged progress, results, evidence panel/
      overlay, write-back modal) + snapshots.

## 7. Capture

- [ ] 7.1 Tests: auto-title derivation, kind selection mapping (thought→source,
      insight→remember), confirmation shows path, error keeps input, escape
      guard.
- [ ] 7.2 Implement Capture screen + snapshots.

## 8. Review

- [ ] 8.1 Tests: queue rendering (severity/categories/reasons/refs), item
      context fetch, dismiss/snooze(date-required)/reopen invocations, state
      refresh, Studio pointer for unsupported depth, empty state.
- [ ] 8.2 Implement Review screen + snapshots.

## 9. Status

- [ ] 9.1 Tests: view-model merges doctor/resource/readiness/hook-check/install
      info; failing checks show remediation; no secret values rendered; render
      triggers no writes/downloads.
- [ ] 9.2 Implement Status screen + snapshot.

## 10. Packs

- [ ] 10.1 Tests: catalog list with benefits, current selection load,
      multi-select apply persists via supported path, stable IDs, reopen shows
      persisted state.
- [ ] 10.2 Implement Packs screen + snapshot; record the science-research /
      people-relationships pack assessment in design.md (implement only if the
      existing catalog model supports them cleanly).

## 11. Adopt

- [ ] 11.1 Tests: pre-init scan-only succeeds against a temp folder and writes
      nothing (tree hash unchanged); report rendering; write modes gated behind
      explicit confirmation; never pointed at a real vault.
- [ ] 11.2 Implement Adopt screen (path input, scan report, gated next actions)
      + snapshot.

## 12. Continue

- [ ] 12.1 Tests: recent-context composition from supported sources; honest
      empty state when nothing is available; copyable continuation packet.
- [ ] 12.2 Implement Continue screen scoped to real backend support.

## 13. Settings

- [ ] 13.1 Tests: mode read/switch persists via mode module; storage location
      shown; no secret display; unsupported settings not editable.
- [ ] 13.2 Implement Settings screen + snapshot.

## 14. Onboarding

- [ ] 14.1 Tests: unresolvable vault routes to first-run; choose/create/adopt
      paths; skip lands on Home; revisit later reachable.
- [ ] 14.2 Implement first-run flow + snapshot.

## 15. Hardening + layout floors

- [ ] 15.1 Layout tests: no horizontal overflow at 80x24 on every primary
      screen; wide layout adds panels at 120x40.
- [ ] 15.2 Loading/empty/error/offline/policy-denial states intentional on every
      screen (tests + snapshots for representative cases).

## 16. Delivery

- [ ] 16.1 `docs/tui.md` + README pointer; key map documented.
- [ ] 16.2 Full lean suite + latency gate + `exomem demo --json` green; TUI
      suite green with the extra; snapshot goldens committed.
- [ ] 16.3 Benchmark smoke: `graph_value_benchmark.py`, `product_flow_benchmark
      --flow fresh_setup --flow search_recall`, latency gate — no regression.
- [ ] 16.4 Synthetic dogfood journey executed and recorded; similarity review vs
      sibling products recorded; `openspec validate add-terminal-ui --strict`
      green.
