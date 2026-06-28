## Why

kb-mcp's opt-in ASR diarization (`KB_MCP_DIARIZE`) labels who-spoke-when but only
*anonymously*: `[Speaker A]: …`, `[Speaker B]: …`. For a personal vault the high-value
question is "what did **I** say in that meeting?" vs the other person — which anonymous
labels can't answer and `find` can't target. Q already runs a production speaker pipeline
that resolves anonymous clusters to **named** speakers via voice-embedding profiles. This
change ports that approach into kb-mcp — adapted to a vault (no DB; a small local profile
store) — so diarized transcripts carry real names and become findable by speaker.

It stays **pure-substrate**: voice-embedding speaker-ID is deterministic *measurement*
(ECAPA embedding + cosine match against an enrolled centroid), the same class as ASR/OCR/
CLIP — not a reasoning LLM, no judgment.

## What Changes

- **Voice-embedding speaker attribution** on top of the existing pyannote diarization:
  per anonymous cluster, compute a speechbrain **ECAPA** (192-dim) centroid, match it
  against enrolled **voice profiles** by cosine with margin/threshold rules (ported from
  Q), and relabel matched clusters with the profile name. Unmatched clusters keep stable
  anonymous labels (`Speaker A/B…`).
- A small local **voice-profile store** (JSON; operational infra — NOT note metadata, NOT
  a queryable markdown sidecar) mapping `name → {centroid, threshold, samples, is_self}`.
- A **CLI enrollment** path: `python -m kb_mcp enroll-speaker --name <N> [--self] <audio>`
  — extract an ECAPA centroid from a voice sample and persist a profile. `--self` marks the
  vault owner (the "what did I say" primary case). Companion `list-speakers` / `remove-speaker`.
- Bake in Q's hard-won correctness lessons: **TF32-disabled** ECAPA inference for embedding
  parity, **average-linkage** over-split cluster merge, **max-overlap** segment→turn
  assignment (port the pure-NumPy `speaker_attribution` + `speaker_assignment` modules).
- New dependency `speechbrain` added to the existing `diarization` extra. Gated pyannote
  weights continue to honor `HUGGINGFACE_TOKEN`.

Out of scope (future changes): a `speaker:` filter on `find`; a full cpWER/DER eval harness
(Q-grade); auto-enrollment / cross-corpus speaker clustering; MCP-tool enrollment (CLI only
for now); the broader engraph-style CLI/REST surface standardization (its own change).

## Capabilities

### New Capabilities
- `speaker-diarization`: named-speaker attribution for diarized media via local
  voice-embedding profiles — default-off, soft-fail, pure-substrate (measurement only).

## Impact

- Code: `src/kb_mcp/extract.py` (attribution hook in `_diarize`); new
  `src/kb_mcp/voice_profiles.py` (store), `src/kb_mcp/speaker_attribution.py` +
  `src/kb_mcp/speaker_assignment.py` (ported pure-NumPy logic), `src/kb_mcp/enroll_speaker.py`
  + a subcommand in `src/kb_mcp/__main__.py`; `media_worker.py` + `preserve.py` carry names
  through the extracted sidecar.
- Deps: `speechbrain` in the `diarization` extra; ECAPA model (~80–150 MB) downloads from HF
  at first use on the GPU box (same pattern as bge/whisper/CLIP).
- Default-off ⇒ zero behavior change when `KB_MCP_DIARIZE` is unset OR no profiles are
  enrolled — output is byte-identical to today's anonymous diarization.
- GPU: ECAPA runs on GPU but is subject to the same whisper-cuDNN-shadow gotcha as CLIP —
  it falls back to CPU under that condition (`KB_MCP_VOICE_DEVICE` override), soft-fail.
