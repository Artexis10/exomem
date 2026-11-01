# Hidden parts delta

## ADDED Requirements

### Requirement: Hidden parts are text of the one Source page

An artifact SHALL keep exactly one addressable Source page when its extracted text continues on part pages. Part pages SHALL be text of that page, not pages of the artifact. Browse, overview, the working set and the vault listing SHALL NOT show part pages. Audit counts and the viewer map SHALL NOT count a part page as a memory. Moving a Source page and its original SHALL move the part pages with them in one transaction. Removing a Source page SHALL remove its part pages. A part page SHALL be readable exactly where its Source page is readable.

#### Scenario: The folder lists the original and its page
- **WHEN** a long document is stored and its folder is listed
- **THEN** the listing shows the original and its Source page only

#### Scenario: A move carries the parts
- **WHEN** a Source with part pages is moved to another folder
- **THEN** a search finds the Source at its new path with the same matched part
- **AND** no part page remains at the old path

#### Scenario: Parts are not unprocessed sources
- **WHEN** audit checks for unprocessed sources
- **THEN** it reports the Source page and none of its part pages
