#!/usr/bin/env bash
# Fail when a Python distribution in a container image is licensed only under
# GPL or AGPL.
#
# Usage: check-image-licences.sh [trivy image flags] IMAGE
#   IMAGE is a tag in the local Docker daemon or a registry reference, such as
#   a digest. `--platform linux/arm64` picks one platform of a multi-platform
#   image.
#
# trivy reads the licence that each installed distribution declares and
# classifies it against the SPDX identifiers in image-licences/trivy.yaml. A
# distribution offered under a choice of licences that includes a licence that
# is not GPL or AGPL fails only until image-licences/elections.yaml records the
# election. Operating-system packages are out of scope here: the scan of the
# shared libraries in each image lists them.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pinned="$(sed -n 's/^TRIVY_VERSION=//p' "${here}/../infra/tool-versions.env" | head -n 1)"
running="$(trivy --version | sed -n 's/^Version: //p')"
[[ -n "${pinned}" && "${running}" == "${pinned}" ]] \
  || { echo "trivy ${running:-missing} is not the pinned ${pinned}" >&2; exit 1; }

exec trivy image --skip-version-check \
  --config "${here}/image-licences/trivy.yaml" \
  --ignorefile "${here}/image-licences/elections.yaml" "$@"
