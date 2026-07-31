# The Exomem terminal UI (`exomem tui`)

A persistent, keyboard-first surface over the same product commands the CLI,
MCP, and REST doors expose. One long-lived process (no per-command Python
startup), guided access to capture, recall, review, adoption, packs, and
diagnostics — and an honest face for the engine's own signals: warming lanes,
degraded retrieval, structured errors with remediations.

The TUI is a thin client. Every knowledge read and write goes through the
unified command registry via the same invocation seam as the CLI
(`src/exomem/product_invoke.py`): identical coercion, vault resolution, owner
principal, writer lease, and governance egress. It adds no second storage or
retrieval path and never writes vault files directly.

## Install

The UI stack is an optional extra so lean and server installs never carry it:

    uv sync --extra tui              # source checkout
    pip install 'exomem[tui]'        # package install

Without the extra, `exomem tui` prints a one-line install hint and exits
non-zero. Snapshot/interaction tests additionally need the dev group:

    uv sync --extra tui --group tui-dev

Supported platforms: Linux, WSL, macOS, and Windows Terminal (Textual renders
on all four). An interactive terminal is required — a piped invocation exits
with status 2.

## Launch

    exomem tui                       # uses $EXOMEM_VAULT_PATH
    exomem tui --vault /path/to/vault

With no resolvable vault the Welcome flow opens: point at an existing vault,
create a fresh one (init), or scan a folder read-only first. Every step is
skippable; everything stays reachable from Home afterwards.

## Screens

| Key (from Home) | Screen | What it is |
|---|---|---|
| 1 | Continue | local continuation checkpoints from the client hooks, rendered as copyable resume packets |
| 2 | Ask | measured recall with evidence — results cite source pages; deep-context packet copyable; degradation announced at the results |
| 3 | Capture | thought (immutable source) or insight (governed note); title derived from the first line; no folder decisions |
| 4 | Review | the Epistemic Inbox queue with the exact supported triage: dismiss, snooze (dated), reopen; deeper flows point at the Review Studio |
| 5 | Adopt | read-only scan of any folder (works pre-init); write modes are separate confirmed steps; originals never rewritten |
| 6 | Packs | multi-select over the built-in catalog, persisted like `setup` does; packs guide interpretation, never folder structure |
| 7 | Status | doctor preflight, resources, warm state, hooks, install provenance — each warning with its remediation |
| 8 | Settings | compute mode (quiet/normal/performance, persisted), theme toggle, vault identity |

Ask presents *measurements*, not generated prose: Exomem is a pure substrate —
the server measures, your model reasons. The copyable context packet exists
precisely to hand that evidence to a reasoning model.

## Key map

Global: `ctrl+p` command palette · `?` help overlay · `esc` back (guarded —
non-empty input is never silently discarded) · `ctrl+q` quit (confirmed).
Home: `1`–`8` open screens · `u` refresh · `q` quit. Lists: arrows and `j/k` ·
`enter` opens · highlighted rows show focus. Ask: `enter` submit · `esc`
cancel the run · `e` evidence preview · `y` copy context packet · `w` save an
insight. Review: `d` dismiss · `s` snooze · `o` reopen · `v` cycle state view
· `u` refresh. Capture: `ctrl+s` save. Adopt: `m` save manifest · `c` copy as
sources (both confirmed).

## Governed write-back

Saving an insight (`w` on Ask, or Capture's insight kind) writes through
`remember`. Two governed rules are handled for you, visibly:

- every compiled note needs at least one semantic unit — the editor pre-fills
  an editable `## Observations` line, and if a draft has none, one is appended
  restating the title;
- a new page without a qualifying typed relation needs an explicit relation
  disposition — the TUI runs the validate→commit draft flow and records your
  confirmation as a `reviewed_none` disposition with an audit reason. Connect
  the note properly later via `exomem connect` or the Review Studio.

## Local vs Hosted

This TUI operates the local engine in-process. A Hosted/remote connection is
not part of this surface today; `exomem doctor --profile remote` and the
connector docs cover remote posture, and the Status screen shows local install
provenance and the managed-service version when one is configured.

## Limitations

- Long-lived process, no file watcher: out-of-band edits (e.g. Obsidian)
  refresh lexical search via mtime checks, but the semantic sidecar drifts
  until a server or `exomem index` pass. The Ask screen's refresh action
  (`u`) clears in-process caches.
- Binary media import (`/upload`, `process_media`) is not in the TUI yet —
  capture text, or use adoption for markdown folders.
- Review depth: proposal-first flows (supersede, compile, accept relations)
  deliberately live in the browser Review Studio; the TUI links to it (the
  Studio requires the http transport to be serving).
- Cooperative cancellation: a cancelled ask stops affecting the UI
  immediately; the underlying computation may finish in the background.

## Troubleshooting

- `exomem tui` exits 2 with "needs an interactive terminal": stdin/stdout is
  piped or redirected — run it in a real terminal.
- Install hint on launch: the `tui` extra is missing — `uv sync --extra tui`.
- "partial results — still warming": the background warm hasn't finished;
  results are lexical-only until it does. `exomem warm` pre-loads models
  explicitly.
- Ask shows skipped lanes on a lean install: expected — without the
  `embeddings` extra retrieval runs keyword/BM25 only, and the TUI disables
  the model lanes at startup so this is not reported as degradation.
- Writes reporting `MUTATION_BUSY`/`WRITER_LEASE_REQUIRED`: another process
  (usually the running server) holds the mutation boundary — retry after it
  finishes; reads stay available.

## Running the TUI tests

    uv run --frozen --extra tui --group tui-dev python -m pytest tests/tui tests/test_tui_entry.py -q

Snapshot goldens live in `tests/tui/__snapshots__/` and render exclusively
from the deterministic fake backend with synthetic paths (the committed SVGs
are scanned by the public-artifact privacy gate — never regenerate them
against a real vault). Intentional regeneration:

    uv run --frozen --extra tui --group tui-dev python -m pytest tests/tui/test_snapshots.py --snapshot-update

The lean matrix (no `tui` extra) skips `tests/tui` entirely; `tests/test_tui_entry.py`
and `tests/test_product_invoke.py` run everywhere.
