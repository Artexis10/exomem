## ADDED Requirements

### Requirement: Compiled writes may ask the writing agent one note-cohesion question

When a compiled-note mutation commits to a page whose `note_cohesion` review item is open, the successful result SHALL include one `structure_suggestion` with `kind` `note_cohesion`. The suggestion is carried on the existing commit result and projected into the default compact terminal, for every compiled writer: `remember`, `edit_memory`, `observe_memory`, and `replace_memory`.

The suggestion SHALL report the page's semantic-unit count as `units`, the size step it reached as `step_units`, the next size step as `next_step_units`, the review item's `ref` and `fingerprint`, and a fixed `question`. The question SHALL ask the agent whether the page answers more than one independent question. It SHALL tell the agent to keep a coherent page whole by dismissing the item, and to propose a split only through the curation lane. The suggestion SHALL NOT claim that the page is overloaded, and SHALL NOT report a strength, a score, a confidence, reason codes or terms.

The suggestion SHALL be withheld when the `structural_suggestions` delegation class is `off`, or when the `note_cohesion` family disposition is `quiet` or `off`.

The suggestion is advisory. It SHALL NOT add a value to the closed set of keys a client branches on for mutation outcome. It SHALL NOT alter `status`, `mutated`, `path`, `warnings_count`, mutation identity, or replay behaviour. When the item is not open, the key SHALL be absent rather than null or empty. The response layer SHALL re-validate the payload against bounds declared for this kind, and SHALL drop a payload that fails them.

#### Scenario: A page that reached a step asks the writing agent

- **WHEN** a compiled write commits and leaves the page with 14 semantic units, and no decision is recorded for that page at the 14-unit step
- **THEN** the write succeeds with its existing committed terminal unchanged
- **AND** the default compact response carries one `structure_suggestion` with `kind` `note_cohesion`, `units` 14, `step_units` 14, `next_step_units` 21, the item's `ref` and `fingerprint`, and the question
- **AND** the suggestion carries no strength, score, reason codes or terms

#### Scenario: A kept-whole page stays quiet until its next step

- **WHEN** the agent has dismissed the page's item at the 14-unit step, and a later write leaves the page with 20 units
- **THEN** the response contains no `structure_suggestion` key
- **AND** when a further write brings the page to 21 units, the response carries the question again with `step_units` 21

#### Scenario: An unanswered item asks again on the next write

- **WHEN** a page's item is open and nobody has answered it, and another compiled write commits to that page
- **THEN** that response carries the question again with the same `ref` and `fingerprint`

#### Scenario: A user who turned structural suggestions off is not asked

- **WHEN** the `structural_suggestions` delegation class is `off`, or the `note_cohesion` family is quiet, and a compiled write commits to a page with an open item
- **THEN** the response contains no `structure_suggestion` key
- **AND** the item stays open in the review queue

#### Scenario: A small page never asks

- **WHEN** a compiled write commits and leaves the page with 8 semantic units
- **THEN** the response contains no `structure_suggestion` key

#### Scenario: Every compiled writer can carry the question

- **WHEN** a page with an open item is mutated through `remember`, `edit_memory`, `observe_memory`, or `replace_memory`
- **THEN** each committed result can carry the question
- **AND** the question has the same shape whichever writer produced it

### Requirement: The note-cohesion question never weakens a committed write

The question SHALL be failure-isolated. An exception, an unreadable review state, or an undecidable result SHALL cause the suggestion to be omitted. It SHALL NOT fail, delay, retry, roll back, or alter the committed mutation or its terminal.

The system SHALL NOT move, rename, split, retitle, rescope, create, or delete any page as a consequence of the question. Every fact in the suggestion SHALL come from the page named in the same response.

#### Scenario: A failure leaves the write committed

- **WHEN** the note-cohesion check raises during an otherwise successful compiled write
- **THEN** the mutation remains committed with its existing terminal
- **AND** no `structure_suggestion` is present
- **AND** no warning, error code, or retry is produced for the caller

#### Scenario: An unreadable review state asks nothing

- **WHEN** the review state cannot be read while a compiled write commits to a page past a step
- **THEN** the response contains no `structure_suggestion` key

#### Scenario: The payload names only the written page

- **WHEN** the question is returned in a vault with pages the caller may not see
- **THEN** the payload contains no path, title, project or count that belongs to another page

## MODIFIED Requirements

### Requirement: Capture reports classification debt in one advisory suggestion

