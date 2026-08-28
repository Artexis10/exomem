<!-- authority:non-specification -->

<!-- authority:implementation-reference -->

# Benchmark fairness contract

An operational companion used when running and reviewing benchmarks. It is not
a separate specification authority. Normative source: OpenSpec capability
`benchmark-fairness-contract` (change `add-competitive-benchmark-programme`);
this document is the human-readable implementation reference. History: the 2026-08-08
independent audit rejected the prior head-to-head because its author
configured the competitor; this contract exists so that defect class is
structurally impossible, not merely discouraged.

## The one-line rule

**Competitor-side configuration is competitor-authored, or it does not run.**

## What that means operationally

1. **Provenance, per knob.** Every configuration value applied to a
   competitor traces to their code (file:line) or their documentation (URL),
   recorded in a machine-checked provenance table. A knob without provenance
   refuses to run. The strongest form — used wherever possible — is running
   the competitor's own provider classes or whole harness unmodified.
2. **Guest posture.** Head-to-head rows come from the competitors' own
   harnesses (Basic Memory's bm-bench; Supermemory's MemoryBench) with
   Exomem as a guest provider. This repository authors only Exomem's own
   integration — the posture every vendor takes on a public suite.
3. **One reader, one judge, one ledger.** Competitor-harness ANSWER/EVALUATE
   stages are never republished; ingest/search artifacts are exported and
   re-judged under the direct lane's frozen reader and the suite's official
   judge, so accuracy differences cannot hide in prompt or judge deltas
   (MemoryBench, notably, excludes failed questions from accuracy — we
   re-count them).
4. **Glue disclosure.** What this repository did author for each row —
   projectors, drivers, exporters — is size-accounted (files, LOC,
   endpoints) in the fairness matrix. Gross asymmetry between products'
   glue is itself a reportable finding.
5. **Cross-competitor defects go upstream.** A defect found in one
   competitor's configuration of another (e.g. a document-search row
   labelled as memory search) is filed as an upstream issue with evidence —
   never fixed here, which would be the retired defect class in reverse.
6. **Harness faults are never contender losses.** Unreachable services,
   failed model loads, unbuilt indexes, near-zero retrieval, version-drift
   regressions: these invalidate rows, for every product equally. The prior
   programme published a competitor at 0.000 with state "ok" while its
   embedding model failed to download; that is the exact outcome this rule
   forbids.
