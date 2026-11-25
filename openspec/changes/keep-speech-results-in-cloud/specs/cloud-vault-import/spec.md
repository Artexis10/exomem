## ADDED Requirements

### Requirement: A window keeps a page with its part folder

In a series of windows, `exomem cloud import` SHALL sort the members of a part folder with their page, and SHALL NOT place a window boundary between a page and its part folder. For window sizing, a page and its part folder SHALL count as one member. Text SHALL still travel last.

#### Scenario: A boundary would split a page from its parts

- **WHEN** the next window's budget ends between a long transcript's page and its part folder
- **THEN** the page and its part folder travel together in one text window

#### Scenario: Many long transcripts in one folder

- **WHEN** one folder holds more long transcripts, with their part folders, than one window can carry
- **THEN** the series splits the folder between pages, and each window holds whole pages with their part folders
