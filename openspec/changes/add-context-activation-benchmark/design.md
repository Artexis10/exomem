# Design: add-context-activation-benchmark

## Context

Audit of the benchmark substrate at `main` 19762189 (2026-09-16): a new §7 amendment
family is withheld at scenario loading until the founder acknowledges it
(`benchmarks/epistemic/amendments.py:95-107`, `epistemic-state-bench` "Registration is
not release"), and acknowledgment is bound by the byte chain and revision binding in
`benchmarks/protocol/contracts.py:350-366,616-661`, which is why sequences 2, 4 and 5
remain pending. The released `f32 utility_action_episode` family (sequence 6,
acknowledged 2026-09-13) grades by observed environment state, its variants are a
code tuple (`benchmarks/membench/utility/schema.py` `VARIANTS`), and
`benchmarks/epistemic/journeys/f27_replay.py` already drives a real `claude -p` in
isolation with env stripping, strict MCP config and harness-fault handling. The
repository's own worst benchmark-defect class is cases that are unwinnable by
construction; the referent benchmark pinned false resolution at zero
(`openspec/specs/referent-resolution/spec.md:119`). Live reproduction on 2026-09-16
showed the insight note that quotes a fixture turn ranking first for that turn —
fixture contamination is real.

## Goals / Non-Goals

Goals: a benchmark that can falsify the compiler before it exists; per-class results
with duals; zero tolerance for confident false activation; a ceiling arm that strikes
unwinnable cases; a run that is cheap enough to repeat; no private data in the
repository.

Non-goals: a new amendment family in v0; a model judge inside `f32`; coding-task cases
(the adverse coding prior is recorded and kept separate); any aggregate score.

## Decisions

The current delivery decision is deterministic product-path acceptance plus observed ordinary use under `close-memory-loop`. Paid A1–A5 experiments are explicitly deferred and require separate authorization. D2, D6 and D8 specify the protocol only if that comparative experiment is later run; its results and thresholds are not replaced by a claim from deterministic CI. Scorer unit tests remain instrument tests.

The corpus must be built through normal supported writers into actual canonical entity/hub/Records/Planning structure, then indexed/published and consumed by the real `activate_context` path. Assert those structural prerequisites before scoring. Include capture-to-fresh-session integration, source-origin fan-out and interruption recovery. Repairing corpus shape changes its digest and requires a fresh deterministic result; do not recycle earlier packet scores or weaken gold/poison thresholds. Keep private episode replays local and publish synthetic fixtures only.

- **D1 — Two layers, no new family.** Layer A (deterministic audit) is an
  unregistered instrument with no claim standing so it can run today; Layer B rides
  `f32` as new variants (code tuple) so it runs and scores today. Judged conversational
  cases (C1, C7) are labelled findings, never claims, per the f27 precedent. A sequence-7
  amendment converting findings into claims may be filed later and is expected to sit
  pending.
- **D2 — Five arms when the optional comparison runs.** A4 (nudged recall) is mandatory within that experiment: without it any A3 gain is
  attributable to "we told it to look". A5 (oracle packet) is mandatory: a case A5
  cannot win against A1 is void and is never scored against the compiler; A5 is
  instrument evidence, never product performance.
- **D3 — Fixtures.** C1 AI-usage complaint (gold: the AI subscriptions collection, the
  weekly-limit insight, the capacity-ceilings pattern); C2 "planning to cook this"
  (grill/equipment page and cooking-method insights; twin: photographing); C3 an
  Exomem implementation turn worded to avoid self-contamination (Planning items +
  OpenSpec pointer; twin: the same shape for another project's Planning collection); C4
  named/unnamed person (entity + failure notes; twins: no name, shared first name →
  ambiguous); C5 resource whose latest Records state makes it unavailable (twin: an
  available resource must not be qualified); C6 no-memory turn (0 tokens; twin: a
  unit-conversion turn that does carry a domain cue); C7 ambiguous domain ("AI search";
  twin: a scoped variant that resolves); C8 supersession chain (active head + marked
  ancestors; twin: unchained active note → no marking); C9 C2's own turn scored on a
  tree padded with ~200 adjacent evidence/transcript pages (twin: C2's twin's own
  turn, on that same padded tree — an ordinary negative control again; padding
  robustness compares C9 against C2's own unpadded-tree score, round-two revision).
  The synthetic corpus mirrors
  these shapes with generated names; the private instrument uses the real pages.
- **D4 — Scoring.** Deterministic layer: per case × anchor kind, recall/precision with
  poison, twin false activation split by status, abstention, supersession, tokens,
  latency; duals always; mechanism-removal test. Agent layer: reminder test as primary
  (deterministic, no grader), blind extraction + model-free intersection, blind rubric,
  counted costs. No aggregate.
- **D5 — Thresholds and stopping criteria** as pinned in the spec; the latency bound
  is replaced by a measured constant from the naive-path baseline before freezing.
- **D6 — Optional comparative protocol.** Reuse `f27_replay.py` wholesale; pin model, provider, effort, CLI
  and exomem versions, prominence, corpus and fixture digests on the manifest; n = 1
  for pre-implementation baselines, n = 5 for the A3 comparison; rotated arm order; the
  same person authors fixtures and gold but does not grade; hard cost cap with
  per-episode reservation.
- **D7 — Privacy.** CI uses only the synthetic corpus; the real-vault snapshot is local,
  digest-pinned, contamination-filtered, and its report is preserved as Evidence in the
  owner's knowledge base.
- **D8 — Order within an authorized comparative experiment.** Naive-path latency → A5 ceilings (strike unwinnable
  cases) → A1 floor → A2/A4 (if A4 already clears the bar, that is the cheapest
  falsification) → deterministic baseline from `ask_memory` output labelled against
  gold/poison.
- **D9 — Reference identity and derived state.** Keep producer references intact.
  Freeze benchmark-owned current-state bindings from authored fixture relationships
  and canonical readback before activation. Bind the table and reference map to the
  exact and logical corpus identities. Validate projection role and state-entry
  consistency, including the unit's `updated` date, before awarding relevance;
  never trust a packet's provenance to create
  a binding. Every distinct surfaced reference stays in the precision denominator,
  while recall counts canonical gold identities once. Known poison bindings cannot
  be escaped through malformed metadata or supersession claims. Identity-only
  oracle calls retain their existing interface. Only `oracle_packet` and legacy
  `unknown` mechanisms allow missing bindings; all other mechanism labels require
  both the binding and its digest. Binding-optional runs cannot establish product
  acceptance. The Planning collection/item identity
  mismatch is a separate product-contract question, not a current-state projection
  or a reason to collapse arbitrary fragments. New scoring reports carry the binding
  digest; prior reports are not rewritten.
- **D10 — Subscription telemetry.** Reuse existing token accounting where its
  semantics fit, but keep subscription runs outside the metered API billing ledger.
  Preserve CLI usage counters, model/effort/version identity, timings and failure
  attempts. Unknown counters, quota deductions and charges remain explicitly
  unknown. Optional API-equivalent estimates need separate labelled provenance.
  Model and effort are run configuration, not fixture-specific product logic.
  Respect the authorized inexpensive subscription model and bounded run budget;
  never silently fall back to API billing.

## Amendments

### Exomem-native whole-system evaluation (2026-10-04)

**What is already sound.** `membench.utility` already pairs the same actor with/without Exomem, grades downstream environment state, retains product losses and budget exhaustion, binds reproducible identities and separates unknown costs. The memory-loop observation instrument distinguishes ordinary initiation from scripted effects and binds candidate decisions, receipts and publication to later usefulness. Preservation tests already distinguish original bytes from client transcription, and learned-cue/name tests cover fresh-session reuse and reversal. Keep those foundations. The current utility smoke exercised no capture or retrieval, so it does not demonstrate memory benefit; current text-shaped compiler cases do not establish the multimodal loop.

**Industry evidence and deliberate differences.** Anthropic's [agent-evaluation guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) supports observed outcomes, inspectable traces, separate capability/regression suites and calibrated graders. Its [context-engineering guidance](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) supports minimum-sufficient context and retrieval when needed. Exomem already independently uses outcome grading, bounded context and deterministic checks. These sources challenge treating packet recall or schema-byte reduction as sufficient evidence. Adopt trace-connected lifecycle cases and measured trade-offs; retain Exomem's governed provenance, temporal/epistemic distinctions and active-agent semantic decisions rather than copying a vendor's memory architecture. A new storage engine, graph engine, tool rename or internal reasoning agent requires evidence that the existing boundary cannot achieve the task, not a public leaderboard score.

**One programme, proportional layers.** Relevant CI runs cheap deterministic invariants and synthetic lifecycle integration. The same scenario vocabulary supplies bounded agent-in-the-loop trials and occasional frontier/public-client canaries; real dogfood failures feed synthetic regressions after retaining the actual invocation, packet, source state and correction where available. Generalize failure mechanisms with held-out seeds and varied names, schemas, language and distractors, not every cross-product. Keep capability challenges separate from established regressions. External memory suites are periodic calibration and required only for the specific comparative claim made about them. Do not add a parallel runner, evaluator database or mandatory frontier judge.

**First lifecycle case and expansion.** Reuse `close-memory-loop` 3.14's mixed text/image/dataset episode. Verify original preservation, attributable interpretation, correct role/destination, source-bound row/knowledge fan-out, conversational correction, historical/source preservation, publication and a later useful answer/action. Include an unchanged-source replay and a counterexample that must not inherit a local correction. Subsequent small cases cover documents, original audio/video with time-located observations, contradictory/stale evidence, Planning versus observed events, unavailable evidence, disclosure and no-useful-memory. Scripted writes or pre-extracted transcripts prove only the stages actually exercised. Each case names its distinct failure, available modalities and supported writer/storage/adapter coverage. File-mode success, a forced activation or one frontier canary cannot establish SQLite, no-nudge or universal-agent acceptance.

**Quality vector and accounting.** Reuse the existing usage ledger and observation records, adding only missing fields:

Do not require the actor to make an extraction mistake to reach a correction test. Record first-pass interpretation as observed. A separate controlled correction branch may seed a stale or mistaken projection or provide new corrective evidence; label that intervention and grade repair/reuse, not spontaneous error or no-nudge capture. The actor sees only ordinary task input and the declared correction, never evaluator targets. Broader semantic-rubric experiments remain distinct from the existing model-free `f32` slice and require their applicable registration before comparative claims.

| Dimension | Evidence and denominator |
| --- | --- |
| Task utility and harm | Same actor/task-world paired outcomes where comparison is run; include no-memory, stale/distractor and unhelpful-memory cases. Keep zero-use, missed capture and evaluable product failures in denominators. |
| Epistemic and lifecycle correctness | Supported versus unsupported claims, omissions, origin independence, correct semantic destination, source attribution, correction/supersession/currentness, governed admission and do-no-harm counterexamples. Deterministic state/readback checks where possible; blinded, calibrated judgment only for meaning that state checks cannot decide. |
| User-visible latency | Capture acknowledgement, ready-for-use publication, activation/retrieval/query/write/compile and whole-task wall time, with cold/warm state, load, sample counts and distributions. Setup and asynchronous completion are separate, never silently excluded. |
| Context, tokens and cost | Discovery/instruction/bootstrap input, injected/returned context, model input/output/cache/reasoning usage and actual billed charge by phase. Byte counts, tokenizer estimates, subscription usage and currency estimates remain labelled separately; unknown is not zero. |
| Tool calls and retries | Actual public calls and failed/repeated calls per phase and completed task; preserve failed attempts and follow-up correction/retrieval. Fewer calls is not success if the useful action or provenance is lost. |
| Background and storage work | Attributable index/graph/extraction work, CPU/wall time, peak memory, bytes written/stored and deferred backlog over a declared observation/drain window. Report remaining work, incomplete attribution or unknown cost; a fast acknowledgement cannot hide deferred work. |
| Human correction burden | Distinguish supplied fixture correction from spontaneous human correction; count nudges, unnecessary confirmations, correction turns, unresolved/repeated errors and observed interaction time when available. Simulated effort and missing human time are labelled, never passed off as measured user labour. |

Reports bind product/model/provider/effort/client/tool/policy/fixture versions, environment, storage mode, observation window and metric source. Show per-workflow results and uncertainty, not a weighted score or a universal price for epistemic mistakes. Compare Pareto trade-offs: a utility gain with higher latency or correction burden is not automatically accepted. Existing integrity/permissions and published resource/context bounds remain hard contracts. For new unmeasured dimensions, first record a baseline, then preregister the intended improvement and acceptable regression before measuring the candidate; do not invent an arbitrary universal ceiling or tune the criterion to the candidate result. The approved owner-only S1 timing exception remains narrow and explicit.

**Execution and claims.** Extend current deterministic checks first, then one bounded ordinary-agent journey before increasing coverage. Agent/model calls remain explicitly selected under existing spend/usage caps; this planning change does not dispatch paid runs or require a public benchmark before delivery. The original A1–A5 comparison protocol, frozen gold/scorers and historical reports remain intact. New multimodal/correction cases carry their own versioned scenario/metric identities and their applicable existing registration requirements; do not silently fold a new scoring meaning into an old released family. Missing coverage stays open for that capability, without blocking independent compiler/tool/S1 slices.

### Ordinary-use delivery checkpoint (2026-10-04)

The next user-visible acceptance event is a rich-turn → governed capture/readback → publication → fresh-session useful answer journey through the installed public interface. Reuse the existing memory-loop observation machinery, not another framework. Retain actual activation arguments, packets, follow-up retrieval and answers; distinguish delivery, selection, argument construction, compiler relevance and agent use. Include several relevant topics and a negative/ambiguous control without a private harness hint or reminder naming the expected memory. Skills/hooks may improve delivery, but forced calls prove transport, not ordinary initiation. Label reconstructed dogfood replays separately from original incidents whose invocation/packet is missing. This supplements the unchanged corpus and thresholds; narrow corrections and component passes do not establish general compiler acceptance.

Rulings from the programme orchestrator after the first real-compiler run. The
fixtures (turns, gold, poison) and the pre-registered thresholds stay frozen.
Every scoring amendment is reported beside the raw pre-registered score, never
in its place, and the recorded report carries both columns.

- **A1 — Corpus v4 (2026-09-28).** The corpus is repaired and gets a new
  identity, `context-activation-corpus-v4`; the v3 report stays as history.
  The corpus vault registers its own observation categories in its
  semantic-language registry and maps them onto context roles in its own
  role override, through the supported schema writers, as a real vault
  would. The gold notes' categories are not edited. About 115 ordinary,
  topic-diverse Notes pages that share vocabulary with the fixture turns bring the
  vault to about 150 indexed pages. They are authored as a user's editor writes them, while every
  gold, poison and canonical structure stays writer-built. Reason: a corpus
  the product refuses (37 indexed pages against the carry's floor of 100,
  unregistered categories) measures the corpus, not the compiler. The
  dogfood vault has 4,672 pages and a registry.
- **A2 — Unit-to-parent recall (2026-09-28).** A served unit whose
  ref names a keyed page (`<page ref>#unit-…`) counts toward recall of that
  page's canonical identity, through a parent map frozen from canonical
  readback before activation. It adds nothing to precision, and nothing to
  poison accounting. Reported beside the raw D9 score, which keeps every
  fragment distinct.
- **A3 — Planning identity is not amended (2026-09-28).** Serving the
  collection when the turn is about an item is a product defect: the item is
  what the agent needs. C3 and T3 stay red with that reason pinned; the
  product task is close-memory-loop 6.12.
- **A4 — Hedged poison (2026-09-28).** Follows the hedging rule this spec
  pre-registered before any result. On a twin, a poison anchor served
  `partial` beside a `partial` anchor from the twin's own gold is a hedge,
  not poison. A poison anchor served `resolved`, through any other channel,
  or as a lone `partial` with no correct candidate beside it stays poison.
  Reported beside the raw score. A4 removes only the poison hit; it never
  marks the case as hedging, so it cannot waive a status mismatch.
- **A5 — Product reds kept (2026-09-28).** T4 (a bare first name stays
  `partial`, so the turn abstains `unresolved` rather than `ambiguous`) and
  T7 (the scoped turn also resolves the market hub) are product defects,
  pinned red and routed to the identity and activation work. No amendment.
- **A6 — Continuity group (2026-09-28).** Every one of the eighteen cases is
  cold, and the scorer excludes `recent_context`. A separate keyed group of
  fresh-session cases, with gold for the recent-context block and its
  referents, is pre-registered with a pinned digest before its first run, and
  reported whatever it shows. It is not one of the eighteen and does not
  enter their verdict.
- **A7 — Ambiguity candidates in precision (2026-09-28).** The raw scorer
  leaves ambiguity candidates out of precision, so a positive case can list a
  wrong page as a candidate beside its gold and still pass (C7). Disclosed,
  and the raw scorer stays frozen. The opt-in amendment puts every ambiguity
  candidate of a non-twin case into the precision denominator, like a served
  anchor. It is reported in its own column beside the raw and A2+A4 scores.
  Twins are unchanged: a candidate outside a twin's gold is already a false
  activation.
- **A8 — A carried gold page counts (2026-09-28).** On a positive case, a gold
  page served `retrieval_carried` with at least one of its own units
  satisfies the expected `resolved` status, and every `#unit-` fragment of a
  bound gold page is credited to that page in precision as well as recall
  (extending A2, which amended recall only). The product labels carried
  context honestly, and what the user needs is the right page and fact.
  Reported in its own column beside the raw, A2+A4 and A7 scores.
- **A9 — Agent-choice scoring (2026-09-28).** Rationale: the primary agent
  reasons and chooses; the compiler's job is recall within budget with
  truthful labels. A positive case (not a twin, with a gold of its own)
  passes when both hold. (1) Every gold page arrives under an honest label (a
  `resolved`, `partial` or `retrieval_carried` anchor, or an ambiguity
  candidate), and the case's gold facts are present in served material.
  (2) Nothing is mislabelled: no non-gold page is served as a resolved anchor
  or as a `current_state` entry, and no poison is served as resolved. Extra
  honestly labelled partial or ambiguity siblings do not fail the case, within
  the packet budget (the token hard cap and the packet's own character limit).
  Twins, and the no-memory case C6, are scored unchanged: nothing resolved and
  no poison. A9 replaces only the verdict, never a raw metric. Its digest
  (`agent_choice_digest`: the rule text and the source applying it) was pinned
  before its first run on product packets. Reported beside the raw score and
  every other amendment.
- **A10 — Invalid twins (2026-09-28).** A negative twin whose turn a
  pre-registered fixture page genuinely answers is a fixture-design defect:
  serving that page is correct behaviour, and pinning it as a product red
  would push the compiler to be wrong. The frozen fixture pages are not
  touched. Pre-registered list, with page and reason: T1
  (`t1_fitness_goal_note`, C1's poison, a step-count goal) and, latent, T2
  and T9 (`t2_camera_gear_note`, C2's and C9's poison, the photography gear;
  the carry does not reach it today). Under A10 these twins are reported
  "invalid, excluded"; raw scores them as-is. Its digest
  (`invalid_twins_digest`) was pinned before its first run. Reported beside
  the raw score and every other amendment.
- **R3 and R4 — Reds kept (2026-09-28).** C2 and C9 stay red: a shared tag is
  not corroboration on a real vault, and semantic corroboration is future
  sensed-model work. C3 stays red: it relies on workspace context a cold run
  lacks, and its keyed variant belongs in the continuity group.

## Risks / Trade-offs

- The variant loophole: fixtures added under a released family could enter a scored
  table without a receipt; mitigated by freezing digests in this change and voiding
  manifests without them.
- Agent variance dwarfs single readings (measured in the no-nudge dev runs); mitigated
  by n = 5 with individual and modal outcomes and by refusing means across cases.
- The premise correction about receipt ordering is inferred from the spec and call
  sites, not reproduced; a probe (acknowledge a copy of the sequence-2 receipt naming
  its introduction commit) precedes any reliance on it.

## Migration Plan

Additive: corrected fixtures, real product-path CI, scorer, variants and a runbook. Fixture writers operate in isolated synthetic state, never a live vault. Existing variant identities and receipts are untouched, asserted by a byte-identity test. Corrected corpus bytes receive new digests; earlier reports remain historical rather than silently rescored.

## Open Questions

- Whether a sequence-7 amendment should be filed in parallel to give the judged cases
  claim standing later (expected to sit pending).
- The exact wording of C3 that avoids contaminating the Planning collection with the
  benchmark's own vocabulary.
