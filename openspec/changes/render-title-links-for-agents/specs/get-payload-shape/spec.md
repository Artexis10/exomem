## ADDED Requirements

### Requirement: Public reads show links by title

`read_page` on the public tool shape SHALL show each link in the body and in the unit texts of a page that is released in full as `[[Title]]`, with its anchor and alias kept, when the target is visible to the caller, the title of the target contains none of `/`, `#`, `|`, `[`, `]`, a backtick or a newline, and writing `[[Title]]` back resolves to the same page. Every other link SHALL keep its stored form. A link in code, a fenced block or an origin carrier SHALL keep its stored form. The title of a target that the caller may not see SHALL NOT be read. `content_hash` and `id` SHALL stay computed from the stored bytes.

The stored form SHALL also stay in these cases:
- No current link resolver is resident in memory, as on a cold read. The read SHALL NOT build one, and `edit_note` SHALL accept both forms, so a cold read costs the agent no retry.
- The page is a raw type (Source, Evidence), which keeps its recorded text.
- The release level of the page for the caller is below full.
- The caller is not on the public tool shape: REST and CLI replies SHALL keep the stored form.

#### Scenario: A link is shown by title
- **WHEN** a caller on the public tool shape reads a page whose body links another page by path
- **THEN** the body shows `[[Title]]` for that link
- **AND** the stored page, the `content_hash` and the `id` are unchanged

#### Scenario: An ambiguous title keeps the stored form
- **WHEN** two visible pages share a title and a body links one of them
- **THEN** the body shows the stored link for it

#### Scenario: A withheld target keeps the stored form
- **WHEN** a restricted caller reads a page that links a page the caller may not see
- **THEN** the body shows the stored link for it
- **AND** the response carries no title of the withheld page

#### Scenario: A read with no resident resolver keeps the stored form
- **WHEN** a caller on the public tool shape reads a page before any current link resolver is resident
- **THEN** the body shows the stored link
- **AND** an `edit_note` with that stored text applies

#### Scenario: A REST read keeps the stored form
- **WHEN** a caller reads the same page through REST or the CLI
- **THEN** the body shows the stored link
