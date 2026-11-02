## ADDED Requirements

### Requirement: A session closes by updating its pages before it records the recap

At a decision or stopping point, the shipped skill, the engagement reference, the `episode_memory` tool description, the Stop-hook episode ask and the `episode_due` advisory SHALL tell the agent to close in two steps. First the agent SHALL update each page whose knowledge the session changed, with the ordinary writers (`edit_note`, `change_observations`, `supersede_note`). The pages are entity facets, decisions and workflow contracts. Then the agent SHALL record one recap, link each Decided item to the page it belongs to with `[[...]]`, and set `about` to the pages it updated. The Stop-hook episode ask SHALL stay one short line, and its byte ceilings SHALL change only by the added words. The existing rules SHALL hold: record only when the user asks or the live `proactive_capture` disposition permits it, record nothing when nothing durable happened, and treat a hook reminder as no permission.

The `record` response SHALL carry a `compile` block when the recap names at least one page the caller may see or has at least one Decided item. The block SHALL list each page the recap names through `about` or through a `[[...]]` link in a Decided item, with its reference, vault-relative path, title and last update time. The update time SHALL be the frontmatter `updated` value, else the file modification time, and the block's `diagnostics` SHALL say which source gave each time. The block SHALL count the Decided items that carry no resolvable visible link as `unlinked_decided`, and SHALL lead with one `outcome` sentence that an agent can repeat as is.

The server SHALL NOT claim that a page was written in this session, and SHALL NOT use a time window to decide it: the episode ledger binds recap inputs only and ordinary writes carry no episode key. A page the caller cannot see, and a link that resolves to no page, SHALL be omitted and SHALL NOT be named or counted, so a withheld page reads exactly like an absent one. A recap that names no page and has no Decided item SHALL carry no `compile` block. When the server cannot check the links, the block SHALL say so and SHALL omit `unlinked_decided`, because an unknown count is never zero. A failed check SHALL NOT refuse the recap.

#### Scenario: Linked and about pages are listed with their update time

- **WHEN** a recap's Decided items link two visible pages and its `about` names a third
- **THEN** the `record` response carries a `compile` block that lists the three pages with their update times
- **AND** `unlinked_decided` is 0
- **AND** the block's `diagnostics` name the source of each update time, and the `outcome` sentence does not

#### Scenario: A withheld or unresolved link is omitted and not counted

- **WHEN** one recap links a page that the caller cannot see, and a second recap links a page that does not exist
- **THEN** the two `compile` blocks are byte-identical
- **AND** neither block names or counts the withheld page

#### Scenario: An unlinked Decided item is counted

- **WHEN** a recap holds three Decided items and only one links a visible page
- **THEN** `unlinked_decided` is 2
- **AND** the `outcome` sentence says that 2 decided items name no page

#### Scenario: No Decided items and no pages give no block

- **WHEN** a recap has no Decided item and names no page
- **THEN** the `record` response has no `compile` key

#### Scenario: A failed link check is reported as unchecked

- **WHEN** link resolution fails while the recap is prepared
- **THEN** the recap is still recorded
- **AND** the `compile` block says that its links were not checked and omits `unlinked_decided`

#### Scenario: The block survives the compact envelope

- **WHEN** a real MCP call records a recap whose Decided item links a visible page
- **THEN** the compact response keeps the `compile` block
- **AND** `user_summary` is still the first key
