# Design: serve a named page's Records sums in recall

## Context

`page_citations` already serves `cited_by_count` on the leads of a `recall_context` packet. It reads the derived `item_references` index under the caller's view, through one admission: `_readable_fields` for a collection's link fields, `_visible_targets` for the cited pages and `_filters` for rows that a collection releases one at a time. This change sums the citing rows instead of listing them.

Primary-source basis: the roster row "Context is wrong, bloated, stale, or missing" (Effective context engineering). The source favours bounded, high-signal context that is attached where the model will look for it. That shapes D2, D4 and D5.

## Decisions

**D1. The collection declares the arithmetic.** `subject` names every field that the sums use. The server never guesses a field by its name, as `working_set_state` does for its state and date fields, and it never branches on a state value. The role types are the schema's own closed type tokens.

**D2. The block hangs off the turn slice's named leads.** The candidates are the leads that `_cite_packet_leads` reads, in order, whose evidence holds `exact_alias`: the turn spelled the page's own title or alias. A lead that the turn only resembles could put another person's numbers in the answer. The block sits at the top level of the packet, beside `turn_slice`. The turn slice resolves its leads before task discovery runs, so a deadline spent in discovery still leaves the leads and the block. Method projection and `next_action` stay as they are.

**D3. The gate has two halves.** A pair needs a lead cited through `subject.field`, and a turn that holds a term of the collection's effective claims. The claim check is `collection_claims.matched_claims` over the turn's normalized terms, as activation routing uses. One matched term suffices, unlike routing's two-term minimum (`MIN_CLAIM_COVERAGE`): routing picks a collection from words alone, and here the named page is the second signal. Naming a person alone does not serve the person's tally. A turn about the tally that names nobody serves nothing.

**D4. Admission is the citation reader's, and the proof comes first.** `subject_records` calls `_readable_fields`, `_visible_targets` and a new `page_citations.citing_rows`. `citing_rows` returns the indexed cited-row join with the row release applied. The index shape and the release stay in `page_citations`.
- Before any entry of any state, one probe proves that the caller may read a row that cites the lead. It stops at the first such row. Where a collection releases its rows one at a time, `reference_resolution.rows_released` decides its first 64 index entries.
- A pair without the proof gets no entry, so an `unavailable`, `warming` or `omitted` entry never says that hidden rows cite the page.
- A withheld row is absent from every sum and count.
- A withheld collection, link field or page adds nothing.

The entry therefore never reports `partial`, which would say that hidden rows exist.

**D5. One probe and two statements per pair.** After the proof:
- The grouped aggregate joins `item_references` to the rows. It sums the measure where the stored value is a plain number, counts the null or missing measures and dates, and groups by the state.
- The newest read orders by `order` and stops at `recent` rows.

On typed rows, the aggregate groups by the state's stored cell and decodes one row per group. Equal cells hold equal values, so the decode costs one call per group, not one per row. The unknown counts read the stored cell too. `NativeLayout.cell_sql` names that cell, so the bit layout stays in the layout. Field metadata is read once for both statements. A collection on the legacy typed encoding reads `unavailable`. The block runs in its own read session on the `records` query profile: 1 s of CPU and 2 s of wall clock. Recall sets no request-budget checkpoint, so the session never shrinks to the request's remaining time, and the block can run up to 2 s past a spent request budget. A deadline that passes in an admission or a proof makes the whole block `unavailable`; one that passes inside a proven pair's sums makes that entry `unavailable` (D8).

