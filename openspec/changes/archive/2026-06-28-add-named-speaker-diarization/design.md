# Design — Named-speaker diarization

## Context

exomem already has anonymous diarization (`extract._diarize`: pyannote
`speaker-diarization-3.1` → per-segment max-overlap label → `[Speaker A]:` turns,
gated by `KB_MCP_DIARIZE`, soft-fail to plain transcript). What it lacks is the step
from `SPEAKER_00` to **a name**. Q solves exactly this in production; this change ports
Q's portable core and adapts its storage to a vault.

## Goals / non-goals

- **Goal:** diarized transcripts carry enrolled names (`[Hugo]: …`) and stay anonymous
  (`[Speaker A]: …`) for unknown voices; names are BM25-findable in the transcript + the
  structured `speakers:` field.
- **Goal:** stay pure-substrate; default-off; soft-fail; zero change when unused.
- **Non-goal:** Q's full eval harness, cross-corpus clustering, a `find` speaker filter,
  MCP-tool enrollment. (Future changes.)

## Pure-substrate justification

ECAPA voice embedding is a frozen, deterministic function audio→vector (same class as
bge/CLIP/Whisper). Attribution is a fixed cosine comparison against an enrolled centroid
with deterministic thresholds — a *measurement*, not a judgment, and not an LLM. The human
decides who to enroll (a brain act, done once via CLI); the server only measures "this
cluster's voiceprint is within τ of the Hugo centroid." In-bounds.

## Architecture (port-from-Q, adapted)

Pipeline per media file when `KB_MCP_DIARIZE` is set AND ≥1 profile is enrolled:

1. **Diarize** (existing): pyannote → anonymous `Turn(start, end, speaker)` list.
2. **Embed clusters** (new): for each anonymous cluster, sample its spans and compute a
   speechbrain **ECAPA** (192-dim) centroid via `voice_embed.embed_spans(audio, spans)`.
   TF32 disabled for parity (`torch.backends.cuda.matmul.allow_tf32 = False`).
3. **Merge over-split** (new, ported): average-linkage agglomerative merge of clusters
   whose centroids are within `merge_threshold` (0.50 cosine) — recovers true speaker
   count when pyannote over-splits one voice.
4. **Attribute** (new, ported `speaker_attribution.attribute_clusters`): cosine each merged
   centroid against each profile; assign the profile name iff
   `score ≥ profile.threshold` AND `score − second_best ≥ margin` AND
   (`score ≥ threshold + confident_delta` OR `score − other_groups_best ≥ rel_gap`).
   Unmatched → stable `Speaker A/B…` by first-onset order.
5. **Assign + render** (existing + ported `speaker_assignment.assign_span` max-overlap):
   relabel each ASR segment with its turn's resolved label; collapse consecutive
   same-label segments → `[<label>]: text` turns + structured `speakers:` field.

Threshold defaults (from Q's evidence): `merge_threshold=0.50`, `margin=0.05`,
`confident_delta=0.15`, `rel_gap=0.10`, default per-profile `threshold=0.40`. All overridable
via `KB_MCP_VOICE_*` env vars.

## Decisions

- **Profile store = local JSON, not sqlite, not the vault content.** Few speakers (owner +
  a handful), human-inspectable, trivially backed up. Path: `<embeddings-sidecar-dir>/
  voice_profiles.json` (operational infra beside the embedding sidecar — NOT under the
  vault's note trees, NOT a queryable markdown sidecar; consistent with the "no sidecar on
  notes" rule which is about *note metadata*, not server infra). Schema:
  `{ "<name>": {"centroid": [192 floats], "threshold": 0.40, "samples": int, "is_self": bool,
  "updated": iso8601} }`.
- **Enrollment = CLI, not MCP tool.** Enrolling a voice is a desk-side admin act on a local
  audio file, not a vault-content write — it fits the `python -m kb_mcp` CLI (beside
  `install-hook`/`install-skill`), not the connector tool surface. A profile can be enrolled
  from multiple samples (averaged centroid; `samples` counts them). `--self` sets `is_self`.
- **Ported logic is pure-NumPy + unit-tested first.** `speaker_attribution.py` and
  `speaker_assignment.py` carry no torch and are the TDD anchor (deterministic thresholds,
  overlap math) — mirrors Q's torch-free `backend/scripts/common/` modules.
- **GPU device follows the CLIP precedent.** ECAPA via speechbrain uses torch's cuDNN; when
  whisper's cuDNN-12 PATH-prepend is active it can shadow torch's cuDNN-13 (the bug that
  broke CLIP). So ECAPA gets a `_voice_device()` that returns CPU when ASR/whisper is active,
  with `KB_MCP_VOICE_DEVICE` override — exactly the CLIP fix.
- **Soft-fail everywhere.** Missing `speechbrain`/model/GPU, an embedding error, or zero
  profiles → fall through to today's anonymous diarization (or plain transcript). Never raises.

## Data flow through the sidecar

`extract._diarize` already returns `(labeled_text, speakers)`; `speakers` entries gain a
resolved `speaker` name where matched. `media_worker._run_extraction` →
`preserve.update_sidecar_*` persist the named turns unchanged in shape — names just become
real strings instead of `Speaker A`. No sidecar schema migration.

## Risks

- Gated HF models (pyannote, and speechbrain's ECAPA if gated) need `HUGGINGFACE_TOKEN` on
  the box — same as existing diarization; documented.
- Short/cross-talk clusters embed poorly → low confidence → stays anonymous (correct
  failure mode: never *mis*-name, prefer `Speaker A`).
- First-run ECAPA download adds startup latency on first diarized file only (lazy singleton).
