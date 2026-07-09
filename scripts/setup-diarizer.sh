#!/usr/bin/env sh
# Build the isolated speaker-diarization sidecar venv (sidecar/diarizer/.venv).
#
# Run once per machine at deploy time. Requires uv on PATH. The running service
# later invokes this sidecar Python by path; it never builds the venv at runtime.
#
# Usage:
#   sh scripts/setup-diarizer.sh
#   sh scripts/setup-diarizer.sh --prewarm

set -eu

prewarm=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prewarm|-p)
      prewarm=1
      ;;
    --help|-h)
      sed -n '2,10p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      echo "usage: sh scripts/setup-diarizer.sh [--prewarm]" >&2
      exit 2
      ;;
  esac
  shift
done

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found on PATH; install uv first: https://docs.astral.sh/uv/" >&2
  exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
diarize_dir="$repo_root/sidecar/diarizer"

if [ ! -d "$diarize_dir" ]; then
  echo "diarizer sidecar directory not found: $diarize_dir" >&2
  exit 1
fi

echo "Building diarizer sidecar venv in $diarize_dir ..."
uv sync --directory "$diarize_dir"

py="$diarize_dir/.venv/bin/python"
if [ ! -x "$py" ]; then
  echo "sidecar venv build failed: $py missing" >&2
  exit 1
fi

"$py" - <<'PY'
import warnings
warnings.filterwarnings("ignore")
import torch
from pyannote.audio import Pipeline
from faster_whisper.audio import decode_audio
print(
    "diarizer sidecar OK | torch",
    torch.__version__,
    "| cuda",
    torch.cuda.is_available(),
    torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU",
)
PY

if [ "$prewarm" -eq 1 ]; then
  if [ -z "${HUGGINGFACE_TOKEN:-}${HF_TOKEN:-}" ]; then
    echo "HUGGINGFACE_TOKEN/HF_TOKEN not set; skipping prewarm (gated download would fail)." >&2
  else
    echo "Prewarming pyannote weights (downloads to the shared HF cache)..."
    "$py" - <<'PY'
import os
from pyannote.audio import Pipeline
model = os.environ.get("EXOMEM_DIARIZE_MODEL", "pyannote/speaker-diarization-3.1")
token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
Pipeline.from_pretrained(model, token=token)
print("weights cached")
PY
  fi
fi

echo "Done. Sidecar python: $py"
echo "Next: set HUGGINGFACE_TOKEN, EXOMEM_DIARIZE=1, enroll a speaker, then restart the service."