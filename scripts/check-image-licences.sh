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

# One scan, read as JSON: a forbidden licence fails the check, and each licence
# that trivy cannot classify (free text, or a licence no category lists) is
# printed as a warning. Free text cannot be classified reliably, so it is shown
# to the release reader instead of gating the release.
report="$(mktemp)"
trap 'rm -f "${report}"' EXIT
trivy image --skip-version-check --quiet \
  --config "${here}/image-licences/trivy.yaml" \
  --ignorefile "${here}/image-licences/elections.yaml" \
  --severity CRITICAL,UNKNOWN --exit-code 0 --format json --output "${report}" "$@"

jq -r '.Results[]? | .Licenses[]? | select(.Severity == "UNKNOWN")
  | "::warning title=Unclassified licence::\(.PkgName): \(.Name)"' "${report}"
forbidden="$(jq -r '.Results[]? | .Licenses[]? | select(.Severity == "CRITICAL")
  | "\(.PkgName): \(.Name) (\(.FilePath // "-"))"' "${report}")"
if [[ -n "${forbidden}" ]]; then
  echo "GPL- or AGPL-only Python distributions:" >&2
  echo "${forbidden}" >&2
  exit 1
fi
echo "No GPL- or AGPL-only Python distribution found."
