## ADDED Requirements

### Requirement: A shown link title is accepted in an edit and in an observation

`edit_note` (`replace_text` and `edits[]`) SHALL count `old_text` in the stored body first, as before. Only when the count is 0 SHALL it count `old_text` in the body as `read_page` shows it, and SHALL replace the stored bytes of each match. A match that starts or ends inside a shown link SHALL be refused with `STRING_NOT_FOUND` and a hint that `old_text` must include whole links. `replace_all`, `AMBIGUOUS_MATCH` and the validate-only preview SHALL count the same way. The page SHALL keep the stored link form after the edit. `add_observation` and `edit_observation` SHALL store a `[[Title]]` in a unit as the same path form that a note body stores.

#### Scenario: An edit copied from the shown body applies
- **WHEN** a caller copies a span with `[[Title]]` from `read_page` into `replace_text`
- **THEN** the edit applies to the stored bytes of that span
- **AND** the stored page keeps `[[path]]` for the link and changes nothing else

#### Scenario: A span that cuts a link is refused
- **WHEN** `old_text` starts or ends inside a shown link
- **THEN** the edit refuses with `STRING_NOT_FOUND` and a hint to include whole links

#### Scenario: An observation with a title link stores the path form
- **WHEN** a caller adds or edits an observation whose text contains `[[Title]]` for an existing page
- **THEN** the stored unit holds the path form of that page
