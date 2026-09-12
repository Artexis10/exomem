## Scope and inspected foundation

Assessment: **medium extension**. A credible first slice includes the actor seam, paired isolation, a small action world and outcome accounting. It is not a small score-field addition, and it does not need an architectural rewrite.

Inspection baseline: main `ae02dba7`, 13 September 2026. Pending work is identified separately so implementation does not duplicate it or assume it has merged.

| Existing component | Reuse | Missing for utility |
| --- | --- | --- |
| `membench/schema.py`, `generate.py`, `oracle.py`, `procedural.py` | Seeded neutral inputs, temporal/procedural truth, deterministic identity | Actor-visible observations separated from private action targets |
| `membench/runner.py`, `artifacts.py` | Immutable run artifacts and adapter conventions | Paired downstream outcomes; zero-hit invalidation must not be inherited |
| Track C `natural_prompt_driver.py`, `witness_join.py` | Real Claude sessions, isolated MCP configuration, activation evidence | Natural/explicit are prompt modes, not memory/no-memory controls |
| `membench/hermetic_env.py`, `clock.py` | Hermetic environment and controlled clock primitives underlying those drivers | Explicit utility-arm persistence and reset boundaries |
| Track D `runner.py` | Cheap scripted longitudinal, correction and review checks | No model-driven actor or downstream counterfactual |
| `epistemic/schema.py`, `runner.py`, `scoring.py`, `journeys/f27_replay.py` | Lifecycle operations, fresh sessions, snapshots and deterministic predicates | Agent-written memory is assessed primarily as memory state, not later task utility |
| `scripts/product_flow_benchmark.py` | Real product surface and operational checks | Scripted flow does not establish agent benefit |
| `protocol/budget.py` | Reservation, settlement, held spend and stop state | Episode/pair reservations and phase-level attribution |
| PR #1126, `benchmarks/lme/native_agent.py` | Generic API/tool loop, broker, limits and isolated phase envelopes | Pending at inspected head `512d4ff6`; messages and pilot scheduling are LME-specific |

The OpenAI-compatible judge backend is not itself an actor. Resolve PR #1126's ownership and integration status before consuming its loop. Prefer a thin utility adapter over the agreed native-agent module, with the smallest parameter seam for task messages/tool policy. Do not copy the loop, require a generic runtime extraction or import the LME replay scheduler. If the necessary module has not landed, coordinate its minimal prerequisite commit explicitly. Open report/suite PRs #1046/#1047 may supply reporting integration later; the first slice can emit the existing immutable JSON artifacts.

## First vertical slice

Use one tiny configuration/action world built from seeded procedural facts. It is a benchmark fixture, not a feature-sized coding project. Names, values, project identities and dates vary by seed; all variants share the same action grammar and difficulty envelope.

Each episode has three bounded sessions:

1. **Experience:** perform a small action and observe feedback that establishes a reusable procedure or project constraint. The memory arm decides what to capture through shipped tools and instructions.
2. **Change:** encounter a change, scoped conflict or unrelated but attractive procedure. The memory arm decides what to revise, preserve or disregard.
3. **Action:** a fresh agent must apply a configuration or submit an executable action. A deterministic grader checks the resulting state, scope and prohibited side effects. Merely stating the right fact does not pass.

The three variants are helpful history, a self-contained final task with no useful prior memory, and stale/semantically attractive history contradicted by current evidence. Initially compare no-memory and current Exomem: one seed gives six episodes and eighteen sessions. This is an instrument smoke test, not evidence of general product value or of coding performance.

The no-memory actor may retain exactly the same ordinary workspace and environment state as the Exomem actor. Durable task artifacts are legitimate memory available to both. No hidden transcript log or evaluator file is mounted in either workspace. The helpful variant must still offer a realistic benefit beyond those ordinary artifacts; otherwise the result should be neutral. No task may require guessing information the control could not reasonably encounter or recover: if history is irrecoverable by design, label the scenario an information-retention test and report it separately from workflow-efficiency claims.

## Minimal architecture and contracts

Extend `membench`; do not create a separate runner framework or result database.

| Proposed seam | Responsibility |
| --- | --- |
| `membench/utility/schema.py` | Versioned episode, arm, phase, action outcome and pair-result records |
| `membench/utility/scenarios.py` | `generate_episode(seed, variant)` returns separate actor observations and evaluator-only targets; reuse BuildContext and procedural oracle logic |
| `membench/utility/action_world.py` | Bounded common action tools and deterministic state transition/grading; no model judge |
| `membench/utility/actor.py` | Thin adapter to the agreed PR #1126 native-agent module, with explicit task messages and tool policy instead of LME role prompts |
| `membench/utility/runner.py` | Fresh cells/sessions, arm order, pair reservation, trace capture, phase transitions and outcome classification |
| `membench/utility/scoring.py` | Pure paired metrics and coverage, consuming observed action outcomes rather than agent self-reports |
| `benchmarks/run.py`, `benchmarks/README.md` | One utility subcommand, opt-in paid flag, reproducible manifest and result-reading instructions |

