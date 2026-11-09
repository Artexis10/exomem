## ADDED Requirements

### Requirement: A Page Reply Names The Page By Title And Short Id

Over MCP, the compact reply of a page-subject write SHALL carry the page's `title` and short `id` in place of its `path`, and `user_summary` SHALL name the page by its title. A write that reports several pages SHALL carry `pages`, each with `title` and `id`. A search hit for a page SHALL carry `id` in place of `path` and keep its `title` and `ref`; a search with `detail="full"` SHALL keep the `path` beside `id`. A page read SHALL carry `id` beside its `path`. A page that cannot be read as a Markdown page SHALL keep its path. `response_detail="full"` SHALL keep the path under `diagnostics.operational`. The MCP reply SHALL be named after the egress release, and the CLI and REST replies SHALL keep their shapes. A tool whose subject is a file SHALL keep `path` in its reply. A compact reply that replays an earlier committed write SHALL carry `replayed: true`, and every other compact reply SHALL carry no `replayed` key. A replay's `user_summary` repeats the first reply's text, so `replayed` is the only sign that the retry wrote nothing.

#### Scenario: A create reply names the page by title
- **WHEN** a caller creates a page over MCP
- **THEN** the reply carries `title` and `id`, no `path`, and `user_summary` reads "Saved -> <title> (<store name>)"

#### Scenario: Full detail keeps the path
- **WHEN** the same write is requested with `response_detail="full"`
- **THEN** `diagnostics.operational.path` holds the vault path

#### Scenario: A hit names the page by id
- **WHEN** a search returns a page hit
- **THEN** the hit carries `id`, `title` and `ref` and no `path`, unless the search asked for `detail="full"`

#### Scenario: A replayed reply says so
- **WHEN** a caller repeats a committed write and the server returns the stored result
- **THEN** the reply carries the same `user_summary`, `title` and `id` as the first reply and `replayed: true`
- **AND** the first reply carries no `replayed` key
