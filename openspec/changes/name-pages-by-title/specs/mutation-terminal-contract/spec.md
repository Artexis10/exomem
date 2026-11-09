## MODIFIED Requirements

### Requirement: Compact Success Is The Default Projection

Committed and replayed product mutations SHALL return a compact default projection led by `user_summary`, then `ok`, `status`, `mutated`, primary `path`, original `request_id`, stable receipt identity, and `warnings_count`. `user_summary` SHALL be one plain sentence built only from fields the stored terminal holds, so a repeat request with the same idempotency key returns the same text: a verb, then the path, or the first path and the count of the others. A batch with per-file states SHALL read them from its projected rows: `Saved` SHALL cover only the files this call stored, a batch with a failed or already-stored file SHALL read "Saved S of T files -> <first stored path> and S-1 more; F files could not be saved" plus "K already saved" when K > 0, and a batch that stored nothing SHALL NOT read as "Saved" ("Already saved -> <path>", "Nothing saved; F files could not be saved", or both counts). A single file whose bytes were already stored SHALL read "Already saved -> <path>". The sentence SHALL carry no code, id or reason. Every sentence SHALL end with the name of the store that took the write in parentheses; the name SHALL come from `cloud_cell.store_identity()` at render time and never from the stored terminal, so it is constant per deployment. Every write operation SHALL declare its verb from a closed set (saved, updated, moved, deleted, replaced, discarded or restored), and a write that declares none SHALL carry no `user_summary`. A replacement leads with the new page and names the old one after a comma. A result whose status is `replayed` (the effect was already in place) SHALL read "Already" plus the lower-cased verb. A refusal or a validation result SHALL carry no `user_summary`. Over MCP, the reply SHALL name each page after the egress release: the primary `path` is replaced by the page's `title` and short `id`, several pages by `pages`, each with `title` and `id`, and `user_summary` names each page by its title. A page that cannot be read as a Markdown page keeps its path, and a tool whose subject is a file keeps `path`. A reply that also lists pages in nested rows keeps `path` throughout, so that one reply never mixes the two forms. The CLI and REST projections keep `path`, and the stored terminal is unchanged. A scenario below that reads a path describes the stored terminal. `response_detail="full"` SHALL add the complete leaf result under `diagnostics`, and keeps the page's path under `diagnostics.operational`; `response_detail="legacy"` SHALL return the pre-change raw leaf result during the compatibility window.

#### Scenario: Default committed response is decisive
- **WHEN** a governed product mutation succeeds without an explicit response detail
- **THEN** the first-level response identifies it as committed and mutated
- **AND** verbose semantic, index, warning, and transition diagnostics are not mixed into the compact terminal fields

#### Scenario: Full diagnostics are requested
- **WHEN** the same mutation is requested with `response_detail="full"`
- **THEN** the compact terminal fields are unchanged
- **AND** the complete existing leaf payload is available under `diagnostics`

#### Scenario: A committed save leads with a plain summary
- **WHEN** a governed product mutation saves one file without an explicit response detail
- **THEN** the first key of the response is `user_summary`, reading "Saved -> <path> (<store name>)"
- **AND** a delete reads "Deleted -> <path> (<store name>)", a move reads "Moved <from> -> <to> (<store name>)", a supersede reads "Replaced -> <new path>, was <old path> (<store name>)", and a result whose status is `replayed` reads "Already saved -> <path> (<store name>)"
- **AND** a repeat request with the same idempotency key returns the first call's text, because it returns the stored terminal

#### Scenario: The summary names the store that took the write
- **WHEN** the same committed terminal is projected by a personal deployment and by an Exomem Cloud cell
- **THEN** the personal sentence ends with "(personal Exomem)" and the Cloud sentence ends with "(Exomem Cloud)"
- **AND** every other key and its order are unchanged

#### Scenario: A partial batch never reports a failed file as saved
- **WHEN** a batch of three files stores two and fails one
- **THEN** `user_summary` reads "Saved 2 of 3 files -> <first stored path> and 1 more; 1 file could not be saved (<store name>)"
- **AND** a batch that stores nothing reads "Already saved -> <path> (<store name>)" or "Nothing saved; 1 file could not be saved (<store name>)", never "Saved"
- **AND** a single file whose bytes were already stored reads "Already saved -> <path> (<store name>)"
- **AND** a repeat request with the same idempotency key returns the same text

#### Scenario: An MCP create reply names the page by title
- **WHEN** a caller creates a page over MCP
- **THEN** the reply carries `title` and `id`, no `path`, and `user_summary` reads "Saved -> <title> (<store name>)"

#### Scenario: Full detail keeps the path
- **WHEN** the same MCP write is requested with `response_detail="full"`
- **THEN** `diagnostics.operational.path` holds the vault path

#### Scenario: A reply with nested page rows keeps one form
- **WHEN** an MCP write reply holds a primary `path` and a nested row that also holds a `path`
- **THEN** the reply keeps `path` in both places and carries no `title` or `id` for the primary page
