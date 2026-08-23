# attention-queue Specification

## Purpose
Give reviewers one daily, measurement-only front door over the default
`bridge_review`, `corpus_contradictions`, `stale_review`,
`unprocessed_source`, and `relation_debt` queues while retaining opt-in access
to registered typed semantic categories. The operation deterministically ranks
and deduplicates existing audit findings via Reciprocal Rank Fusion, never mutates
the vault or changes `find` ranking, reports explicit truncation, and keeps
bridge-review causes private and read-only.

## Requirements

### Requirement: Unified Review Surface Composed From The Epistemic Queues

The default `attention` category union SHALL preserve the existing review queues and the already-shipped `relation_debt` queue while adding `bridge_review` and deterministic `entity_type_unregistered` audit findings. Its default category and tiebreak-preference order SHALL retain the existing ordering and SHALL compose unregistered entity types without giving them a dismiss-to-silence path. The broader registered attention category set SHALL continue to admit its existing typed semantic categories. `attention` SHALL consume one audit pass over its selected categories and SHALL remain read-only.

`bridge_review` SHALL use read-only reference resolution; it SHALL not write canonical governance facts or review state during scanning. A release grant's bridge SHALL surface per approved audience for only these generic, bridge-path-anchored causes: due review date, bridge edited/stale approval, source or relevant restriction changed, and source unavailable/ambiguous. Detail, metadata, related paths, review context, and public responses SHALL not disclose restricted source title, path, ref, or other provenance.

#### Scenario: Default attention composes the effective queue union

- **WHEN** `attention` is called without a category filter
- **THEN** it composes bridge review, contradiction, stale-review, unprocessed source, relation-debt, and unregistered-entity-type findings without removing any existing queue
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Category subset and registered-category validation

- **WHEN** `attention` is called with `categories=["entity_type_unregistered"]`
- **THEN** only unregistered-entity-type items are surfaced
- **AND** the registered existing categories remain accepted while an unregistered category raises a `ValueError` naming the valid set

#### Scenario: Unregistered type resolves only from state

- **WHEN** an unregistered-type attention item is dismissed or snoozed without changing the registry or pages
- **THEN** unchanged audit state remains eligible to surface
- **AND** every reason reports a `decision` key, with the recorded action on ordinary reasons and `null` on state-resolved-only reasons
- **AND** registering the type or moving the pages clears the item on the next pass

#### Scenario: Source drift produces a private bridge finding

- **WHEN** an approved dependency changes, is deleted, or resolves ambiguously
- **THEN** the bridge surfaces with a generic `bridge_review` cause and no source provenance

#### Scenario: All four queues compose into one list
- **WHEN** `attention` is called with the historical four-queue category subset over a vault that has
  stale, contradiction, unprocessed-source, and relation-debt findings
- **THEN** it returns a single `items` list drawn from all four queues, plus a
  `summary` of the contributing-finding count per category
- **AND** no governed note under the vault is created, modified, moved, or deleted

#### Scenario: Category subset and invalid category
- **WHEN** `attention` is called with `categories=["relation_debt"]`
- **THEN** only relation-debt items are surfaced
- **AND** calling it with an unregistered category raises a `ValueError` naming
  the complete registered set

#### Scenario: A due prediction reaches the daily surface unasked
- **WHEN** `attention` is called without a category filter over a vault holding a
  due, unresolved prediction
- **THEN** that prediction is surfaced as a ranked review item without the caller
  naming the `prediction_window` category

### Requirement: Deterministic Cross-Queue Ranking By Reciprocal Rank Fusion

The system SHALL rank the effective category union by the existing deterministic
weighted Reciprocal Rank Fusion over each queue's emission order, with `k=60` and
equal default weights. It SHALL deduplicate by anchor path (partitioned by a
bridge review's audience partition or a prediction's unit partition where
present). Identical inputs SHALL order by score descending, then the best
contributing category in this exact preference order: `bridge_review`,
`prediction_window`, `corpus_contradictions`, `stale_review`,
`unprocessed_source`, `relation_debt`, then path and partition. Reasons and
category lists SHALL use that same category preference after their intra-queue
rank.

