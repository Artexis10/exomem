## ADDED Requirements

### Requirement: Explicit TUI Entry Point

The system SHALL provide the terminal UI only behind the explicit subcommand
`exomem tui`. Invoking `exomem` with no arguments SHALL retain its existing
behavior (serving the MCP transport) unchanged. When standard input or standard
output is not a TTY, `exomem tui` SHALL exit with status 2 and a one-line
explanation instead of starting the UI. When the optional `tui` dependency extra
is not installed, `exomem tui` SHALL exit non-zero with a one-line install hint naming
`uv sync --extra tui` and `pip install 'exomem[tui]'`, and SHALL NOT print a
traceback.

#### Scenario: Bare invocation is unchanged

- **WHEN** `exomem` is invoked with no arguments
- **THEN** it serves the MCP transport exactly as before this change

#### Scenario: Non-TTY invocation fails gracefully

- **WHEN** `exomem tui` is invoked with stdout redirected to a pipe
- **THEN** the process exits with status 2
- **AND** prints a single-line message stating the TUI needs an interactive
  terminal

#### Scenario: Missing extra degrades softly

- **WHEN** `exomem tui` is invoked in an environment where `textual` is not
  importable
- **THEN** the process exits with a non-zero status and a one-line install hint
- **AND** no traceback is printed

### Requirement: Thin Client Over The Unified Registry

The TUI SHALL perform every knowledge-base read and write by invoking commands
from the unified command registry through the same shared invocation seam the CLI
uses: argument coercion via the registry `Param` specs, vault resolution,
conditional schema injection, active-surface binding, owner-principal scope, and
the writer-lease command wrapper. The TUI SHALL NOT implement a second storage or
retrieval path, SHALL NOT write vault files directly, and SHALL NOT expose an
action whose backing command does not exist on the registry surface it binds.

#### Scenario: A TUI write is a registry write

- **WHEN** the user captures a thought in the TUI
- **THEN** the content is persisted by invoking the same registry command the CLI
  `exomem capture` alias routes to, under the same coercion, guards, principal,
  and writer-lease semantics
- **AND** no TUI module opens or writes a vault file directly for that capture

#### Scenario: No dead controls

- **WHEN** any TUI screen renders an actionable control
- **THEN** activating it either invokes a real registry command or a real local
  diagnostic function, or the control is rendered disabled with a reason

### Requirement: Home Dashboard Shows Actionable Status Only

The Home screen SHALL answer what the user can do now and whether Exomem is
healthy, showing at minimum: the resolved vault identity (or the first-run state),
compute mode, review-queue attention count, enabled knowledge packs, hook
installation state, and any active warming/degraded state. Every warning shown on
Home SHALL name a next action. Home SHALL NOT render an unbounded metrics wall.

#### Scenario: Healthy vault summary

- **WHEN** the TUI starts against an initialized vault
- **THEN** Home shows the vault identity, compute mode, pack selection, and the
  count of open review-attention items
- **AND** each shown warning includes a next action or an explanation

#### Scenario: Warming is visible, not silent

- **WHEN** the underlying engine reports components still warming
- **THEN** Home indicates that retrieval is temporarily degraded and which lanes
  are affected, rather than presenting full-fidelity results as if warm

### Requirement: Ask Presents Measured Recall With Evidence

The Ask flow SHALL accept a query with minimal friction, run retrieval without
blocking the UI, and present ranked results with source identity. It SHALL make
evidence inspectable: a result can be expanded to preview the underlying page
content via the registry read command. It SHALL surface retrieval degradation
markers (skipped/warming lanes) at the point of answer. It SHALL support scope
narrowing (project/type filters) without requiring it. It SHALL offer a copyable
deep-context packet built from the registry's deep recall mode. Consistent with
the pure-substrate constraint, the Ask flow SHALL NOT generate prose answers; it
presents measurements and preserved content only.

#### Scenario: Query with citations

- **WHEN** the user submits a query on the Ask screen
- **THEN** the UI stays responsive while retrieval runs
- **AND** results are listed with their vault path/title identity
- **AND** selecting a result shows a bounded preview of the underlying page

Every result set SHALL open with a retrieval header naming the number of
results and the measured elapsed time, so the cost of the measurement is
visible rather than implied.

#### Scenario: Degraded retrieval is announced

- **WHEN** retrieval returns a warming/degraded marker
- **THEN** the Ask screen states that results are partial, names the affected
  lanes near the results, marks the header as covering the lexical lane only,
  and states that the query re-runs itself once the lanes are warm

#### Scenario: Warming resolves without the user acting

- **WHEN** results were partial because lanes were still loading
- **THEN** the screen re-runs the same query once readiness reports the lanes
  are warm, within a bounded number of checks

