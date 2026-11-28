## ADDED Requirements

### Requirement: A governed reclassification preview completes at the load target

A background reclassification preview SHALL run under its own build budget. On Linux, it SHALL complete the load fixture below and reuse the settled referrer folder's listing. It SHALL use full lookup if a folder's listing exceeds the cache budget. A concurrent Records read SHALL answer within its request budget. An interrupted or cancelled build SHALL return a typed public error and no report facts.

#### Scenario: A large governed preview completes

- **WHEN** an owner on Linux previews a Source in a vault with two settled, flat folders of 10,001 entries each
- **AND** one folder contains 10,000 governed referrers and one other Note, and the other contains Sources
- **THEN** the background build completes and every report page is released within its request budget
- **AND** a Records read during the build answers within its request budget

#### Scenario: An interrupted build returns a typed error

- **WHEN** the preview build is interrupted or cancelled
- **THEN** a request for its run returns a typed public error with no report, page or digest

### Requirement: A dependent governed referrer is released without rereading its bytes

On Linux, terminal release SHALL use held referrer evidence with live proof of inherited scopes for eligible container members, extraction parts and scene frames. It SHALL verify every other dependent row in full.

#### Scenario: A dependent referrer releases from held evidence

- **WHEN** a report page on Linux holds a governed referrer with a current container parent, extraction-part Source, or scene-frame video
- **THEN** release reads none of the referrer's bytes
- **AND** a parent retag that restricts disclosure, or a replaced container payload, withholds that row and refuses the page

#### Scenario: Sibling rows share the current physical parent

- **WHEN** eligible parts or frames share a settled parent file
- **THEN** release uses the same physical parent as ordinary membership
- **AND** release reads that parent once and rechecks its version before reusing its selector inputs

#### Scenario: An ineligible row takes the full path

- **WHEN** a row is a bridge page, a release-grant path or a grant's bridge dependency, or the referrer changed after the report was built
- **OR** release runs on native Windows
- **OR** a required proof is unavailable, including a part or frame whose parent belongs to the store or inherits its own scopes
- **THEN** release proves that row through the full path and refuses the page if its value changes
