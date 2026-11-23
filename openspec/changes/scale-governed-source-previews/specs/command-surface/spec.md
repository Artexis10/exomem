## ADDED Requirements

### Requirement: A governed reclassification preview completes at the load target

A background reclassification preview on a governed vault SHALL complete for 10,000 governed referrers in one folder. It SHALL resolve each referrer's physical name without listing that folder once per referrer, keep its progress across its own budget, and discard its report only when a change can alter the report. A concurrent Records read SHALL answer within its own request budget while the build runs. An interrupted or cancelled build SHALL return a typed public error and no report facts.

#### Scenario: A large governed preview completes

- **WHEN** an owner previews a Source with 10,000 governed referrers in one folder
- **THEN** the background build completes and every report page is released within its request budget
- **AND** a Records read during the build answers within its request budget

#### Scenario: An unrelated generation change keeps the report

- **WHEN** an index or governance generation that cannot change the report moves during the build
- **THEN** the completed report stays releasable
- **AND** a change to a referrer, the Source or the requesting owner's current authorization still refuses it

#### Scenario: An interrupted build returns a typed error

- **WHEN** the preview build is interrupted or cancelled
- **THEN** a request for its run returns a typed public error with no report, page or digest

### Requirement: A dependent governed referrer is released without rereading its bytes

The terminal release SHALL release a referrer that has a container parent or bridge dependency from held evidence for its own bytes, plus current proof of each dependency. It SHALL take the full path when the referrer or any dependency changed.

#### Scenario: A dependent referrer releases from held evidence

- **WHEN** a completed report page holds a governed referrer with a current container parent
- **THEN** release reads none of the referrer's bytes
- **AND** a changed parent sends that row down the full path

#### Scenario: A bridge dependency or a changed referrer takes the full path

- **WHEN** a held referrer's bridge dependency changed, or the referrer itself changed, after the report was built
- **THEN** release proves that row through the full path and refuses the page if its value changes
