#!/usr/bin/env sh
# Build the speaker-diarization sidecar venv from a source checkout.
#
# The venv lives under the machine-local state root, keyed by the sidecar's lock
# (see src/exomem/diarizer_sidecar.py). A managed service builds it with
# `<release python> -m exomem.diarizer_sidecar`; scripts/upgrade.sh does that
# after each switch while EXOMEM_DIARIZE is on.
#
# Usage:
#   sh scripts/setup-diarizer.sh
#   sh scripts/setup-diarizer.sh --prewarm

set -eu
repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
exec uv run --no-sync --directory "$repo_root" python -m exomem.diarizer_sidecar "$@"
