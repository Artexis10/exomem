# Build the speaker-diarization sidecar venv from a source checkout.
#
# The venv lives under the machine-local state root, keyed by the sidecar's lock
# (see src/exomem/diarizer_sidecar.py). After it is built: set HUGGINGFACE_TOKEN
# (and accept the conditions for pyannote/speaker-diarization-3.1 and
# pyannote/segmentation-3.0 on huggingface.co), set EXOMEM_DIARIZE=1, and restart
# the service.
#
# Usage:
#   pwsh -File scripts/setup-diarizer.ps1            # build the venv
#   pwsh -File scripts/setup-diarizer.ps1 -Prewarm   # also download the gated pyannote weights now
param([switch]$Prewarm)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Arguments = @("run", "--no-sync", "--directory", $RepoRoot, "python", "-m", "exomem.diarizer_sidecar")
if ($Prewarm) { $Arguments += "--prewarm" }
& uv @Arguments
if ($LASTEXITCODE -ne 0) { throw "diarizer sidecar setup failed" }
