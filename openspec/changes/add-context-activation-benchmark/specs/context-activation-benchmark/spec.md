# context-activation-benchmark

## ADDED Requirements

### Requirement: Deterministic activation audit instrument
The repository SHALL provide a model-free activation audit that runs in CI over a
seeded synthetic corpus shaped like nine cold-start cases and nine frequency-matched
negative twins, with gold anchors, poison anchors, expected roles, must-include and
must-exclude facts and the expected abstention status authored per case before any
retrieval is run. The padded case (C9) SHALL be the grill case's turn (C2) on a corpus
tree padded with distractors that carry the domain's vocabulary, its twin (T9) SHALL be
C2's twin turn on that same padded tree, and padding robustness SHALL compare C9
against C2's own score on the unpadded tree. Every fixture and every packet SHALL
record the corpus tree (its distractor count) it belongs to, and a padding comparison
whose two packets share a tree SHALL fail. Gold and poison facts SHALL be
pre-registered phrases; the model-free intersection SHALL match on content words after
stopword removal and light stemming, SHALL require at least one discriminating content
word not shared with any other fact of the same case and at least two overlapping
content words, where a negation word never counts and a word counts only when both
sides agree on whether it is negated (a word is negated when the content word before
it is a negation word), so that a denial of a fact is not credited as asserting it, and the fixture suite SHALL prove that the confusable fact
sets (the same-first-name persons, the three AI-search hubs and the supersession
ancestors) are pairwise non-matching. A fact MAY carry several pre-registered
phrasings; the phrasing set, like the gold and poison lists, is frozen by the fixture
digest at the first run and a later edit voids that run. Polarity beyond the negation
rule is not modelled, and the blind intersection is a secondary metric that never
gates falsification. The audit SHALL score each case and each anchor kind separately,
SHALL publish every metric with its dual from the same run, SHALL publish no weighted
aggregate at any level, SHALL carry no epistemic-bench registry row and no comparative
claim, and SHALL include a mechanism-removal test that turns the audit red when the
compiler is disabled.

#### Scenario: Gold is authored before retrieval
- **WHEN** a fixture's gold or poison list is edited after a run manifest that
  references the fixture digest exists
- **THEN** the digest no longer matches and the run is reported void, not rescored

#### Scenario: Mechanism removal is visible
- **WHEN** the audit runs with `EXOMEM_DISABLE_WORKING_SET` set
- **THEN** every positive case reports failed activation and the audit exits red

### Requirement: Pre-registered thresholds
The audit SHALL pin, per case and per anchor kind: activation recall on gold at least
0.90; activation precision at least 0.80, computed over every ref the packet surfaces
as a resolved anchor, unit or pointer and excluding superseded ancestors the packet
credits as marked, with a poison count of zero; `resolved` false activation on twins
equal to zero, where a false activation is a `resolved` anchor, unit, pointer,
current-state entry or ambiguity candidate outside the twin's own gold set (a twin
designed to resolve on a narrow gold of its own is not a false activation); `partial`
activation on twins whose expected status is `unresolved` limited to at most one twin
per run, reported separately (a twin without gold of its own cannot hedge as
`ambiguous`, because naming any ambiguity candidate is itself a false activation; a
twin whose expected status is `ambiguous` is scored on that expectation, not as
hedging); the no-memory case resolving to `unresolved` with zero anchors and zero
injected characters; the supersession case carrying the active head and marking every
superseded ancestor; a current-state statement of at most 200 characters, a longer one
failing the case as a packet-contract violation; packet size p50 at most 900 tokens
and p95 at most 1,500 with a hard refusal above 2,000, percentiles taken ceil-rank so
that over the eighteen packets of one run the p95 bound is the run's maximum and the
hard refusal is reached only by larger runs; and two latency bounds: the compiler's own `working_set.*` stages at p50 at most
800 ms and p95 at most 2,500 ms on the reference corpus, and end-to-end
`activate_context` no slower than the measured naive-path baseline on the same cell
(p50 10,745 ms / p95 16,488 ms over 18 nonce queries on the personal cell on
2026-09-16, recorded in the fixture manifest), the end-to-end bound to be tightened
once `accelerate-governed-recall` lands.

#### Scenario: Twin false resolution fails the audit
- **WHEN** any twin turn yields an anchor with status `resolved` that is not in the
  twin's gold set
- **THEN** the audit reports the case as failed regardless of every other metric

#### Scenario: Hedged twin activation is reported, not punished as resolution
- **WHEN** at most one twin expected to be `unresolved` yields only `partial` anchors
  and no units, pointers, current state or ambiguity candidates
