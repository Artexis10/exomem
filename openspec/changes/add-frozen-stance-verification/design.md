# Design — frozen stance verification

## D1. The admission rule is code, not posture

A verifier is admitted by a registry entry pinning four things: the model name,
the sha256 digest of its resolved weights, the label-map version it was
verified against, and the fixture set that verified it. At load, the resolved
weights are hashed and compared to the pin; a mismatch, a missing model, or an
unverified (name, label-map) pair refuses the verifier. Refusal degrades to
**absence** — the entry simply carries no stance — never to the lexical
heuristic wearing the verifier's `method` label. The heuristic remains what it
is today (a differently-labelled, unprivileged measurement) and is not part of
this slice's queue enrichment.

## D2. Stance is enrichment, not a new entry kind

`meta.provenance` keeps its closed set (`asserted` / `proximity`). A proximity
entry gains an optional `meta.stance` block: `label`, `method: "nli"`,
`model_digest`, `label_map_version`, and the score as a measurement (precedent:
the cosine already rides the entry). Three invariants keep it inside the
matrix: attaching or changing a stance never changes `signal_version` (no
dismissal resurfaces because labelling arrived — the same guarantee the
provenance-labelling requirement made); ranking is untouched (asserted above
proximity stands; stance never reorders); and canon, decisions, retrieval and
policy never read it — it exists for the reader and the decider on the review
surface.

## D3. Invocation point: the audit's contradiction pass

Enrichment runs where the queue is already assembled — the asynchronous
audit/sweep path — bounded per pass and failure-isolated per entry (an
exception leaves that entry unenriched and recorded as degraded, mirroring the
existing soft-fail discipline). Nothing runs at write time: `corpus_aware`'s
`_refine_contradictions` call is removed, not capped. The default write path is
byte-identical (the `EXOMEM_CLAIM_LEVEL` gate is off today); the opt-in
write-time sharpening clause is retired deliberately — a stance verdict
belongs on the queue entry a reader triages, not in a write response emitted
under latency budget. This is a stated behaviour change for gate-on users, not
a silent one.

## D4. The label map is a versioned artifact

The current logit-threshold logic in `_nli_polarity` becomes **label map v1**:
an in-repo, versioned mapping from cross-encoder logits to the closed label
set. Changing a threshold, the label set, or the direction convention bumps the
version and requires re-verification against the fixture set before the
(digest, label-map) pair is admitted again. The map is data reviewed in diff,
not arithmetic buried in code.

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
future work); ranking or attention changes; any write-path latency budget
change (the path gets shorter, and the latency gates simply keep holding).
