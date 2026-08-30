# Add frozen stance verification

## Why

The contradiction queue is stance-blind by its own admission. The cosine band
`[0.82, 0.90)` is a proximity measurement; the claim-level polarity heuristic in
`src/exomem/claims.py` self-describes as "a lexical stand-in for a real NLI
model"; and the NLI cross-encoder backend is marked **WIRED BUT UNVERIFIED**,
selected by an unpinned environment knob (`EXOMEM_CLAIM_NLI_MODEL`), with its
one invocation path (`_refine_contradictions`) sitting inside synchronous
write-time warning generation (`corpus_aware.py`). The cost of stance-blindness
is precision: concordant-evidence pairs surface beside genuine conflicts,
burning reader trust and agent tokens — the exact failure the (withheld,
sequence-2) bench family f22 makes a red line with its concordant twin.

The authority-and-effects matrix, ratified 2026-08-30 as the constitution's
model clause, admits a frozen verifier under a strict rule: **pinned model
digest, versioned label map, output restricted to provenance-marked review-queue
labels, off the synchronous write path, no vault text in instruction position** —
never entering canon, decisions, retrieval, ranking, or policy. The
`epistemic-graph` spec already requires any model-backed polarity path to be
optional, default-off, soft-failing, measurement-labelled and propose-only;
today those properties hold by configuration accident. This slice (S9 of the
no-nudge programme, report §17) makes them hold by construction, and makes the
verifier tier real with its first occupant.

## What Changes

- **New capability `frozen-verifiers`.** The admission rule as spec: a verifier
  runs only under a pinned weights digest, a versioned label map, and a green
  verification fixture set; anything else degrades to *absence* (no label),
  never to a differently-produced label wearing the verifier's name. Output is
  provenance-marked review-queue enrichment only.
- **`contradiction-queue` delta.** Proximity pair entries may carry a model
  stance label (`contradict` / `refine` / `duplicate` / `unrelated`) as
  asynchronous enrichment with full identity (method, model digest, label-map
  version). Stance never changes an entry's `signal_version`, never moves
  ranking, and never appears on the synchronous write path.
- **The write path loses the in-path call.** `_refine_contradictions` leaves
  write-time warning generation entirely. The default path is byte-identical
  today (the gate is off); the opt-in `EXOMEM_CLAIM_LEVEL` write-time
  sharpening is retired, and stance appears on the queue entry instead.
- **`nli` optional extra.** The cross-encoder dependency ships as a default-off
  extra; the model weights are pinned by digest through the existing offline
  model cache; the label map (the current threshold logic, promoted to a
  versioned in-repo artifact) is label map v1.
- **Local-only in v1.** Hosted activation (an int8 classifier fits the cell
  envelope) is a separate decision and out of scope here.

## Impact

- Affected specs: `frozen-verifiers` (new), `contradiction-queue` (one added
  requirement).
- Affected code (implementation slice, after approval): `src/exomem/claims.py`
  (pinning, label map, fixture verification), `src/exomem/corpus_aware.py`
  (remove the in-path invocation), the audit/sweep contradiction pass
  (enrichment), `pyproject.toml` (`nli` extra), doctor (verifier status line),
  tests including the verification fixture set.
- Bench linkage, stated honestly: f22 is withheld until sequence 2 is
  acknowledged; this slice claims precision improvement on its *verification
  fixtures* only, and gate-off behaviour stays byte-identical.
