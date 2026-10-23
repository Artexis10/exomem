# Spec Delta

## ADDED Requirements

### Requirement: Derived view reads are surface-equivalent
The generated `read_memory` signature SHALL expose an optional bounded string `continuation` across MCP, REST, Hosted, CLI, OpenAPI and in-process dispatch. The field SHALL be absent by default. A read that does not name a derived-view ref SHALL return the existing response when the field is absent, and SHALL refuse a supplied `continuation` with `INVALID_CONTINUATION`. A derived-view ref is `exomem://map`, `exomem://map/topic/<domain key>`, `exomem://map/unfiled`, `exomem://profile/topic/<domain key>` or `exomem://profile/anchor/<percent-encoded anchor ref>`.
- A read of a derived-view ref SHALL refuse `unit_ref`, `frontmatter_only`, `links`, `include_raw` and `include_history` with `INVALID_VIEW_READ_OPTIONS`.
- A malformed, unknown, expired or differently bound continuation SHALL return the existing `INVALID_CONTINUATION` refusal.
- A continuation bound to the same view and caller but to a different generation SHALL return `CONTINUATION_STALE`.
- No adapter SHALL decode the token into a caller-selectable offset, principal, session, purpose or generation.

The same trusted principal and request SHALL receive the same page and generation on every surface, modulo the registered transport framing exclusions. A continuation minted on one surface SHALL open the same next page on every other surface; its bytes carry the second it was minted and a signature over it, so two tokens for the same next page can differ.

#### Scenario: Cross-surface view parity
- **WHEN** the same trusted principal reads consecutive pages of `exomem://map` through MCP, REST, Hosted, CLI and in-process dispatch
- **THEN** each surface returns the same items, order and generation, and each surface's continuation opens the same next page on every other surface

#### Scenario: A page read after a catalogue change
- **WHEN** a caller presents a continuation after the view's generation changed
- **THEN** the read returns `CONTINUATION_STALE` and no items

### Requirement: Remember accepts a description and topics
The generated `remember` signature SHALL expose an optional one-line `summary` and an optional `topics` input, a value or a list of domain keys, on every generated surface. Absent inputs SHALL leave the written page and the response unchanged. A supplied `summary` SHALL be stored in the page's `summary` field, and supplied `topics` in its `topics` field, after the domains resolver accepts each value.

#### Scenario: Remember stores a description and topics
- **WHEN** an agent calls `remember` with `summary` and two `topics` values, one of them new
- **THEN** the page carries both fields, and the new value is registered in the same write
