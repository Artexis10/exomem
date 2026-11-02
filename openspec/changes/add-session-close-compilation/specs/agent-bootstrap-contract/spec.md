## ADDED Requirements

### Requirement: A session closes by updating its pages before it records the recap

At a decision or stopping point, the shipped skill, the engagement reference, the Stop-hook episode ask and the `episode_due` advisory SHALL tell the agent to close in two steps. First the agent SHALL update each page whose knowledge the session changed, with the ordinary writers (`edit_note`, `change_observations`, `supersede_note`). The pages are entity facets, decisions and workflow contracts. Then the agent SHALL record one recap, link each Decided item to the page it belongs to with `[[...]]`, and set `about` to the pages it updated. The Stop-hook episode ask SHALL stay one short line, SHALL make its page step conditional on live policy, and its byte ceilings SHALL change only by the added words. The skill and the engagement reference SHALL say that, at `off` or when the user asked only for a recap, a recap request alone does not authorize page edits, and that a link with the page's file name works when its title is long, because a Decided item holds 200 characters. The `episode_memory` tool description SHALL tell the agent to link each Decided item to its page as `[[Page]]` and SHALL point to the `compile` block. The existing rules SHALL hold: record only when the user asks or the live `proactive_capture` disposition permits it, record nothing when nothing durable happened, and treat a hook reminder as no permission. No text SHALL ask the agent to record the same recap a second time.

#### Scenario: The close step reaches a client with no hook

- **WHEN** a tool-only client reads the `episode_memory` tool description
- **THEN** the description tells the agent to link each Decided item to its page as `[[Page]]`
- **AND** it points to the `compile` block of the response

#### Scenario: The Stop hook asks for the page step only under live policy

- **WHEN** the Stop hook fires its episode ask
- **THEN** the ask puts the page step after the condition that live policy permits capture
- **AND** it ends with "Otherwise do nothing."

### Requirement: The record response compiles the pages the recap names

The `record` response SHALL carry a `compile` block when the recap names at least one page the caller may see or has at least one Decided item. The block SHALL list each page the recap names through `about` or through a `[[...]]` link in a Decided item, with its reference, vault-relative path, title and last update time. The update time SHALL be the frontmatter `updated` value, else the file modification time, and the block's `diagnostics` SHALL say which source gave each time. A listed page that cannot be read SHALL stay listed with its update time `unknown` and the source `unreadable`, and SHALL NOT make its Decided item count as unlinked. The block SHALL count the Decided items that carry no resolvable visible link as `unlinked_decided`, and SHALL lead with one `outcome` sentence that an agent can repeat as is. The `outcome` SHALL state the result only and SHALL NOT order a write. When live policy permits capture and the block lists a page, the block SHALL carry a separate `next_step` that conditions a page update on the agent's judgement: a decided item changes a listed page that the session has not updated. No text of the block SHALL ask the agent to record the recap again.

The server SHALL NOT claim that a page was written in this session, and SHALL NOT use a time window to decide it: the episode ledger binds recap inputs only and ordinary writes carry no episode key. A page the caller cannot see, and a link that resolves to no page, SHALL be omitted and SHALL NOT be named or counted, so a withheld page reads exactly like an absent one. A recap that names no page and has no Decided item SHALL carry no `compile` block. The link check SHALL take the writers' resolver snapshot without the disk fallback, so that a record never walks the vault. When the server cannot check the links, because resolution fails or the snapshot declines on a cold cache, the block SHALL say so and SHALL omit `unlinked_decided`, because an unknown count is never zero. An unchecked link SHALL NOT refuse the recap.

#### Scenario: Linked and about pages are listed with their update time

- **WHEN** a recap's Decided items link two visible pages and its `about` names a third
- **THEN** the `record` response carries a `compile` block that lists the three pages with their update times
- **AND** `unlinked_decided` is 0
- **AND** the block's `diagnostics` name the source of each update time, and the `outcome` sentence does not
- **AND** the `outcome` sentence states that the recap names 3 pages, and orders no write

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

#### Scenario: The next step appears only when live policy permits capture

- **WHEN** a recap names a page and live policy permits proactive capture
- **THEN** the `compile` block carries a `next_step` that conditions a page update on the agent's judgement
- **WHEN** live policy does not permit proactive capture
- **THEN** the same block carries no `next_step`, and its `outcome` is unchanged

#### Scenario: An unreadable page stays listed

- **WHEN** a Decided item links a visible page whose file cannot be read
- **THEN** the page stays in the list with its `updated` value `unknown` and its source `unreadable`
- **AND** `unlinked_decided` does not count the item

#### Scenario: A failed or declined link check is reported as unchecked

- **WHEN** link resolution fails, or the writers' resolver snapshot declines because no warm cache exists
- **THEN** the recap is still recorded and no resolver is built from the vault
- **AND** the `compile` block says that its links were not checked and omits `unlinked_decided`
- **AND** a declined snapshot starts one background warm, so that the next record is checked

#### Scenario: The block survives the compact envelope

- **WHEN** a real MCP call records a recap whose Decided item links a visible page
- **THEN** the compact response keeps the `compile` block
- **AND** `user_summary` is still the first key
