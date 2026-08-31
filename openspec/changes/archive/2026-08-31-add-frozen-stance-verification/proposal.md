# Add frozen stance verification

## Why

The contradiction queue is stance-blind by its own admission. The cosine band
`[0.82, 0.90)` is a proximity measurement; the claim-level polarity heuristic in
`src/exomem/claims.py` self-describes as "a lexical stand-in for a real NLI
model"; and the NLI cross-encoder backend is marked **WIRED BUT UNVERIFIED**,
selected by an unpinned environment knob (`EXOMEM_CLAIM_NLI_MODEL`). Claim
polarity reaches the product on **two paths today**, both behind the
`EXOMEM_CLAIM_LEVEL` master gate (default off): the synchronous write path
(`corpus_aware._refine_contradictions`), which sharpens warning text and mints
the `contradiction-band` advisory kind; and the asynchronous audit sweep
(`audit._pair_polarity`), which already writes `meta.polarity`,
`meta.polarity_score`, and `meta.polarity_method` onto `corpus_contradictions`
entries plus a rendered polarity note. On both, the method silently falls back
to the lexical heuristic unless `EXOMEM_CLAIM_POLARITY_NLI` is also set. The
cost of stance-blindness is precision: concordant-evidence pairs surface beside
genuine conflicts — the exact failure the (withheld, sequence-2) bench family
f22 makes a red line with its concordant twin.

The authority-and-effects matrix, ratified 2026-08-30 as the constitution's
model clause, names this claim-polarity seam non-compliant until re-laned or
removed, and admits a frozen verifier under a strict rule: **pinned model
digest, versioned label map, output restricted to provenance-marked
review-queue labels, off the synchronous write path, no vault text in
instruction position**. The `epistemic-graph` requirement "Model-Backed Graph
Suggestions Respect Pure Substrate" already demands optional, default-off,
soft-failing, measurement-labelled, propose-only; today those properties hold
by configuration accident. This slice (S9 of the no-nudge programme, report
§17) is the re-laning: it makes them hold by construction, consolidates the
two paths into one admitted channel, and makes the verifier tier real with its
first complete admission boundary. Activation remains refused until a real
model snapshot is hashed and pinned in a separate reviewed change.

## What Changes

- **New capability `frozen-verifiers`.** The admission rule as spec: a verifier
  runs only under a pinned weights digest, a versioned label map, a green
  verification fixture set, and its opt-in gate; anything else degrades to
  *absence* (no label), never to a differently-produced label wearing the
  verifier's name. The pin registry is a repository artifact — no runtime
  configuration adds or alters a pin. Output is provenance-marked review-queue
  enrichment only.
- **`contradiction-queue` delta.** The existing `meta.polarity` enrichment is
  re-laned: after this change it is produced only by the admitted verifier
  (`polarity_method: "nli"`), gains `polarity_model_digest` and
  `polarity_label_map_version`, and is bound to the `signal_version` it was
  computed against. Heuristic-method queue labels are retired — the lexical
  stand-in had no admission control. A model polarity label never changes an
  entry's `signal_version`, never moves ranking, and is distinct from the
  reader-recorded competing-alternatives pair stance, which is a triage
  disposition and is untouched.
- **`command-surface` delta.** The write path loses the in-path call:
  `_refine_contradictions` leaves write-time warning generation, retiring the
  `contradiction-band` advisory kind with it (its only producer was that call).
  The canonical write-path-advisory requirement is modified to two kinds
  (near-duplicate, overlap). A dismissal recorded against a retired
  contradiction-band identity does not transfer: the pair may resurface once
  under the overlap identity — a stated one-time cost borne only by gate-on
  users, since the kind never fired on the default path.
- **Knob fate.** `EXOMEM_CLAIM_LEVEL` keeps gating the claim subsystem;
  `EXOMEM_CLAIM_POLARITY_NLI` (default off) remains the verifier's opt-in
  gate, now additionally requiring admission; `EXOMEM_CLAIM_NLI_MODEL` is
  retired — model identity comes only from the in-repo pin registry, and a
  set-but-ignored value is reported.
- **`nli` optional extra.** The cross-encoder dependency ships as a default-off
  extra; the label map (the current threshold logic, promoted to a versioned
  in-repo artifact) is label map v1. This slice ships an empty pin registry:
  model weights must be resident, hashed, and added by reviewed repository pin
  before the tier can admit, and runtime loading never fetches them.
- **Local-only in v1.** Hosted activation (an int8 classifier fits the cell
  envelope) is a separate decision and out of scope here.

## Impact

- Affected specs: `frozen-verifiers` (new), `contradiction-queue` (one added
  requirement), `command-surface` (one modified requirement: write-path
  advisory kinds).
- Affected code (implementation slice, after approval): `src/exomem/claims.py`
  (pin registry, label map, fixture verification, knob retirement),
  `src/exomem/corpus_aware.py` (remove `_refine_contradictions` and its
  invocation, the `contradiction-band` partition, `_POLARITY_CLAUSE`, and the
  now-dead `DupCandidate.polarity` / `polarity_score` / `polarity_method`
  fields), `src/exomem/audit.py` (`_pair_polarity` and its rendering become
  the single admitted channel), `pyproject.toml` (`nli` extra), doctor
  (verifier status line), tests including the verification fixture set.
- Affected tests, named: `tests/test_write_advisory_suppression.py`
  (contradiction-band suppression cases) and `tests/test_claims.py` (gate-on
  polarity pins) assert the retired behaviour and are updated red-first with
  the change, not deleted around it.
- Bench linkage, stated honestly: f22 is withheld until sequence 2 is
  acknowledged; this slice claims precision improvement on its *verification
  fixtures* only. Byte-identity claims are scoped to the default gate-off
  path; gate-on users get two stated changes (no write-time polarity, admitted
  verifier or absence on the queue).
