## ADDED Requirements

### Requirement: An ordinary Records query can read during store work

When the service admits the query to an independent snapshot, it SHALL answer without waiting for the store thread. Source proof, selected rows, and returned version SHALL come from the same committed snapshot. The query SHALL keep its existing deadline and release checks. Unavailable authority or capacity keeps the ordinary refusal contract.

#### Scenario: A held store request does not block a filtered page

- **WHEN** the service detects a competing request or maintenance turn on the store thread
- **AND** a reader slot is available
- **AND** an owner with unrestricted access and an empty release policy sends an ordinary filtered query on an admitted non-derived collection
- **AND** the service verifies current authority within the query budget
- **THEN** the service returns the page from a committed snapshot within the Records budget

#### Scenario: A query needs the ordinary store release path

- **WHEN** a query requests one named row, an aggregate, or agent history
- **THEN** the service uses its ordinary store route and release checks

#### Scenario: A row commits during the read

- **WHEN** an append commits after the query establishes its source proof
- **THEN** the query returns the rows and version of the snapshot it already opened
- **AND** a later query can return the appended row