The runner calls the common actor and existing product cell/public tools. It must not use oracle data to author Exomem notes. Privileged evaluator targets remain outside actor mounts, prompts, tool schemas, errors and filenames. The reference actor used for deterministic instrument tests is explicitly labelled synthetic and cannot supply a product utility result.

The evaluator-only manifest binds scenario version/seed, hashes of visible inputs and evaluator targets, product commit, scaffold/skill/config versions, actor request and reported model identity, provider, reasoning setting, generation parameters, tool schema, budgets and clocks. The entire manifest, seeds and target hashes remain outside actor access: low-cardinality targets can be recovered from unsalted hashes. Actor views receive only opaque episode identities and permitted observations. The manifest records each arm's declared persistent state. An arm identity never reveals whether memory is deliberately stale or which action is expected.

## Fair comparisons and diagnostic controls

The two initial arms share actor, effort, common tools, starting task world, ordinary persistence and resource limits. Both receive the same notice that they may retain observations in the ordinary workspace. Exomem adds its shipped memory tools and instructions as the treatment; record their token cost and label the treatment product-as-shipped, not a retrieval-engine-only causal claim. Separate sessions must actually reset conversational state. Use `hermetic_env.py` and `clock.py` to isolate configuration, vaults, logs, credentials, caches and automatic hooks from the operator's personal environment. Rotate arm order deterministically by seed and record retries; a failed arm cannot receive extra attempts that its pair does not receive under the declared retry policy.

Two distinct experiment modes prevent confounding:

- **Whole-system utility:** each Exomem revision gets its own agent-authored capture, maintenance and later action. Compare it to the same no-memory control. This covers the full lifecycle and charges all phases.
- **Context mechanism:** fork one witnessed, agent-authored memory checkpoint into current-context and JIT/abstinent policies. Holding capture fixed isolates context selection. Report it separately; it cannot establish that capture or maintenance improved.

Record memory attentiveness and context aggressiveness as independent settings. First use the shipped product default. Later vary one at a time. Do not silently modify global product settings to make a treatment work.

Add oracle context only after the initial instrument passes: give the same actor sufficient valid facts available by the session cutoff, not the expected action or future outcome. Add matched irrelevant, stale and poisoned memory controls with injection provenance. These measure susceptibility and instrument sensitivity; they are not substitutes for agent-authored lifecycle runs.

To answer whether a code change helped, compare the candidate Exomem revision with a frozen reference revision on the same seeds and actor configuration, alongside no-memory. Cached reference outcomes are comparable only while their full manifest remains compatible. They cannot support a paired latency claim across changed infrastructure. Writer/maintenance changes require replaying those phases; a frozen checkpoint is only sufficient for context-only diagnostics.

## Scores, harm and failure accounting

Report each scenario family separately. For valid completed pairs, let C and M be binary downstream success for control and memory:

- Utility lift: mean(M - C), in percentage points; show wins, losses, both-pass and both-fail counts.
- Harm: count(C=1, M=0) / count(C=1), with numerator and denominator. A zero denominator is undefined, not zero harm. Also show losses / all valid pairs.
- Action damage: unconditional prohibited/destructive-action counts for each arm, including both-fail pairs, broken down by frozen side-effect class. Do not erase damage by reducing every failure to the same bit.
- Oracle gap, when present: mean(O - M) on matched valid pairs. A failed oracle control flags instrument/actor limitations; it is not an automatic excuse to erase an Exomem loss.
- Efficiency: input, output, cache read/write and reasoning usage under the provider's documented accounting; wall time, model time, tool time, turns, peak context and cumulative delivered context. Split capture, maintenance and action. Show costs for failed and invalid attempts too. Do not double-count reasoning tokens already included in output.
- Diagnostics: capture coverage, stale/current selection, wrong-entity transfer, provenance, contradiction handling and memory payload size. These explain action outcomes and never award substitute success.

Small pilot counts are descriptive. Add paired uncertainty intervals and held-out seeds before using runs for regression decisions. Report per-family results and explicit scope; do not collapse them into a weighted score or infer neutrality from a non-significant tiny sample. Keep the model-free instrument suite as a hard CI gate; stochastic utility regressions initially trigger investigation and a bounded confirmation run, not an automatic flaky merge failure.