`meta.signal_version` for `bridge_review` SHALL deterministically include bridge
bytes, review date, approval identity, cause, and dependency-state digest, and
SHALL exclude today. A due date SHALL surface review work without expiring an
otherwise exact release. Triage, dismissal, and snooze SHALL neither approve nor
renew a bridge; a dismissed item SHALL resurface only when facts change. Audience
partitions SHALL be independent. Exact reapproval of current bridge and
dependency state SHALL clear stale findings.

#### Scenario: Rank-major interleave at equal weights

- **WHEN** each default queue contributes several findings in its emission order
- **THEN** equal weights surface every queue's rank-1 before any rank-2, with ties
  broken by the effective category preference
- **AND** running the ranking twice over identical findings yields identical output

#### Scenario: Equal-score categories use the effective tiebreak

- **WHEN** equal-weight queues contribute equal-score items
- **THEN** the deterministic order prefers bridge review, prediction window,
  contradiction, stale review, unprocessed source, then relation debt before
  path/partition

#### Scenario: Due review remains stable across dates

- **WHEN** the same due bridge is scanned on different later dates without a fact
  change
- **THEN** its signal version remains stable and its release is not expired solely
  by the due date

#### Scenario: Exact reapproval clears a stale review

- **WHEN** a stale bridge receives a new exact release approval after review
- **THEN** the stale bridge-review finding no longer appears for that audience

### Requirement: Multi-Signal Additivity With Dedup By Anchor

The system SHALL dedup items by anchor path into one item per path carrying a `reasons`
list (one reason per contributing finding), and a path flagged by more than one queue
SHALL receive the sum of its per-queue RRF votes so it ranks above any item flagged by
only one queue at the same per-queue rank. A `corpus_contradictions` pair SHALL surface
under its anchor path with the other endpoint preserved in the reason's `related_paths`;
the second endpoint SHALL NOT become its own item unless independently flagged.

A partitioned queue SHALL refine that anchor rather than escape it. Where a path carries
partitioned findings, the system SHALL emit one item per partition and SHALL fold that
path's unpartitioned findings into every one of them, contributing both their reasons and
their RRF votes. A path's unpartitioned findings MUST NOT form an additional item of their
own whenever that path also carries a partitioned finding, so a note is never listed once
for its page-level signals and again for a partition. A path carrying exactly one
partitioned finding SHALL therefore compose exactly one item, and the per-queue votes of
that item SHALL still sum.

#### Scenario: A doubly-flagged note rises and keeps both reasons

- **WHEN** note `N` appears in both `stale_review` and as a `corpus_contradictions`
  anchor
- **THEN** `N` is a single item whose `categories` lists both, whose `reasons` holds both
  findings, and whose score equals the sum of the two RRF votes
- **AND** `N` ranks above an otherwise-equivalent item flagged by only one queue

#### Scenario: A partitioned queue co-flagging with a page-level queue stays one row

- **WHEN** note `N` carries one due prediction and is also flagged by `stale_review`
- **THEN** `N` composes exactly one item whose `categories` lists both, whose `reasons`
  holds both findings, and whose score is the sum of the two RRF votes
- **AND** `N` ranks above an otherwise-equivalent item flagged by only one queue

#### Scenario: Several partitions each inherit the page-level reasons

- **WHEN** note `N` carries two due predictions and is also flagged by `relation_debt`
- **THEN** `N` composes two items with distinct review identities, each carrying its own
  prediction reason and the shared `relation_debt` reason
- **AND** no additional item is composed for the `relation_debt` finding alone

#### Scenario: Contradiction pair preserved under its anchor

- **WHEN** a contradiction finding has `path=A` and `paths=[A,B]`
- **THEN** the item's path is `A` and its contradiction reason carries
  `related_paths=[A,B]`
- **AND** `B` is not surfaced as its own item unless `B` is independently flagged

### Requirement: Capped Surfacing With Explicit Counts

