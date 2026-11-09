## ADDED Requirements

### Requirement: Fixity runs on its own budgeted background thread

Exomem SHALL run fixity on a daemon thread of its own, started with the other background components of the server runtime. The thread SHALL run by default, whether or not media extraction is enabled. `EXOMEM_DISABLE_ORIGINAL_FIXITY=1` SHALL stop it, and status SHALL then report `off`. A start, walk or pass failure SHALL be logged and SHALL NOT deny the core service. A failed binding walk SHALL NOT stop the pass, which runs on the table it has. The pass interval, the full-cycle length, the read-rate cap and the binding-walk interval SHALL be configuration, with measured defaults. The re-hash of unchanged originals SHALL stay within the pass's byte budget. Every hash that a pass makes, and every read of a restore candidate from the vault or a local import journal, SHALL stay under the read-rate cap. A pass SHALL NOT read or download an evicted original.

#### Scenario: Media extraction is disabled

- **WHEN** the server starts with `EXOMEM_DISABLE_MEDIA_EXTRACTION` set
- **THEN** the fixity thread starts and its passes run

#### Scenario: The operator turns fixity off

- **WHEN** the server starts with `EXOMEM_DISABLE_ORIGINAL_FIXITY=1`
- **THEN** no fixity thread starts and the status block reports `off`

#### Scenario: A pass stays within its budget

- **WHEN** a pass runs over unchanged originals whose total size exceeds its byte budget, and one original is evicted
- **THEN** the pass hashes no more bytes than the budget allows
- **AND** the pass requests no download of the evicted original

### Requirement: Fixity keeps its own state and finds originals through their bindings

Fixity SHALL keep its state in its own table, keyed by vault path and payload SHA-256. A row SHALL hold the file identity, the last verification time and one state: `unverified`, `verified`, `restored`, `missing`, `corrupt_no_copy`, `evicted` or `unmatched`. A finding about changed bytes SHALL carry one cause: `edited`, `corrupted`, or `unknown` when no identity was recorded before the change. These are closed protocol values that this specification fixes. Fixity SHALL find originals through the existing sidecar-binding, member-manifest and Source-page walks, and SHALL NOT depend on a derived lexical index. A Source page holds a text original when its parsed front matter names `original_sha256` and binds no file. A note that is not a plain file, such as a link, SHALL bind nothing and SHALL NOT stop the walk. A pass that cannot read a binding SHALL keep the row for the next pass. The binding walk SHALL run at start and then at its configured interval, once a day by default. The expected hash SHALL always come from the binding.

#### Scenario: The lexical index is absent

- **WHEN** the derived lexical index is missing and a pass runs
- **THEN** the pass finds every bound original and every pool blob

#### Scenario: A note is a link

- **WHEN** the vault holds a symbolic link to a note and a hard-linked note
- **THEN** the binding walk and the pass both complete
- **AND** every other bound original is verified

#### Scenario: A binding cannot be read during a pass

- **WHEN** an original is absent and its binding cannot be read in that pass
- **THEN** the pass keeps the row and counts it as rescheduled

#### Scenario: The fixity table is lost

- **WHEN** the fixity table is deleted and the server restarts
- **THEN** the binding walk rebuilds the table from the bindings, with every row `unverified`
- **AND** each later hash is compared with the hash its binding names

### Requirement: Each pass checks presence and identity before the budgeted re-hash

Each pass SHALL first stat every original in the fixity table. It SHALL record an absent original as `missing`. It SHALL hash an original whose identity differs from the recorded identity in the same pass. It SHALL then re-hash originals within its budget, `unverified` rows first and then the oldest verification first.

#### Scenario: An original changes between passes

- **WHEN** an original's modification time changes after its last verification
- **THEN** the next pass hashes that original, whatever its place in the re-hash order

#### Scenario: Unchanged originals are re-hashed oldest first

- **WHEN** a pass has budget left after the presence check
- **THEN** it re-hashes the original with the oldest verification before any newer one

### Requirement: A mismatch restores only from a copy whose hash matches the binding

An original whose bytes do not hash to its binding's SHA-256 is damaged. Fixity SHALL restore a damaged original only from a copy whose bytes hash to that SHA-256. Candidate copies SHALL be the media-store version, a pool blob and another stored original. On a local install, the outside file that a local import journal recorded SHALL also be a candidate. Before it moves anything, fixity SHALL re-check under the vault mutation guard that the file and its binding are unchanged since the hash. It SHALL move the changed bytes to Trash with their protection, and SHALL check the file once more just before the replace. When a verified copy cannot be put in place, fixity SHALL record `corrupt_no_copy` with that copy and the failure code in the route. It SHALL keep the last verification time, and the next pass SHALL try again. It SHALL NOT record the changed bytes' hash as the expected hash. With no matching copy, it SHALL change nothing and record `corrupt_no_copy`. When the changed bytes are in Trash and only the replace failed, a later attempt SHALL NOT put a second copy of the same bytes in Trash.

#### Scenario: A matching copy exists

- **WHEN** a stored original's bytes change while its identity stays the recorded one, and a pool blob holds its recorded bytes
- **THEN** the original again hashes to its recorded SHA-256
- **AND** the corrupted bytes are recoverable from Trash
- **AND** the row is `restored` with the cause `corrupted`

#### Scenario: No matching copy exists

- **WHEN** a stored original is corrupted and no copy hashes to its recorded SHA-256
- **THEN** the file is byte-identical to its state before the pass
- **AND** the row is `corrupt_no_copy`, and the binding still names the original hash

