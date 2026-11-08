## MODIFIED Requirements

### Requirement: A session closes by recording the recap and citing it

At a decision or stopping point, the shipped skill, engagement reference, Stop-hook episode ask and `episode_due` advisory SHALL tell the agent to record one recap, then compile or explicitly dismiss every Decided item before citing it. The agent SHALL update each item's canonical page with the ordinary writers (`edit_note`, `change_observations`, `supersede_note`). Only after the whole pass is complete SHALL the agent cite the recap from updated compiled notes. Entity, plan and record pages take no citation. The guidance SHALL explain that one compiled note's citation holds every line that the episode's page held when it was cited, that a later recap which changes the Decided lines re-opens them, and that citation linkage is not semantic coverage. The Stop-hook episode ask SHALL stay one short line within its established byte ceilings and SHALL make capture conditional on live policy. At `off`, or when the user asked only for a recap, a recap request alone SHALL NOT authorize page edits. The `episode_memory` tool description SHALL state the completion-before-citation condition and SHALL point to the `compile` block. The existing rules SHALL hold: record only when the user asks or the live `proactive_capture` disposition permits it, record nothing when nothing durable happened, and treat a hook reminder as no permission. No text SHALL ask the agent to record the same recap a second time, and none SHALL ask the agent to link a decided item to a page.

#### Scenario: The close step reaches a client with no hook

- **WHEN** a tool-only client reads the `episode_memory` tool description
- **THEN** the description tells the agent to finish or dismiss every Decided item before citing the recap from updated compiled notes
- **AND** it points to the `compile` block of the response

#### Scenario: The Stop hook asks for the citation only under live policy

- **WHEN** the Stop hook fires its episode ask
- **THEN** the ask puts the citation step after the condition that live policy permits capture
- **AND** it ends with "Otherwise do nothing."

### Requirement: The record response reports which decided items no live page cites

The `record` response SHALL carry a `compile` block when the recap holds at least one Decided item. The block SHALL carry `cite`, the ref of the recap just recorded, `decided`, the count of Decided items, and `open`, the count of the Decided lines of the episode's page that no cited text of the episode holds. The cited texts of an episode are its page and each revision file that a live page references. A text SHALL count as cited when a live compiled page that the caller can see names it in `sources:`. The block SHALL lead with one `outcome` sentence that an agent can repeat as is. The `outcome` SHALL state the coverage result only, SHALL say that the count is by citation, and SHALL NOT order a write. When live policy permits proactive capture, a separate `next_step` SHALL ask the agent to compile or explicitly dismiss every Decided item before citing the recap from updated compiled notes. When live policy does not permit it, the block SHALL carry no `next_step` and its `outcome` SHALL be unchanged. No text of the block SHALL ask the agent to record the recap again.

The record SHALL NOT walk the vault to count. It SHALL read the episode's own page and revision files and the pages that cite them through the writers' resolver snapshot taken without the disk fallback. It SHALL probe the snapshot before it reads any revision. When that snapshot is cold, or the count fails, the block SHALL report `open` as `unchecked` for every recap with Decided items, whatever the recap's `ingested_into` holds, SHALL say in the `outcome` that the citation was not checked, and SHALL NOT report 0. When the page's Decided count differs from the count just recorded, the block SHALL report `open` as `unchecked`. An unchecked count SHALL NOT refuse the recap. A citing page that the caller cannot see SHALL read as absent, so that the block equals the block of a vault without that page. A recap with no Decided item SHALL carry no `compile` block.

A recap that changes the Decided lines of an episode page that a live compiled page cites SHALL clear the page's `ingested_into` back-references in the atomic write that revises the page. Source closure restores a back-reference when a compiled note that names the page is next saved. A recap that leaves the Decided lines unchanged SHALL keep the back-references.

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

#### Scenario: A later recap changes a cited Decided line

- **WHEN** a live compiled page cites an episode's page and the episode records again with a changed Decided line
- **THEN** the block reports that line as open
- **AND** the block reports `open: 0` again once a compiled note that names the page is saved

## ADDED Requirements

### Requirement: A repeat recap revises the episode's one page in place and keeps the previous text in history

`episode_memory` `record` SHALL keep one page for each episode and audience group. When that page exists and the recap's digest differs from the page's `episode_digest`, `record` SHALL rewrite the page in place and, in the same atomic write, create one compiled-history entry that holds the page's exact previous bytes. The page's ref and path SHALL NOT change. Its title, body, `episode_digest`, Decided lines and `about` links SHALL follow the new recap. When the digest is identical, `record` SHALL write nothing. When another writer changed the page after the recorder read it, the write SHALL fail on the page's content hash and be read and retried once, as the recorder does today. When the episode has no page, `record` SHALL create it. `record` SHALL NOT create a second page for a revision and SHALL NOT mark any page superseded.

History entries SHALL be retained as the `semantic-write-contract` capability defines for episode pages. They SHALL be create-only, SHALL stay outside ordinary reads, indexing, titles and search, and SHALL be readable through `read_memory` with `include_history=true`. The system SHALL NOT rotate, prune or cap them. A failed history write SHALL fail the whole revision, so that the page never changes without its previous text retained.

#### Scenario: Three recaps of one episode

- **WHEN** an agent records three different recaps for one episode key
- **THEN** one recap page exists with the third recap's text
- **AND** two history entries hold the first and second texts in recording order
- **AND** `read_memory` with `include_history=true` returns both

#### Scenario: The same recap is recorded again

- **WHEN** an agent records a recap whose digest equals the page's digest
- **THEN** nothing is written and the page and history are unchanged

#### Scenario: History does not reach search or notes

- **WHEN** an episode page has history entries
- **THEN** search, titles and the Sources catalogue list the episode once
- **AND** no history entry appears in any ordinary read

#### Scenario: A history write fails

- **WHEN** the history entry cannot be created during a revision
- **THEN** the page keeps its previous bytes and the call fails without a partial write

### Requirement: Only the episode recorder revises a Source in place

The episode recorder SHALL be the only writer that rewrites the text of a Source. It SHALL rewrite only a page that is a recap Source in the episode folder, whose `episode` key and filename group equal the caller's recap, and SHALL refuse any other target. `edit_note`, `replace`, `supersede_note` and every other writer SHALL keep refusing Sources. The append-only guard for Sources SHALL stay unchanged for every other path.

#### Scenario: A hand edit of a recap page

- **WHEN** an agent calls `edit_note` or `replace` on a recap page
- **THEN** the call is refused as for any Source

#### Scenario: The recorder is pointed at another Source

- **WHEN** a revision targets a Source that is not the caller's episode page
- **THEN** the recorder refuses and writes nothing

### Requirement: A vault with the earlier recap layout keeps working until a reviewed fold

A vault whose episodes hold several revision files SHALL keep working after upgrade. `record` SHALL revise the episode's live page in place and SHALL leave its superseded revision files untouched. Coverage SHALL read every file of the episode as before. The `fold-recap-revisions` curation step SHALL be the only way that superseded revisions move into history. Upgrade SHALL NOT apply it, and each applied batch SHALL be undoable through compensation. Every revision that a live page references SHALL stay a file, so each existing citation resolves before, during and after a fold.

#### Scenario: A recap is recorded on an unfolded episode

- **WHEN** an agent records a recap for an episode that still holds four superseded revision files
- **THEN** the live page is revised in place with a history entry
- **AND** the four files are unchanged

#### Scenario: Citations survive the fold

- **WHEN** an owner applies a recap fold to an episode whose superseded revision a compiled note cites
- **THEN** the citing note's reference still resolves to the cited text before, during and after the step
