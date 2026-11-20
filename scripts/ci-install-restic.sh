#!/usr/bin/env bash
# Install restic on a CI runner from the upstream release.
#
# The personal vault backup tests run a real restic against a disposable
# repository, and they fail in CI without it. The Dockerfile pins the release
# and its SHA-256 for the cloud image; this script reads the same pin, so one
# edit moves both. The release archive needs no package index.
#
# The workflow restores the archive from the Actions cache into
# `$RUNNER_TEMP/restic-archive`, keyed on this file. The archive name carries
# the version, so a version change downloads a new archive. A cached archive
# that fails the checksum is downloaded again.
set -euo pipefail

dockerfile="$(dirname "${BASH_SOURCE[0]}")/../Dockerfile"
pin() { sed -n "s/^ARG $1=//p" "${dockerfile}" | head -n 1; }
version="$(pin RESTIC_VERSION)"
sha256="$(pin RESTIC_SHA256_AMD64)"
[[ -n "${version}" && -n "${sha256}" ]] || { echo "restic: no pin in the Dockerfile" >&2; exit 1; }
archive_name="restic_${version}_linux_amd64.bz2"
archive="${RUNNER_TEMP:?}/restic-archive/${archive_name}"
target="${RUNNER_TEMP}/restic"

verified() {
  [[ -f "${archive}" ]] && echo "${sha256}  ${archive}" | sha256sum --check --strict --status -
}

mkdir -p "${archive%/*}" "${target}"
if verified; then
  echo "restic ${version}: cached archive"
else
  rm -f "${archive}"
  # The same bounded retry as the age install: seven attempts inside 150 s,
  # so the step ends inside its five-minute limit.
  curl --fail --silent --show-error --location \
    --retry 6 --retry-delay 20 --retry-max-time 150 --retry-all-errors \
    --connect-timeout 10 --max-time 30 \
    --output "${archive}" \
    "https://github.com/restic/restic/releases/download/v${version}/${archive_name}"
  verified || { echo "restic ${version}: archive checksum mismatch" >&2; exit 1; }
fi
# Rename into place: writing over a binary that a process still runs fails with "Text file busy".
bunzip2 --stdout "${archive}" > "${target}/restic.new"
chmod 755 "${target}/restic.new"
mv -f "${target}/restic.new" "${target}/restic"
"${target}/restic" version
echo "${target}" >> "${GITHUB_PATH:?}"