#### Scenario: A verified copy cannot be put in place

- **WHEN** a copy verifies but the restore cannot move the changed bytes to Trash
- **THEN** the original is unchanged and the row is `corrupt_no_copy`, with the copy and a failure code in the route
- **AND** the next pass restores the original once the cause is gone

#### Scenario: An outside program edits an original

- **WHEN** an outside program rewrites a stored original and a matching copy exists
- **THEN** fixity restores the original and keeps the edited bytes in Trash
- **AND** the finding carries the cause `edited`

#### Scenario: A governed write rebinds the path during a pass

- **WHEN** a binding starts to name another hash after fixity hashed its file
- **THEN** fixity moves nothing and restores nothing for the earlier hash

### Requirement: A text original is verified and never repaired automatically

A text original is the text that a Source page fences, bound by the page's `original_sha256`. Fixity SHALL hash that text's UTF-8 bytes on each verification. On a mismatch it SHALL change nothing and record `corrupt_no_copy` with the cause and a route, because a repair is a Source page write. A text original whose fenced text has never matched its hash, such as a page from a renderer that trimmed the fenced text, SHALL be `unmatched`: it is counted in status and is neither an attention finding nor in the readiness count, and it becomes `verified` when its text matches. A change outside the fence, such as a line added after it, SHALL leave the page a text original. In store mode, when the store's own page still fences text that hashes to `original_sha256`, the route SHALL name that page first.

#### Scenario: The fenced text of a Source page changes

- **WHEN** an outside program edits the fenced text of an imported Source page
- **THEN** the page is unchanged by the pass
- **AND** the row is `corrupt_no_copy` with the cause `edited`

#### Scenario: The fenced text has never matched

- **WHEN** a Source page fences text that has never hashed to its `original_sha256`
- **THEN** the row is `unmatched`, attention lists no finding for it, and the readiness count does not include it

#### Scenario: A line is added after the closing fence

- **WHEN** a person adds a line after the closing fence of an imported Source page
- **THEN** the row is `corrupt_no_copy`, and it stays reported after the next binding walk

### Requirement: A missing original is reported and never restored automatically

Fixity SHALL report a missing original with its path, its expected SHA-256 and a restore route. The route SHALL name each candidate copy that fixity finds whose recorded size matches. Fixity SHALL NOT read a candidate to name it, so the owner checks its hash before use. Fixity SHALL NOT restore a missing original by itself.

#### Scenario: An original is deleted by hand

- **WHEN** a person deletes a stored original and keeps its binding, and a matching copy exists
- **THEN** the row is `missing` and the report names the copy as a candidate
- **AND** no file appears at the original's path

### Requirement: Fixity results reach the owner without new agent steps

Attention SHALL compose an `original_fixity` category in its default union, with one finding for each original in `restored`, `missing` or `corrupt_no_copy`. A `restored` finding SHALL be informational. A finding SHALL carry the path, the state, the cause when there is one, the expected SHA-256 and the route. Another audience SHALL receive a copy outside the vault by its kind, without its path. `exomem status` SHALL carry an `originals` block. The block SHALL report `unknown` until the first pass and the first binding walk complete, and SHALL NOT report a zero for an unknown count. When the last walk failed, the block SHALL name its failure code. When the last successful walk is older than two walk intervals, the block SHALL report `unknown` with that walk's time. When the last pass is older than three pass intervals, the block SHALL report `unknown` with the last pass time. After a pass, it SHALL report the count of each state, the last pass time, the last pass's unreadable and rescheduled originals, and the age of the oldest verification. A pass that stopped early SHALL say that its counts are partial. Attention SHALL report the category's coverage as `unknown` before the first pass, after a stale pass and while fixity is off. Fixity SHALL add no tool and no agent step.

#### Scenario: Status before the first pass

- **WHEN** `exomem status` runs before any pass has completed
- **THEN** the `originals` block reports `unknown` and no count

#### Scenario: The walk cannot read a note

- **WHEN** the binding walk cannot read a note on an install whose table no walk has filled, and a pass runs
- **THEN** the `originals` block and the readiness answer report `unknown` with the walk's failure code, and no count

#### Scenario: A damaged original reaches attention

- **WHEN** a pass records `corrupt_no_copy` for an original
- **THEN** default attention lists one `original_fixity` finding with that path, the expected SHA-256 and the route

### Requirement: A Cloud cell reports fixity without content

A Cloud cell's readiness answer SHALL carry the count of originals in `missing` or `corrupt_no_copy`. It SHALL carry `unknown` before the first pass and the first walk, `unknown` with the last pass time when that pass is stale, and `unknown` with the last walk time when that walk is stale. An `unknown` answer SHALL name the last walk's failure code when it failed. A `restored` original SHALL NOT enter the count, because it needs no action. It SHALL carry no path, name or hash. The count SHALL NOT make the cell unready. cellctl SHALL alert when the count is above zero. An unknown or unreadable answer SHALL neither fire nor resolve the alert.

#### Scenario: A cell holds a damaged original

- **WHEN** a pass on a Cloud cell records `corrupt_no_copy`
- **THEN** readiness stays ready and carries a count of one, with no path
- **AND** cellctl fires the alert through its existing receiver

#### Scenario: A cell has not finished its first pass

- **WHEN** cellctl reads a readiness answer whose fixity count is `unknown`
- **THEN** cellctl neither fires nor resolves the alert