- **THEN** the audit passes the twin and reports the count under its own metric

#### Scenario: Confusable facts never match each other
- **WHEN** the intersection is asked whether an assertion of one same-first-name
  person's fact matches the other person's fact, or one AI-search hub's fact matches
  another hub's, or one supersession ancestor's fact matches the other's
- **THEN** it reports no match

#### Scenario: Padding comparison refuses packets from one tree
- **WHEN** the padded case and its unpadded reference are scored from packets that
  record the same distractor count
- **THEN** padding robustness fails with a reason naming the shared tree

### Requirement: Agent arms and controls
The agent-in-the-loop layer SHALL compare, with model, prompt, effort, tools and
clock held constant: A1 control with Exomem absent, carrying no Exomem environment
variable, no MCP configuration and no tool allowlist flag; A2 raw recall with `ask_memory`
available and no instruction; A3 the compiler packet injected pre-inference or
obtained by one mandated `activate_context` call; A4 nudged recall, which is A2 plus a
search-first instruction; and A5 an oracle packet hand-written per case as an
instrument-only ceiling. A case for which A5 does not beat A1 SHALL be struck from
the report and never scored against the compiler. A3 SHALL be compared against A4, not
only A2, so that availability and query formulation are separated.

#### Scenario: Unwinnable case is struck
- **WHEN** the oracle-packet arm fails to beat the control arm on a case across the
  pre-registered repeats
- **THEN** the case is marked struck in the report and contributes to no verdict

### Requirement: Agent-layer scoring without gold leakage
Each case SHALL carry a pre-registered reminder turn — the literal correction the user
would send if the agent missed the context — and the primary agent-layer score SHALL be
whether the first response already reflected that fact. A grader that sees only the
turn and the response SHALL list asserted facts and requested facts; a model-free step
SHALL intersect those lists with gold and poison. A per-case 0/1/2 usefulness rubric
SHALL be graded blind to arm with anchors written before any run. Recovery searches,
injected tokens, latency and cost SHALL be counted without judgement. Harness faults
SHALL be reported as blocked, never as losses, and adverse outcomes SHALL remain in the
denominator.

#### Scenario: Grader never sees gold
- **WHEN** a grading prompt is assembled for any arm
- **THEN** it contains the turn and the response only, and the gold and poison lists
  are applied afterwards by the deterministic intersection

### Requirement: Run protocol, stopping criteria and manifests
Pre-implementation baselines SHALL run at n = 1 per arm per case for A1, A2, A4 and
A5; the A3 comparison SHALL run at n = 5 per arm per case reporting individual and
modal outcomes and never a mean across cases; arm order SHALL rotate by seed and case
index; the total paid budget SHALL be capped and reserved per episode before launch.
The mechanism SHALL be reported falsified if A3 fails to beat A4 on the reminder test
in at least five of nine cases, if any twin yields a `resolved` false activation, if A3
uses a poison fact where A2 and A4 used none, if the no-memory case injects any
context, if the supersession case presents superseded knowledge as current, or if A3
harms more than one case the control arm got right; accepted for v0 only if A3 beats
A4 in at least seven of nine cases with every deterministic threshold met, zero poison
use and a p95 packet at most 1,500 tokens; and indeterminate otherwise. The no-memory
case counts as a win for A3 only when A3 answers correctly with zero injected
characters and zero memory searches while A4 searched or injected. Because the padded
case shares the grill case's query, the bar of seven tolerates the loss of at most one
distinct query and never the grill query; the report SHALL state that reading beside
the count. Every run
manifest SHALL carry the fixture-set digest, the corpus digest and the threshold
digest; a manifest missing any of them SHALL void the run. All eighteen fixtures SHALL
run or no verdict SHALL be published.

#### Scenario: Partial fixture set yields no verdict
- **WHEN** a run covers fewer than all nine cases and nine twins
- **THEN** the report states "no verdict" and publishes per-case results only

### Requirement: Private real-vault instrument
The same scorer SHALL be runnable locally over a digest-pinned snapshot of a private
vault from which notes that quote the fixture turns are excluded, on a quiesced cell
with a nonce in every query; its results SHALL be preserved as Evidence in the owner's
knowledge base and SHALL NOT be committed to the repository. The CI layer SHALL use
only the synthetic corpus so the public artifact privacy gate passes.

#### Scenario: Meta-note contamination is excluded
- **WHEN** the private snapshot is assembled
- **THEN** any page whose body contains a fixture turn verbatim is excluded and listed
  in the snapshot manifest
