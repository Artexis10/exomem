<!-- authority:non-specification -->

# Hosted-inference boundary — candidate jobs and decision thresholds

Status: boundary document only. **No hosted inference is implemented or
scheduled by this document**, and the pure-substrate rule stands: no model
ever enters the product's decision path on the serving boundary. This page
exists so the benchmark can later compare `reasoning: client` vs
`reasoning: hosted` profiles without new harness code, and so any future
hosted-inference proposal must beat predeclared, measurable thresholds
instead of vibes.

## How the benchmark encodes the comparison

- Run manifests carry a `reasoning` axis (`client` today; `hosted` reserved).
  Same corpus, same scorers, same gates — only the profile changes.
- Until a hosted implementation exists, hosted rows in any run matrix are
  `blocked-until-implemented`. Nothing here pre-authorizes building it.

## Candidate server-side jobs, each with its justification threshold

Thresholds reference membench dimensions (per-dimension, no aggregate) on the
public seeded corpus, recommended profiles, quiesced reference hardware.
"Client baseline" means the shipped client-side path measured by the same
suite. A hosted candidate must beat the threshold **and** carry an explicit
privacy note (what content crosses the boundary, under which governance
projection) before it earns a proposal.

| Job | What it would do | Measured justification threshold |
|---|---|---|
| Background compilation | Compile captured sources into notes off-device | Track D capture-fidelity + current-state correctness ≥ client baseline while reducing manual steps ≥40%, with provenance retention unchanged (100% of compiled notes cite their sources) |
| Contradiction/staleness detection | Semantic contradiction sweeps beyond the cosine band | Detection recall on planted contradiction pairs ≥0.8 where the client-side band scores <0.5, at p95 sweep latency the client cannot reach on reference hardware |
| Pack-specific extraction | Domain-pack entity/relation extraction | Connection-discovery precision AND recall both ≥ client baseline +0.2 on predeclared hidden-link sets, decoy rejection not worse |
| Deep synthesis for thin clients | Multi-note synthesis where the client model is small | Blind pairwise human preference ≥70% over client baseline on Track D synthesis rubrics, with zero deterministic-gate regressions |
| Policy-aware redaction (closes the L4 gap) | Span-level redacted excerpts | Governance dimension: L4 renders as true redacted-excerpt with 0 leak-gate failures across the governance family, where today L4 renders as L3 |
| First-party mobile/web chat | Hosted answerer over governed retrieval | Track C mode-13 human acceptance script passes; leak gates 0 fail; latency p95 under a predeclared budget |
| Multimodal extraction | OCR/ASR for clients without media extras | Multimodal family: PNG/PDF-only facts become answerable (factual_qa pass on those queries goes from unsupported/fail to pass) with citation identity intact |
| Batch evaluation | Running this benchmark's judge phases server-side | Judge N-sample variance unchanged vs desk-side backends; no credential ever enters the runner (file-handshake preserved) |

## The one model-backed tier that exists, and the rule it runs under

The frozen stance verifier (claim polarity on the contradiction review queue)
is the only model-backed verifier in the product, and it is **local-only**;
hosted activation is a separate decision this document does not pre-authorize.
It is not an exception to the pure-substrate rule — it is the shape any
model-backed tier has to take to be admissible at all, and any hosted
inference proposal above inherits it:

- A **pinned identity**: a repository pin naming the model, exact upstream
  revision, ordered artifact manifest, sha256 digest of those declared names
  and bytes, label-map version, and fixture set that verified the pair. Extra
  cache files and revisions do not alter that identity. No runtime
  configuration, environment value, or vault content may add, select, or alter
  a pin.
- **Refusal degrades to absence.** Gate unset, digest mismatch, missing
  weights, missing dependency, or a red fixture set means no label — never a
  differently-produced label wearing the verifier's name.
- **A fixed classification input.** Two claim texts enter as a classification
  pair; there is no prompt assembly and no generation, so vault text never
  reaches instruction position.
- **Provenance-marked review-queue output only.** Labels carry their method,
  model digest, and label-map version, and never enter note canon, decisions,
  retrieval, ranking, policy, or any synchronous write path.

The admitted local checkpoint is multilingual, but Exomem's acceptance claim is
deliberately narrower than its model card: the production fixture set checks
English, German, French, Estonian, and mixed English/Estonian pairs. Its neutral
fallback means only that the NLI head did not establish contradiction or
entailment at the reviewed thresholds; it does not establish topical
unrelatedness.

## Why the current verifier is not in Hosted

Hosted activation remains withheld. This change adds no `nli` extra or verifier
weights to the Hosted image, no verifier capability grant, and no
`EXOMEM_CLAIM_POLARITY_NLI` gate to a cell. The selected safetensors checkpoint
is about 532 MiB before Torch/runtime overhead; the available full ONNX export
is about 1,064 MiB. The upstream quantized ONNX export is smaller at about
323 MiB, but it failed a genuine-contradiction fixture: its two directional
contradiction probabilities were about 0.29 and 0.50, far below the reviewed
0.93 symmetric threshold. It is therefore not an admissible capacity shortcut.

A future Hosted proposal must first pass the exact multilingual fixture set and
measure image size, cold and warm latency, peak RSS, concurrent-cell capacity,
idle reclamation, scheduling, and failure isolation on the actual cell runtime.
It may then propose an isolated serving shape or a separately calibrated
artifact. Local admission does not grant Hosted admission.

See the `frozen-verifiers` capability spec for the normative statement.

## Standing costs any proposal must price in

Added latency and infrastructure cost; a larger privacy surface (content
leaving the user's machine — must ride the existing governed egress: audience
projections, withhold notices, disclosure receipts); operational burden
(cells, updates, attestation); and the strategic cost of weakening the
local-first claim. A candidate that beats its threshold but cannot state its
governance projection is not eligible.

## Explicit non-goals

No server-side reasoning in retrieval/ranking/policy decisions (pure
substrate); no default-on hosted anything; no benchmark-driven auto-adoption —
a threshold met creates *permission to propose*, nothing more.
