# Hidden parts delta

## ADDED Requirements

### Requirement: Hidden parts are text of the one Source page

An artifact SHALL keep exactly one addressable Source page when its extracted text continues on part pages. Part pages SHALL be text of that page, not pages of the artifact. Browse, overview, the working set and the vault listing SHALL NOT show part pages. Audit counts and the viewer map SHALL NOT count a part page as a memory. A move of a Source page, with or without its original, SHALL move its part pages in the same transaction. A part page SHALL be readable exactly where its Source page is readable. Its scope membership, and with it the audiences that may read it, SHALL be its Source page's, derived from the part's path. A governed projection catalog SHALL NOT hold a part page as an item of its own, so the text of a part never surfaces there as a separate document.

#### Scenario: The folder lists the original and its page
- **WHEN** a long document is stored and its folder is listed
- **THEN** the listing shows the original and its Source page only

#### Scenario: A move carries the parts
- **WHEN** a Source page with part pages is moved to another folder, with or without its original
- **THEN** a search finds the Source at its new path with the same matched part
- **AND** no part page remains at the old path

#### Scenario: A part belongs to the scopes of its Source
- **WHEN** a governance scope selects a Source page by its stable identifier
- **THEN** each part page of that Source belongs to that scope

#### Scenario: Parts are not unprocessed sources
- **WHEN** audit checks for unprocessed sources
- **THEN** it reports the Source page and none of its part pages
