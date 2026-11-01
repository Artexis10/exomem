# Part pages in export and deletion delta

## ADDED Requirements

### Requirement: Export keeps hidden extraction parts

The durability registry SHALL classify a hidden part page of a Source's extraction as canonical vault content, so export and restore keep it. Other unregistered hidden state SHALL stay disposable.

#### Scenario: A part page is exported
- **WHEN** the registry classifies the path of a part page
- **THEN** the class is canonical
- **AND** an unregistered hidden file beside it stays disposable

### Requirement: Trash carries the part pages of a Source

Trashing a Source page SHALL move its hidden part pages to Trash beside it and remove their index rows. Recovering the page SHALL bring its part pages back beside it and index them again. Only emptying Trash SHALL remove part pages. When a Source page is removed outside the service, reconcile SHALL report its part pages and leave them.

#### Scenario: A trashed Source comes back whole
- **WHEN** a Source page with part pages is trashed
- **THEN** a search finds no text of its part pages
- **AND** after its recovery, its part pages are beside it and a search finds text from its last part
