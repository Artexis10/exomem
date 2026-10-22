# Spec Delta

## ADDED Requirements

### Requirement: Derived view reads are surface-equivalent
The generated `read_memory` signature SHALL expose an optional bounded string `continuation` across MCP, REST, Hosted, CLI, OpenAPI and in-process dispatch. The field SHALL be absent by default and SHALL NOT change the response of a read that does not name a derived-view ref. A derived-view ref is `exomem://map`, `exomem://map/topic/<domain key>`, `exomem://map/unfiled`, `exomem://profile/topic/<domain key>` or `exomem://profile/anchor/<percent-encoded anchor ref>`.
- A read of a derived-view ref SHALL refuse `unit_ref`, `frontmatter_only`, `links`, `include_raw` and `include_history` with `INVALID_VIEW_READ_OPTIONS`.
- A malformed, unknown, expired or differently bound continuation SHALL return the existing `INVALID_CONTINUATION` refusal.
- A continuation bound to the same view and caller but to a different generation SHALL return `CONTINUATION_STALE`.
- No adapter SHALL decode the token into a caller-selectable offset, principal, session, purpose or generation.

The same trusted principal and request SHALL receive the same page, generation and continuation bytes on every surface, modulo the registered transport framing exclusions.

#### Scenario: Cross-surface view parity
- **WHEN** the same trusted principal reads consecutive pages of `exomem://map` through MCP, REST, Hosted, CLI and in-process dispatch
- **THEN** each surface returns the same items, order, generation and continuation bytes

#### Scenario: A page read after a catalogue change
- **WHEN** a caller presents a continuation after the view's generation changed
- **THEN** the read returns `CONTINUATION_STALE` and no items

### Requirement: Remember accepts a description and topics
The generated `remember` signature SHALL expose an optional one-line `summary` and an optional `topics` input, a value or a list of domain keys, on every generated surface. Absent inputs SHALL leave the written page and the response unchanged. A supplied `summary` SHALL be stored in the page's `summary` field, and supplied `topics` in its `topics` field, after the domains resolver accepts each value.

#### Scenario: Remember stores a description and topics
- **WHEN** an agent calls `remember` with `summary` and two `topics` values, one of them new
- **THEN** the page carries both fields, and the new value is registered in the same write
