## ADDED Requirements

### Requirement: Observe-Memory Refusals Distinguish Legacy Schema From Policy

When `observe_memory` refuses a target because it is not a writable compiled
page, it SHALL keep the existing refusal code
(`OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`, `FRONTMATTER_REQUIRED` for a
frontmatterless target, or `OUTSIDE_GOVERNED_ROOT` for a target whose real
page lives outside the governed Knowledge Base root) and SHALL distinguish
the cause in the accompanying `remediation` and `resolution` fields rather
than leaving the caller to improvise:

- An untyped page (frontmatter present, `type` missing or not in
  `COMPILED_TYPES`), a frontmatterless page, or a page outside the governed
  Knowledge Base root SHALL receive a `remediation` sentence that names the
  concrete safe move — the exact `suggested_path`, the `part_of` target, the
  in-place alternative, and the migration pointer, in that order — plus a
  structured `resolution` of shape `{"action": "create-dated-child",
  "suggested_path": <string>, "link": {"relation": "part_of", "target":
  <legacy page path>}, "in_place": <string>, "migration": <string>}`.
  `suggested_path` SHALL colocate in the legacy page's own directory when
  that page is under the governed Knowledge Base root, and SHALL otherwise
  resolve under `Notes/Research/<parent-dir-slug>/` inside the Knowledge
  Base. `suggested_path` SHALL name a path that does not already exist,
  de-collided the same way page creation de-collides a filename.
- A target whose access tier is not read-write (readonly, excluded, or any
  other non-read-write tier) SHALL receive the same code, a `remediation` that
  names the tier and states the page is policy-protected, and no `resolution`.
- `_COMPILED_PAGE_TYPES` SHALL NOT be widened by this requirement. `entity`
  remains structural, not compiled.
- No existing error code SHALL be renamed, and no new error code SHALL be
  introduced to carry this distinction.

#### Scenario: Untyped page inside the Knowledge Base offers a dated-child resolution
- **WHEN** `observe_memory` targets a page with frontmatter but no compiled
  `type`, under `Knowledge Base/`
- **THEN** it refuses with `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`
- **AND** the raised error's `remediation` sentence names the exact
  `suggested_path` and the `part_of` target, and its `resolution.suggested_path`
  is a dated child in the same directory with `link` targeting the legacy
  page at relation `part_of`

#### Scenario: Frontmatterless page gets the same shape of answer
- **WHEN** `observe_memory` targets a page with no frontmatter delimiters
- **THEN** it refuses with `FRONTMATTER_REQUIRED`
- **AND** the error carries the same shape of `remediation` and `resolution`
  as an untyped page, without synthesizing frontmatter on the page itself

#### Scenario: A page outside the Knowledge Base root gets the same shape of answer
- **WHEN** `observe_memory` targets a page whose real file lives outside the
  governed Knowledge Base root
- **THEN** it refuses with `OUTSIDE_GOVERNED_ROOT`
- **AND** the error carries the same shape of `remediation` and `resolution`
  as an untyped page, with `resolution.suggested_path` under
  `Notes/Research/<parent-dir-slug>/` inside the Knowledge Base and `link`
  targeting the outside page at relation `part_of`

#### Scenario: Policy-protected tier gets tier remediation and no resolution
- **WHEN** `observe_memory` targets a page whose access tier is `readonly` or
  `excluded`
- **THEN** it refuses with `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`
- **AND** the `remediation` names the tier and states the page is
  policy-protected
- **AND** no `resolution` is present

#### Scenario: A pre-existing same-dated child shifts the suggested path
- **WHEN** a page already exists at the path `suggested_path` would otherwise
  name
- **THEN** the offered `suggested_path` is de-collided (the `-2` form, or
  further, exactly as ordinary page creation de-collides a filename) rather
  than naming a path that already exists

#### Scenario: Compiled-page type set is not widened
- **WHEN** this requirement's resolution logic runs
- **THEN** `_COMPILED_PAGE_TYPES` (`semantic-write-contract`'s
  `COMPILED_TYPES`) is unchanged and `entity` remains excluded from it
