## ADDED Requirements

### Requirement: Restricted reads under a class policy skip the store wait

A restricted read under a policy with a `sensitivity` scope SHALL skip the pending-intent wait and lag notice. This is an exception to "A read reflects the writes acknowledged before it, or says what lags". The read SHALL use the pinned lineage proof. The existing release test for caller restriction SHALL also cover a connector acting as the owner. Other reads SHALL keep the ordinary store read-door behaviour.

#### Scenario: A pending intent under a class policy

- **WHEN** a restricted reader starts under a class policy while an intent is pending
- **THEN** the read decides from the pinned lineage proof without waiting for the intent or reporting lag
- **AND** an unrestricted owner read still uses the ordinary wait and lag notice

#### Scenario: A fenced owner reads a pending page

- **WHEN** a fenced connector acting as the owner reads before a class change has derived
- **THEN** the connector follows the restricted read rule and gets an affected pending page as missing
