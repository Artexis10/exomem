## Purpose

Let the owner see whether a method that activation served was then used, from evidence the service already keeps.

## ADDED Requirements

### Requirement: The owner can see whether a served method was used

The owner served-context command SHALL report, for each method that retained served-context rows served, how often it was served and what use evidence followed each serving.
Use evidence SHALL be computed when the owner reads it, from the newest `MAX_JOINED_ROWS` (2000) retained rows that served a method and the existing local read and write logs. The report SHALL say when that cap applied. Activation SHALL do no extra work for it.

#### Scenario: The readout caps its join

- **WHEN** more than 2000 retained rows served a method
- **THEN** the report joins the newest 2000 and says that the cap applied

### Requirement: Records writes reach the write log

A Records item write that commits within its `record_memory` call (an item write or a bulk upsert) SHALL be logged in the write log under its collection's manifest path, as a page write is logged under its page.
An import starts a job whose batches commit later, outside the call, so the write log SHALL NOT attribute them, and the readout's meaning SHALL say so.

#### Scenario: A committed bulk upsert is logged

- **WHEN** a bulk upsert commits rows into a collection
- **THEN** the write log holds one write under that collection's manifest path

### Requirement: Use evidence is observed, not observed or unknown

For one serving, `opened` SHALL be observed when the read log holds a read of the method page, or for a skill a file in its folder, after the serving and within `METHOD_USE_WINDOW_MINUTES` (120).
`followed` SHALL be observed when the write log holds a successful write in that window to a page the method links to, into a collection whose manifest it links to, or into the collection a manifest method is.

#### Scenario: A collection method followed by a write into itself

- **WHEN** a selected collection is served and an item write into that collection succeeds within the window
- **THEN** the report counts that serving with `followed: observed`

### Requirement: Use evidence never claims an absence it cannot see

Evidence SHALL be `unknown` when the log is disabled, absent or rotated past the window start, or when the window has not closed and holds no matching event. A method that links to no page or collection SHALL report `followed` as unknown.
`opened` and `followed` SHALL be reported separately, each as observed, not observed or unknown. The report SHALL say the logs belong to one host and service, not one conversation, so concurrent work by the same principal can produce the evidence.

#### Scenario: A served skill followed by a write to its destination

- **WHEN** a skill that links to a collection is served and a write to that collection succeeds 10 minutes later
- **THEN** the report counts one serving of the skill with `followed: observed`

#### Scenario: A served method with no later read or write

- **WHEN** a method that links to a page is served and the logs hold no later read of it and no write to the linked page within the window
- **THEN** the report counts one serving with `opened: not_observed` and `followed: not_observed`

#### Scenario: Disabled logging reads as unknown

- **WHEN** the read and write logs are disabled for the service
- **THEN** every serving reports `opened: unknown` and `followed: unknown`, never `not_observed`
