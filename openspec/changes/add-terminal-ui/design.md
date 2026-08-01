# Design — add-terminal-ui

## Context

Exomem has no terminal UI. The interactive prior art is the browser Review
Studio (`src/exomem/studio/`, served at `/studio/` by the http transport), two
`input()`-driven wizards (`setup_wizard.py`, `remote_setup_wizard.py`), and the
`_print_*_human` renderers in `__main__.py`. The service layer is the unified
command registry (`commands.py`, 26 product commands) whose single dispatcher —
`writer_lease.invoke_command` — applies the writer lease, mutation lock,
idempotency, and the governance egress boundary for MCP, REST, and CLI alike.
Every leaf is synchronous. The CLI reaches the dispatcher through the inline
body of `__main__._core_op_main`: coerce (`cli_ops.coerce`) → resolve vault →
conditional `source_schema` injection → `capabilities.active_surface(...)` +
`principal.request_scope(owner_principal(surface="cli"))` → `invoke_command`.

Measured constraints that shape this design: one-shot CLI startup costs ~3.1 s
(WSL2) to ~11–12 s (Windows); warm in-process reads are milliseconds while
compiled writes are seconds; retrieval reports per-request `degraded`/`warming`
markers; `review_memory(mode="attention")` runs a full vault audit pass;
mutation/lease locks can block behind another process and surface structured
retryable codes.

## Decisions

### TUI framework decision record (2026-07-31): Textual, behind a default-off `tui` extra

Evaluated: **Textual 8.x** (chosen), **prompt_toolkit**, **Rich-only**, and
**urwid**. prompt_toolkit is a line-editing/layout toolkit, not an application
framework: screens, focus management, command palette, responsive layout, and a
headless test story would all be hand-built (the sibling-product audit shows
where hand-rolled terminal input loops end up: no history, no paste safety, a
second dispatcher drifting from the CLI). Rich alone has no input loop at all.
urwid is synchronous-first and has no comparable testing ecosystem. Textual
provides an async app model with thread workers and cancellation, CSS-like
responsive styling with container breakpoints, a built-in command palette and
key-binding/footer system, first-class headless testing (`Pilot`), and SVG
snapshot testing via `pytest-textual-snapshot`; it is pure-Python, renders on
Windows Terminal/WSL/macOS/Linux, and builds on `rich`, which is already in the
lock as a transitive dependency.

Cost and containment: `textual` is a real new dependency (plus
`markdown-it-py`/`linkify-it-py`/`platformdirs`, all pure Python). It lives
behind the default-off `[project.optional-dependencies] tui` extra; a lean or
server install never carries it. `exomem tui` without the extra prints a
one-line install hint and exits non-zero — the repo's standard default-off +
soft-fail contract. `pytest-textual-snapshot` lives in the opt-in `tui-dev`
dependency group so the lean dev matrix is unaffected. No model runs, no
network is touched: the TUI is presentation over existing measurements, so the
pure-substrate constraint is untouched.

Kill switch / reversal: the TUI is a leaf client. Removing the extra, the
`tui` dispatch branch, and `src/exomem/tui/` reverts the product to today's
surface with no schema or registry impact.

### One shared invocation seam; the seam owns the ambient bindings

The invocation body of `_core_op_main` is extracted to a reusable
module-level function (same file or a sibling module) with the signature shape
`invoke_product(command_name, raw_kwargs, *, vault_root: Path | None = None,
tier2: bool | None = None, idempotency_key: str | None = None)`. It performs,
in order: registry lookup, `cli_ops.coerce` (CLI-relaxed JSON semantics),
vault resolution (`resolve_vault()` when `vault_root is None`, replicating the
pre-init allowance for `browse_memory` and `adopt_vault` scan-only against an
explicit or env-provided root), conditional `source_schema` injection for
`needs_schema` commands, the `process_media` operation pre-check, and — inside
the function itself — the `capabilities.active_surface(...)` +
`principal.request_scope(owner_principal(surface="cli"))` context binding
around `writer_lease.invoke_command`.

