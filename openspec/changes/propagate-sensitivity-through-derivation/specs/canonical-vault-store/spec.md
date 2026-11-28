## ADDED Requirements

### Requirement: Restricted reads under a class policy skip the store wait

A restricted read under a policy with a `sensitivity` scope SHALL skip the pending-intent wait and lag notice. This includes a fenced owner. The read SHALL prove current lineage from the request's one store snapshot. The ordinary graph readiness checks still apply. Other reads SHALL keep the ordinary store read-door behaviour.

#### Scenario: A pending intent under a class policy

- **WHEN** a restricted reader starts under a class policy while an intent is pending
- **THEN** the read decides from the pinned lineage proof without waiting for the intent or reporting lag
- **AND** an unrestricted owner read still uses the ordinary wait and lag notice

#### Scenario: A fenced owner reads a pending page

- **WHEN** a fenced connector acting as the owner reads before a class change has derived
- **THEN** the connector follows the restricted read rule and gets an affected pending page as missing