When a capture commits while the vault holds sources with no chosen kind, the successful result SHALL include at most one bounded advisory suggestion. The suggestion SHALL be reported through the advisory-suggestion channel that compiled writes also use for the note-cohesion question, and SHALL be distinguished by its own kind value. Sources with no chosen kind are those filed under the `unclassified` folder, the legacy `other` folder, and the legacy-vault import folder.

The suggestion SHALL report a `strength` of `moderate`, a deterministically ordered list of reason codes, the number of such sources, and the folders that hold them. It SHALL NOT report a numeric confidence, score, or probability.

Detection SHALL be deterministic and local. It SHALL reuse the per-folder counts the capture already takes, and it SHALL NOT perform a model call, a network call, or a further scan, and SHALL NOT introduce persistent state. A caller other than the owner SHALL receive no suggestion, because those counts include pages it may not see. The suggestion is advisory: any detection failure, refusal, or absent optional state SHALL leave the committed capture, its location, and its existing result keys unchanged. When no such source exists the key SHALL be absent rather than null or empty.

The suggestion SHALL reach the caller through the committed-mutation response, and the response layer SHALL re-validate it against bounds declared for its own kind rather than forwarding an unvalidated payload. A suggestion whose payload does not satisfy those bounds SHALL be dropped rather than widening the response contract.

#### Scenario: The suggestion reaches the caller through the committed response

- **WHEN** a capture commits in a vault that holds an unclassified source
- **THEN** the caller's committed response carries the suggestion with its kind, strength, reason codes, source count, and folders
- **AND** the response does not carry payload fields belonging to a different advisory kind

#### Scenario: A malformed classification suggestion is dropped, not forwarded

- **WHEN** a classification suggestion whose payload violates the bounds declared for its kind reaches the response layer
- **THEN** the committed response omits the suggestion entirely
- **AND** the capture itself is unaffected

#### Scenario: Legacy and imported sources count as debt

- **WHEN** a capture commits in a vault holding a legacy `Sources/Other/` page or a legacy-vault import
- **THEN** the suggestion counts that page and names its folder

#### Scenario: A vault with no unclassified source stays quiet

- **WHEN** a classified capture commits in a vault where every source has a chosen kind
- **THEN** the result carries no classification suggestion

#### Scenario: Detection failure does not fail the capture

- **WHEN** classification detection raises during an otherwise successful capture
- **THEN** the capture is still committed at its projected location
- **AND** the result reports its normal success outcome with no suggestion key

#### Scenario: An exempt hosted caller receives no owner advisory

- **WHEN** a resolved hosted caller captures a source in a vault that holds classification debt
- **THEN** the capture follows its ordinary authorization rules
- **AND** the result omits the classification-debt advisory unless that caller is the verified owner

## REMOVED Requirements

### Requirement: Compiled writes may return one advisory structural-promotion suggestion

**Reason**: The lexical `scope_divergence` suggestion did not discriminate. On 40 labelled large notes it flagged 8 of 16 overloaded and 7 of 22 coherent notes, and every flagged note had 13 or more units.

**Migration**: Compiled writes return `kind` `note_cohesion` under the requirement "Compiled writes may ask the writing agent one note-cohesion question". Clients that read `strength`, `reasons`, `off_scope_units` or `cluster_terms` stop receiving them.

### Requirement: Structural detection is conservative and never triggered by size

**Reason**: The detector this rule constrained is removed. The new question uses size only to schedule a judgement and claims nothing about the page.

**Migration**: None.

### Requirement: Structural detection is scoped to compiled knowledge

**Reason**: The detector is removed. Its eligibility rules (compiled pages only, breadth-declared pages exempt) move to the `note_cohesion` family in `attention-queue`.

**Migration**: None.

### Requirement: Structural suggestions never weaken a committed write

**Reason**: The detector is removed. The same guarantee now holds under "The note-cohesion question never weakens a committed write".

**Migration**: None.

### Requirement: Structural evidence is confined to the written page

**Reason**: The detector is removed. The confinement rule now holds under "The note-cohesion question never weakens a committed write".

**Migration**: None.

### Requirement: A structural suggestion resolves when the corpus gives its material a home

**Reason**: Corpus resolution belonged to the lexical detector. A recorded keep-whole decision or a split now settles the question.

**Migration**: None.

### Requirement: Structural resolution reads only corpus state the write already holds and fails open

**Reason**: Corpus resolution is removed with the lexical detector.

**Migration**: None.
