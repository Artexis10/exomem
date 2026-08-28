<!-- authority:non-specification -->

<!-- authority:implementation-reference -->

# Terminal review surface — historical exploration

This is non-authoritative product exploration, not approved requirements or an
implementation brief. If the terminal surface is revived, create an OpenSpec
change and make that change the sole durable design and requirements authority.
This historical note was informed by the benchmark-foundation audit
(`docs/memory-proof-benchmark.md`) and by a UX audit of Gray Box's hand-rolled
terminal UI (MIT, `~/projects/graybox`).

## Why a TUI, and what it must make visible

The benchmark's epistemic scenario families define what a power user must be
able to *see* to trust governed memory. The terminal surface should make these
first-class, in this priority order:

1. **Current truth vs history** — active vs superseded state, the
   supersession chain (`review_memory --mode evolution`), and why each
   transition happened. Never render a superseded conclusion as current.
2. **Evidence and provenance** — for any conclusion: its `sources:`,
   evidence artifacts, and derivation chain; one keystroke from claim to
   original.
3. **Attention/health queues** — the epistemic inbox families (near-dup/
   contradiction, stale-review, unprocessed sources, weak connectivity),
   relation queue, and audit categories, with triage (dismiss/snooze/reopen)
   bound to signal fingerprints exactly as `triage_memory` does.
4. **Uncertainty, honestly** — Exomem deliberately has no confidence floats;
   the TUI must render trust signals it actually has (citation count,
   recency, dispute/contradiction flags, authority of sources) and must not
   invent scores.
5. **Policy and disclosure** — when governance is active: what an audience
   would see (disclosure level per item), withhold notices, and recent
   disclosure receipts. Read-only in v1.
6. **Automatic-retrieval status** — whether hooks are installed per client,
   whether the last session's retrieve-nudge fired/injected, cooldown state;
   surfaced from hook/install state and server call traces, so "is seamless
   memory actually on?" has a visible answer.

## Grounding in shipped surfaces (drive these; do not reimplement)

- `review_memory` (13 read-only modes) is the queue backbone;
  `review_item_context` already returns the bounded single-item workspace
  payload a detail pane needs (body, related, provenance, graph, history,
  evolution — all capped). `triage_memory` is the only queue write.
- The browser Review Studio's security model transfers verbatim: an inert
  shell with zero vault data baked in; all data via the authed local API; the
  client filters views but never recomputes rank, severity, or epistemic
  meaning.
- Product commands are the only data path (`--json` envelopes); the TUI is a
  renderer over the same registry the CLI uses, so it cannot drift.

## UX patterns adopted from the Gray Box audit

1. Zero-argument launch opens the full-screen surface; any subcommand stays
   scriptable CLI — one binary, two audiences.
2. Every TUI action is a thin wrapper over the identical product command (no
   parallel implementation).
3. In-place redraw (cursor-up + re-render), no full clears; no heavy TUI
   framework required for v1 (Gray Box proves ~300 lines of ANSI + readchar
   suffices; Rich/Textual is an implementation choice, not a requirement).
4. `hjkl` + arrows; `q`/`Esc` always exits cleanly.
5. Cursor hidden inside try/finally — never leave the terminal broken.
6. Errors and Ctrl-C cancel one action, never the app.
7. Re-read state after every action; the banner always shows active context
   (vault path, governance on/off, service mode) — "which memory am I about
   to touch" stays permanently on screen.
8. Selected-row-only colour; fixed-width command + dim description rows.
9. **Destructive/structured operations stay CLI-only** (supersede, delete,
   merge, governance edits): the TUI links to the exact command instead of
   embedding irreversible multi-arg flows behind keystrokes.
10. Honest `--dry-run` previews wherever a mutating action is exposed.
11. An HTML escape hatch (existing Studio) for what terminals do badly
    (graph canvases); the TUI stays keystroke-fast and does not duplicate it.
12. Read-only browsers for risky domains (Gray Box exposes dupes read-only;
    we expose review queues read-only + triage-only writes).

## Benchmark-driven journeys (v1 scope candidates)

- `exomem review` TUI: queue list → item workspace → triage — the epistemic
  inbox as the home screen.
- Evolution inspector: pick a topic → supersession chain with diffs of the
  conclusion line.
- Retrieval status panel: hooks installed?, last activation, injection
  ladder state.
- One-command benchmark journeys (from Track D) as demo/verification flows:
  `exomem <tui> --journey correction-propagation` replaying J2 against a
  demo vault — the TUI doubles as the inspection surface for benchmark runs.
- Pack selection remains optional/composable: a picker that reads and writes
  `_Packs/selected-packs.json` via the existing command, never a setup gate.

## Constraints

- Pure substrate: the TUI measures and renders; no model calls in the TUI
  process.
- Read-mostly v1: writes limited to triage, relation-accept, pack selection —
  each through its existing governed command.
- No new required runtime dependency without an explicit decision in the
  implementing change; stdlib-first is proven viable.
- Never render vault content into logs/artifacts; respect governance
  projections when rendering (use the same egress-governed commands).
- Production TUI files are owned by the TUI worktree; the benchmark worktree
  will not touch them (parallel-work boundary agreed 2026-07-31).