The system SHALL cap the surfaced items at `limit` and SHALL report the number of items
not shown (`truncated`) plus the number of contradiction pairs the upstream
`corpus_contradictions` cap (`EXOMEM_CONTRADICTION_TOP_N`) itself omitted
(`upstream_truncated`), folding the contradiction queue's trailing summary finding into
that count rather than surfacing it as a review item. It MUST NOT silently truncate:
whenever either count is non-zero it SHALL include an explanatory `note`. A `limit` of
`0` or negative SHALL disable the cap and surface all items (mirroring
`EXOMEM_CONTRADICTION_TOP_N`'s `0 = uncapped`).

#### Scenario: Items beyond the limit are counted

- **WHEN** more eligible items exist than `limit`
- **THEN** exactly `limit` items are surfaced, `truncated` equals the remainder, and a
  `note` states how many more are not shown
- **AND** when `limit` exceeds the eligible count, `truncated` is 0 and no `note` is added

#### Scenario: Non-positive limit surfaces everything

- **WHEN** `limit` is `0` or negative
- **THEN** every eligible item is surfaced, `truncated` is 0, and no `note` is added for
  the cap

#### Scenario: Upstream contradiction cap is folded, not shown

- **WHEN** the `corpus_contradictions` queue emits its trailing summary finding
  reporting upstream-capped pairs
- **THEN** that finding is not surfaced as a review item, `upstream_truncated` carries
  its count, and the `note` reports the upstream-capped pairs separately

### Requirement: Measurement-Only Composition

The composition SHALL be measurement-only. The system MUST NOT mutate any note, MUST NOT
auto-supersede, and MUST NOT change `find` ranking. It MUST NOT read, embed, or compare
note content at attention time beyond the deterministic rank arithmetic over findings
the checks already produced, and MUST NOT perform any cross-item synthesis or judgment.
Each surfaced item SHALL carry review-only guidance that defers the keep / supersede /
reconcile / compile / archive decision to the reader.

#### Scenario: Attention run leaves the vault and find untouched

- **WHEN** `attention` runs over a vault
- **THEN** no file under the vault is created, modified, moved, or deleted
- **AND** `find` ranking is unchanged
- **AND** each item's guidance states the ranking is a measurement, not a judgment that
  anything is wrong, and that nothing is auto-acted

### Requirement: Single Front-Door Command On All Surfaces

The `attention` operation SHALL be defined by a single command-registry entry and SHALL
be reachable as an MCP tool, a REST route (`/api/attention`), and a CLI subcommand
(`kb attention`) with no per-surface code, with its parameters derived as exactly
`categories` and `limit`. Its MCP description SHALL position it as the daily review
front door and SHALL defer the full lint/health report to `audit` so natural-language
tool selection routes correctly.

#### Scenario: One registry entry exposes attention everywhere

- **WHEN** the registry is built
- **THEN** an `attention` MCP tool, an `/api/attention` REST route, and a `kb attention`
  CLI subcommand all exist from the one entry
- **AND** the tool's derived parameters are exactly `categories` and `limit`

### Requirement: Dedicated Corpus Activation Review Composition

The system SHALL expose corpus activation through `review_memory(mode="activation")`. It SHALL rank activation findings by equal-weight Reciprocal Rank Fusion with fixed category preference `unregistered_relation` > `provenance_debt` > `typed_relation_debt` > `relation_debt`, deduplicate by anchor path, preserve all contributing reasons, apply the requested cap with explicit truncation, and return the corpus coverage counts alongside the queue. The default `attention` operation and its default categories SHALL remain unchanged.

#### Scenario: Activation backlog stays out of daily attention

- **WHEN** a vault contains activation findings and `review_memory(mode="attention")` is called without categories
- **THEN** the existing daily attention categories and ordering are used unchanged
- **AND** `review_memory(mode="activation")` returns the separately ranked activation backlog and coverage

#### Scenario: Multiple activation deficits deduplicate and rise

- **WHEN** one page has both provenance and typed-relation debt while another has only typed-relation debt at the same intra-category rank
- **THEN** the first page appears once with both reasons and the sum of its RRF votes
- **AND** it ranks above the page with one signal

### Requirement: Activation Items Use Stable Review Lifecycle

Activation items SHALL receive the same stable review reference, content-bound signal fingerprint, open/all/snoozed/dismissed filtering, and triage behavior as daily attention items. Activation references SHALL be deterministically distinct from daily-attention references for the same target so item lookup and triage resolve the intended queue. Item lookup and triage SHALL resolve activation-only references. A materially changed signal SHALL resurface even if its previous fingerprint was dismissed.

#### Scenario: Activation-only item can be triaged

- **WHEN** an activation item that is absent from default attention is dismissed through `triage_memory`
- **THEN** it is hidden from the open activation view and visible in the dismissed or all view
- **AND** default attention behavior is unaffected

#### Scenario: Changed knowledge resurfaces

- **WHEN** a dismissed page is edited so its measured activation signal version changes while a deficit remains
- **THEN** the new activation fingerprint is open again

#### Scenario: Overlapping queues retain independent triage

- **WHEN** the same page appears in both daily attention and corpus activation
- **THEN** the two items have distinct stable review references
- **AND** dismissing the activation item does not dismiss or resolve to the daily-attention item

### Requirement: Activation Mode Is Shared Across Product Surfaces

The activation mode SHALL be implemented in the shared `review_memory` leaf and SHALL therefore be reachable through the generated MCP tool, REST route, OpenAPI operation, and CLI command without surface-specific activation logic.

#### Scenario: Same activation contract on every surface

- **WHEN** activation is invoked through MCP, `/api/review_memory`, and `kb review_memory --mode activation --json` over the same vault state
- **THEN** each surface returns the same coverage, ordered item paths, categories, and stable references

### Requirement: Derivation double-counting is an optional deterministic review category

The `derivation_double_counting` audit category SHALL walk `sources:`
(`derived_from`) chains and surface two finding kinds. A `support_collapse`
finding SHALL be emitted for an active, writable compiled page of the types
whose frontmatter specification marks provenance required — `research-note`,
`insight`, `failure`, and `pattern` — when two or more of its directly cited
`sources:` have ancestor chains that converge on a shared node; the finding
SHALL name the nearest such shared ancestor and every contributing direct
source. The citing page itself SHALL NEVER be reported as its own shared
ancestor. One converging tail of shared ancestors SHALL produce one finding
naming the nearest node in that tail, not one finding per node in the tail.
A `cycle` finding SHALL be emitted whenever a `sources:` chain is reachable
from itself, including a direct self-reference, regardless of the
originating page's type or status. The category SHALL be optional and absent
from the default audit sweep (`ALL_CATEGORIES`) and from the `attention`
composed queue's category set; it is requested explicitly via
`audit(categories=["derivation_double_counting"])`.

Every finding SHALL be `info` or `warn` severity, never `error`: a `cycle`
finding SHALL be `warn`, a `support_collapse` finding SHALL be `info`. The
category SHALL be strictly read-only — it SHALL NOT mutate a note, rewrite a
relation, or change `find` or `attention` ranking — and SHALL propose review
rather than mutation.

#### Scenario: A diamond of derivation is surfaced as support collapse

- **WHEN** a source `S` is cited by two derived notes `A` and `B`, and a
  third active compiled note `C` cites both `A` and `B` in its `sources:`
- **AND** `derivation_double_counting` is requested explicitly
- **THEN** one `support_collapse` finding is emitted for `C`, naming `S` as
  the shared ancestor and `A`/`B` as the contributing sources

#### Scenario: Independent sources are not flagged

- **WHEN** a compiled note cites two sources whose ancestor chains never
  converge
- **THEN** no `support_collapse` finding is emitted for that note

#### Scenario: A citing page is never its own shared ancestor

- **WHEN** a compiled page cites two sources that each, transitively, cite
  the compiled page back
- **THEN** no `support_collapse` finding for that page names the page itself
  as the `shared_ancestor`

#### Scenario: One converging tail produces one finding, not one per node

- **WHEN** two of a page's directly cited sources both trace up through a
  multi-hop shared tail (for example `C` cited by both, `C` itself citing
  `D`, `D` citing `E`)
- **THEN** exactly one `support_collapse` finding is emitted for that page,
  naming the nearest node in the tail as the shared ancestor

#### Scenario: A circular `sources:` chain is detected and terminates

- **WHEN** a `sources:` chain loops back on itself, including a direct
  self-reference
- **THEN** exactly one `cycle` finding is emitted for that cycle regardless
  of how many nodes in it have outgoing `sources:` edges
- **AND** the walk terminates rather than looping indefinitely

#### Scenario: A capped traversal is visible, never silent

- **WHEN** the chain walk's depth or shared total-edge budget stops
  exploration before it completes
- **THEN** a dedicated `truncated` finding is emitted naming which cap(s)
  were actually hit (`depth`, `edges`, or both) and both configured limits
- **AND** a run that never hits either cap emits no `truncated` finding
- **AND** a per-page `support_collapse` finding computed from a truncated
  closure carries the same reason(s) in its own meta, never a generic flag
  that cannot distinguish which cap was responsible

#### Scenario: Category is opt-in and never auto-repaired

- **WHEN** an audit is computed with no `categories` filter
- **THEN** `derivation_double_counting` findings are absent from the default
  sweep
- **AND** no audit repair pass writes, infers, or removes a `sources:` entry
  on its behalf

### Requirement: Prediction Window Review Queue

The audit registry SHALL expose a `prediction_window` category that surfaces a rich
semantic unit whose authored check date has come due without any recorded sign that the
unit was checked. A unit SHALL be surfaced when all of the following hold: it carries an
authored `check_by` that parses to a calendar date, that date is on or before today, it
carries no `verdict` metadata, and it carries no outbound relation whose registry-resolved
canonical kind is one of `supports`, `contradicts`, `resolves`, or `evidenced_by`.

The resolution test SHALL be unit-local. Only relations authored on the unit itself SHALL
clear it; a relation authored elsewhere on the parent page MUST NOT, and an inbound
relation authored on another page MUST NOT. Relation kinds SHALL be compared after
registry resolution to their canonical key, so a registered alias resolves correctly.

The queue SHALL exclude pages whose `status` is `superseded`, `archived`, or `draft`,
index and log pages, and pages outside a read-write access tier. Findings SHALL carry
severity `info`, SHALL anchor on the parent page's vault-relative path, and SHALL be
ordered most-overdue-first by days past `check_by`, with the parent path and the unit
fingerprint as deterministic tiebreaks.

The check SHALL be a pure measurement over authored Markdown. It MUST NOT write, judge
whether the prediction held, assign a verdict, or change `find` ordering. Its proposed fix
SHALL defer the decision to the reader.

#### Scenario: A due prediction with no verdict and no resolving relation surfaces

- **WHEN** a unit carries `check_by` of a date 14 days ago, no `verdict`, and no resolving
  relation
- **THEN** a `prediction_window` finding is emitted at `info` severity anchored on the
  parent page whose meta reports 14 overdue days
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: A verdict clears the window

- **WHEN** the same unit carries a `verdict`
- **THEN** no `prediction_window` finding is emitted for it

#### Scenario: An outbound resolving relation clears the window

- **WHEN** the same unit carries an outbound relation whose canonical kind is `resolves`,
  `supports`, `contradicts`, or `evidenced_by`
- **THEN** no `prediction_window` finding is emitted for it

#### Scenario: A non-resolving outbound relation does not clear the window

- **WHEN** the same unit's only outbound relation has canonical kind `relates_to`
- **THEN** a `prediction_window` finding is still emitted for it

#### Scenario: A relation on a sibling unit does not clear the window

- **WHEN** a due unit carries no relation and a different unit on the same page carries a
  resolving relation
- **THEN** a `prediction_window` finding is still emitted for the due unit

#### Scenario: A future check date is not yet due

- **WHEN** a unit carries a `check_by` date later than today
- **THEN** no `prediction_window` finding is emitted for it

#### Scenario: A unit without a check date is never surfaced

- **WHEN** a unit carries no `check_by`
- **THEN** no `prediction_window` finding is emitted for it

#### Scenario: The queue is ordered most-overdue-first

- **WHEN** several due units have different overdue ages
- **THEN** the findings are emitted with the largest overdue age first and ties broken by
  parent path and unit fingerprint

### Requirement: Prediction Window Findings Are Identified By Unit Fingerprint

Each `prediction_window` finding SHALL carry the surfaced unit's fingerprint as its
`meta.signal_version` and as its `meta.review_partition`, so that the composed review item
is per-unit rather than per-page and its durable review-state identity is bound to the
unit's authored state.

Two due units on one page SHALL therefore compose as two independent review items sharing
that page's path, each with its own review-state identity, so a decision recorded against
one MUST NOT dispose of the other. Editing a surfaced unit SHALL change its fingerprint and
therefore its review item's fingerprint, so the item resurfaces for review rather than
inheriting a decision recorded against different authored content.

#### Scenario: Two due predictions on one page are two review items

- **WHEN** a page carries two due, unresolved units and `attention` is called with the
  `prediction_window` category
- **THEN** two review items are surfaced sharing that page's path with distinct review
  identities and distinct fingerprints

#### Scenario: Editing a prediction moves its fingerprint

- **WHEN** a surfaced unit's authored content is changed and the queue is recomputed
- **THEN** the finding's `signal_version` differs from the value recorded before the edit

### Requirement: Prediction Window Reaches The Audit Surface

`prediction_window` SHALL be a valid `audit` category, selectable through the existing
`categories` filter and rejected by neither the audit validator nor the attention
validator. Selecting a category set that omits `prediction_window` SHALL reproduce the
prior behaviour of both `audit` and `attention` exactly, so the category can be disabled
without residue.

#### Scenario: The category is selectable on audit

- **WHEN** `audit` is called with `categories=["prediction_window"]`
- **THEN** the due prediction is reported and the summary counts it under that category
  name

#### Scenario: The category is selectable on attention

- **WHEN** `attention` is called with `categories=["prediction_window"]`
- **THEN** the due prediction is surfaced as a ranked review item carrying its reason, and
  no `ValueError` is raised for the category name

### Requirement: Unfinished Experiment Lifecycle Review Queue

The audit registry SHALL expose an `unfinished_experiments` category that surfaces an
experiment page whose declared window has closed without a recorded result. A page
SHALL be surfaced when all of the following hold: its `type` is `experiment`, its
`started` frontmatter parses to a calendar date, its `duration` frontmatter parses to a
finite span of whole days, the elapsed days from `started` to today exceed that span,
and no non-empty `outcome:` is recorded. A page whose `duration` is open-ended or
otherwise does not parse to a finite span MUST NOT be surfaced, because an experiment
that declares no deadline cannot have missed one.

The queue SHALL exclude pages whose `status` is `archived`, `superseded`, or `draft`,
index and log pages, and pages outside a read-write access tier, matching the scope
discipline of the existing measurement-only queues. Findings SHALL carry severity
`info` and SHALL be ordered oldest-first by elapsed days since `started`, with the
vault-relative path as a deterministic tiebreak. Each finding SHALL carry a stable
`meta.signal_version` derived from the page's authored state, plus `started`,
`duration_days`, `elapsed_days`, and `overdue_days`.

The check SHALL be a pure measurement over recorded frontmatter. It MUST NOT write,
conclude, archive, or infer an outcome, and it MUST NOT change `find` ordering. Its
proposed fix SHALL defer the decision to the reader among recording the result,
extending the duration, and archiving the experiment.

#### Scenario: An experiment past its window with no outcome is surfaced

- **WHEN** an active experiment declares `started` 120 days ago and `duration` of
  `30 days` and records no `outcome:`
- **THEN** an `unfinished_experiments` finding is emitted for that page at `info`
  severity whose meta reports 120 elapsed days and 90 overdue days
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: A recorded outcome closes the loop

- **WHEN** the same experiment records a non-empty `outcome:`
- **THEN** no `unfinished_experiments` finding is emitted for it

#### Scenario: A concluded status without an outcome still surfaces

- **WHEN** an experiment past its window carries `status: concluded` but records no
  `outcome:`
- **THEN** an `unfinished_experiments` finding is still emitted, because the status
  records that the experiment stopped and not what it showed

#### Scenario: An open-ended duration is never overdue

- **WHEN** an experiment declares `duration: ongoing` and a `started` date years in the
  past with no `outcome:`
- **THEN** no `unfinished_experiments` finding is emitted

#### Scenario: An experiment still inside its window is not surfaced

- **WHEN** an active experiment declares `started` 10 days ago and `duration` of
  `30 days`
- **THEN** no `unfinished_experiments` finding is emitted

#### Scenario: The queue is ordered oldest-first

- **WHEN** several experiments are past their windows with different elapsed ages
- **THEN** the findings are emitted with the largest elapsed age first and ties broken
  by vault-relative path

### Requirement: Unfinished Experiments Is Registered But Not Default In Attention

`unfinished_experiments` SHALL be a valid `attention` category, selectable through the
existing `categories` filter, and SHALL be rejected by neither the attention validator
nor the audit validator. It MUST NOT be a member of the default attention category
union, so an `attention` call made without a category filter SHALL NOT surface an
`unfinished_experiments` item.

The exclusion SHALL be justified by backlog profile rather than by category kind. The
`started` and `duration` fields this check reads long predate it, so an established
vault can already hold a large population of long-closed windows, and admitting them to
the daily surface at upgrade time would displace the signal already there. A sibling
epistemic-lifecycle category whose fields are new enough to have no such population
SHALL NOT be excluded on this reasoning.

Selecting a category set that omits `unfinished_experiments` SHALL reproduce the prior
behaviour of both `audit` and `attention` exactly, so the category can be disabled
without residue.

#### Scenario: The default attention surface excludes the category

- **WHEN** `attention` is called without a category filter over a vault holding an
  overdue experiment
- **THEN** no `unfinished_experiments` item is surfaced

#### Scenario: The category is selectable

- **WHEN** `attention` is called with `categories=["unfinished_experiments"]`
- **THEN** the overdue experiment is surfaced as a ranked review item carrying its
  reason, and no `ValueError` is raised for the category name

#### Scenario: The category reaches the audit surface

- **WHEN** `audit` is called with `categories=["unfinished_experiments"]`
- **THEN** the overdue experiment is reported and the summary counts it under that
  category name

### Requirement: Asserted Contradictions Rank Above Proximity In Attention

The ranked review surface SHALL preserve the `corpus_contradictions` queue's
asserted-before-proximity emission order as intra-queue rank, so an authored
`contradicts` pair receives a better rank — and therefore a larger Reciprocal Rank
Fusion vote — than any proximity pair from the same queue. The composition SHALL
require no separate ranking rule for asserted entries: emission order remains rank,
exactly as it is for every other queue.

Each surfaced item's reasons SHALL carry the contributing finding's
`meta.provenance` unchanged, so a reader can tell an asserted conflict from a
measured adjacency without leaving the review surface. Asserted entries SHALL carry
the same stable review reference, fingerprint, and triage contract as every other
attention item, and SHALL be dismissable, snoozable, and reopenable through the same
path.

An asserted entry SHALL NOT be mistaken for the contradiction queue's trailing
upstream-cap summary finding: only a finding carrying an upstream truncation count is
folded into `upstream_truncated`.

#### Scenario: Asserted contradiction outranks a proximity contradiction

- **WHEN** the contradiction queue contributes one asserted pair and one proximity
  pair over otherwise unflagged anchors
- **THEN** the asserted pair's item scores above the proximity pair's item
- **AND** its contradiction reason carries `meta.provenance: "asserted"`

#### Scenario: Asserted entries are triageable like any item

- **WHEN** an asserted contradiction item is dismissed through the normal triage path
- **THEN** it leaves the default open view and resurfaces only when either endpoint's
  content changes

### Requirement: Competing Is A Review State Honored By Every Queue

`competing` SHALL be a valid review action and a valid review view alongside `open`,
`all`, `snoozed`, and `dismissed`. An item whose effective state is `competing` SHALL
be excluded from the default open view by the same state filter that excludes
dismissed and snoozed items, so every queue built on the review-state store honors it
without queue-specific code. The reported state summary SHALL count `competing`
alongside the existing states.

For a contradiction item, an item-level review decision SHALL take precedence over
the pair stance; the pair stance SHALL be consulted only when no item-level decision
applies. An item carrying several contradiction pairs SHALL resolve to `competing`
only when EVERY one of its pairs is stanced, because one un-stanced rival is still
open review work. `reopen` on such an item SHALL clear both the item-level record and
the stance on every pair it carries.

Each contradiction reason SHALL carry a reference addressing its own pair's stance,
and a reason whose pair carries a stance SHALL be marked as such, so a partially
stanced item cannot present as an ordinary open item while one of its conflicts is
already dispositioned and suppressing a write-time warning. These annotations MUST
NOT alter the item's review identity or signal fingerprint.

#### Scenario: A competing item leaves the open view

- **WHEN** a contradiction pair carries a `competing` stance
- **THEN** the default open attention view omits it and the state summary counts it
  under `competing`
- **AND** requesting the `competing` view returns it

#### Scenario: Item-level dismissal takes precedence

- **WHEN** a contradiction item carries both a matching item-level dismissal and a
  matching pair stance
- **THEN** its effective state is `dismissed`

#### Scenario: One un-stanced pair keeps the item open

- **WHEN** an item carries two contradiction pairs and only one of them is stanced
- **THEN** the item remains in the default open view
- **AND** the stanced reason is marked as stanced while the other is not
- **AND** every contradiction reason carries a reference to its own pair's stance

#### Scenario: Reopen restores the item to the open view

- **WHEN** `reopen` is applied to a contradiction item that carries a pair stance
- **THEN** the item returns to the default open view

### Requirement: Relation debt is a deterministic attention source
The `relation_debt` audit SHALL surface active, writable compiled pages with no outbound body wikilinks or canonical note/block relations. It SHALL exclude append-only, read-only, archived, superseded, index, hub, and snapshot material. Each finding SHALL be informational, include a content-derived signal version, and propose relation/link review rather than automatic mutation.

#### Scenario: Isolated compiled note is surfaced
- **WHEN** an active writable research note has no outbound links or typed relations
- **THEN** relation debt emits one informational finding for that page
- **AND** adding a canonical relation removes the finding on the next audit

### Requirement: Review state filters after ranking without changing scores
Attention SHALL compute deterministic base scores before applying review state. It SHALL support `open` (default), `all`, `snoozed`, and `dismissed` state views, fill visible items up to the requested limit after filtering, and report hidden-state counts separately. State filtering SHALL NOT change the score or relative order of the remaining items.

#### Scenario: Hidden top item does not waste the visible limit
- **WHEN** the highest-ranked item is dismissed and attention is requested with `state="open"` and `limit=5`
- **THEN** the next five open items are returned in their original relative order
- **AND** the report counts the dismissed item separately

### Requirement: Plan-progress review is a standalone mode outside the Epistemic Inbox

Planned-versus-recorded review SHALL be a standalone read-only `review_memory` mode and SHALL NOT be an attention category. It SHALL NOT contribute items to the default `attention` union or to any registered typed semantic category, SHALL NOT participate in Reciprocal Rank Fusion or any other cross-queue ranking, SHALL NOT mint `exomem://review/<id>` references or signal fingerprints, and SHALL NOT be reachable by `review_item_context`, `triage_memory`, snooze, dismiss, or any other disposition.

The reason is the adjudication boundary the attention queue already draws. Inbox items exist so a reviewer can dispose of a proposed finding; plan-progress divergence is a measured fact about authored intent and recorded observation that the human interprets directly. Enrolling it would also require a rank, and ranking plans by divergence would be exactly the derived judgment the review refuses to make. The existing attention categories, ordering, fusion, dedup, and truncation behaviour SHALL remain unchanged by this mode.

#### Scenario: Default attention union is unchanged
- **WHEN** `review_memory(mode="attention")` runs over a vault whose Planning items diverge from their recorded evidence
- **THEN** the composed queue union, its default category order, and its counts are exactly what they were before plan-progress review existed
- **AND** no plan, evidence binding, or divergence count appears as an attention item

#### Scenario: Plan-progress produces no triageable reference
- **WHEN** a plan-progress review returns items with unresolved bindings and zero completion observations
- **THEN** the response carries plan references rather than review references
- **AND** no returned identifier can be snoozed, dismissed, or otherwise triaged

#### Scenario: Review state store is untouched
- **WHEN** a plan-progress review runs
- **THEN** no review-state entry, fingerprint, or disposition is read for adjudication or written
