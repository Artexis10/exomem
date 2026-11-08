## ADDED Requirements

### Requirement: Kinds and verdicts have one registry source
The built-in unit kinds and verdicts SHALL be entries of the shipped vocabulary pack, and vaults MAY add entries through a reviewed registry save. A kind entry SHALL carry a `traits` list whose values come from the closed kind-trait set that the registry schema declares and code implements. Code SHALL decide what a kind does only from its traits, never from a kind key. Verdict entries SHALL carry labels and descriptions and no behaviours, because a verdict is state. The shipped entries SHALL equal today's built-in kinds and verdicts, and the shipped traits SHALL reproduce the behaviour of the hard-coded kind sets they replace.

#### Scenario: A vault-added kind gains a trait
- **WHEN** a reviewed registry save adds kind `protocol` with trait `procedure`, and a page contains `## Protocol`
- **THEN** role review reads that unit as a procedure, with no code change

#### Scenario: A vault-added verdict changes no standing
- **WHEN** a vault adds verdict `retracted`, and a unit on an active page carries `- verdict: retracted`
- **THEN** the unit parses without error, its verdict is `retracted`, and it keeps active standing

#### Scenario: Shipped kind behaviour is unchanged
- **WHEN** trait reads replace the hard-coded assertion set and the kind tests in the claims reader and role review
- **THEN** activation counts, page claims and role-review results over a fixture vault are identical before and after

### Requirement: A belief is a unit owned by its holder
The language SHALL recognize the kind `belief`. A belief unit SHALL carry a governed `holder` that names an entity, a governed `belief` and an optional governed `basis`, and SHALL point at the claim it assesses through the core relation `assesses`. The `belief` value SHALL be a key or label of the belief-level registry (shipped entries speculative, plausible, likely and near-certain) or a probability from 0 to 1. A probability SHALL be valid only when the unit also carries a `basis`. A belief unit without a `holder`, or with a probability and no `basis`, SHALL produce a deterministic source-addressed error that names the missing key. Exomem SHALL keep the stated value as written, SHALL accept any basis text, SHALL NOT override a holder's belief, and SHALL keep the beliefs of several holders about one claim side by side. Exomem SHALL NOT write a belief unit on a holder's behalf. Calibration SHALL read a probability against the assessed prediction's `check_by` and verdict.

#### Scenario: A sourced forecast is accepted
- **WHEN** a belief unit carries `- holder: Platform team`, `- belief: 0.7` and `- basis: last six release cycles`, and `assesses` a prediction
- **THEN** the unit parses without error, and its belief is 0.7 held by the platform team

#### Scenario: An unsourced number is refused
- **WHEN** a belief unit carries `- holder: Platform team` and `- belief: 0.85` and no basis
- **THEN** the document reports an error that names `basis`

#### Scenario: A level needs no basis
- **WHEN** a belief unit carries `- holder: Finance lead` and `- belief: Likely`
- **THEN** the unit parses without error, its level resolves to `likely`, and the stored text stays `Likely`

#### Scenario: Two holders keep their own beliefs
- **WHEN** the platform team's belief unit says `likely` and the finance lead's says `speculative`, about the same claim
- **THEN** context presents both beliefs with their holders, and neither replaces the other

### Requirement: Asserter and mood keep views and norms distinct
A unit SHALL accept a governed `asserted_by` that names an entity, and a governed `mood` whose value is a key or label of the mood registry (shipped entries is, should, must and may). Context SHALL present a unit with `asserted_by` as that party's claim. Retrieval and context SHALL NOT merge units about one subject whose moods differ.

#### Scenario: A vendor's figure stays the vendor's
- **WHEN** a claim unit says the service has 99.99% uptime and carries `- asserted_by: Acme Hosting`
- **THEN** context presents the figure as Acme Hosting's claim

#### Scenario: A norm stays a norm
- **WHEN** one unit says deploys should need two approvals (mood should), and another says deploys need one approval (mood is)
- **THEN** context presents both with their moods, and neither replaces the other

### Requirement: Event times, quantities and inference modes are governed keys
The language SHALL recognize these governed unit-metadata keys:
- `at` on a `timeline_event`: an ISO date or date and time.
- `quantity_value`, `quantity_unit` and `quantity_error`: one measured quantity. `quantity_value` and `quantity_error` SHALL be numbers, and `quantity_unit` SHALL be present whenever `quantity_value` is.
- `inference_mode`: a key or label of the inference-mode registry (shipped entries deduction, induction, abduction, analogy, statistical inference and testimony), on a unit whose kind has the `assertion` trait.
An invalid value SHALL produce a deterministic source-addressed error that names the key and the expected form.

