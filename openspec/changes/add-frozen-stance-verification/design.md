# Design — frozen stance verification

## D1. The admission rule is code, not posture

A verifier is admitted by a registry entry pinning four things: the model name,
the sha256 digest of its resolved weights, the label-map version it was
verified against, and the fixture set that verified it. The registry is a
**repository artifact** — reviewed in diff, versioned with the code; no
runtime configuration, environment knob, or vault content can add or alter a
pin (`EXOMEM_CLAIM_NLI_MODEL`, the free-form override, is retired). At load,
the resolved weights are hashed once per process and the digest cached;
a mismatch, a missing model, a missing dependency, an unverified
(name, label-map) pair, or the opt-in gate (`EXOMEM_CLAIM_POLARITY_NLI`,
default off) being unset refuses the verifier. Refusal degrades to
**absence** — the entry simply carries no label — never to the lexical
heuristic wearing the verifier's `method` name. The heuristic itself is
retired from queue enrichment: it had no admission control, which is exactly
the seam the ratified matrix names non-compliant. This satisfies the
`epistemic-graph` requirement "Model-Backed Graph Suggestions Respect Pure
Substrate" by construction rather than by configuration.

## D2. The label is enrichment on the existing channel, not a new one

`meta.provenance` keeps its closed set (`asserted` / `proximity`). The
shipped enrichment keys stay: a proximity entry MAY carry `meta.polarity`
(closed set `contradict` / `refine` / `duplicate` / `unrelated`),
`meta.polarity_score`, and `meta.polarity_method` — after this change always
`"nli"` — joined by `meta.polarity_model_digest` and
`meta.polarity_label_map_version`, and by the `signal_version` the label was
computed against. Four invariants keep it inside the matrix: attaching or
changing a label never changes `signal_version` (no dismissal resurfaces
because labelling arrived); a label whose recorded signal_version no longer
matches the entry's is stale and is dropped, never served against changed
content; ranking is untouched (asserted above proximity stands; the label
never reorders); and canon, decisions, retrieval and policy never read it — it
exists for the reader and the decider on the review surface. The model
polarity label is not the reader's competing-alternatives pair stance: that
stance is a recorded triage disposition with its own contract (it moves queue
position by design) and this change does not touch it.

## D3. One channel: the audit's contradiction pass, modified in place

Two invocation paths exist today, both behind `EXOMEM_CLAIM_LEVEL`. The
synchronous one (`corpus_aware._refine_contradictions`) is removed, not
capped — along with `_POLARITY_CLAUSE`, the `contradiction-band` advisory
partition, and the `DupCandidate.polarity*` fields that exist only to carry
its result. The asynchronous one (`audit._pair_polarity` and its rendering)
is **modified in place** to become the sole channel: it enriches only through
the admitted verifier, and the heuristic fallback inside
`claims.classify_polarity` no longer reaches queue metadata. Enrichment is
bounded by construction — it runs over the surfaced set, already capped at
`EXOMEM_CONTRADICTION_TOP_N` — and failure-isolated per entry (an exception
leaves that entry unenriched and recorded as degraded, mirroring the existing
soft-fail discipline). The rendered polarity note keeps naming the label and
method. The default write path is byte-identical (the gate is off today and
the contradiction-band kind never fired without it); for gate-on users this
is a stated behaviour change, not a silent one: no write-time polarity, and
heuristic-method queue labels replaced by admitted-verifier labels or absence.

## D4. The label map is a versioned artifact

The current logit-threshold logic in `_nli_polarity` becomes **label map v1**:
an in-repo, versioned mapping from cross-encoder logits to the closed label
set. The map declares the logit column semantics and their order — a model
whose head orders entailment/contradiction differently is a different
(digest, label-map) pair and needs its own verified version. Changing a
threshold, the label set, the column order, or the direction convention bumps
the version and requires re-verification against the fixture set before the
pair is admitted again. The map is data reviewed in diff, not arithmetic
buried in code.

## D5. No vault text in instruction position, by construction

The verifier is a cross-encoder: two claim texts enter as the classification
pair and logits come out. There is no prompt assembly, no instruction template,
and no generation. The spec pins the input shape so a future "better" verifier
cannot quietly become a prompted generative model behind the same seam.

## D6. Verification fixture set

Golden claim pairs with expected labels — drawn from the f22 corpus shapes
(genuine contradiction, concordant evidence, restatement, unrelated) plus the
heuristic's known failure cases — checked at admission time and in CI with the
extra installed. The slice's precision claim is made against these fixtures
only; comparative bench claims stay withheld with sequence 2.

## D7. Out of scope

Hosted activation; any second verifier slot (alias NN remains S5-adjacent
future work); ranking or attention changes; the competing-alternatives stance
contract; any write-path latency budget change (the path gets shorter, and
the latency gates simply keep holding).
