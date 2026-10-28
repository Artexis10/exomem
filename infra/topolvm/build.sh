#!/usr/bin/env bash
set -euo pipefail

provider_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_repo=${1:?provide a pinned upstream Git checkout}
output_root=${2:?provide an unused output directory}
mode=${3:-build}
pin=$(cat "$provider_root/upstream-commit")
[[ $(git -C "$source_repo" rev-parse HEAD) == "$pin" ]]
[[ ! -e "$output_root" ]]
mkdir -p -- "$output_root"
git -C "$source_repo" archive "$pin" | tar -x -C "$output_root"
git -C "$output_root" apply --check "$provider_root/source.patch"
git -C "$output_root" apply "$provider_root/source.patch"
cp -R -- "$provider_root/overlay/." "$output_root/"
if [[ "$mode" == prepare ]]; then
    exit 0
fi

tools_root=${TOPOLVM_TOOLS_ROOT:?provide upstream-versioned bin and include directories}
make -C "$output_root" \
    CONTROLLER_GEN="$tools_root/bin/controller-gen" \
    PROTOC="PATH=$tools_root/bin:$PATH $tools_root/bin/protoc -I=$tools_root/include:." \
    pkg/lvmd/proto/lvmd.pb.go pkg/lvmd/proto/lvmd_grpc.pb.go manifests generate-api
if [[ "$mode" == generate ]]; then
    exit 0
fi
[[ "$mode" == build ]]
# An archive has no Git metadata; the pinned source commit supplies provenance.
make -C "$output_root" GOFLAGS="${GOFLAGS:-} -buildvcs=false" build-topolvm
