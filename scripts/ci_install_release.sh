#!/usr/bin/env bash
# Install a published exomem release into a new virtual environment for the
# installed journey's compatibility checks. Dependencies come from the release's
# own uv.lock with their hashes, so a later upstream release cannot change what
# those checks run. The wheel installs from a local file URL with its digest, so its
# provenance record names the file and the digest that the journey verifies.
#
# Usage: ci_install_release.sh VERSION VENV WHEEL_SHA256
set -euo pipefail

version=$1
venv=$2
sha=$3
repository=${GITHUB_REPOSITORY:-Artexis10/exomem}
# The journey re-reads the wheel that the provenance record names, so the wheel stays in place.
work=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/exomem-release.XXXXXX")

curl -fsSL --retry 3 -o "$work/release.json" "https://pypi.org/pypi/exomem/$version/json"
wheel_url=$(jq -r '[.urls[] | select(.filename | endswith(".whl"))][0].url' "$work/release.json")
wheel="$work/$(basename "$wheel_url")"
curl -fsSL --retry 3 -o "$wheel" "$wheel_url"
echo "$sha  $wheel" | sha256sum -c -

mkdir "$work/project"
for file in pyproject.toml uv.lock; do
  curl -fsSL --retry 3 -o "$work/project/$file" "https://raw.githubusercontent.com/$repository/v$version/$file"
done
# --no-config ignores the tag's uv version pin; the frozen export needs none of its uv settings.
(cd "$work/project" && uv export --quiet --no-config --frozen --no-dev --no-emit-project -o "$work/locked.txt")
# --require-hashes needs --hash; the #sha256 fragment puts the digest into the provenance record.
echo "exomem @ file://$wheel#sha256=$sha --hash=sha256:$sha" >> "$work/locked.txt"

uv venv "$venv" --python 3.13
uv pip install --python "$venv/bin/python" --require-hashes -r "$work/locked.txt"
