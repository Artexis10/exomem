## ADDED Requirements

### Requirement: An Import Apply Ends Its Call For A Waiting Interactive Write

`adoption_studio(action="apply")` and `import_memory` apply SHALL end their call between chunks when an interactive writer waits for the vault mutation boundary and the call has committed at least one chunk. The reply SHALL carry `progress`, the `continuation` of the next chunk and `stop_reason: interactive_write_waiting`. The call SHALL NOT release the boundary in the middle of a chunk or of a call, and a run SHALL NOT take a lease. A call that has committed no chunk SHALL commit its first chunk before it checks, so every call progresses. A call that stops for another reason SHALL NOT carry this `stop_reason`. A call passing the returned cursor SHALL commit only the remaining items, each once, as for a request-budget stop.

#### Scenario: An agent write arrives during an apply

- **WHEN** an apply runs over several chunks
- **AND** an interactive write, from the agent's own session or from the CLI, waits for the boundary while a chunk runs
- **THEN** the write commits once and is not refused `MUTATION_BUSY`
- **AND** the apply call returns the items committed so far, a `continuation` and `stop_reason: interactive_write_waiting`

#### Scenario: The next call finishes the import

- **WHEN** a call that stopped for a waiting write is followed by calls that pass each returned cursor
- **THEN** every planned item is applied exactly once
- **AND** the run becomes `applied` when every item has an outcome

#### Scenario: No writer waits

- **WHEN** no interactive writer waits during an apply
- **THEN** the call runs until its request budget or the end of the plan, as before
- **AND** the reply carries no `stop_reason`

### Requirement: Every Import Step Yields For A Waiting Interactive Write And Stops Starting Work Before It Is Overdue

`plan`, `finish` and each step of a `file` call SHALL end their call at the next item or step boundary when an interactive writer waits for the vault boundary and the call has handled one item or step. Each SHALL stop starting items once the room left to its hold under the overdue limit cannot cover the delivery reserve, whatever the request budget. A call that ends for a waiting writer SHALL carry `stop_reason: interactive_write_waiting`; the next call SHALL resume without repeating an item.

#### Scenario: An agent write arrives during a slow plan

- **WHEN** a `plan` call for a page runs over many items
- **AND** an interactive write, from the agent's own session or from the CLI, waits for the boundary while an item is planned
- **THEN** the write commits once and is not refused `MUTATION_BUSY`
- **AND** the plan call returns the progress so far, a `continuation` and `stop_reason: interactive_write_waiting`

#### Scenario: A second file call arrives during a slow plan

- **WHEN** a `file` call runs the `plan` step of a page and a second `file` call waits for the boundary
- **THEN** the second call is served and is not refused `MUTATION_BUSY`
- **AND** the first call returns `filing: working` with its continuation, and later calls plan each remaining item once

#### Scenario: A file call meets a waiting write between steps

- **WHEN** a `file` call has run one step and an interactive writer waits
- **THEN** the call returns `filing: working` before it starts the next step
- **AND** the next call carries on from the saved phase

#### Scenario: A later step reads no Source page without room

- **WHEN** a `plan` step that follows another step of its call has to read the earlier imports of a vault with many Sources that the process has not read
- **AND** an interactive writer waits, or the hold nears the overdue limit
- **THEN** the step ends before it plans an item, with a `continuation`, and the writer commits
- **AND** the next call carries on from the pages already read, and the plan covers every item once

#### Scenario: The first step of a call reads a floor of Source pages

- **WHEN** a `plan` or `apply` that opens its call has to read the Sources in full and its room is already spent
- **THEN** it reads 2,000 pages before it asks whether to stop, and then ends with a `continuation`
- **AND** each call moves that read on, so a call with no room still progresses

#### Scenario: An apply whose Sources seal broke ends its full read

- **WHEN** an `apply` has to read every Source page because a Source was filed after its plan
- **AND** an interactive writer waits during that read
- **THEN** the call ends before it files a chunk, with a `continuation` and `stop_reason: interactive_write_waiting`
- **AND** the next call carries on from the pages already read

#### Scenario: A finish call yields and resumes

- **WHEN** a `finish` call has removed some staged copies and an interactive writer waits, or its budget cannot cover the delivery reserve
- **THEN** the call returns with the run still in its phase, reports none of the copies it did not reach as unverified, and carries `stop_reason: interactive_write_waiting` or `call_budget_spent`
- **AND** a later `finish` removes the remaining copies, each once, and completes the run
- **AND** the recall check, the coverage and the manifest run only in a call that removed no copy

#### Scenario: Work that no step can split runs to its end

- **WHEN** a call is inside a piece of work that cannot stop part-way, such as the first summary of a stage
- **THEN** it runs to its end and the call ends after it, so the hold can pass the overdue limit by that piece's length
- **AND** the design lists each such piece with its cost