**D6. The packet budget decides what fits, and nothing is dropped in silence.** The block is charged to `budget.used_chars` in its compact JSON size, before the leads' `cited_by` lists, so citations never starve it. The block holds `entries` and `omitted`.
- The first three pairs carry their numbers. Each later pair is an `omitted` stub: anchor, collection, state and a reason that says to ask about it alone.
- An entry that does not fit is served without `newest`. When that does not fit either, it becomes an `omitted` stub.
- `omitted` counts every pair that the cap or the budget left without its entry, including one with no room for its stub.
- A lead whose `ready` entry carries `newest` keeps `cited_by_count`, and its `cited_by` leaves out that ledger's citations through `subject.field`: the entry's sums and newest rows stand for them. The citation read itself counts those rows and lists none of them (`page_citations.for_hits`, `unlisted`), so the three it lists are other collections' citations whichever id sorts first. An entry without `newest`, from a `recent` of 0 or a room cut, leaves `cited_by` whole.

When not even the block's frame fits, the packet carries no block. A failure before a ledger applies is logged at debug level and leaves the packet unchanged; a failure after is the block's own state or a proven pair's, and a collection's own refusal leaves that collection out (D8).

**D7. Each entry leads with a sentence the agent can repeat.** A `ready` entry's `summary` is built from its own values at render time: the anchor, the sum over every state, each state's sum, the unknown counts and the newest date. The template adds words, never values. The total is arithmetic over every state, not a balance: the collection's method page says which states count toward what is owed. An entry without numbers leads with its `reason`. Only a `ready` entry carries `measure`.

**D8. Once a ledger applies, the block says what it could not read.** A ledger applies when the packet holds a lead whose evidence holds `exact_alias` and the caller may read a collection that declares `subject` on a readable link field and holds a term of the turn. Both facts come from the packet, the vault and the turn. The ledgers are chosen before any page is looked at, as `page_citations` decides its state before it reads a page, so whether the block may carry a state never depends on the page asked about. Only a collection whose current manifest declares `subject` is admitted. That answer is kept by manifest hash, so a collection that is no ledger adds no statement once its manifest has been read. From then on:
- A page index that cannot be read makes the block `unavailable`.
- A read session that fails in an admission or in a proof, by a spent deadline or a cancellation, makes the block `unavailable`, with no entries, so a partial read never stands for the whole. A proof that fails in a later pair drops the entries of the pairs already proven, which keeps one rule for every failure.
- A failure inside a proven pair's sums, in the aggregate or the newest read, makes that entry `unavailable` and gives the block no state: the pair is already proven readable.
- A collection whose own admission fails while the session is live, such as one whose layout is being converted, is left out with a debug log. `_candidate` tells the two apart by calling the session's `check`, which raises again only for a spent or cancelled session. A collection that stays unreadable on every read would otherwise hide the block, or make it `unavailable`, on every turn.
- A ledger whose `subject.field` index is still building makes the block `warming`, whatever it proved, because the index may not yet hold a pair it will hold. A pair that it does prove stays a `warming` entry.

Both states lead with a `reason` and name no page and no collection. One residual stays: a proof runs only for a lead that some row cites in the index, readable or not, so a deadline can land in a proof only for such a lead. The state that it leaves names nothing.

## Load target

The Records load target for a filtered query applies per (anchor, collection): p95 at most 300 ms at 5,000 cited rows and at most 1 s at 100,000. `benchmarks/subject_records/latency.py` measures `subject_records.summaries` on a generated ledger in both encodings. It runs outside the pull-request tier. The recorded run meets the target: served p95 at most 26.06 ms at 5,000 rows and 258.11 ms at 100,000. A served call runs 39 statements on typed rows and 33 on JSON rows; a gated call runs 14 and 11. Those counts do not change with the rows. A collection whose manifest declares no `subject` adds none of them.

## Risks / Trade-offs

- SQLite sums floating-point amounts with its own compensated sum, which can differ in the last digit from the query engine's exact accumulator. Integer amounts are exact, and an integer sum past the 64-bit range reads `unavailable`.
- A number stored outside SQLite's plain range, such as a large decimal, makes the entry `unavailable` rather than inexact.
- `subject` was an unchecked key before this change. A manifest that used it for another purpose now fails to parse, and the error names the key to remove.
