# epistemic-state-bench — delta

## ADDED Requirements

### Requirement: Loop-closure scenario families f15–f19

The pre-registration SHALL define five additional scenario families, each with kind, public-coverage statement, core assertions, and acceptance predicates in the existing registry style:

- `f15 prediction_window` (corpus): ingest a hypothesis and a dated prediction; advance the clock past the window. Core assertions: `due_prediction_surfaced` (the overdue prediction is queryable/surfaced through the documented interface), and after a resolving observation, `verdict_state_retrievable`.
- `f16 plan_record_linkage` (corpus + operational): a plan bound to a records view; later records diverge from intent. Core assertions: `divergence_surfaced_without_mutation` — divergence is surfaced for review AND nothing auto-mutates the plan.
- `f17 derivation_collapse` (corpus): one source, two derived notes, a third citing both as independent support. Core assertion: `support_collapse_inspectable` — the support structure is inspectable so double-counting is visible.
- `f18 negative_result_retention` (corpus): a refuted hypothesis with refuting evidence. Core assertion: `refuted_retrievable_at_full_standing` — retrievable, distinguishable from active AND from superseded, never demoted for being refuted.
- `f19 loop_composite` (operational journey): a goal → hypothesis → prediction → intervention → records → review → revision journey across at least three sessions and one engine restart; scores the system, not retrieval.

#### Scenario: Families are deterministic and judge-free

- **WHEN** any f15–f19 assertion is evaluated
- **THEN** it runs against neutral state snapshots with the harness's single fixed matching rule, and no judge may overturn a deterministic result

#### Scenario: Acceptance predicates admit structurally different products

- **WHEN** a product satisfies a family's property through any of at least two structurally different documented representations
- **THEN** the assertion passes, and a product whose own materials claim the property scores fail (not not_applicable) when it is absent

### Requirement: Post-ratification amendment governance

Any change to families, assertions, predicates, or gates after ratification SHALL land only as a dated, reasoned §7 Amendment entry accompanied by a `preregistration-amendment-receipt.v1` acknowledged by the founder.

#### Scenario: Amendment without receipt is invalid

- **WHEN** the working pre-registration differs from the ratified base without a complete receipted amendment chain
- **THEN** the drift check reports a named failure and comparative runs refuse

#### Scenario: Catastrophic-set candidacy is adjudicated at acknowledgment

- **WHEN** an amendment proposes adding an assertion to the catastrophic set (f18's `refuted_retrievable_at_full_standing` is proposed)
- **THEN** the acknowledgment records the founder's accept-or-strike decision explicitly

#### Scenario: Pending acknowledgment withholds only the amended families

- **WHEN** the f15–f19 amendment receipt is receipted but not yet acknowledged
- **THEN** f15–f19 MUST NOT back a comparative run, score, or published claim, AND f01–f14 runs, the contract identity, the amendment chain and every consumer that names no amended family proceed unchanged

### Requirement: Registration is not release

The frozen registry SHALL mirror §1 and §2 of the amended pre-registration, f15–f19 and their assertions included, so a drift between code and document is a named failure. Being registered SHALL NOT make a family runnable: a family introduced by an amendment whose receipt is unacknowledged SHALL be refused at every surface that runs, scores, or records it — scenario loading, scenario evaluation, family-row assembly, run-manifest construction, and manifest loading for a claim. The refusal SHALL carry the typed pending-acknowledgment error naming the amendment sequence and the family.

Scenario loading is the primary choke point: because no `Scenario` for a withheld family can be constructed through the loader, no downstream consumer can receive one. The remaining surfaces cover objects built without the loader.

The released/withheld decision SHALL be answerable from the working receipt bytes alone, without Git history, so an ordinary fixture load does not depend on a checkout's history being present. Unreadable receipts SHALL fail closed, withholding every family an amendment introduced.

#### Scenario: An amended family is registered but not runnable

- **WHEN** f15–f19 are present in the §1 registry and the sequence-1 receipt is still pending
- **THEN** loading, evaluating, or scoring a scenario for any of those families refuses with the typed pending-acknowledgment error, while f01–f14 proceed unchanged

#### Scenario: The code mirror cannot drift from the receipt chain

- **WHEN** the registry's amendment-introduced family mapping is compared against the families derived from the receipt chain
- **THEN** they MUST be equal, so the cheap receipt-bytes check cannot silently disagree with the Git-derived identity

### Requirement: Ratified-identity drift check

The suite SHALL verify that the working pre-registration file is byte-identical to the ratified base sha, or equals the base evolved through the receipted amendment chain (each receipt's amended sha matching the file state after its amendment, the final receipt matching the current file).

#### Scenario: Stale or silently-edited pre-registration is a named finding

- **WHEN** the working file matches neither the ratified base nor the receipted chain
- **THEN** the check fails with a finding naming the expected and actual identities, and no comparative table may publish
