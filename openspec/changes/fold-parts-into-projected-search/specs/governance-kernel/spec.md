## ADDED Requirements

### Requirement: A governed catalog searches a long Source in full

A governed projection catalog SHALL hold the text of each further part of a Source's extracted text as content of that Source's full-text variant row. Projected search SHALL match a part only through that row: an audience whose decision selects a variant below full text SHALL NOT match a part. A match on a Source whose full-text row holds parts SHALL return one hit on the Source with `matched_part`, which is 1 when the Source page's own text matched, and a `passage` whose `offset` and `length` count the stored text and open a `read_memory` window at the match. A part too large for one search document SHALL end the Source's parts in the catalog as a missing part does, and SHALL NOT refuse the write. A part SHALL follow its Source through every catalog change of that Source. A projector refresh SHALL reuse the stored vector of each row whose projection text is unchanged.

#### Scenario: A phrase in the last part is found
- **WHEN** the owner searches for a phrase that only the last part of a long Source holds
- **THEN** search returns one hit on the Source with `matched_part` equal to the last part
- **AND** `passage.text` contains the phrase
- **AND** `read_memory` at `passage.offset` returns a window that contains the phrase

#### Scenario: A phrase in the first part names it
- **WHEN** the owner searches for a phrase that only the Source page's own part of a long Source holds
- **THEN** search returns one hit on the Source with `matched_part` equal to 1
- **AND** `read_memory` at `passage.offset` returns a window that starts with `passage.text`

#### Scenario: A part too large to search ends the parts
- **WHEN** a part of a long Source holds more text than one search document may hold, and the Source is written
- **THEN** the write succeeds
- **AND** a phrase in an earlier part is found, and a phrase in that part is not

#### Scenario: An audience below full text never matches a part
- **WHEN** an audience that may read only an excerpt of the Source, or nothing of it, searches for a phrase that only a part holds
- **THEN** the response equals the response for a phrase that no page holds

#### Scenario: Parts follow a scope change
- **WHEN** an edit of the Source's tags, or a move into a folder that a path scope selects, takes the Source out of an audience's full text
- **THEN** no part of the Source matches for that audience
- **AND** a reverse edit makes its parts match again

#### Scenario: Re-extraction replaces the parts
- **WHEN** a Source is extracted again into fewer parts
- **THEN** a phrase from the removed tail finds nothing
- **AND** a phrase in the new last part returns a hit with that part as `matched_part`

#### Scenario: An existing catalog gains parts on upgrade
- **WHEN** the projector refresh runs on a catalog that a release before parts published
- **THEN** the refreshed catalog holds part text
- **AND** a phrase in the last part of a long Source is found

#### Scenario: The refresh measures no unchanged text again
- **WHEN** the projector refresh republishes a row whose projection text is unchanged
- **THEN** the embedding model is called for no such row
- **AND** the new row carries the stored vector of the old row
