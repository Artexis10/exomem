## ADDED Requirements

### Requirement: File evidence carries a caption and a citation

`save_evidence` SHALL accept `description` together with `files`. The one caption SHALL apply to every file in the call and SHALL be written on each newly stored original's page. A file that was already stored SHALL keep its page unchanged and SHALL report `caption` as `not_recorded`. Each kept file's result row SHALL carry `cite`, a wikilink to the file's page.

#### Scenario: A caption lands with the original

- **WHEN** an agent calls `save_evidence` with one file and a `description`
- **THEN** the original's page holds the description
- **AND** the result row reports `caption` as `recorded` and carries `cite`

#### Scenario: An observation cites the kept file

- **WHEN** an agent writes an observation whose text includes the row's `cite`
- **THEN** the original's page lists that note among its inbound links

#### Scenario: A caption has no page to land on

- **WHEN** a `save_evidence` call with `description` also carries an adoption or `archive="members"`
- **THEN** the call is refused before anything is stored

### Requirement: An evidence upload can save on arrival

`upload_files` SHALL accept `scope`, `category` and an optional `description` on the evidence lane only, and SHALL refuse them on any other lane or without both segments. Such a ticket SHALL record its destination when it is issued, and MAY omit `expected_sha256`. When its send holds the bytes, the server SHALL save them through the same command as `save_evidence`, under the ticket's principal, and SHALL reply with the stored receipt.

#### Scenario: One call and one send keep the file

- **WHEN** an agent calls `upload_files(lane="evidence", scope, category)` and the file is sent with the returned ticket
- **THEN** the file is stored under `Evidence/<scope>/<category>/`
- **AND** the send's reply carries `stored_path`, `hash` and `cite`, and no hold remains

#### Scenario: A send cannot redirect the save

- **WHEN** the send carries a `scope` form field
- **THEN** the send is refused and nothing is stored

### Requirement: A failed save on arrival keeps the hold

When the save on arrival fails, the reply SHALL carry the held handle and the refusal, and the hold SHALL stay redeemable by `save_evidence` until it expires.

#### Scenario: The agent can still keep the file

- **WHEN** the save on arrival is refused
- **THEN** the send's reply names the refusal and carries the held `file`
- **AND** `save_evidence` with that `file` keeps it before the hold expires

### Requirement: A person can send one file from a page

On a personal server, the reply to a destination ticket SHALL include a `page` step whose URL opens `/upload/send` with the ticket in the URL fragment. The page SHALL tell the person to cover any card number, security code or password before sending. A Cloud cell SHALL NOT offer the page step.

#### Scenario: A client with no shell keeps the file

- **WHEN** an agent with no shell receives a destination ticket and gives the person the page link
- **THEN** the person picks the file on the page, and the page shows the stored path and hash

### Requirement: The send page holds no secret

`/upload/send` SHALL return the same static bytes for every ticket and SHALL contain no ticket. It SHALL be served with a content security policy that allows only its own script and same-origin connections, with `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.

#### Scenario: The ticket stays out of the page and its requests

- **WHEN** a person opens the page link
- **THEN** the served page holds no ticket, and the ticket travels only in the fragment and the send's authorization header

### Requirement: Kept text never holds a card number

Every capture that keeps caller text, namely a text original, a caption and an extracted text or transcription, SHALL remove each payment card number before the text is hashed or written. A card number has 13 to 19 digits, optionally grouped by single spaces or hyphens. Its issuer prefix and length belong to a card scheme, and its Luhn check digit holds. The kept text SHALL show where the number was, keeping its last four digits.

#### Scenario: A card number in a text original is removed

- **WHEN** an agent saves a text original and a caption that each contain a card number
- **THEN** neither the stored text nor the page holds the number
- **AND** the result's warnings say a card number was removed

#### Scenario: Identifiers are kept

- **WHEN** a kept text contains an IMEI and a SHA-256 digest
- **THEN** both are stored unchanged

### Requirement: Kept text never holds a labelled credential

The same captures SHALL remove each credential shape that the egress scrubber names explicitly, without the scrubber's high-entropy heuristic. The result SHALL carry a warning that names the field and what was removed.

#### Scenario: A labelled key in a caption is removed

- **WHEN** a caption contains a labelled API key
- **THEN** the page holds a removal marker in its place, and the result's warnings say so
