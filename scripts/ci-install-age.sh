#!/usr/bin/env bash
# Install the age encryption tools on a CI runner from the upstream release.
#
# The protected-import tests skip without the real `age` and `age-keygen`, so
# this step fails rather than continue without them. The release archive is
# pinned by version and SHA-256. It needs no package index and no dpkg, whose
# mirror download of this one package timed out several CI jobs.
set -euo pipefail

version="v1.2.1"
sha256="7df45a6cc87d4da11cc03a539a7470c15b1041ab2b396af088fe9990f7c79d50"
archive="age-${version}-linux-amd64.tar.gz"
target="${RUNNER_TEMP:?}/age"

mkdir -p "${target}"
# GitHub answered release downloads with HTTP 504 for at least 38 s on
# 2026-11-08, and curl's own backoff spent four retries in about 15 s. A fixed
# 20 s delay spreads seven attempts over two minutes. No retry starts after
# 150 s, so the step ends inside its five-minute limit.
curl --fail --silent --show-error --location \
  --retry 6 --retry-delay 20 --retry-max-time 150 --retry-all-errors \
  --connect-timeout 10 --max-time 30 \
  --output "${target}/${archive}" \
  "https://github.com/FiloSottile/age/releases/download/${version}/${archive}"
echo "${sha256}  ${target}/${archive}" | sha256sum --check --strict --quiet -
tar -xzf "${target}/${archive}" -C "${target}" --strip-components=1 age/age age/age-keygen
"${target}/age" --version
echo "${target}" >> "${GITHUB_PATH:?}"
