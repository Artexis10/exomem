## Why

`EXOMEM_DIARIZE` diarization is **enrollment-ready but pipeline-blocked** on the live box. The
named-attribution layer (ECAPA voice profiles → cosine → `[Hugo]: …`) works, but the pyannote
*who-spoke-when* pipeline does not load: the service runs a custom **torch-2.12+cu132** (Blackwell)
build for whisper/CLIP/bge, and pyannote + torchaudio are fundamentally incompatible with it —
six distinct version walls (torchcodec native-lib load failure; `torchaudio.AudioMetaData` and
`list_audio_backends` removed in torchaudio 2.11; speechbrain 1.x `LazyModule` breaks
`inspect.getmodule`; `huggingface_hub` removed `use_auth_token`; the `use_auth_token`→`token`
kwarg rename). These are **version walls, not GPU walls** — shimming each is endless and CPU torch
alone does not fix them. The custom cu132 pin is what forces torchaudio 2.11 (the only torchaudio
that pairs with it), which forces the broken pyannote/speechbrain/hf_hub combo.

## What Changes

- **Run only the pyannote pipeline in an isolated sidecar venv on a standard CUDA torch**, as a
  subprocess. A new `sidecar/diarizer/` uv project pins Q's proven Blackwell stack
  (`pyannote.audio 4.x` package loading the `speaker-diarization-3.1` model / `torch 2.9.1+cu130`
  — which still ships `sm_120` kernels, "cu130 required; cu126 lacks sm_120" / `torchaudio 2.9.1`,
  Python 3.12). It runs **on the GPU** (`EXOMEM_DIARIZE_DEVICE=auto`, ~20× faster than CPU —
  measured 47s vs 18min for a 37-min track on an RTX 5080) and falls back to CPU. A standalone
  `worker.py` decodes audio (faster-whisper's PyAV `decode_audio` — same decoder as the main ASR,
  so turns share the whisper timebase), runs the pipeline, and writes `{"turns":[…]}` JSON to an
  out-file. (The bleeding-edge `torch 2.12+cu132` the main service uses is the one pyannote can't
  ride — it forces torchaudio 2.11, which dropped the APIs pyannote needs; `2.9.1+cu130` is the
  sweet spot with both `sm_120` and pyannote compatibility.)
- **`extract._run_diarization` becomes a thin subprocess seam**: locate the sidecar interpreter →
  spawn the worker → parse the out-file → `[(start, end, label)]`. Everything downstream
  (`_resolve_named_labels` → `voice_embed.embed_spans` ECAPA → `speaker_attribution` → `_diarize`)
  is unchanged and stays in the main venv — the sidecar only replaces the anonymous-turns step.
- **Remove `pyannote.audio` from the main `pyproject.toml` `diarization` extra** (keep `speechbrain`
  for ECAPA). This retires the per-`uv sync` `uv pip uninstall torchcodec` dance and removes a
  latent hazard: pyannote was the only thing pulling `torchcodec`, which breaks `sentence-transformers`
  (the bge/CLIP embedding stack) on the next restart.
- `scripts/setup-diarizer.ps1` provisions the sidecar venv (per box, deploy-time only — never at
  service runtime). New env vars `EXOMEM_DIARIZE_SIDECAR_PYTHON`, `EXOMEM_DIARIZE_TIMEOUT`.

It stays **pure-substrate**: the pyannote pipeline is a frozen clustering model (deterministic
transduction, no LLM) — the same class as ASR/OCR/CLIP. Moving it to a subprocess changes only
*where* it runs, not what it is.

Out of scope (future): a long-lived sidecar worker (this MVP spawns per file; fine for a
single-threaded media worker + occasional uploads); an ONNX (sherpa-onnx) backend that would
remove torch from the sidecar entirely.

## Capabilities

### Modified Capabilities
- `speaker-diarization`: the who-spoke-when pipeline now runs in an isolated CPU-torch sidecar
  subprocess instead of in-process — default-off, soft-fail (any sidecar failure → plain
  transcript), pure-substrate. Named attribution is unchanged.

## Impact

- Code: `src/exomem/extract.py` (`_run_diarization` rewritten to the subprocess seam; the dead
  in-process pipeline symbols `_load_diarization_pipeline` / `_get_diarization_pipeline` /
  `_DIARIZATION_PIPELINE` / `_DIARIZATION_LOCK` deleted — they held the only main-venv
  `import pyannote.audio`). New `sidecar/diarizer/{pyproject.toml,uv.lock,worker.py}`,
  `scripts/setup-diarizer.ps1`. `pyproject.toml` + `uv.lock` drop pyannote (and its transitive
  `torchcodec`) from the main venv.
- Deps: a second ~sub-GB CPU-torch venv, provisioned once per box via `setup-diarizer.ps1` (uv
  auto-fetches Python 3.12). pyannote checkpoints remain HF-gated (`HUGGINGFACE_TOKEN` + accepted
  conditions for `speaker-diarization-3.1` + `segmentation-3.0`).
- Default-off + soft-fail ⇒ zero behavior change when `EXOMEM_DIARIZE` is unset, the sidecar is
  unbuilt, or anything fails — output is byte-identical to the plain transcript.
- The sidecar's safety depends on the media worker staying single-threaded (one diarizer
  subprocess at a time); the timeout scales with audio duration so a hung child can't silently
  kill a valid long job, but also can't block the queue forever.