#### Scenario: A quantity without a unit is refused
- **WHEN** a finding carries `- quantity_value: 42` and no `quantity_unit`
- **THEN** the document reports an error that names `quantity_unit`

#### Scenario: An inductive finding records its mode
- **WHEN** a finding carries `- inference_mode: induction`
- **THEN** the parsed unit's inference mode is `induction`

### Requirement: Registry labels are aliases in any language
Every entry of the kind, verdict, mood, belief-level, inference-mode and dimension registries SHALL carry `labels` keyed by language. Parsing SHALL accept a label as an alias of its entry's key. The registry check SHALL refuse a label that collides with another entry's key or label in the same registry.

#### Scenario: A Finnish heading is the same kind
- **WHEN** the kind `claim` carries the label `Väite` for Finnish, and a page contains `## Väite`
- **THEN** the unit parses as kind `claim`

## MODIFIED Requirements

### Requirement: Prediction Is A Governed Core Semantic Kind
The semantic-unit language SHALL recognize `prediction` as a portable, product-owned core rich kind in the shipped vocabulary pack, with the heading alias `Predictions`. It SHALL resolve through the same core kind ring as every other built-in kind, so registry resolution, kind filters, schema profiling, and governed writers accept it without any per-vault registry entry. A vault-owned registry entry that shadows the built-in `prediction` kind SHALL report the existing canonical-collision finding rather than silently overriding product vocabulary.

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
The semantic-unit language SHALL recognize `verdict` as a governed rich unit-metadata key whose value is exactly one key or label of the verdict registry (shipped entries `abandoned`, `confirmed`, `inconclusive`, `qualified` and `refuted`), matched after Unicode NFKC normalization and casefolding. Any other value, including any numeric value, SHALL produce a deterministic source-addressed error whose remediation names the registered verdicts and states that a degree of belief belongs in a belief unit. The normalized value SHALL be projected onto the parsed unit and included in its serialized form. A verdict SHALL NOT change a unit's lifecycle, standing, or rank.

#### Scenario: A valid verdict is normalized and projected
- **WHEN** a rich unit carries the metadata row `- verdict: Refuted`
- **THEN** the parsed unit's verdict is `refuted` and the document reports no error

#### Scenario: An unknown verdict value is rejected
- **WHEN** a rich unit carries the metadata row `- verdict: probably-wrong`
- **THEN** the document reports an `invalid_rich_verdict` error bound to that source line

#### Scenario: A numeric verdict is refused with the no-confidence reason
- **WHEN** a rich unit carries the metadata row `- verdict: 0.7`
- **THEN** the document reports an `invalid_rich_verdict` error whose remediation states that a confidence is not a verdict and points to a belief unit

#### Scenario: A refuted unit keeps active standing
- **WHEN** a rich unit on an active page carries `- verdict: refuted`
- **THEN** the unit remains an ordinary active unit and its parent page status is unchanged

### Requirement: Governed Unit Metadata Is Rich-Form Only And Reserved
Governed unit-metadata keys SHALL be available only in the rich authoring form, because compact observations carry no metadata rows. `verdict`, `check_by`, `holder`, `belief`, `basis`, `asserted_by`, `mood`, `at`, `quantity_value`, `quantity_unit`, `quantity_error` and `inference_mode` SHALL be treated as reserved metadata rows, so a rich unit whose only content is governed metadata SHALL still be reported as an empty rich unit. For one release after a key becomes reserved, an invalid value of that key SHALL produce a warning instead of an error, so pages that already use the key as an ordinary row keep parsing.

#### Scenario: A metadata-only prediction is still empty
- **WHEN** a page contains `## Prediction` followed only by `- verdict: refuted` and `- check_by: 2026-11-01`
- **THEN** the document reports an `empty_rich_unit` error for that heading

#### Scenario: Compact observations carry no governed metadata
- **WHEN** a compact observation line is parsed
- **THEN** the resulting unit has no verdict and no check-by value

#### Scenario: A newly reserved key warns during its first release
- **WHEN** a page written before this change carries `- mood: cheerful` on a claim in the first release that reserves `mood`
- **THEN** the document reports a warning that names `mood` and the registered moods, and no error
