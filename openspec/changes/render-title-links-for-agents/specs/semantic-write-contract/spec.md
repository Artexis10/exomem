## ADDED Requirements

### Requirement: A shown link title is accepted in an edit and in an observation

On the public tool shape, `edit_note` (`replace_text` and `edits[]`) SHALL count `old_text` in the body as `read_page` shows it, and SHALL replace the stored bytes of each match. Only when that count is 0 SHALL it count the stored bytes, so a snippet in the stored form still applies, and then it SHALL NOT count an occurrence that covers only part of a link shown by title. A caller that is not on the public tool shape SHALL count the stored bytes only. A match that starts or ends inside a shown link SHALL be refused: with `STRING_NOT_FOUND` and a hint that `old_text` must include whole links when every occurrence is of that kind, and with `AMBIGUOUS_MATCH` and a hint to add surrounding text when others are outside links. `replace_all`, `AMBIGUOUS_MATCH` and the validate-only preview, with its context lines, SHALL count the same way. The page SHALL keep the stored link form after the edit, and a link that `new_text` repeats unchanged from the matched text SHALL keep its stored bytes, without normalization, where that link is live in the page as edited. `add_observation` and `edit_observation` SHALL store a `[[Title]]` in a unit as the same path form that a note body stores, and SHALL return the unresolved-link warnings that a body write returns.

#### Scenario: An edit copied from the shown body applies
- **WHEN** a caller copies a span with `[[Title]]` from `read_page` into `replace_text`
- **THEN** the edit applies to the stored bytes of that span
- **AND** the stored page keeps `[[path]]` for the link and changes nothing else

#### Scenario: A snippet is not matched inside a path the caller cannot see
- **WHEN** `old_text` occurs once in the body as shown, and also inside the stored path of a link
- **THEN** the edit counts one match and `replace_all` leaves the stored path unchanged

#### Scenario: A stored snippet is not matched inside a hidden path
- **WHEN** `old_text` is absent from the body as shown and occurs only inside the stored path of a link shown by title
- **THEN** the edit refuses with `STRING_NOT_FOUND` and the page is unchanged

#### Scenario: A snippet inside a link title and outside it is ambiguous
- **WHEN** `old_text` occurs once outside links and once inside the title of a shown link
- **THEN** the edit refuses with `AMBIGUOUS_MATCH` and a hint to add surrounding text, also under `replace_all`

#### Scenario: A span that cuts a link is refused
- **WHEN** `old_text` starts or ends inside a shown link
- **THEN** the edit refuses with `STRING_NOT_FOUND` and a hint to include whole links

#### Scenario: An observation with a title link stores the path form
- **WHEN** a caller adds or edits an observation whose text contains `[[Title]]` for an existing page
- **THEN** the stored unit holds the path form of that page
