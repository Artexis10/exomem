# Market map evidence — 2026-08 (web recon, URLs cited in-line)

Input evidence for `docs/strategy/exomem-competitive-strategy-2026-08.md`.
Status vocabulary is mandatory: **shipped / documented / prototype /
unverified**. Items flagged `VERIFY` need a human check before quoting.

## Primary competitors

**Supermemory** (supermemory.ai) — shipped, MIT periphery repo (28.8k★),
engine closed. Pricing: Free $0 / Pro $19 / Max $100 / Scale $399 (self-host
parity gated to Scale+; the free `supermemory local` binary is deliberately
degraded: no connectors, no hosted MCP, BYO extraction model). SHIPPED
epistemic-adjacent features: memory versioning (`isLatest`, history
listing), soft forgetting with dryRun, **inference-review queue**
(approve/decline/undo on derived facts, down-ranked until reviewed), graph
relations. NOT shipped: file-canonical state; decision/hypothesis/unknown
typing; typed change rationale. **Citation hygiene: the widely-repeated
"~85.9% LongMemEval" does NOT appear at the primary source.** Their research
page (supermemory.ai/research/longmembench/) claims **95% Recall@k=15** —
retrieval recall, not end-to-end QA accuracy — on LongMemEval-S with a
GPT-4o judge, under a session-based ingestion protocol that their own page
notes deviates from the official round-by-round methodology, with Zep's
comparison row copied from Zep's paper. The 85.2–85.9 figures live only on
third-party aggregators. Any strategy/report row cites the 95%-recall claim
with its caveats, never the aggregator number. Threat: HIGH and urgent —
they already shipped what could have been assumed to be differentiators
(versioning, review queue) at $19/mo entry.

**Basic Memory** (basicmachines.co) — shipped OSS (AGPL-3.0 core, 3.6k★) +
per-seat cloud (Team $15/seat, Business $30/seat; VERIFY vs earlier
$15-flat beta posture). Markdown+frontmatter canonical, bidirectional sync,
MCP everywhere, **15 documented `memory-*` skills** (14 enumerated on the
docs page; VERIFY the 15th; the OSS skills repo ships 9 — the 15 is the
documented/cloud surface). No evidence/claim split, no
decision/hypothesis/unknown typing, no revision store (lifecycle =
folder-move archival; cloud advertises "version history (fair use)"), no
published benchmarks at all. Threat: HIGH on positioning (owns the
file-canonical shape), LOW on epistemics — the entire epistemic contract is
open ground.

## Formal / research competitors

**Kumiho** (kumiho.io) — shipped commercial platform + preprint
(arXiv:2603.17244, not peer-reviewed). AGM belief revision over a
proprietary Neo4j graph engine (Community binary = EULA/NOASSERTION; OSS
peripherals MIT/Apache). Pricing $0→$170+/mo. Strongest
current-vs-historical model in the field (immutable typed revisions,
temporal validity, decision lineage). Caveats to quote: AGM postulates are
scenario-tested (49 scenarios), not machine-checked; K*7/K*8 open by their
own admission; headline 93.3% is on LoCoMo-*Plus*, their own variant, while
standard-LoCoMo F1 is an unremarkable 0.531–0.565; the Apache benchmark
harness requires their proprietary API. NOT file-canonical (marketing's
"markdown artifacts" are adjacent exports, not canonical state). Threat:
HIGH on narrative (they own "formal belief revision" vocabulary + an AWS
workshop citation), MEDIUM on product; they cannot follow onto
file-canonical ground without abandoning their architecture.

**Eywa** (eywa.to, arXiv:2605.30771) — prototype/unverified. "Evidence
before belief" — immutable Evidence, deterministic typed Signals, revisable
Beliefs validated against evidence; the closest conceptual match to
Exomem's evidence/claim split. No code release, no pricing, single named
researcher; claimed LoCoMo 90.19% / LongMemEval-S 88.2%
retrieval-sufficiency with per-question artifacts on a page that 403s bots
(VERIFY by hand). Threat: MEDIUM-HIGH conceptually, LOW today.

**OIDA** (arXiv:2604.11759) — paper + **reusable CC-BY-4.0 corpora**
(github.com/kakashi-ventures/oida-evaluation-corpora): nine epistemic
classes incl. DECISION/HYPOTHESIS/QUESTION with inverse-decay modeled
ignorance; 259 docs / 89 queries in BEIR layout — the most directly
reusable public artifact for the wedge thesis (synthetic, small, authors
note their own metric circularity). Implementation not released. Prior-art
consequence: Exomem cannot claim taxonomy novelty; differentiation is
file-canonical EXECUTION of the taxonomy.

