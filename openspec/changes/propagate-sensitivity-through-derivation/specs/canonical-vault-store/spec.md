## ADDED Requirements

### Requirement: Restricted reads under a class policy skip the store wait

A non-owner read under a policy with a `sensitivity` scope SHALL skip the pending-intent wait and lag notice. This is an exception to "A read reflects the writes acknowledged before it, or says what lags". The read SHALL use the pinned lineage proof. Other reads SHALL keep the ordinary store read-door behavior.

#### Scenario: A pending intent under a class policy

- **WHEN** a restricted reader starts under a class policy while an intent is pending
- **THEN** the read decides from the pinned lineage proof without waiting for the intent or reporting lag
- **AND** an owner read still uses the ordinary wait and lag notice
