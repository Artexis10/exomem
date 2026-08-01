# Design — memory-proof benchmark v0.2 (writable-knowledge frontier)

Direction: maximize coverage of digitally-writable knowledge. The Polanyi
boundary (tacit knowledge) is acknowledged out of scope; the family registry
makes that boundary explicit and auditable instead of implicit.

Key decisions, inheriting everything already binding in
`add-memory-proof-benchmark` (oracle-derived expectations only, template
isolation, parity reports, unsupported-never-zero, no aggregate, gates
final, falsification register):

- **Registry-first growth.** Families are data (id, classification,
  rationale, templates); the ontology lint, privacy scan, and canary rules
  apply to every new family automatically. Rubric-track families route
  through the existing blind-judge handshake with predeclared rubric JSON —
  no new judge machinery.
- **Oracle extensions stay pure.** Procedural chains add an ordered-steps
  claim shape; quantitative adds an arithmetic evaluator over TypedValue
  quantities (unit-aware, tolerance-bearing); negation adds a
  recorded-false status usage pattern (existing DISPROVED/REVOKED states —
  no schema change expected); counterfactual "considered and rejected" uses
  existing claims with rejection assertions. Cross-lingual extends the
  wordbank with additional syllabaries under the same privacy-scan test.
- **Governance wiring is adapter-side.** Corpus PolicySet → exomem
  `_Governance/` YAML + persona→principal mapping through public surfaces
  (`govern_memory`, policy files documented in the scaffold references);
  runner passes persona per query; capability declared only when wired. If
  a required governance behaviour is unreachable through public surfaces,
  that is a STOP with its own minimal additive product proposal — never a
  harness simulation of governance.
- **Industry-standard posture.** Replication kit + versioned releases +
  onboarding doc make third-party participation possible; the publication
  gate (judge–human agreement before judged comparative tables; held-out
  seed for published numbers) is the credibility spine. Upstream PR of the
  Track-A provider remains the distribution wedge into the existing
  ecosystem.

Execution: fable-delegate lanes per family group after the registry lands;
media-profile family gated on `uv sync --extra media`; long-horizon release
generated desk-side (not CI). Ownership boundary unchanged: benchmarks/**,
tests/test_membench_*.py, docs — no production code beyond the (unlikely)
explicitly-proposed additive seam.
