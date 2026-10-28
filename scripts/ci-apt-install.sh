#!/usr/bin/env bash
# Install Debian packages on a CI runner with bounded, retried mirror access.
#
# Socket timeouts do not bound the complete operation, so each phase also has
# an external deadline inside sudo. Root can then stop its own child processes.
# The shell retries index updates; apt retries network acquisition. Neither
# guarantees that another mirror answers.
# Download packages before the one installation. A slow download must not use
# dpkg's allowance. An interrupted installation fails without automatic replay.
set -euo pipefail

: "${CI_APT_UPDATE_TIMEOUT_SECONDS:=45}"
: "${CI_APT_DOWNLOAD_TIMEOUT_SECONDS:=150}"
: "${CI_APT_INSTALL_TIMEOUT_SECONDS:=150}"
: "${CI_APT_ATTEMPTS:=3}"

if [[ $# -eq 0 ]]; then
  echo "usage: ${0##*/} <package>..." >&2
  exit 2
fi

installed() {
  [[ "$(dpkg-query --show --showformat='${db:Status-Status}' "$1" 2>/dev/null)" == installed ]]
}

missing=()
for package in "$@"; do
  installed "${package}" || missing+=("${package}")
done

if [[ ${#missing[@]} -eq 0 ]]; then
  echo "already installed, no mirror needed: $*"
  exit 0
fi

apt_get() {
  local seconds="$1"
  shift
  sudo timeout --kill-after=10 "${seconds}" apt-get \
    -o Acquire::Retries=3 \
    -o Acquire::http::Timeout=15 \
    -o Acquire::https::Timeout=15 \
    "$@"
}

for attempt in $(seq 1 "${CI_APT_ATTEMPTS}"); do
  if apt_get "${CI_APT_UPDATE_TIMEOUT_SECONDS}" update; then
    break
  fi
  if [[ "${attempt}" -eq "${CI_APT_ATTEMPTS}" ]]; then
    # Deliberately not fatal. The runner may hold indexes new enough to resolve
    # these packages anyway, and if it does not, the install's own error names
    # the missing package -- which is a better report than one invented here.
    echo "::warning::apt-get update did not finish in ${CI_APT_ATTEMPTS} bounded attempts; attempting the install against whatever indexes exist"
    break
  fi
  echo "::warning::apt-get update exceeded ${CI_APT_UPDATE_TIMEOUT_SECONDS}s (attempt ${attempt}/${CI_APT_ATTEMPTS}); retrying"
  sleep $((attempt * 5))
done

apt_get "${CI_APT_DOWNLOAD_TIMEOUT_SECONDS}" install --yes --download-only "${missing[@]}"
apt_get "${CI_APT_INSTALL_TIMEOUT_SECONDS}" install --yes --no-download "${missing[@]}"

for package in "$@"; do
  if ! installed "${package}"; then
    echo "::error::required package is not installed: ${package}"
    exit 1
  fi
done