#### Scenario: Cancellation leaves the UI consistent

- **WHEN** the user cancels an in-flight query
- **THEN** the UI returns to an idle Ask state without stale results appearing
  later from the cancelled run

### Requirement: Governed Write-Back From Ask

Turning a retrieval outcome into durable knowledge SHALL be possible only through
the governed registry write commands, with an explicit user confirmation step
that shows what will be written and where. The TUI SHALL NOT silently mutate
memory as a side effect of asking.

#### Scenario: Saving an insight is explicit

- **WHEN** the user chooses to save a conclusion from the Ask screen
- **THEN** the TUI shows an editable draft (title, type, content) and requires
  explicit confirmation
- **AND** the write is performed by a registry write command and its result
  (path) is shown

### Requirement: Low-Friction Capture

The Capture flow SHALL let the user persist a typed or pasted thought with no
mandatory folder choice, no ontology decision, and no required metadata beyond
what the backing commands require; required titles SHALL be auto-derived from the
first line and remain editable. Capture SHALL preserve the raw original through
the governed capture path, distinguish capture kinds (raw source vs compiled
note) in friendly language, show a confirmation naming the stored path, and
surface failures with the registry error code and remediation.

#### Scenario: Fastest capture path

- **WHEN** the user opens Capture, types a thought, and confirms
- **THEN** the content is captured through the governed registry path with an
  auto-derived title and default kind
- **AND** the confirmation names the stored vault path

#### Scenario: Capture failure is actionable

- **WHEN** the backing command rejects a capture
- **THEN** the error code, message, and remediation from the shared error
  envelope are shown
- **AND** the typed content is not lost from the input

### Requirement: Review Queue With Honest Triage

The Review screen SHALL list Epistemic Inbox attention items with severity,
categories, reasons, and stable references, and SHALL show a bounded item context
on demand via the registry review-context command. It SHALL expose exactly the
triage actions the backend supports — dismiss, snooze (with required date), and
reopen — and SHALL NOT render approve/merge/supersede controls that have no
backing command in the TUI. For deeper proposal-first flows the screen SHALL
point the user at the Review Studio.

#### Scenario: Triage an item

- **WHEN** the user dismisses an attention item with an optional rationale
- **THEN** the registry triage command is invoked with the item's stable
  reference
- **AND** the queue view reflects the new state without a full restart

#### Scenario: Unsupported depth defers to the Studio

- **WHEN** an item's resolution requires a proposal-first flow (e.g.
  supersession)
- **THEN** the TUI shows how to open the Review Studio rather than a
  non-functional control
- **AND** the pointer states that the Studio requires the http transport to be
  serving (a stdio-only install has no Studio running)

### Requirement: Continuation Surface

The Continue screen SHALL list locally recorded continuation checkpoints from
the installed client hooks (when present) with client identity, session
identity, and age, SHALL render a selected checkpoint as the same resume packet
the hooks themselves render, and SHALL offer copying that packet. Checkpoint
access SHALL be read-only and SHALL go through a public reader helper rather
than private hook internals. When no checkpoints exist (hooks not installed, or
nothing recorded yet) the screen SHALL say so honestly and name the
hook-installation step that enables them.

#### Scenario: Resume packet is available

- **WHEN** continuation checkpoints exist for an installed client
- **THEN** the Continue screen lists them and renders the selected checkpoint's
  resume packet with a copy action
- **AND** rendering performs no writes to the checkpoint store

#### Scenario: Honest empty state

- **WHEN** no continuation checkpoints exist on this machine
- **THEN** the screen states that none were found and names the hook-install
  step that enables them

### Requirement: Safe Adoption Dry Run

The Adopt flow SHALL scan a user-chosen path without modifying it, including
before any Knowledge Base is initialized (pre-init scan-only), and SHALL present
what was found (file/markdown/folder totals, pack suggestions, safe next
actions). Any write mode (save-manifest, copy-as-sources, compile-selected) SHALL
require an explicit confirmation that names the mode and destination. Adoption
SHALL never rewrite the user's original files, and TUI tests SHALL never point at
a real user vault.

#### Scenario: Pre-init scan works

- **WHEN** the user scans a folder before a Knowledge Base exists
- **THEN** the scan-only report renders (totals, likely packs, safe next
  actions)
- **AND** nothing on disk is created or modified by the scan

#### Scenario: Writes are gated

- **WHEN** the user proceeds from a scan to copy-as-sources
- **THEN** the TUI requires an explicit confirmation naming the mode and the
  governed destination before invoking the write mode

### Requirement: Pack Selection