7. **Readiness is proven, not assumed.** Exit codes are not evidence.
   Basic Memory: vector-chunk counts + configuration + log line.
   Supermemory: terminal document status plus a memories-mode canary
   (`done` never implies extraction; its default dreaming mode has no
   completion signal and renders `readiness-unverifiable`, disclosed, not
   invalid — invalidating a product's default mode would be its own bias).
   Exomem: doctor checks with refusal-not-degradation.
8. **Variants never collapse.** Hosted vs local Supermemory run different
   extraction models; document-search vs memory-search are different
   products in effect; git-backed vs plain Basic Memory differ on history.
   Each is a registered variant with its own row and disclosure text.
9. **Pre-registration is reconstructed, not selected.** Epistemic scenario
   families, assertions, acceptance predicates, and the strategy decision
   gates are founder-ratified before any competitor run. Ratification leaves
   the approved bytes unchanged and adds an immutable receipt. Every
   comparative manifest pins a Git revision and carries the complete typed
   receipt chain through that revision; validators re-read the document and
   discover each receipt's unique full-history introduction. A plan-supplied
   digest is only a checked assertion. Later changes are ordered amendments,
   and mutation, omission, reordering, branch substitution, or a receipt edit
   that is later restored all refuse.
10. **Independent adversarial review before publication.** The
    auto-generated adversarial packet (assumptions, confounds,
    suspicious-win flags, challenge-artifact paths, pre-registration hash)
    goes to a reviewer with no stake — the same review shape that produced
    the 2026-08-08 REJECT — and every material objection is fixed or
    published alongside the claim. A result showing a competitor ahead is a
    valid, publishable outcome of this programme.
11. **Transport glue is not competitor configuration.** We may write the
    process bridge needed to run a competitor's own provider inside another
    harness: loopback authentication, isolated work roots, bounded retries,
    persistent descriptors, evidence capture, and cleanup. Canonical-event
    hygiene such as digesting evidence-labelled session IDs happens before
    the observation boundary. From that normalized boundary, the bridge must
    forward the competitor's renderer, search arguments, defaults,
    exceptions, and return objects unchanged. Every observation seam and LOC
    is disclosed. A silent embedding fallback or ambiguous non-JSON result is
    an invalid environment row, never an empty competitor miss. Basic
    Memory's MemoryBench wrapper necessarily performs one full reindex per
    unique session while its own harness groups a corpus; that asymmetry is
    printed beside the paired rows rather than optimized away. Each reindex
    needs fresh fallback and project/document readiness proof; an earlier
    session's positive counts never authorize a later receipt.
12. **Provider-visible identity stays neutral.** MemoryBench may retain raw
    case/session IDs for its own correlation, but evidence markers such as
    `_abs` are replaced with positional digests before any renderer or index
    sees them. Private mappings stay in protected evidence. Isolation glue
    must also prove that every configured project path is benchmark-owned;
    it may not discover or index an operator's existing memory directory.
13. **Privileged calls cross one attested boundary.** Epistemic drivers receive
    only a serializable broker proxy. The parent broker owns credentials,
    sockets, SDKs, CLIs, and provider filesystems; untrusted driver code runs
    in a fail-closed Bubblewrap namespace with no provider roots or network.
    An authenticated invocation log is checked against the closed
    `driver_surface_id × provider × variant` matrix before assertions run.
    Undeclared or unaudited calls refuse. A documented capability gap is a
    named noncomparability exclusion, never an unsupported score.
14. **Every epistemic claim replays.** Every product and control cohort cell,
    including passes, has provider/variant-bound evidence. Validation opens
    every path component without following symlinks, verifies digests and
    snapshots, reconstructs the assertion context, and requires the frozen
    assertion to reproduce the exact outcome. The exact `grep-markdown` and
    `no-memory` rows share the ordered cohort. If either control passes an
    assertion instance, the product keeps its five-valued outcome but loses
    product-signal eligibility for every strategy gate. Any unreplayable cell
    suppresses the public cohort table rather than emitting a partial claim.

## Fairness-matrix row (rendered per lane × provider × variant)

| Field | Content |
|---|---|
| config source | file:line / URL for every competitor-side setting |
| config authored by | competitor · exomem (disclosed) · shared harness |
| exomem-authored glue | file list + LOC + endpoints + reviewer disposition |
| asymmetries | enumerated, each with direction (favours whom) |
| capability declarations | N/A, absent_by_design, unavailable counts |
| readiness | verification method + evidence path (or readiness-unverifiable) |
| pins | product version/commit/binary, dataset sha, model ids |
| blocked measurements | with reasons and one-command unblock paths |

## Reviewer checklist (what to attack first)

- Does any competitor knob lack provenance? (automatic disqualification)
- Do the equivalence-gate diffs contain unexplained mismatches or expired
  exceptions?
- Are Exomem's projector/driver LOC materially larger than competitors'?
- Did any row score a product where the manifest shows an environment fault?
- Does any comparative claim rest on a family containing an N/A?
- Does every privileged call have an authenticated sandbox receipt and an
  exact matrix entry, with capability gaps excluded rather than scored?
- Does every product/control cell replay from provider-bound evidence, and do
  control passes disappear from every product-signal gate count?
- Is any judged number published without the structural-blinding fix and a
  judge–human agreement measurement?
- Does any cost claim include unmetered server-side extraction?
- Is any latency comparison rendered from the known-unvalidated host?
