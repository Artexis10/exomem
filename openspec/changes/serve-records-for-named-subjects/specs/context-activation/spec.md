# context-activation

## ADDED Requirements

### Requirement: A named page's ledger rides the packet

A served `recall_context` packet SHALL carry a top-level `subject_records` block, outside `task_context`, when a ledger applies and the block holds an entry or its own state; otherwise it SHALL carry no such key. A ledger applies when a lead's evidence holds `exact_alias` and a readable collection declares `subject` and holds a turn term in its effective `claims`. An entry pairs that lead with a ledger citing it through `subject.field`. The block SHALL NOT change method items or `next_action`.

#### Scenario: A named person's ledger is served

- **WHEN** a ledger declares `subject`, its rows cite two people, and the turn names one of them and a claim term of the ledger
- **THEN** `subject_records` holds one `ready` entry for that person, and the other person's rows count nowhere in it

#### Scenario: A turn that does not touch the ledger gets no block

- **WHEN** the turn names the person but holds no claim term of the ledger
- **THEN** the packet carries no `subject_records` key

#### Scenario: A page no row cites gets no block

- **WHEN** the turn holds the ledger's claim term and names a page that no row of the ledger cites
- **THEN** the packet carries no `subject_records` key

#### Scenario: A lead the turn does not name gets no block

- **WHEN** a lead's evidence does not hold `exact_alias`
- **THEN** no entry is built for that lead

#### Scenario: A starved task context keeps the ledger

- **WHEN** the request's deadline runs out after the turn slice resolves its leads and before the task context is built
- **THEN** the packet still carries the `ready` entry

### Requirement: A ready entry reports its sums and unknowns honestly

A `ready` entry SHALL lead with `summary`, a sentence built at render time from its own values. It SHALL carry `anchor`, `collection`, `measure`, `state`, `by_state` when `state` is declared, `total`, `unknown`, up to `recent` `newest` rows, and the `capture.method` page when it resolves for the caller. A row with no measure SHALL count in `rows` and `unknown.measure`, never in a sum. A sum SHALL keep its values' most decimal places, and with no known measure SHALL be null.

#### Scenario: An unknown amount is not a zero

- **WHEN** one cited row has no amount and another has no date
- **THEN** the entry counts both rows, reports `unknown.measure` 1 and `unknown.order` 1, and the missing amount adds nothing to any sum

#### Scenario: Decimal amounts sum without float noise

- **WHEN** a person's settled rows hold 0.1, 0.2 and -4.15
- **THEN** the settled sum is -3.85 in `by_state` and in `summary`

#### Scenario: The summary repeats the values

- **WHEN** a `ready` entry is served
- **THEN** its first key is `summary`, and the sentence names the anchor, each state's sum, the unknown counts and the newest date

### Requirement: The hook shows every ledger entry

The Claude Code hook SHALL render the `subject_records` block in every layout that it renders a resolved packet in: the block's state and reason, and one line per entry with its `summary` and `measure.description`, or its page, collection and `reason`. Lines that its ceiling cuts, and pairs without an entry, SHALL be counted on a last line. When no whole line fits, one line SHALL count the entries.

#### Scenario: A hook-served turn sees the tally and what its sign means

- **WHEN** a hook-delivered packet carries a `ready` entry and an `omitted` stub
- **THEN** the injected block holds the entry's `summary` with its `measure.description`, and the stub's `reason`, each with the collection's ref

#### Scenario: A ceiling too small for any tally line

- **WHEN** the hook's ceiling holds no whole tally line
- **THEN** the injected block still holds one line that counts the entries

### Requirement: A measure's description is bounded

A `ready` entry's `measure.description` SHALL be the measure field's `description`, cut to 200 characters and ending in `…` when longer, so a long description cannot crowd an entry's sums out of the packet.

#### Scenario: A long description is cut

- **WHEN** the measure field's `description` runs to thousands of characters
- **THEN** the entry stays `ready` with its sums, and its `measure.description` holds at most 200 characters ending in `…`

### Requirement: A total is arithmetic, not a balance

`total.sum` SHALL be the arithmetic sum of the measure over every state, and `summary` SHALL NOT call it a balance. Which states count toward what is owed is for the collection's method page to say.

#### Scenario: A provisional row counts in the total

- **WHEN** a person's rows hold 55 settled and 25 provisional
- **THEN** `total.sum` is 80, and `by_state` shows the two states apart

### Requirement: An entry without numbers says why

A `warming`, `unavailable` or `omitted` entry SHALL lead with `reason`, a sentence that the agent can repeat, and SHALL carry `anchor`, `collection` and `state`. It SHALL carry no `measure`, no sum and no count. A link index still building for `subject.field` SHALL make the entry `warming`.

#### Scenario: A building index says so

- **WHEN** the ledger's link index for `subject.field` is still building
- **THEN** the entry is `warming` with a reason and carries no sum and no `measure`

### Requirement: A ledger entry admits rows as lead citations do

The block SHALL use the admission of the packet's lead citations. Before it says anything of a pair, the server SHALL prove that the caller may read a row that cites the lead through `subject.field`. A pair without that proof SHALL get no entry of any state. Only then SHALL a field the sums need that the caller may not read make the entry `unavailable`. A row that the caller may not read SHALL be absent from every sum and count.

#### Scenario: A withheld row leaves no trace

- **WHEN** a purpose-narrowed rule withholds one cited row from the caller
- **THEN** the entry's sums and counts are those of the rows that the caller may read, and the entry stays `ready`

#### Scenario: A withheld amount field gives no total

- **WHEN** the caller may read the ledger's rows but not its amount field
- **THEN** the entry is `unavailable` with a reason and carries no sum

