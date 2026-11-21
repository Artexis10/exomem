## ADDED Requirements

### Requirement: Ordinary search returns a verified transcript passage

Exomem SHALL split timestamped Source text at whole timed lines within the existing chunk limits, encode the chunks and retain their positions in the released text. Older transcript Sources SHALL use those chunks after normal incremental publication. Their original bytes SHALL stay intact.

In default hybrid search, Exomem SHALL lift the highest ranked transcript in the neutral top three when a matching passage can be verified. It SHALL appear in the top three ahead of any summary in that prefix that cites it. Explicit compiled-preference settings SHALL retain their ranking policy. The passage SHALL include verbatim matched lines, stored-text offsets, and recorded timestamps when present. A result SHALL expose only passage bytes, positions, and ranking evidence admitted for the requesting principal. Incomplete publication SHALL remain visible.

#### Scenario: A paraphrase matches a timestamped statement

- **WHEN** a paraphrase finds a competitive passage in a transcript whose linked summary omits the statement
- **THEN** default search returns the Source in the first three hits above that summary
- **AND** the passage gives the exact lines and their timestamp at a position the reader can open

#### Scenario: Publication or access cannot prove a passage

- **WHEN** the winning row has stale bytes or its text is withheld
- **THEN** Exomem omits the passage and its ranking preference
- **AND** it reports incomplete retrieval when publication prevents verification

#### Scenario: Equal words occur at two positions

- **WHEN** the same words occur twice with different timestamps
- **THEN** the passage points to the verified occurrence that supplied the match