Retain scheduled, attempted, valid, invalid and missing pair counts. Product zero hits, absent capture, wrong decisions, memory-tool errors, refusal and exhaustion of a declared actor/resource budget are observable task failures when the environment remains evaluable. They are not invalid merely because retrieval or completion failed. Independently demonstrated bad fixtures, credential/transport outages or broken harness isolation are infrastructure faults. Freeze this classification before running; record the fault evidence and preserve spend. Unknown charges remain reserved and halt further spending under the existing ledger contract. Existing epistemic integrity diagnostics remain visible; they must not suppress a valid downstream loss.

The existing harness sometimes invalidates zero-hit retrieval and incomplete native sessions. This utility protocol therefore needs an explicit amendment, including the distinction between declared task-budget failure and a broken instrument. Register one new operational family, `utility_action_episode`, in `epistemic/PREREGISTRATION.md` §1 and `epistemic/registry.py`, allocating its numeric id from the integration head. Its deterministic assertions grade action state and prohibited effects; it does not import the judge-based AT-1 dimensions excluded by the existing protocol. Extend the existing receipt chain and mirrored amendment-family map. The utility runner and report reader must explicitly call `protocol.contracts.require_amended_families_released` with that family before comparative execution or claims; ordinary manifest identity validation is insufficient. Reuse `epistemic.amendments.require_family_released` at fixture release boundaries. Test pending, acknowledged, mismatched and unrelated-family cases. Do not change other tracks' denominator rules. Development instrument tests remain clearly labelled before acknowledgment. The concrete receipt retains the existing founder acknowledgment requirement.

## Cost and execution schedule

Regular development tests are deterministic and network-free. Paid execution defaults off. Begin with the six-episode smoke test only after instrument tests and protocol acknowledgment. Set a maximum total of $2 for that pilot. Proposed limits are eight model calls per phase including retries, 48,000 total input tokens and 2,000 total output tokens per call including any billed reasoning, and a separately enforced phase deadline. At a pinned endpoint price ceiling of $0.15/M input and $0.50/M output, the conservative model reservation is $0.3936 per pair (48 calls), or $1.1808 for three pairs. These are maximum model reservations, not measured expected costs. Verify the endpoint quote against the [provider catalog](https://openrouter.ai/z-ai/glm-5.3-flash), include any additional metered operations and require all three reservations to fit before launching the pilot. A more expensive endpoint must fail preflight or use explicitly revised limits; it cannot silently raise the cap. Preserve partial coverage if execution faults after launch.

The previously tested GLM-5.3-Flash high configuration is a candidate for the actor, not a validated substitute for the user's workflow. Pin a supported model identifier, provider, effort and reported identity; disable provider/model fallback. Save the provider's pricing snapshot and supported-parameter checks. A public alias or canonical catalog slug alone does not prove immutable weights. Refuse incomparable identity/config drift, and never quietly switch to a frontier model.

After the smoke test establishes real tool use, reliable grading and both useful/harmful instrument sensitivity, freeze a small held-out seed set. Expand one family at a time within an explicit ledger cap. Run an occasional bounded Opus coding canary to check transfer to the actual workflow. Do not use full LongMemEval replay as the regression loop or automatically resume previous runs.

Borrow dependent action/feedback episodes from [MemoryArena](https://arxiv.org/abs/2602.16313) and incremental updates/forgetting controls from [MemoryAgentBench](https://arxiv.org/abs/2507.05257). No external dataset, large replay scheduler or LLM judge is required for the first slice.

## Delivery phases and acceptance

1. **Shared actor and instrument:** reconcile PR #1126, extract only the common runtime, add deterministic scenario/action/scoring tests and prove isolation with adversarial actor views. Add the versioned utility protocol amendment. A fake actor establishes harness behavior only.
2. **Smallest valuable implementation:** connect the same fixture to real Exomem public tools and the pinned actor; run the six-episode paired smoke under its cap after acknowledgment. Deliver traces, action outcomes, lifecycle diagnostics, cost and coverage. The acceptance criterion is credible measurement, including honest zero/negative lift, not a positive result.
3. **Follow-up, outside this change — routine regression:** frozen reference/candidate comparisons, held-out seeds, paired intervals and bounded confirmation policy; checkpoint-based JIT/oracle/negative controls with attentiveness fixed. Freeze task-success and efficiency-regression thresholds separately before monitoring; successful-but-costly runs can trigger efficiency investigation without being relabelled task failures. Extend to ambiguity, context pressure, provenance and overlapping updates after each fixture earns its cost.
4. **Follow-up, outside this change — external validity:** occasional frontier real-workflow canaries and optional external calibration. Keep coding, synthesis and information-retention claims separate.

The next implementation should complete phases 1 and 2 together as one narrow feature. Those phases are the acceptance boundary for this change; phases 3 and 4 are a sequenced roadmap requiring follow-up OpenSpec changes, not open acceptance tasks here. Shipping only a synthetic scoreboard would leave the product question unanswered. The present delivery is the reviewed OpenSpec plan; no live utility result is claimed.
