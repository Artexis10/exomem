## ADDED Requirements

### Requirement: A governed reclassification preview completes at the load target

A background reclassification preview SHALL run under its own build budget. On Linux, it SHALL complete for 10,000 governed referrers in one folder, resolving their physical names without listing that folder once per referrer. A concurrent Records read SHALL answer within its own request budget while the build runs. An interrupted or cancelled build SHALL return a typed public error and no report facts.

#### Scenario: A large governed preview completes

- **WHEN** an owner previews a Source with 10,000 governed referrers in one folder on Linux
- **THEN** the background build completes and every report page is released within its request budget
- **AND** a Records read during the build answers within its request budget

#### Scenario: An interrupted build returns a typed error

- **WHEN** the preview build is interrupted or cancelled
- **THEN** a request for its run returns a typed public error with no report, page or digest

### Requirement: A dependent governed referrer is released without rereading its bytes

On Linux, the terminal release SHALL release a referrer that inherits scopes from a container parent, an extraction-part Source, or a scene-frame video from held evidence for its own bytes, plus current proof of each dependency read from that dependency. It SHALL use full verification when the referrer or any dependency changed, for parts and frames with a container binding, for bridge rows, and on native Windows.

#### Scenario: A dependent referrer releases from held evidence

- **WHEN** a report page on Linux holds a governed referrer with a current container parent, extraction-part Source, or scene-frame video
- **THEN** release reads none of the referrer's bytes
- **AND** a retagged parent, or a replaced container payload, withholds that row and refuses the page

#### Scenario: A bridge row or a changed referrer takes the full path

- **WHEN** a row is a bridge page, a release-grant path or a grant's bridge dependency, or the referrer itself changed after the report was built
- **THEN** release proves that row through the full path and refuses the page if its value changes
