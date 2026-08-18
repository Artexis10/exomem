#!/usr/bin/env bash
# Install Debian packages on a CI runner with bounded, retried mirror access.
#
# `apt-get` applies no usable wall-clock bound of its own. `Acquire::*::Timeout`
# governs individual socket operations, and a mirror that dribbles bytes -- or
# stalls somewhere apt is not watching the clock -- sails straight past it. That
# was measured, not assumed: with those options set, a fetch still hung for the
# full five minutes a caller allowed it.
#
# So the bound that matters is external. Each attempt runs under `timeout`, and
# it runs *inside* `sudo` so the kill lands on a root-owned `apt-get` rather
# than on `sudo` itself -- otherwise the child survives, keeps the dpkg lock,
# and the retry fails on the lock instead of the mirror.
#
# The layering is deliberate:
#   per attempt   `timeout`               -- a stalled mirror loses its turn
#   across        the retry loop          -- a different mirror may answer
#   outermost     the caller's step cap   -- backstop if both are wrong again
# Retries are only worth having if a stall ends an attempt rather than the loop.
set -euo pipefail

: "${CI_APT_UPDATE_TIMEOUT_SECONDS:=45}"
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

apt_get "${CI_APT_INSTALL_TIMEOUT_SECONDS}" install --yes "${missing[@]}"