#### Scenario: A withheld amount and withheld rows say nothing

- **WHEN** the caller may read neither the amount field nor any row that cites the person
- **THEN** the packet carries no `subject_records` key and does not name the ledger

### Requirement: Nothing a turn names is dropped in silence

The block SHALL hold `entries` and `omitted`, and SHALL count in `budget.used_chars` before the leads' `cited_by` lists. At most three entries SHALL carry numbers; each later pair SHALL be an `omitted` stub. An entry that does not fit SHALL drop `newest`, then become an `omitted` stub. `omitted` SHALL count the pairs that the cap or budget left without an entry, stubbed or not. A lead whose `ready` entry has `newest` SHALL fill `cited_by` from fields other than that ledger's `subject.field`.

#### Scenario: A small packet keeps the sums

- **WHEN** the full entry does not fit the packet's remaining characters but the entry without `newest` does
- **THEN** the packet carries the entry without `newest`, and `budget.used_chars` counts it

#### Scenario: A nearly full packet names what it left out

- **WHEN** only a stub fits the packet's remaining characters
- **THEN** `entries` holds an `omitted` stub for the pair, `omitted` is 1, and the stub carries no number

#### Scenario: A fourth person is named as left out

- **WHEN** a turn's leads are cited by four people's rows of one ledger
- **THEN** the first three entries carry their numbers and the fourth is an `omitted` stub

#### Scenario: The newest rows stand for their ledger's citations alone

- **WHEN** a lead's `ready` entry carries `newest`, a row of another collection also cites the lead, and the ledger's collection id sorts before or after the other's
- **THEN** the lead keeps `cited_by_count`, and its `cited_by` holds the other collection's citation and none of the ledger's

#### Scenario: An entry without newest rows leaves the citations whole

- **WHEN** the ledger declares `recent` 0, so its `ready` entry carries no `newest`
- **THEN** the lead's `cited_by` keeps the ledger's citations

### Requirement: A block that cannot be read says so

Once a ledger applies, the block SHALL say what it could not read. A read session spent or cancelled in an admission or a proof and a page index that cannot be read SHALL each make the block `unavailable`, led by a `reason`, with no entries and `omitted` 0. A ledger whose `subject.field` index is still building SHALL make the block `warming`, beside the entries other ledgers prove. Neither state SHALL name a page, and whether a ledger applies SHALL NOT depend on the page asked about.

#### Scenario: A deadline spent proving a pair makes the block unavailable

- **WHEN** the read session's deadline passes during a pair's proof
- **THEN** `subject_records` leads with a `reason`, is `unavailable`, holds no entries and has `omitted` 0

#### Scenario: A deadline spent admitting a ledger makes the block unavailable

- **WHEN** one ledger applies and the read session's deadline passes while a second ledger is admitted
- **THEN** `subject_records` is `unavailable` and holds no entries, so the second ledger is not left out in silence

#### Scenario: A page index that cannot be read reads alike for every page

- **WHEN** the page index cannot be read, and the turn names a page that the ledger's rows cite or one that no row cites
- **THEN** both packets carry the same `unavailable` block, which names neither page

#### Scenario: A freshly declared ledger says warming

- **WHEN** a ledger is declared and its index of `subject.field` is not built yet
- **THEN** `subject_records` leads with a `reason`, is `warming`, holds no entries and names neither the page nor the ledger

#### Scenario: A turn no ledger applies to hears nothing of a failure

- **WHEN** the page index cannot be read, and the turn holds no claim term of any ledger
- **THEN** the packet carries no `subject_records` key

### Requirement: A collection that refuses its own admission costs the other ledgers nothing

A collection whose own admission fails while the read session is live, such as one whose layout is being converted, SHALL add no entry and SHALL NOT give the block a state. The ledgers admitted beside it SHALL be served as if it were absent, whichever collection's id sorts first.

#### Scenario: A collection mid-conversion leaves the ledger served

- **WHEN** a collection that declares `subject` has no published layout, and its id sorts before or after the ledger's
- **THEN** the ledger's entry is `ready` with its sums, and the block carries no state of its own

### Requirement: A pair whose sums cannot be read says so in its entry

A failure inside a proven pair's grouped aggregate or newest read SHALL make that pair's entry `unavailable`, led by a `reason` and carrying no number. The failure SHALL NOT by itself give the block a state, because the pair was already proven readable.

#### Scenario: A sum past the integer range makes its entry unavailable

- **WHEN** a person's rows hold an amount whose sum passes the integer range
- **THEN** the entry is `unavailable` with a reason and no total, and the block carries no state of its own

### Requirement: A ledger block is bounded in statements and time

Each pair SHALL cost one existence probe that stops at the first readable row, and two statements over its cited rows: a grouped aggregate and a read bounded by `recent`. No statement SHALL run per row. The block SHALL read in its own records-profile session of 1 s of CPU and 2 s of wall clock. Recall sets no request-budget checkpoint, so the block can run up to 2 s past a spent budget. Each pair SHALL meet the Records filtered-query target: p95 at most 300 ms at 5,000 rows and 1 s at 100,000.

#### Scenario: The statements do not grow with the rows

- **WHEN** a person is cited by four rows, and then by forty-four
- **THEN** the entry costs the same two statements over its cited rows both times

#### Scenario: A collection that is no ledger adds no statement

- **WHEN** the vault gains a collection with a link field whose manifest declares no `subject`
- **THEN** once its manifest has been read, a call runs as many statements as before

#### Scenario: A whole call runs a fixed number of statements

- **WHEN** the benchmark ledger serves one pair on typed rows, at 5,000 and at 100,000 cited rows
- **THEN** a served call runs 39 statements and a gated call 14, both times; on JSON rows the counts are 33 and 11
