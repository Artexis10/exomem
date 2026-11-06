## Context

`episode_memory` `record` writes a bounded recap as an `episode` Source. Nothing in
the recap ties it to the pages that the session changed, so a page can stay stale
while the recap says the work happened.

Four facts bound the design:

- The episode ledger binds recap inputs only. Ordinary writes (`edit_note`,
  `change_observations`, `supersede_note`) carry no episode key, so the server
  cannot know which writes belong to a session.
- A client may have no hook, no skill and no second call. The tool response is the
  one channel that every client reads.
- Whether a page states a recap line is a judgement of meaning. A model that made
  it on real recaps did not beat the base rate of the answer, so the server makes
  no such judgement and no word list or pattern stands in for one.
- Source closure already records a citation. A live compiled page that names a
  Source in `sources:` makes closure write the page into the Source's
  `ingested_into`, and `source_coverage.evaluate` reads that edge.

## Decisions

### Coverage is citation linkage

A recap is a Source with `ingested_into: []`. It counts as compiled for a caller
when `source_coverage.evaluate` finds a live compiled page that the caller can see
and that cites the recap in `sources:`. The wording everywhere is "cited by a live
page (citation only; items unassessed)". Coverage counts recaps and lines. It never
claims that a page states a decided item, because the agent that wrote the page
made that judgement and the server cannot check it.

### An episode is its revisions, and a line is open until a cited revision holds it

Revisions of one episode share the frontmatter `episode` key and the filename group
that the recorder retires revisions within. The live revision is the newest one
whose status is not superseded.

A Decided line is read back from the recap body with `episode_capture.parse`, which
accepts only a body in the recorded shape. A hand edit therefore yields no lines
and closes none. A Decided line of the live revision is open when, by exact string
equality, no cited revision holds it. A recap that is superseded after a page cited
it keeps that page's lines covered, so a later revision with the same lines is not
due and a later revision with one new line is due with one open line.

`episode_coverage.evaluate` returns the episode key, the live revision path and
ref, `captured`, the Decided count, the open lines and the cited revision count.
The same function serves the backlog family and the `record_episode` response.
The caller's `lookup` decides which citing pages exist, so a page the caller cannot
see reads as absent and the caller's item equals the item of a vault without it.

Known limit: entity, plan and record pages hold no `sources:` claim. Their updates
alone leave an episode uncited. In a mixed recap, one compiled note's citation holds
every line in that revision, including entity items. The compiler therefore cites
only after every Decided item is compiled or explicitly dismissed. This is an agent
completion convention, not a server check of meaning. A later change can add a claim
form for entities; Planning and Records need their own contracts.

### The backlog family ships off

The family `uncompiled_episode` sits beside `unprocessed_source` in the audit and
the due-state projection, with one item per episode that has an open line.

- **Recaps stay out of `unprocessed_source`.** The check keeps its skip of the
  `episode` source kind. It is the server-reserved kind, not a vocabulary entry. A
  dismissal of a recap must never arm the quiet offer on a real Source.
- **Dark by default.** `review_state.DEFAULT_DISPOSITIONS` holds one entry,
  `uncompiled_episode: off`, with the reason that a recap list is noise until its
  owner wants it. The owner turns it on through the existing disposition setting, so
  no new setting exists. The dispositions view lists a shipped default as a row
  until the owner sets one. The family stays out of
  `attention.DEFAULT_ATTENTION_CATEGORIES`, so no carrier offers it.
- **Item meta.** The episode key, the live revision ref, `captured`, `due_since`
  (the capture time, with no grace constant, because the consumer filters by age),
  the Decided count, the open count and a `signal_version` over the live revision,
  the sorted open lines and the citing pages. The `signal_version` makes a
  dismissal bind to the open set.
- **Projection.** `due_state.SCHEMA_VERSION` rises to 4 and the family sorts after
  `SOURCE_BACKLOG`. The projection serves the family to the owner only: its stored
  rows count lines against the owner's view of the citing pages, so a count served to
  a restricted caller would move with a page withheld from it. A restricted caller
  gets the family from its own audit, which reads a withheld citing page as absent. A write that cites or supersedes a recap refreshes the rows of
  the whole episode, because a citation of one revision changes the coverage of the
  live one. The write delta and a full rebuild agree.

### The effect block distinguishes clearance from unknown action

`review_memory(mode="dispositions")` reports `surfaced`, `dismissed`, `snoozed`,
`cleared`, `acted` and `open` per family. Every family reports `acted` as `unknown`,
never 0. Current citation state does not prove that a new citation followed
surfacing: reconciliation can discover an old citation or an existing citing page
can become live. A missing item counts as `cleared` when the current projection is
known, whether citation, supersession, deletion or a dropped line explains it.
Attribution needs a matching mutation receipt, which this change does not add.
`surfaced` is a lower bound, because an item shown where nothing
is recorded is not counted, and the block's `sources` says so. `window_days` sets
the window, 1 to 90 days and 7 by default, and a value outside the range is refused
with a message that names the range.

### The record path reports, never walks

`record` returns a `compile` block when the recap holds a Decided item. It carries:

