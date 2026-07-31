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

#### Scenario: Degraded retrieval is announced

- **WHEN** retrieval returns a warming/degraded marker
- **THEN** the Ask screen states that results are partial and names the affected
  lanes near the results, not only in a log

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

### Requirement: First-Run Onboarding

When no vault resolves, the TUI SHALL open a first-run flow that lets the user
choose an existing vault path, create/initialize a fresh one, or scan a folder
for adoption, then optionally choose packs and see whether agent hooks are
installed — each step skippable and revisitable later. Completing (or skipping
through) onboarding SHALL land on a working Home screen. Onboarding SHALL NOT
force optional features.

#### Scenario: Fresh machine to first capture

- **WHEN** the TUI starts with no resolvable vault and the user completes the
  guided steps with a new vault path
- **THEN** the vault is initialized through the existing init path, optional
  pack selection is offered, and the session lands on Home ready to capture

### Requirement: Keyboard-First Navigation And Accessibility

All primary screens SHALL be fully operable by keyboard with visible focus, a
discoverable help overlay, and a global command palette reaching every major
action. Back/escape SHALL never silently discard non-empty typed input without
confirmation. Destructive actions SHALL require confirmation. Status SHALL never
be conveyed by color alone; the UI SHALL respect `NO_COLOR` and degrade its
glyphs to ASCII when the terminal encoding cannot render Unicode symbols.

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
- **THEN** representative screens (Home, Ask with results, Review, Adopt report,
  Status, first-run) are compared against committed goldens at 80x24 and 120x40
