## ADDED Requirements

### Requirement: A session closes by recording the recap and citing it

At a decision or stopping point, the shipped skill, the engagement reference, the Stop-hook episode ask and the `episode_due` advisory SHALL tell the agent to close in two steps. First the agent SHALL record one recap. Then the agent SHALL update each page that a decided item changes, with the ordinary writers (`edit_note`, `change_observations`, `supersede_note`). The agent SHALL cite the recap in the `sources:` of each compiled note it updates, and SHALL update an entity, plan or record page without a citation, because only a compiled note holds a `sources:` claim and the line of an uncited page stays open by design. The Stop-hook episode ask SHALL stay one short line, SHALL make its citation step conditional on live policy, and its byte ceilings SHALL change only by the added words. The skill and the engagement reference SHALL say that, at `off` or when the user asked only for a recap, a recap request alone does not authorize page edits. The `episode_memory` tool description SHALL tell the agent to cite the recap in the `sources:` of each compiled note it updates and SHALL point to the `compile` block. The existing rules SHALL hold: record only when the user asks or the live `proactive_capture` disposition permits it, record nothing when nothing durable happened, and treat a hook reminder as no permission. No text SHALL ask the agent to record the same recap a second time, and none SHALL ask the agent to link a decided item to a page.

#### Scenario: The close step reaches a client with no hook

- **WHEN** a tool-only client reads the `episode_memory` tool description
- **THEN** the description tells the agent to cite the recap in the `sources:` of each compiled note it updates
- **AND** it points to the `compile` block of the response

#### Scenario: The Stop hook asks for the citation only under live policy

- **WHEN** the Stop hook fires its episode ask
- **THEN** the ask puts the citation step after the condition that live policy permits capture
- **AND** it ends with "Otherwise do nothing."

### Requirement: The record response reports which decided items no live page cites

The `record` response SHALL carry a `compile` block when the recap holds at least one Decided item. The block SHALL carry `cite`, the ref of the recap just recorded, `decided`, the count of Decided items, and `open`, the count of the live revision's Decided lines that no cited revision of the episode holds. A revision SHALL count as cited when a live compiled page that the caller can see names it in `sources:`. The block SHALL lead with one `outcome` sentence that an agent can repeat as is. The `outcome` SHALL state the coverage result only, SHALL say that the count is by citation, and SHALL NOT order a write. When live policy permits proactive capture, the block SHALL carry a separate `next_step` that conditions a page update on the agent's judgement: a decided item changes a note. When live policy does not permit it, the block SHALL carry no `next_step` and its `outcome` SHALL be unchanged. No text of the block SHALL ask the agent to record the recap again.

The record SHALL NOT walk the vault to count. It SHALL read the episode's own revisions and the pages that cite them through the writers' resolver snapshot taken without the disk fallback. It SHALL probe the snapshot before it reads any revision. When that snapshot is cold, or the count fails, the block SHALL report `open` as `unchecked` for every recap with Decided items, whatever the recap's `ingested_into` holds, SHALL say in the `outcome` that the citation was not checked, and SHALL NOT report 0. When the live revision's Decided count differs from the count just recorded, the block SHALL report `open` as `unchecked`. An unchecked count SHALL NOT refuse the recap. A citing page that the caller cannot see SHALL read as absent, so that the block equals the block of a vault without that page. A recap with no Decided item SHALL carry no `compile` block.

#### Scenario: The block names the recap to cite and the open count falls once a page cites it

- **WHEN** a recap with two Decided items is recorded, a live compiled page cites it, and the episode records again with the same two Decided items
- **THEN** the first block carries `cite` equal to the recap ref, `decided: 2` and `open: 2`
- **AND** the second block carries `open: 0` and no `next_step`

#### Scenario: A cold resolver snapshot reports the count as unchecked

- **WHEN** a recap's earlier revision is cited and the writers' resolver snapshot is cold
- **THEN** the recap is still recorded and no resolver is built from the vault
- **AND** the block reports `open: "unchecked"` and its `outcome` says that the citation was not checked

#### Scenario: A cold snapshot hides whether a withheld page cites the recap

- **WHEN** the writers' resolver snapshot is cold, and one recap is cited by a page withheld from the caller while another recap has no citing page
- **THEN** both blocks are equal apart from the recap ref, and both report `open: "unchecked"`

#### Scenario: The next step appears only when live policy permits capture

- **WHEN** live policy permits proactive capture
- **THEN** the block carries a `next_step` that conditions a page update on the agent's judgement
- **WHEN** live policy does not permit proactive capture
- **THEN** the same block carries no `next_step`, and its `outcome` is unchanged

#### Scenario: A recap with no Decided item gives no block

- **WHEN** a recap has no Decided item
- **THEN** the `record` response has no `compile` key

#### Scenario: A withheld citing page reads as no citation

- **WHEN** the only live page that cites a recap is withheld from the caller
- **THEN** the caller's open count equals the count of a vault where that page does not exist
