#!/usr/bin/env bash
# Install Debian packages on a CI runner with bounded, retried mirror access.
#
# `apt-get` applies no wall-clock bound of its own. When a mirror completes the
# TCP handshake and then stops sending, the fetch blocks indefinitely rather
# than failing, so the step inherits whatever timeout the job happens to carry.
# See the call site in .github/workflows/ci.yml for what that cost.
#
# Two independent bounds, because they fail differently: `Acquire::*::Timeout`
# breaks a silent stall on one request, and the retry loop survives a mirror
# that is refusing outright while a sibling mirror still answers. Callers add a
# step-level `timeout-minutes` as the outer backstop.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: ${0##*/} <package>..." >&2
  exit 2
fi

apt_get() {
  sudo apt-get \
    -o Acquire::Retries=3 \
    -o Acquire::http::Timeout=15 \
    -o Acquire::https::Timeout=15 \
    "$@"
}

for attempt in 1 2 3; do
  if apt_get update; then
    break
  fi
  if [[ "${attempt}" -eq 3 ]]; then
    echo "::error::apt-get update failed on three attempts; the package mirrors are unreachable from this runner" >&2
    exit 1
  fi
  echo "::warning::apt-get update failed (attempt ${attempt}/3); retrying"
  sleep $((attempt * 5))
done

apt_get install --yes "$@"