The Packs screen SHALL list the built-in knowledge-pack catalog with a one-line
benefit per pack, show the currently persisted selection, and support
multi-select enable/disable persisted through the supported pack-selection
operations. Pack identifiers SHALL remain stable even if display names improve.
The screen SHALL explain that packs guide interpretation and retrieval defaults
and SHALL NOT force a folder structure or a rigid ontology on the user.

#### Scenario: Enable two packs

- **WHEN** the user multi-selects two packs and applies
- **THEN** the persisted selection reflects both packs via the supported
  persistence path
- **AND** re-opening the screen shows the same selection

### Requirement: Actionable Status Screen

The Status screen SHALL present read-only diagnostics assembled from existing
functions: doctor preflight summary, resource posture (mode/models/media/
deferred work), readiness/warming state, hook installation checks, and install
provenance. Every failing or warning check SHALL show its remediation. The
screen SHALL NOT display secret values, and SHALL NOT trigger model downloads or
writes as a side effect of rendering.

#### Scenario: Failing check has a next step

- **WHEN** a diagnostic check fails
- **THEN** the Status screen shows the check, its state, and a concrete
  remediation command or explanation

### Requirement: Safe Settings Subset

The Settings screen SHALL expose only settings a user can safely understand and
change from the TUI — at minimum the persisted compute mode (quiet/normal/
performance) — grouped intelligibly, with the storage location of each setting
shown. Secrets SHALL never be displayed in plaintext. Destructive or
data-affecting settings SHALL require confirmation, and anything not supported
by a real persistence path SHALL NOT be rendered as editable.

#### Scenario: Change compute mode

- **WHEN** the user switches the compute mode in Settings
- **THEN** the mode is persisted through the supported mode-write path and the
  new value is reflected immediately

### Requirement: First Run Is An Accreting Ledger

When no vault resolves, the TUI SHALL open a single first-run screen that
accretes answers rather than stepping through separate wizard screens. Each
answered step SHALL collapse into one `✓` receipt line at the top of the
screen, and the active question SHALL own the rows beneath the ledger. The
steps SHALL be vault → packs → capture → ask, where choosing to scan a folder
first inserts a read-only scan step before any vault write. Every step except
the vault SHALL be skippable, and skipping SHALL itself leave a readable line.
The vault question's escape SHALL be an explicit "Skip for now" option that
lands on a working, vault-less Home. First run SHALL NOT reopen once a vault
is connected.

#### Scenario: Fresh machine to first capture

- **WHEN** the TUI starts with no resolvable vault and the user answers the
  vault, packs, capture, and ask steps
- **THEN** the vault is initialized through the existing init path, the packs
  selection is persisted through the existing pack path, the capture is written
  as an immutable Source, and the closing state cites the captured note as the
  result of the user's own query

#### Scenario: Nothing is written before it is announced

- **WHEN** the user selects "Create a fresh vault" and enters a path
- **THEN** a preview naming the folder, its governed layer, and the number of
  files that will be created renders BEFORE any write, and the vault is created
  only after the user confirms that step

#### Scenario: Escape rewinds one line and stops at a write

- **WHEN** the user presses escape after a step that performed no write
- **THEN** that ledger line is removed and its question becomes active again
- **WHEN** the user presses escape at a line that recorded a write
- **THEN** the line stays, and the screen states that a recorded write cannot
  be rewound

#### Scenario: Creating on an existing vault connects to it

- **WHEN** the user points "Create a fresh vault" at a folder that already
  holds a Knowledge Base
- **THEN** the session connects to that vault instead of erroring, and the
  receipt says the folder already held one

#### Scenario: A failed write recovers in place

- **WHEN** a vault write fails
- **THEN** the screen renders the failure as a status line, states that nothing
  was changed, and offers selectable recoveries — never API language such as a
  `force` parameter, and never a dead end

### Requirement: Receipt Language For Completed Actions

Every completed action SHALL render as a one-row receipt in a shared format:
status glyph, then a status word, then the detail, with the glyph-and-word pair
padded to a fixed label column so receipts align when read down. Receipts SHALL
name what actually happened, including the destination path and any count the
action produced. A receipt SHALL never wrap: the detail is fitted to the cell
budget, and where a path must be shortened it SHALL be truncated from the left
so the filename survives.

#### Scenario: A save states where it went

- **WHEN** a capture is written
- **THEN** a receipt renders `✓ saved` with the kind and the destination path,
  plus the note's semantic unit when it has one

#### Scenario: Receipts survive a narrow terminal

- **WHEN** a receipt's detail exceeds the available cells at 80 columns
- **THEN** the status glyph and word remain intact and the detail is truncated,
  with paths losing leading segments rather than the filename

### Requirement: Session Receipts Are Process State

Home SHALL show a "This session" block listing the receipts recorded by the
running process, and SHALL state that these are session-local while the files
themselves live in the vault. This block SHALL NOT be read from or written to
the backend.