**GEM/MemState** (arXiv:2605.26252) — prototype; state-level operator
correctness (ingestion/revision/forgetting/retrieval) over KuzuDB; **no
license file** → not reusable. **TOKI** (arXiv:2606.06240) — theory;
bitemporal operator algebra typing four contradiction-resolution heuristics
with isolation preconditions and audit rows; no official code. Defensive
move: state which heuristic Exomem's supersession implements and what
write-time anomalies it admits before an evaluator asks. **Always-On
Agents survey** (arXiv:2606.30306, 435 works) — citable third-party
validation: the field "concentrates more heavily on accumulating and
retrieving state than on governing, recovering, or relinquishing it";
watch its AOEP-v0 protocol (scores state-mutation/recovery obligations —
incl. rollback/relinquishment Exomem hasn't designed for).

## Blindside findings (not in the original brief)

**Google Open Knowledge Format (OKF)** — SEVERE strategic risk. v0.1
2026-06-12, v0.2 2026-07-25 (github.com/GoogleCloudPlatform/knowledge-catalog,
Apache-2.0, 8.4k★): a one-page vendor-neutral spec for Markdown+YAML
knowledge directories, v0.2 adding trust signals — `sources` (with
credibility fields), `generated`, `verified`, `status`
(draft/stable/deprecated), `stale_after`, and derived trust tiers
(unverified → machine-confirmed → human-reviewed). Ecosystem formed in ~8
weeks (pi-llm-wiki 478★, okf-skills, okf-gem, …). OKF does NOT model:
contested claims, decisions, hypotheses, modeled ignorance, supersession.
**Strategic option to evaluate in the report: adopt OKF as serialization
and compete one layer up on the epistemic types it omits.** "Your memory is
portable Markdown" is now table stakes, not a position.

**AgentCairn** (github.com/ccf/agentcairn, Apache-2.0+patent-grant, 571
commits, beta) — near-verbatim restatement of the contract: "Markdown is
canonical… edit a fact by hand; the next reconciled read honors it";
non-lossy history; `valid_from`/`valid_until`/`superseded_by`; disposable
DuckDB index; broad cross-agent surface (Claude Code/Codex/Cursor/
OpenCode/Desktop). Missing: evidence/claim split;
decision/hypothesis/unknown. Threat HIGH.

**Remnic** (github.com/joshuaswarren/remnic, MIT, 4.8k commits, multiple
releases/day) — most mature shipped file-canonical epistemic memory:
frontmatter `confidence`/`valid_at`/`invalid_at`; categories incl. `fact`,
`decision`, `correction`, `commitment`; "human-gated correction engine
with append-only supersession"; conflict inspection / evidence X-ray /
approval views; **ships its own benchmark (MemCorrect: recalls right fact,
accepts correction, stops serving stale one) with CI gates**. Missing:
hypothesis/unknown/contested; evidence-vs-claim split (confidence scalar
instead). Threat HIGH.

**ProjectMem** (MIT, 566★) — append-only typed event log
(Issue/Attempt/Fix/Decision/Note), deterministic pre-action gate against
repeating failed fixes; coding-scoped. **obsidian-second-brain** (MIT,
3.9k★) — supersession-aware search since v0.14. **SLEUTH**
(arXiv:2607.12267) — Confirmed Facts / Active Hypotheses / Open Questions:
the closest published match to hypothesis+unknown typing; paper only.
Watchlist: MemTX (2607.23929), TGMS (2607.10265), agent-native-memory
survey (2606.24775), MemSyco-Bench, MemLeak.

**Net position finding:** the file-canonical category is crowded and
moving weekly; the genuinely unoccupied ground is the FULL epistemic
contract — evidence held distinct from claims plus explicit
decision/hypothesis/unknown/contested typing with review and supersession —
articulated publicly only by a corpus (OIDA) and papers (SLEUTH, Eywa),
shipped by no one. That, not Markdown, is the position.

## Suite runability verdicts (feeds benchmarks/suites/)

- **STALE — LOCKFILE.** Full public release exists (found in Appendix G,
  p.37 of the PDF, not linked from the abstract): code
  github.com/icedreamc/STALE (MIT, verified; static since 2026-05-19),
  dataset huggingface.co/datasets/STALEproj/STALE (CC-BY-4.0 verified; sha
  617c51dc200b5ab09970834144c7e51c77959af0; 400 instances × 3 probe dims:
  state validation / stale-premise robustness / implicit downstream
  adaptation; LongMemEval MIT distractors). Includes the runnable CUP-Mem
  prototype. Verify the "anonymized form" release has no functional
  redaction.
- **MemOps — LOCKFILE with caveat.** arXiv:2607.12893 +
  github.com/MemTensor/MemOps (MIT verified; ~4 weeks old, 5★, single
  lab). Best public match to the operations taxonomy (leakage vs
  over-forgetting split). No adapter interface — integration = OpenAI-
  compatible shim or fork of the test stage; needs UltraChat download.
  Not load-bearing.
- **MemoryAgentBench — LOCKFILE.** ICLR 2026, MIT confirmed (code +
  dataset), 420★, dormant since 2026-05-21. **Conflict_Resolution split =
  8 rows** (large rows, 60–100 questions each, but still the thinnest
  slice) — never over-claim from it; pair with STALE for statistical
  weight. No documented plug-in contract: extend AgentWrapper following
  the vendored mem0/cognee/letta patterns.

## Open verification items

Eywa research artifacts (bot-blocked page) · Basic Memory 15th skill +
current pricing structure · MemState license absence · whether
toki-bitemporal-memory repo is author-affiliated (assume not) · STALE
anonymization completeness · Supermemory Scale-tier self-host parity
details.
