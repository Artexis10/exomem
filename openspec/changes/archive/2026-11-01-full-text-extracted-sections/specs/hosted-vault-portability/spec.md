# Part pages in export and deletion delta

## ADDED Requirements

### Requirement: Export keeps hidden extraction parts

The durability registry SHALL classify a hidden part page of a Source's extraction as canonical vault content, so export and restore keep it. Other unregistered hidden state SHALL stay disposable.

#### Scenario: A part page is exported
- **WHEN** the registry classifies the path of a part page
- **THEN** the class is canonical
- **AND** an unregistered hidden file beside it stays disposable

### Requirement: Deleting a Source removes its part pages

Removing a Source page SHALL remove its hidden part pages and their index rows, as it removes a video's scene-frame derivatives.

#### Scenario: A deleted Source leaves no part
- **WHEN** a Source page with part pages is removed
- **THEN** its hidden folder and the index rows of its part pages are gone
