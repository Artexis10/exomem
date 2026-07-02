# Build the isolated speaker-diarization sidecar venv (sidecar/diarizer/.venv).
#
# Run ONCE per box at deploy time. Requires `uv` on PATH (same as the main `uv sync`); uv
# auto-fetches a compatible Python 3.12 for the sidecar. NOT needed at service runtime — the
# running service only invokes the sidecar's python.exe by path (extract._diarizer_sidecar_python).
# pyannote runs here, isolated, on a STANDARD CUDA torch (2.9.1+cu130, Blackwell sm_120) because it
# is incompatible with the main venv's bleeding-edge torch-2.12+cu132. The sidecar runs on GPU when
# available (EXOMEM_DIARIZE_DEVICE=auto, default) and falls back to CPU.
#
# Usage:
#   pwsh -File scripts/setup-diarizer.ps1            # build the venv
#   pwsh -File scripts/setup-diarizer.ps1 -Prewarm   # also download the gated pyannote weights now
#
# After this: set HUGGINGFACE_TOKEN (+ accept conditions for pyannote/speaker-diarization-3.1 and
# pyannote/segmentation-3.0 on huggingface.co), set EXOMEM_DIARIZE=1, and restart the service.
param([switch]$Prewarm)

$ErrorActionPreference = "Stop"

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$DiarizeDir = Join-Path $RepoRoot "sidecar\diarizer"

Write-Host "Building diarizer sidecar venv in $DiarizeDir ..."
uv sync --directory $DiarizeDir   # creates sidecar/diarizer/.venv from pyproject.toml + uv.lock

$Py = Join-Path $DiarizeDir ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "sidecar venv build failed: $Py missing" }

# Import smoke: proves the pinned stack (pyannote 4 / torch 2.9.1+cu130 / torchaudio 2.9.1)
# assembles and reports whether the GPU is visible (Blackwell sm_120). torchcodec's libtorchcodec
# load warning on Windows is harmless — we pre-decode and pass pyannote a waveform dict.
& $Py -c "import warnings; warnings.filterwarnings('ignore'); import torch; from pyannote.audio import Pipeline; from faster_whisper.audio import decode_audio; print('diarizer sidecar OK | torch', torch.__version__, '| cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"

if ($Prewarm) {
    # Pull the gated pyannote weights into the shared HF cache now, instead of on the first upload.
    # Needs HUGGINGFACE_TOKEN in the environment + accepted model conditions.
    if (-not ($env:HUGGINGFACE_TOKEN -or $env:HF_TOKEN)) {
        Write-Warning "HUGGINGFACE_TOKEN/HF_TOKEN not set - skipping prewarm (gated download would fail)."
    } else {
        Write-Host "Prewarming pyannote weights (downloads to the shared HF cache)..."
        & $Py -c "import os; from pyannote.audio import Pipeline; m=os.environ.get('EXOMEM_DIARIZE_MODEL','pyannote/speaker-diarization-3.1'); t=os.environ.get('HUGGINGFACE_TOKEN') or os.environ.get('HF_TOKEN'); Pipeline.from_pretrained(m, token=t)"
        Write-Host "  weights cached."
    }
}

Write-Host "Done. Sidecar python: $Py"
Write-Host "Next: set HUGGINGFACE_TOKEN, EXOMEM_DIARIZE=1, enroll a speaker, then restart.ps1."
