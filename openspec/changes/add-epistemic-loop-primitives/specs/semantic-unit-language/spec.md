## ADDED Requirements

### Requirement: Prediction Is A Governed Core Semantic Kind
The semantic-unit language SHALL recognize `prediction` as a portable, code-owned core rich kind with the heading alias `Predictions`. It SHALL resolve through the same core kind ring as every other built-in kind, so registry resolution, kind filters, schema profiling, and governed writers accept it without any per-vault registry entry. A vault-owned registry entry that shadows the built-in `prediction` kind SHALL report the existing canonical-collision finding rather than silently overriding product vocabulary.

#### Scenario: A prediction heading parses as a governed rich unit
- **WHEN** a compiled page contains `## Prediction` followed by a substantive body
- **THEN** the parsed document contains one rich semantic unit whose kind is `prediction`

#### Scenario: The plural heading resolves to the singular kind
- **WHEN** a compiled page contains `## Predictions` followed by a substantive body
- **THEN** the parsed unit's kind is `prediction`

#### Scenario: Prediction needs no registry entry
- **WHEN** the core semantic-language registry resolves the kind label `prediction` against a vault with no semantic-language registry file
- **THEN** resolution succeeds with core status and a definition rather than an unregistered status

#### Scenario: A registry extension may not shadow the built-in kind
- **WHEN** a semantic-language registry proposal declares a `prediction` kind
- **THEN** validation reports a `canonical_collision` error finding naming that key

### Requirement: Verdict Is A Governed Categorical Unit-Metadata Key
The semantic-unit language SHALL recognize `verdict` as a governed rich unit-metadata key whose value is exactly one of `abandoned`, `confirmed`, `inconclusive`, `qualified`, or `refuted`, matched after Unicode NFKC normalization and casefolding. Any other value, including any numeric value, SHALL produce a deterministic source-addressed error whose remediation names the closed set and states that a numeric confidence is not a stored field. The normalized value SHALL be projected onto the parsed unit and included in its serialized form. A verdict SHALL NOT change a unit's lifecycle, standing, or rank.

#### Scenario: A valid verdict is normalized and projected
- **WHEN** a rich unit carries the metadata row `- verdict: Refuted`
- **THEN** the parsed unit's verdict is `refuted` and the document reports no error

#### Scenario: An unknown verdict value is rejected
- **WHEN** a rich unit carries the metadata row `- verdict: probably-wrong`
- **THEN** the document reports an `invalid_rich_verdict` error bound to that source line

#### Scenario: A numeric verdict is refused with the no-confidence reason
- **WHEN** a rich unit carries the metadata row `- verdict: 0.7`
- **THEN** the document reports an `invalid_rich_verdict` error whose remediation states that confidence is not a stored field

#### Scenario: A refuted unit keeps active standing
- **WHEN** a rich unit on an active page carries `- verdict: refuted`
- **THEN** the unit remains an ordinary active unit and its parent page status is unchanged

### Requirement: Check By Is A Governed Date Unit-Metadata Key
The semantic-unit language SHALL recognize `check_by` as a governed rich unit-metadata key whose value is a strict ISO calendar date spelled `YYYY-MM-DD`. A value that is not an exact ISO calendar date, including a timestamp or an abbreviated date, SHALL produce a deterministic source-addressed error. The validated value SHALL be projected onto the parsed unit and included in its serialized form.

#### Scenario: A valid check-by date is projected
- **WHEN** a rich unit carries the metadata row `- check_by: 2026-11-01`
- **THEN** the parsed unit's check-by value is `2026-11-01` and the document reports no error

#### Scenario: A non-canonical date is rejected
- **WHEN** a rich unit carries the metadata row `- check_by: 2026-1-1`
- **THEN** the document reports an `invalid_rich_check_by` error bound to that source line

#### Scenario: A timestamp is rejected
- **WHEN** a rich unit carries the metadata row `- check_by: 2026-11-01T09:00:00Z`
- **THEN** the document reports an `invalid_rich_check_by` error

### Requirement: Governed Unit Metadata Is Rich-Form Only And Reserved
Governed unit-metadata keys SHALL be available only in the rich authoring form, because compact observations carry no metadata rows. `verdict` and `check_by` SHALL be treated as reserved metadata rows, so a rich unit whose only content is governed metadata SHALL still be reported as an empty rich unit.

#### Scenario: A metadata-only prediction is still empty
- **WHEN** a page contains `## Prediction` followed only by `- verdict: refuted` and `- check_by: 2026-11-01`
- **THEN** the document reports an `empty_rich_unit` error for that heading

#### Scenario: Compact observations carry no governed metadata
- **WHEN** a compact observation line is parsed
- **THEN** the resulting unit has no verdict and no check-by value