#### Scenario: Actions accumulate in the session log

- **WHEN** the user captures a note and then asks a question
- **THEN** Home's session block lists both actions as receipt lines, labelled
  as session-local

### Requirement: Recovery Template For Failures, Empty, And Degraded States

Every failure, empty result, and degraded state SHALL render in one template:
a status glyph and word, one line stating what happened, a dim line stating
what was and was not changed, and a list of selectable next actions with the
recommended one pre-selected. A traceback SHALL never reach the screen, a
failure SHALL never be rendered in the accent color, and no failure state SHALL
leave the screen inoperable.

#### Scenario: A failed read offers real next actions

- **WHEN** a registry call fails
- **THEN** the screen shows the structured message, states that nothing was
  changed, and offers selectable recoveries such as retrying or opening Status

#### Scenario: An empty result recovers rather than apologises

- **WHEN** a query returns no results
- **THEN** the screen states that recall never invents and offers rephrasing,
  widening the scope to the whole vault, and capturing what the user knows

### Requirement: Keyboard-First Navigation And Accessibility

All primary screens SHALL be fully operable by keyboard with visible focus, a
discoverable help overlay, and a global command palette reaching every major
action. Back/escape SHALL never silently discard non-empty typed input without
confirmation. Destructive actions SHALL require confirmation. Status SHALL never
be conveyed by color alone; the UI SHALL respect `NO_COLOR` and degrade its
glyphs to ASCII when the terminal encoding cannot render Unicode symbols.

Selection SHALL be marked by a bar in the row's first cell in addition to any
background tint, so the cursor is visible without color. `u` SHALL refresh the
current screen's data on every screen that holds data.

#### Scenario: Selection is visible without color

- **WHEN** the UI renders under `NO_COLOR` with an ASCII glyph set
- **THEN** the selected row still carries a bar in its first cell, and every
  status still reads as a glyph followed by a word

#### Scenario: Help is one key away

- **WHEN** the user presses the help key on any primary screen
- **THEN** an overlay lists the active key bindings without leaving the screen

#### Scenario: Escape protects typed input

- **WHEN** the user presses escape with unsaved text in a capture or ask input
- **THEN** the TUI either keeps the input or asks for confirmation before
  discarding it

### Requirement: Responsive Terminal Layouts

The TUI SHALL remain fully usable at 80x24 by simplifying layout (collapsing
side panels into on-demand overlays) rather than overflowing, and SHALL use the
additional space at 120x40 and wider for persistent detail panels. No primary
flow SHALL horizontally overflow at 80 columns.

#### Scenario: Narrow terminal simplifies

- **WHEN** the TUI renders at 80x24
- **THEN** every primary screen fits without horizontal overflow and all primary
  actions remain reachable

### Requirement: Non-Blocking Operations With Honest Cancellation

Every registry invocation from the TUI SHALL run off the UI thread. In-flight
reads SHALL be cancellable, and a cancelled read's late result SHALL NOT mutate
the UI. Mutations SHALL NOT be silently abandoned: once confirmed, a write shows
progress until a terminal success or error state is rendered.

#### Scenario: Slow write stays visible

- **WHEN** a confirmed write takes seconds to complete
- **THEN** the UI shows an in-progress state for that write until it reports
  success or a structured error, and other screens remain usable meanwhile

### Requirement: Deterministic Headless Testing

The TUI SHALL ship an automated test suite that runs headless without paid
credentials or network: view-model/unit tests, service-boundary tests against a
temporary synthetic vault, keyboard-navigation pilot tests, snapshot tests for
representative screens at 80x24 and 120x40, empty/loading/error-state tests,
cancellation tests, and non-TTY behavior tests. The suite SHALL skip cleanly
when the `tui` extra is not installed so the lean test matrix is unaffected, and
SHALL never touch a real user vault.

#### Scenario: Lean environment stays green

- **WHEN** the repository test suite runs without the `tui` extra installed
- **THEN** TUI tests are skipped, not failed, and no other suite behavior
  changes

#### Scenario: Snapshots pin representative screens

- **WHEN** the TUI snapshot suite runs with the `tui` extra installed
- **THEN** representative screens (Home ready/warming/degraded/`NO_COLOR`, the
  first-run ledger at welcome, preview, mid, and done, Ask with results,
  partial, empty, error and preview, Capture composing, its governed question
  and its receipt, Review queue, triaged, context and empty, Adopt, Status,
  Packs, Settings, Continue) are compared against committed goldens at 80x24,
  100x30, and 120x40

#### Scenario: Goldens do not diff on timing

- **WHEN** a snapshot renders a state that displays a measured retrieval time
- **THEN** the elapsed measurement is pinned by the test harness so the stored
  frame never differs by a millisecond
