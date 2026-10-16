## ADDED Requirements

### Requirement: Restricted direct-read refusals complete at a fixed floor

A restricted caller is a caller from whom the release plane could withhold anything, RAW included: `egress.caller_restricted` is true for it. This covers non-owner audiences, CF-Access scopes, and owners under a connector ceiling. For a restricted caller, every refusal of a direct read SHALL complete no sooner than the repository floor F after the dispatcher received the request. The direct reads are `read_memory` and its `get` and `fetch` routes, by path, by memory reference and by unit reference. F SHALL be one repository constant of 55 ms with no environment or caller override, and a change to F SHALL be a change to this requirement. Behind the MCP and REST surfaces, the surface SHALL hold the response, an error or a result, until F has passed, after its own handling of the refusal, its logging included. That wait SHALL run on the event loop and SHALL hold a worker thread for at most its last 3 ms.

The floor SHALL apply to refusals only. A successful read, an L1 to L4 notice included, SHALL return as soon as it is ready. The refusal of a caller that is not restricted SHALL return as soon as it is ready. The dispatcher SHALL ask whether the caller is restricted only after the read refused. A read by path SHALL ask it once before it renders the page, and for an owner in file mode that question SHALL load no policy.

A refusal whose own work runs past F SHALL complete late with its original refusal. The system SHALL NOT refuse, change or replace a direct read because of its completion time. The system SHALL record each padded refusal, its work time and whether it overran F, in an owner-only call span that carries no path, title or reference. The MCP surface SHALL log a refusal that carries a stable code as one line without its traceback, and SHALL keep the traceback of an exception without a code.

For a restricted caller, a read by path SHALL decide whether the page is withheld entirely before the optional work that grows with the vault or with the page's history: links, history, the query log and the heat signal. That early refusal SHALL use the same release decision as the release gate, SHALL return the same missing-path response, and SHALL leave the same disclosure receipt. An owner without a connector ceiling SHALL keep the existing order.

The work of a refusal SHALL NOT grow with the number of files in the vault, except for one directory check per directory, the first refusal after a file was added, removed or renamed, and the first withheld refusal in a process, which builds the memory-reference index. That refusal MAY complete after F.

This requirement covers pages up to the governed capacity of 1 MiB per item. The refusal of a larger page MAY complete after F.

#### Scenario: A withheld path and a missing path refuse alike, at the floor

- **WHEN** a restricted caller reads a withheld page by path, and then reads the same path after the page is deleted
- **THEN** both reads refuse with byte-identical responses
- **AND** each refusal completes no sooner than F after the dispatcher received it

#### Scenario: A hidden-only reference and an absent reference refuse alike, at the floor

- **WHEN** a restricted caller reads a memory reference that only a withheld page holds, and then reads the same reference after the page is deleted
- **THEN** both reads refuse with byte-identical responses
- **AND** each refusal completes no sooner than F after the dispatcher received it

#### Scenario: A unit read of a withheld page refuses at the floor

- **WHEN** a restricted caller reads a unit reference on a withheld page, and then reads the same unit reference after the page is deleted
- **THEN** both reads refuse with byte-identical responses
- **AND** each refusal completes no sooner than F after the dispatcher received it

#### Scenario: A notice returns without the floor

- **WHEN** a restricted caller reads a page that the release decision serves at L1 to L4
- **THEN** the read returns the notice, not a missing-page refusal
- **AND** the read returns as soon as it is ready

#### Scenario: An owner refusal is not padded

- **WHEN** an owner without a connector ceiling reads a missing path
- **THEN** the refusal returns as soon as it is ready

#### Scenario: An overrun completes late and is recorded for the owner

- **WHEN** the work of a restricted caller's refusal runs past F
- **THEN** the read completes with its original refusal, after F, and is not refused because of time
- **AND** the owner-only call span records the overrun with no path, title or reference

#### Scenario: A withheld page refuses before the optional work

- **WHEN** a restricted caller reads a withheld page by path with links and history
- **THEN** the read refuses before it reads the edit log or the inbound links, and writes no query-log row for the page
- **AND** the disclosure receipt equals the receipt that the release gate records for that page

#### Scenario: Through the MCP surface, a withheld path and a missing path take the same time

- **WHEN** a restricted caller reads a withheld page by path through the MCP surface, and then reads a missing path in the same folder, in turn several times
- **THEN** the two refusals read alike, apart from the path that each names
- **AND** each completes no sooner than F after the dispatcher received it, and their median completion times do not differ beyond timing noise

#### Scenario: A file added after an earlier refusal is still withheld from error text

- **WHEN** an error for a restricted caller names a withheld page that was created after an earlier error
- **THEN** the error text replaces the reference to that page
