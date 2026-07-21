#!/usr/bin/env bash
# Deploy the HA edge worker labeled with the current git SHA.
#
# A bare `wrangler deploy` still works, but /__version then reports
# "git_sha": "unlabeled" and doctor's ingress check surfaces that as a
# warning. This script exists so the deploy identity is not silently lost.
#
# Usage:
#   bash deploy/cloudflare-ha/deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

die() { echo "deploy: $*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git not found; install git to resolve the deploy SHA."
command -v npx >/dev/null 2>&1 || die "npx not found; install Node.js to run wrangler."

git_sha="$(git rev-parse --short HEAD)" || die "git rev-parse --short HEAD failed; is this a git checkout?"

echo "Deploying exomem-ha-edge at ${git_sha} ..."
npx wrangler deploy --var "WORKER_GIT_SHA:${git_sha}"