The binding lives *inside* the seam because ContextVars do not propagate into
worker threads and an unbound principal fails silently closed (governance
egress degrades results to empty — indistinguishable from "no knowledge
found"). With the seam owning the binding, a mis-scheduled call is structurally
impossible rather than a convention.

Kept CLI-side (not part of the seam): `_normalize_cli_edit` (CLI flag sugar;
`invoke_command` independently normalizes), the `EXOMEM_IDEMPOTENCY_KEY`
environment passthrough, and the `strict_failed` exit-code mapping. The CLI is
re-wired through the seam behavior-identically; existing CLI tests are the
guard. The extraction lands as its own commit so concurrent branches can
reconcile against a minimal diff.

### Surface identity: reuse `surface="cli"`

`Command.surfaces` only contains `{"mcp","rest","cli"}`; a `"tui"` surface
string would make `product_commands_for("tui")` empty and would perturb the
capability fingerprint for zero user value. The principal's surface string is
not consumed by receipts/decisions/query logs. The TUI is a local
owner-process CLI-family surface and binds the exact CLI descriptor; process
logs already distinguish it (`logging_config` `process="cli"` file logging —
verified file-only, so it cannot corrupt the screen).

### Concurrency model

- Every seam call runs in a Textual thread worker (`@work(thread=True)` /
  `run_worker`). The UI thread never blocks on retrieval, audit, or writes.
- **Single-flight per screen.** Thread workers are cooperatively cancelled:
  a "cancelled" ask keeps consuming CPU until the leaf returns, so each screen
  keeps at most one in-flight read; a new submit supersedes (drops) the old
  result via a per-screen generation counter. Late results from a superseded
  generation never mutate the UI.
- Writes are never abandoned: once confirmed they render progress until a
  terminal success/error. Structured codes (`MUTATION_BUSY`,
  `WRITER_LEASE_REQUIRED`, `MUTATION_WARMING`, `REVIEW_ITEM_CHANGED`,
  `PLAN_STALE`, …) map to retry/refresh UI states using the shared
  `cli_ops.error_dict` envelope — code, message, remediation — never raw
  tracebacks. Stale-token codes always refresh-and-re-present, never
  auto-retry.
- Optimistic concurrency: triage binds `expected_fingerprint` from the listed
  item; a mismatch reloads the queue.
- Long-lived process caches (`find` caches, embedding index memos, semantic
  contract/activation manifest caches) are cleared by the explicit Refresh
  action and on vault switch — the same reset seams the test fixtures use.

### Startup sequence

`exomem tui` is added to `_CLI_ONLY_SUBCOMMANDS` and dispatched before the
serve fallthrough; the `exomem.tui` package is imported lazily inside the
branch (the lazy-import gate gains `textual` in its forbidden-modules list for
non-TUI invocations). The app paints the shell immediately; the registry
import (seconds, cold) happens in a worker before the first data fetch. After
vault resolution the app calls `warmup.start_background(vault_root)` — it
respects compute mode and `EXOMEM_DISABLE_WARMUP` and soft-fails — so the
readiness/warming states are real and the first ask does not pay cold load
inline. Startup also replicates `_configure_local_search_capabilities`'s lean
probe (disable embeddings/ranking/CLIP for the process when
torch/sentence-transformers are absent) so a healthy lean install does not
render false "degraded" alarms. The TUI never imports `exomem.server` (that
would spawn watchers/warm threads it must not own).

Vault choice: resolved once at startup; `RuntimeError` from `resolve_vault` is
the first-run detector. Onboarding commit (choose/create/adopt) sets
`EXOMEM_VAULT_PATH` exactly once on the app thread (precedent: `demo.py`),
then passes `vault_root` explicitly to every seam call — never per-invocation
env mutation from workers.

### Screens map to audited capabilities only

| Screen | Backing |
|---|---|
| Home | vault resolve state; `mode.resolved()`; bounded attention count (`review_memory` attention, small limit, cached until Refresh); `knowledge_packs.selected_pack_state`; hook status readers; `readiness.snapshot()` |
| Ask | `ask_memory` (+`deep=True` packed context, `explain` opt-in); preview via `read_memory`; per-request `degraded`/`warming` markers rendered at the results; write-back = editable draft → confirm → `remember`. No generated prose — pure substrate presents measured recall |
| Capture | thought/paste → `capture_source` (title auto-derived from first line, editable; `source_schema` injected by the seam); insight → `remember`; confirmation names the stored path; errors keep the input |
| Review | `review_memory` (attention + state views), `review_item_context`; triage = dismiss / snooze(until required) / reopen only (`review_state.VALID_ACTIONS`); Studio pointer includes the http-transport prerequisite |
| Continue | continuation checkpoints via a small public reader added to `install_hook.py` (wrapping the hook module's state roots + `render_continuation`); read-only; honest empty state naming `exomem install-hook` |
| Adopt | `adopt_vault mode=scan-only` (pre-init safe, zero writes) → report; save-manifest / copy-as-sources behind explicit confirmation naming mode + destination; Adoption Studio lifecycle stays in the Studio (v2 candidate) |
| Packs | `list_builtin_packs` / `selected_pack_state` / `write_selected_packs(source="tui")` with `KB_NOT_INITIALIZED` handling; stable IDs |
| Status | `doctor.doctor` (read-only), `resource_status.collect`, `readiness.snapshot`, `install_hook.check_hooks`, `install_info.report`; remediation on every warn/fail; no secrets; rendering performs no writes/downloads |
| Settings | compute mode via `mode.write_mode` (config path shown), appearance (theme/ASCII), vault identity; only real persistence paths are editable |
| First-run | choose path / create (`init_vault`) / adopt-scan; optional packs; hooks visibility; all skippable; lands on Home |

Pack-selection write is the one recorded non-registry write: no registry
command writes pack selection today and `setup_wizard` calls
`knowledge_packs.write_selected_packs` directly, which itself goes through
`vault.batch_atomic_write` (access-tier check, write-fence validation, atomic
commit). The TUI follows that precedent rather than inventing a registry
command in this change.

New-pack assessment (brief item): `science-research` overlaps the existing
`technical` + `business` signal sets substantially; `people-relationships` has
genuinely distinct signals (people, relationships, meetings) not covered by
`personal-records`. Verdict: neither is required for v1; if catalog data adds
cleanly during the Packs work, `people-relationships` is the only candidate —
otherwise both are recorded as follow-ups, not padded into the list.

### Visual system

Emotional target: calm, trustworthy, fast, precise. Concretely:

- **One accent** (warm amber family) used only for focus, selection, and the
  active-screen marker — never for status. Status uses the universal
  green/yellow/red *paired with glyphs and words* (`ok` / `warn` / `fail`), so
  nothing is conveyed by color alone. Neutrals otherwise; secondary text dim.
- **Restrained chrome**: a one-line header (product + screen left, vault
  identity + mode right), content in padded panels with at most one border
  weight, a footer of key hints (toggleable). No ASCII wordmark, no emoji
  menus, no decorative rules — deliberate distance from both sibling products'
  visual identities (checked again at the similarity gate).
- **Status language**: every warning reads *what → why → next action*, reusing
  the registry's own remediation strings.
- **Glyph policy**: Unicode `● ○ ▲ ✗ ✓ ▸ ▌ │ ─ → …` with an automatic ASCII
  fallback of identical arity (`* o ! x + > > | - -> ...`) when the encoding
  cannot render them; `NO_COLOR` selects a monochrome skin that keeps hierarchy
  in dim/bold/reverse. Both paths are asserted by test rather than assumed.
- **Breakpoints**: 80×24 single-column with evidence/detail as overlays;
  ≥100 columns two-pane (list + detail); ≥120 adds the persistent evidence
  panel on Ask/Review. No horizontal overflow at 80 columns anywhere.
- Dark and light terminal themes via Textual's theme system; no hard-coded
  backgrounds that fight terminal-native palettes.

### Design pass 2 (2026-08-01): the ledger and the receipt language

The v1 screens were correct but read as configuration. A dedicated design
session produced a locked direction — first run as an **accreting ledger**,
and the daily loop restyled in the same **receipt language** — recorded in
Exomem as "Exomem TUI redesign locked — ledger first-run and receipt language"
(project `exomem`). The palette comes from "Substrate Design System v2 —
tokens locked, per-brand accents, terminal amber confirmed" (project
`substrate`), which pins Exomem's terminal accent to phosphor amber `#ffb000`
(256-colour 214, 16-colour yellow 3) on warm near-black with warm off-white
text, under the same live-state-only rule. What that changed, and why:

- **First run is one screen, not a wizard.** Answered steps collapse into `✓`
  receipt lines; the active question owns the rows below them. The transcript
  *is* the progress indicator, so the rail widget a stepped wizard would need
  never has to claim a state the screen cannot prove. `esc` rewinds one line
  and stops at any line that recorded a write, because a folder that exists
  cannot be un-created and a UI that implies otherwise is lying.
- **Receipts are the shared vocabulary.** `glyph + word` padded to a fixed
  label column, then the detail. The same shape renders setup steps, save
  confirmations, health lines, and Home's session log, so the language of the
  first run is the language of the daily loop.
- **One recovery template.** Failure, empty, and degraded states all render as
  status line → what happened → dim statement of what was and was not changed →
  a *selectable* list of next actions with the best pre-selected. This replaced
  three ad-hoc shapes (an error notice widget, a prose empty state, and a
  notification toast) with one, and it is why no state can dead-end.
- **The accent boundary became a rule, not a habit.** Amber lights only live or
  confirmed state — the current step, `▸ retrieved`, live wikilinks, the
  cursor, focus, the selection bar. It is never chrome and never an error, so
  the eye can trust it. Errors are red-with-a-glyph-and-a-word.
- **Selection is a bar in column zero**, rendered into the option prompt rather
  than drawn by the widget, so it survives multi-line rows, ASCII terminals,
  and `NO_COLOR` (where it becomes reverse video plus `>`).
- **A `Skin` object replaced scattered style literals.** Rich spans cannot
  resolve Textual CSS variables, so styled text is built from one object
  carrying the glyph set and the color roles. That is what makes the `NO_COLOR`
  path testable instead of aspirational: the mono skin is a value you can pass
  to a pure renderer and assert on.

Where the drawn frames and the backend disagreed, the backend won and the
deviation is recorded here rather than papered over:

- **Paths truncate from the left** (`…/Insights/…limits.md`), including in
  result rows where the frame showed a right-truncated path. The filename is
  the identifying part; the spec's own overflow rule says so, and behavior
  beats layout when they conflict.
- **The file count in the first-run preview is measured**, not the literal
  "14 files" in the copy deck: it counts the packaged scaffold, so the promise
  stays true as the scaffold grows.
- **Snooze does not offer "after the next sweep."** The triage contract takes a
  date; an option the backend cannot express is exactly the fake affordance the
  rest of the screen refuses to draw.
- **The Review context pane labels what the payload actually contains**
  (`what` / `page` / `related` / `measured`) instead of the frame's
  `newer` / `older`. The review-context envelope does not label which side of a
  contradiction is newer, and guessing would be fabrication.
- **Retrieval timing is routed through the app** (`elapsed_ms`) so snapshots
  can pin it; a golden that diffs by a millisecond is a golden nobody trusts.

Two Textual behaviors shaped the implementation and are worth stating because
both produced real bugs before they were understood: message handlers are
dispatched to **every class in the MRO** (so screens must not chain
`super().on_mount()`, and only one class in a modal's hierarchy may call
`dismiss`), and `Widget._render` / `MessagePump._running` are framework
attributes that a screen must not shadow.

### Key map (documented in docs/tui.md)

Global: `ctrl+p` command palette · `?`/`f1` help overlay (when not typing) ·
`esc` back/cancel (guarded when input is non-empty) · `ctrl+q` quit ·
`r`-less global refresh via palette. Home: `1`–`8` (and letter mnemonics)
open Continue/Ask/Capture/Review/Adopt/Packs/Status/Settings; `q` quits with
confirmation. Lists: `↑/↓` and `j/k`, `enter` opens, `/` filters where a
filter exists. Ask: `enter` submits, `esc` cancels the run, `e` evidence,
`y` copy context packet, `w` write-back. Review: `enter` context, `d` dismiss,
`s` snooze, `o` reopen, `u` refresh. Capture: `ctrl+s` saves. Small, coherent,
screen-local where letters could collide with typing.

Pass 2 added keys only; nothing was removed or re-pointed. `u` refresh is now
standardized on every data screen. Home's list is focusable, so `enter` opens
the highlighted row. Ask gains `u` (re-run) and an `esc` that unwinds one layer
at a time — results → the query (kept) → back. Capture gains `tab` (cycle
kind) and `e` (edit the derived title). First run uses `esc` to rewind one
ledger line and `s` to skip an optional step.

`e` on Capture is gated by `check_action` so it types an "e" while the text
area has focus and is the shortcut everywhere else; `shift+tab` moves focus out
of the text area, which is how the shortcut stays reachable without stealing a
character from the writer.

### Testing and goldens

`tests/tui/` inherits the autouse lean conftest (module-scope
`pytest.importorskip("textual")`-style guard so the directory skips without
the extra). Layers: view-model unit tests; seam tests on a synthetic temp
vault (fixture-copy pattern) asserting owner-visible reads from a worker
thread; Pilot navigation tests; snapshot goldens at 80×24 and 120×40 rendered
**exclusively from the deterministic FakeBackend with synthetic POSIX-style
paths** — committed SVGs under `tests/` are scanned by the fail-closed
public-artifact privacy gate, so goldens must never be regenerated against a
real vault or embed host paths. Regen workflow (`--snapshot-update`) is
documented in docs/tui.md; goldens pin to the locked Textual version. CI gains
one `tui` job (`uv run --frozen --extra tui --group tui-dev python -m pytest
tests/tui -q`), mirroring the `retrieval-eval` extra-install precedent.

### Similarity review vs sibling products (2026-07-31)

An explicit side-by-side check against the recorded distinctiveness
inventories of Gray Box (MIT, hand-rolled ANSI menu TUI) and Basic Memory
(AGPL, Typer/Rich CLI, no TUI). No source code, help text, branding, ASCII
art, or characteristic phrasing was taken from either; Basic Memory's tree was
treated as read-only reference throughout (AGPL — principles only, no
derivation). Checked dimensions and outcomes:

- **Branding/composition**: no wordmark/figlet banner, no emoji menu, no
  pointer-glyph menu rows (highlight is a background bar); Home is a
  destinations+status two-column dashboard, unlike either product.
- **Menu set/order**: eight destinations (Continue/Ask/Capture/Review/Adopt/
  Packs/Status/Settings) — overlaps only in product-generic words; neither
  sibling's set, order, or icon pairing.
- **Key bindings**: number-key destinations plus screen-local action letters;
  neither product uses this scheme. Retained *generic* terminal conventions on
  usability grounds: arrows/enter/escape, `j/k`, `?` help, `q` quit,
  `ctrl+p` palette, master-detail layouts, red/yellow/green status colors
  (always paired with glyph + word, never color alone).
- **Color/spinner identity**: warm-amber accent (vs their cyan/blue
  identities); no braille spinner (elapsed-seconds status text instead).
- **State phrasing**: empty/loading/error strings written fresh; errors render
  the registry's own code + remediation envelope — exomem's existing product
  language, unlike Basic Memory's markdown error documents or Gray Box's
  advisory banners. "Nothing needs attention in this view" matches exomem's
  own CLI renderer, not a sibling.
- **Workflows**: capture = editor + auto-title + kind radio (vs prompt-line
  capture / title+folder-required); ask = measured recall with evidence panel
  (vs LLM answer with sources / search table).
- **Runtime side-by-side** was limited to static inspection and their own
  documentation: both sibling TUIs need a real TTY (and Gray Box's ask needs a
  configured LLM key) which the test environment does not provide; the
  recorded inventories were produced from full source audits instead.

### Known limitation (documented)

A long-lived TUI process runs no file watcher (that is the server's job): out
-of-band edits refresh lexical lanes via mtime-checked caches, but the
semantic sidecar drifts until a server or `exomem index`/audit pass. The
Refresh action clears in-process caches; docs/tui.md names the limitation.
Binary media upload (`/upload`, `process_media` subprocess) stays out of v1.
