## ADDED Requirements

### Requirement: Unicode Display Titles Are Lossless

Every newly created governed Markdown page SHALL store the exact caller-supplied display title as a valid Unicode `title` frontmatter scalar and SHALL expose that title in a canonical H1. Title storage MUST NOT depend on filename transliteration or the availability of a language-specific package.

#### Scenario: Japanese title survives a compiled-note write

- **WHEN** a caller creates a note titled `睡眠`
- **THEN** the resulting frontmatter contains a YAML string equal to `睡眠`
- **AND** the body exposes `睡眠` as its canonical H1

#### Scenario: YAML-significant Unicode title remains parseable

- **WHEN** a title contains Unicode plus YAML-significant punctuation such as a colon or hash
- **THEN** the written frontmatter parses successfully and yields the exact original title

### Requirement: Display Title And Filename Slug Are Independent

Every title-derived write operation SHALL accept an optional explicit filename `slug`. An explicit slug SHALL be ASCII lowercase kebab-case, SHALL be capped at 100 characters, and SHALL determine only the new filename component. The display title SHALL remain unchanged. Omitting `slug` SHALL preserve the existing automatic filename behavior for compatibility and SHALL warn when non-ASCII input was transliterated lossily.

#### Scenario: Explicit English slug with Japanese title

- **WHEN** a caller creates a note titled `睡眠` with slug `sleep`
- **THEN** the filename component is `sleep`
- **AND** the stored display title remains `睡眠`

#### Scenario: Invalid explicit slug is rejected

- **WHEN** a caller supplies a slug containing whitespace, uppercase letters, Unicode characters, path separators, or more than 100 characters
- **THEN** the write is rejected with a stable validation error before any vault file changes

#### Scenario: Existing compatibility default remains

- **WHEN** a caller omits `slug`
- **THEN** the filename is derived with the existing automatic slug policy
- **AND** no existing page is renamed during upgrade or reconciliation

### Requirement: One Display-Title Resolution Contract

All read and presentation surfaces SHALL resolve a page title using `frontmatter title`, then the first H1, then a humanized filename stem. Search/find, fetch/get metadata, indexes, and wikilink title resolution MUST use the same precedence for the same page.

#### Scenario: Structured title wins consistently

- **WHEN** a page has frontmatter title `睡眠`, an H1 `Legacy heading`, and filename `shui-mian.md`
- **THEN** every read/presentation surface returns `睡眠` as the display title

#### Scenario: Legacy title-less page remains readable

- **WHEN** a legacy page has no frontmatter title but has an H1
- **THEN** every read/presentation surface returns the H1
- **AND** a page with neither title nor H1 falls back to its humanized filename without being rewritten

### Requirement: Imported Frontmatter Is YAML-Safe

Adoption and import writers SHALL serialize user-controlled provenance paths and titles as YAML scalars rather than interpolating them unquoted.

#### Scenario: Imported path contains a colon

- **WHEN** an imported path contains `Step2: Paste your conversation.md`
- **THEN** the resulting source frontmatter parses successfully
- **AND** `imported_from` equals the exact original path
