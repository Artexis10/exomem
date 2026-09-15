# epistemic-utility-regression (delta)

## ADDED Requirements

### Requirement: Context-activation variants under the released utility family
The utility action family SHALL admit context-activation variants as a sibling variant tuple that the
cost-metered utility runner does not include in its default scope, each variant binding one cold-start case or twin to a seeded action
world whose outcome is observable state, and SHALL admit the arms control, raw recall,
compiler, nudged recall and oracle packet as paired arms of the same episode identity.
Adding a variant SHALL NOT change the family's registration, receipt or ratified
identity, SHALL keep the existing variants' seeds and outcomes byte-identical, and
SHALL keep paid replays opt-in and bounded under the existing paid-probe rule.

#### Scenario: Existing variants are unchanged
- **WHEN** the context-activation variant tuple is added beside the existing one
- **THEN** the seeds, oracles and paired outcomes of `helpful_history`,
  `self_contained` and `stale_distractor` are byte-identical to the previous release

#### Scenario: Compiler arm is paired with its controls
- **WHEN** a context-activation variant runs
- **THEN** the control, raw-recall, nudged-recall and oracle arms run on the same
  episode identity and seed as the compiler arm, and each metric is published with its
  dual