- `cite`: the ref of the recap just recorded, which the agent puts in the `sources:`
  of each compiled note that it updates (an entity, plan or record page holds no
  `sources:` claim and takes no citation);
- `decided` and `open`: the Decided count and the count of lines that no cited
  revision holds;
- `outcome`: one sentence that states the result only, for example "This recap has
  3 decided items; 1 is already cited by a live page." It never orders a write and
  never asks for a second record, because a second revision would double the recap;
- `next_step`: compile or explicitly dismiss every Decided item before citing
  this recap from updated compiled notes. It rides only when `capture_sweep._proactive_capture_permitted()`
  allows it, so a recap request alone never orders a page write at `off`.

The count reads the revisions that the record already holds and the cached pages
that cite them, through a resident resolver at the current event checkpoint.
The advisory never proves global corpus membership or schedules a resolver rebuild.
The record checks that resident view before it reads any revision. When the
snapshot is cold, every recap with Decided items reports `open: "unchecked"`, whatever
its `ingested_into` holds, and the outcome says that whether a live page cites the
recap was not checked. The state then depends on the snapshot alone, so a cold
snapshot gives the same block whether a withheld page cites the recap or no page
does. Unknown is never 0. An unchecked or failed count never refuses the recap. A
live revision whose Decided count differs from the count just recorded is reported
as `unchecked` too, never as cited. A recap with no Decided item carries no block.

### The contract text moves from compile-first to record-then-cite

The first design had the agent update pages, link each Decided item as `[[Page]]`
and record last. Linking forces a judgement of which page owns an item, puts a
link that nobody can repair into an immutable Source, and has no server-side check.
The new order records first, so the recap exists to cite. The agent completes every
Decided item before it cites; one citation supplies the structural signal for the
whole revision. The skill, engagement reference, Stop hook ask, `episode_due`
advisory and tool description all say it. The Stop hook ask keeps its condition
that live policy permits capture and stays within its byte ceiling.

### The session-close suite separates capture, citation and fresh recall

`recap_cited` replaces `recap_must_link`. A run passes when it wrote a recap and
every changed seeded page of a compiled note type cites a written recap in
`sources:`. It reports `not_applicable` when no recap was written or no compiled
page changed, and the verdict leaves such a check out. The `--hook-turn` mode sends
the arm's own `EPISODE_ASK`, read from that arm's tree, as one more user turn after
the close turn, because live closes run through the Stop hook and an agent with no
shell never triggers it. Canonical correctness still requires every owed page
update. A recap or citation cannot rescue a missing entity update in a mixed recap.

Generation inputs hold no answer keys. Independently authored keys stay in
scorer-only files. Scoring exports shuffled views for a native blind judge and
imports its structured judgments without starting another model. After capture
ends, a second actor starts with the same isolated
vault and state, a fresh session and only its recall question. It receives no
capture transcript, answer key or changed-page hint. A successful recall requires
the correct answer supported by retrieved canonical content, including excerpts or
units with their own page provenance. A recap or locator alone is insufficient.
The runner preserves the capture snapshot before recall and records
transport failures separately from model failures.

Reports keep canonical correctness, restraint, recap, citation and fresh recall
denominators separate. Paired comparisons use per-case rates, including incomplete
and unavailable stages. A confidence interval that includes zero is no change.

### Spec placement

No canonical spec holds the `episode_memory` record contract. This change writes its
delta against `agent-bootstrap-contract`, which already holds the client episode ask,
the Stop-hook block limits and the scaffold's episode teaching. The family goes to
`attention-queue`, and the effect window and the `acted` count modify the effect
requirement of `command-surface`.

## Risks

- An agent can ignore the close step. The block and the Stop hook repeat it, and
  the real-model task measures how often agents follow it.
- A cold resolver snapshot makes the block report `open: "unchecked"`, and the first
  write into a vault leaves the snapshot stale until it is warm again. The block is
  advisory, so the cost is a weaker hint, not a slower or refused record.
- An episode updated only through entity, plan or record pages stays uncited. A
  mixed recap's citation can hide unfinished work if its compiler skips the
  completion pass. The family stays off until a consumer is implemented and the
  owner approves activation.
- A page can cite a recap and state none of its items. Coverage by citation
  over-counts compiled lines by exactly that gap, and the wording says so.

## Proof plan

- `tests/test_episode_coverage_citation.py` and `tests/test_episode_backlog.py`
  cover the revision chain, the egress twin, the write delta against the full
  rebuild, the default disposition, the absence from attention, and clearance
  without unsupported action attribution.
- `tests/test_episode_compile.py` drives the real `record_episode` tool: the open
  count, the count after a citing page, the cold snapshot, the gated next step and
  the empty recap.
- `tests/test_import_guidance_session_close.py` covers citation, incomplete mixed
  recaps, fresh-client canonical retrieval, native judgments and unavailable recall
  accounting.
- The real-model task runs the close step on the real models with realistic and
  hostile sessions, including a tool-only client, then tests canonical recall in a
  fresh actor session. It adds each failure as a permanent regression case.