#### Scenario: No writer waits and no budget is bound

- **WHEN** no interactive writer waits during an import step and the call binds no request budget
- **THEN** the call stops starting items before its hold reaches the long-holder limit of the mutation lock
- **AND** the reply carries no `stop_reason: interactive_write_waiting`

#### Scenario: The cell's filer lets a waiting writer in before it calls again

- **WHEN** a call of the cell's import filer ends and an interactive writer, in this process or another, waits for the boundary
- **THEN** the filer's next call does not start until that writer has committed, within the foreground-priority cap
- **AND** the filer's own wait for the boundary does not count as an interactive writer's, so an agent's `file` call or the owner's write does not end early for it

### Requirement: Each Call Of An Import Step Moves Its Page On

An item loop of `plan`, of the removal of staged copies in `finish`, and of the reading of an app export SHALL work for a turn of at least one second before a waiting interactive writer ends its call, unless the call's budget is spent. Two callers that file one stage SHALL each move its page on in every call, and the stage SHALL reach its end.

#### Scenario: An agent and the cell's filer file one stage

- **WHEN** an agent's `file` calls and the cell's filer's `file` calls plan one page of many items in turn
- **THEN** each call plans at least a turn of items
- **AND** the page is planned in a bounded number of calls, each item once

#### Scenario: Two agents file one stage

- **WHEN** two agents call `file` on one stage and each call finds the other waiting
- **THEN** each call plans at least a turn of items and the page is planned in a bounded number of calls

### Requirement: A Finish That Removed Staged Copies Leaves Its Checks To The Next Call

`finish` SHALL run its recall check, coverage, residual and manifest only in a call that removed no staged copy. A call that removed copies SHALL end with `stop_reason: finish_check_next_call` and the run in its phase, and the next call SHALL run the checks.

#### Scenario: The removals and the checks take two calls

- **WHEN** a `finish` call removes the staged copies of a page
- **THEN** it returns `stop_reason: finish_check_next_call` and no recall check
- **AND** the next `finish` call runs the checks with the hold's whole room and completes the run

### Requirement: A Finish Starts A Search Only Within Its Room

`finish` SHALL start the recall check and each search of its recall sample only while its budget covers the delivery reserve. It SHALL end the sample at the first search that the index cannot answer yet, and it SHALL report a search that the index cannot answer yet, or that it did not reach, as `pending`, never as `failed`.

#### Scenario: The search index is warming

- **WHEN** the search index answers a `finish` search with its warming refusal
- **THEN** the recall check is `pending` and the sample starts no further search
- **AND** the searches the sample did not reach count as `pending`, not as missed or failed

### Requirement: The Cell's Filer Parks A Stage That Moves Nothing

The cell's filer SHALL stop calling `file` on a stage once `adoption_run.FILING_STALL_CALLS` `working` replies in a row have repeated the counts and the page position of the reply before them. It SHALL park the stage with the stop `IMPORT_FILING_STALLED` and a reason that names the number of calls, and a later `file` call SHALL list the stage again.

#### Scenario: A stage cannot move

- **WHEN** the filer's `file` calls on a stage return `working` with the same counts and page position, for the stall limit
- **THEN** the stage is parked with `IMPORT_FILING_STALLED` and the filer makes no further call on it
- **AND** an agent's `file` call lists it again with its full count of calls

### Requirement: A File Call Reads An App Export As A Step Of Its Own

When a stage holds app exports, a `file` call SHALL read their conversations as a step that ends the call. It SHALL run a scan step (`start`, `rescan`) only as the first step of a call. The first call of a stage SHALL find the exports before its scan and SHALL NOT read their items.

#### Scenario: A call that read an export ends before it scans

- **WHEN** a `file` call has read conversations of an export of the stage
- **THEN** the call returns `filing: working` and no scan has run in it
- **AND** each next call reads more of the export until it has been read to its end, and the call after the last of them runs the scan as its first step

#### Scenario: The reading ends for a waiting write

- **WHEN** an interactive writer waits while a `file` call reads an export
- **THEN** the call ends after a turn of conversations and the writer commits
- **AND** the next call goes on from the conversations already read

### Requirement: A Source Page Refused For Now Ends A Read Of The Earlier Imports

A `plan` or `apply` read of the earlier imports that meets a Source page which the filesystem refuses for now (`CAPABILITY_UNAVAILABLE`, `IDENTITY_CHANGED`) SHALL end as an incomplete read and SHALL NOT take the page as one with no imports. The next call SHALL read the page again.

#### Scenario: A page is refused during a plan

- **WHEN** a `plan` meets a Source page that the held filesystem refuses for now
- **THEN** the read ends incomplete and the plan files no item against a partial index
- **AND** a later call reads the page again and plans from the whole index
