## ADDED Requirements

### Requirement: A truncated read body under a release strip is withheld

A read that returns a truncated page body for a page under a release strip SHALL return no `body`, and SHALL keep `body_truncated`. A cut can end or start inside a link or path to a stripped source, and the strip does not search the text for it.

#### Scenario: A bounded body of a bridge page carries no fragment of a stripped source

- **WHEN** a caller reads a page under a release strip at a level that returns a bounded body
- **THEN** the response has no `body` and has `body_truncated` set to true
- **AND** no part of a link or path to the stripped source reaches the caller
