## ADDED Requirements

### Requirement: Isolated Diarization Execution

The system SHALL run the pyannote who-spoke-when pipeline in an isolated sidecar virtual
environment (a standard CUDA torch — GPU-capable, CPU fallback) as a subprocess, and SHALL NOT import pyannote in the main service
process. The main process SHALL pass the audio file path to the sidecar and receive speaker turns
as JSON; it SHALL resolve those anonymous turns to enrolled names via the existing main-process
ECAPA attribution, which is unaffected by the sidecar's pyannote version. Any failure of the
sidecar — its venv not provisioned, a spawn error, a nonzero exit, a timeout, or unparseable
output — SHALL be logged and degrade to the plain transcript (or anonymous diarization), and MUST
NOT raise.

#### Scenario: Diarization runs in the sidecar subprocess

- **WHEN** `EXOMEM_DIARIZE` is set and the diarizer sidecar venv is provisioned
- **THEN** the main process spawns the sidecar interpreter on `worker.py` with the audio path and
  an output-file path
- **AND** the sidecar writes `{"turns": [{"start", "end", "label"}, …]}` JSON to the output file
- **AND** the main process parses it into `[(start, end, raw_label)]` and feeds it to the unchanged
  named-attribution path

#### Scenario: Sidecar not provisioned degrades to plain transcript

- **WHEN** `EXOMEM_DIARIZE` is set but `sidecar/diarizer/.venv` (or `EXOMEM_DIARIZE_SIDECAR_PYTHON`)
  resolves to no interpreter
- **THEN** no subprocess is spawned, the condition is logged, and extraction emits the plain
  transcript
- **AND** the result is byte-identical to diarization being disabled

#### Scenario: Sidecar failure soft-fails

- **WHEN** the sidecar subprocess exits nonzero, times out, or writes no parseable turns
- **THEN** the failure is logged and the file's transcript falls back to plain ASR (no diarization)
- **AND** the transcript and its other extracted fields are persisted unchanged

#### Scenario: Main process never imports pyannote

- **WHEN** the main service venv has pyannote removed (the `diarization` extra installs only
  speechbrain for ECAPA)
- **THEN** diarization still functions via the sidecar
- **AND** the main venv never imports `pyannote.audio`, so the cu132 torchcodec/torchaudio
  incompatibility cannot affect the embedding stack

### Requirement: Diarization Sidecar Provisioning

The diarizer sidecar SHALL be a self-contained, reproducibly-pinned uv project that is provisioned
at deploy time and never built or resolved at service runtime. The sidecar SHALL pin a torch /
torchaudio / pyannote combination that is free of the main venv's version walls AND retains
Blackwell `sm_120` GPU kernels (a standard CUDA-13 torch), independent of the main venv's torch
pin. The running service SHALL invoke the sidecar interpreter only by path, selecting GPU or CPU
via `EXOMEM_DIARIZE_DEVICE`.

#### Scenario: Provisioned once per box

- **WHEN** an operator runs `scripts/setup-diarizer.ps1` on a box
- **THEN** the sidecar venv is built from its committed `pyproject.toml` + `uv.lock`
- **AND** the running service needs only the sidecar interpreter path thereafter, never `uv`

#### Scenario: Pinned stack clears the version walls and keeps the GPU

- **WHEN** the sidecar resolves its dependencies
- **THEN** torch is a CUDA-13 build whose `get_arch_list()` includes `sm_120` (the Blackwell GPU),
  and the pyannote package version is one that does not call the torchaudio functions the main
  venv's newer torchaudio removed
- **AND** the pyannote pipeline imports, sees the GPU, and diarizes on it outside the main venv
  (torchcodec may be present but is unused — decoding goes through the pre-decoded waveform path)
