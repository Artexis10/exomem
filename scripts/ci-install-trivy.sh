#!/usr/bin/env bash
# Install trivy on a CI runner from the upstream release.
#
# The image licence check (scripts/check-image-licences.sh) runs trivy. The
# version and the SHA-256 of each release archive live in
# infra/tool-versions.env, the pin that the hosted infrastructure workflow
# reads too, so one edit moves both. The archive needs no package index. A
# runner of either architecture installs the archive that fits it.
set -euo pipefail

versions="$(dirname "${BASH_SOURCE[0]}")/../infra/tool-versions.env"
pin() { sed -n "s/^$1=//p" "${versions}" | head -n 1; }
version="$(pin TRIVY_VERSION)"
case "$(uname -m)" in
  x86_64) platform="Linux-64bit"; sha256="$(pin TRIVY_LINUX_AMD64_SHA256)" ;;
  aarch64 | arm64) platform="Linux-ARM64"; sha256="$(pin TRIVY_LINUX_ARM64_SHA256)" ;;
  *) echo "trivy: no pinned archive for $(uname -m)" >&2; exit 1 ;;
esac
[[ -n "${version}" && -n "${sha256}" ]] || { echo "trivy: no pin in ${versions}" >&2; exit 1; }
archive="${RUNNER_TEMP:?}/trivy-archive/trivy_${version}_${platform}.tar.gz"
target="${RUNNER_TEMP}/trivy"

mkdir -p "${archive%/*}" "${target}"
# The same bounded retry as the age and restic installs: seven attempts inside
# 150 s, so the step ends inside its five-minute limit.
curl --fail --silent --show-error --location \
  --retry 6 --retry-delay 20 --retry-max-time 150 --retry-all-errors \
  --connect-timeout 10 --max-time 60 \
  --output "${archive}" \
  "https://github.com/aquasecurity/trivy/releases/download/v${version}/trivy_${version}_${platform}.tar.gz"
echo "${sha256}  ${archive}" | sha256sum --check --strict --status - \
  || { echo "trivy ${version}: archive checksum mismatch" >&2; exit 1; }
tar -xzf "${archive}" -C "${target}" trivy
"${target}/trivy" --version | head -n 1
echo "${target}" >> "${GITHUB_PATH:?}"
