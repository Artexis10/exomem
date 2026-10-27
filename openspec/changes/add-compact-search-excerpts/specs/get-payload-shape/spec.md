## ADDED Requirements

### Requirement: A truncated read body under a release strip is withheld

A read of a page under a release strip that returns a truncated body SHALL return no `body` and SHALL keep `body_truncated`. A read that returns the whole body SHALL keep it, with each identity of the stripped source removed. The excerpt-level read SHALL set `body_truncated` only when it cut the body. A cut can start or end inside a link or path to a stripped source, and the strip does not search the text for it.

#### Scenario: A truncated body of a bridge page is withheld

- **WHEN** a caller reads a page under a release strip at a level that returns a bounded body, and the body is cut
- **THEN** the response has no `body` and has `body_truncated` set to true
- **AND** no part of a link or path to the stripped source reaches the caller

#### Scenario: A whole body of a bridge page is kept without the stripped source

- **WHEN** a caller reads a page under a release strip at that level, and the body is not cut
- **THEN** the response carries the body with each identity of the stripped source removed and has `body_truncated` set to false
