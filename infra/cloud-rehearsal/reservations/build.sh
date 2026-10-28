#!/usr/bin/env bash
set -euo pipefail

proof_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$proof_root" rev-parse --show-toplevel)
build_root=${1:?provide an unused scratch directory}
[[ ! -e "$build_root" ]]
mkdir -p -- "$build_root"
pin=$(cat "$repo_root/infra/topolvm/upstream-commit")
git clone --filter=blob:none --no-checkout https://github.com/topolvm/topolvm.git "$build_root/upstream"
git -C "$build_root/upstream" checkout --detach "$pin"

# Read generator versions through upstream's own Make definitions.
versions=$(make --no-print-directory -s -C "$build_root/upstream" -f - <<'MAKE'
include versions.mk
.PHONY: proof-versions
proof-versions:
	@echo $(PROTOC_VERSION) $(CONTROLLER_TOOLS_VERSION) $(PROTOC_GEN_GO_VERSION) $(PROTOC_GEN_GO_GRPC_VERSION)
MAKE
)
read -r protoc controller_tools protoc_go protoc_grpc <<< "$versions"
tools_root=${TOPOLVM_TOOLS_ROOT:-$build_root/tools}
if [[ ! -v TOPOLVM_TOOLS_ROOT ]]; then
    mkdir -p "$tools_root/bin"
    curl --fail --silent --show-error --location \
        "https://github.com/protocolbuffers/protobuf/releases/download/v$protoc/protoc-$protoc-linux-x86_64.zip" \
        --output "$build_root/protoc.zip"
    unzip -q "$build_root/protoc.zip" 'bin/protoc' 'include/*' -d "$tools_root"
    GOBIN="$tools_root/bin" go install "sigs.k8s.io/controller-tools/cmd/controller-gen@v$controller_tools"
    GOBIN="$tools_root/bin" go install "google.golang.org/protobuf/cmd/protoc-gen-go@v$protoc_go"
    GOBIN="$tools_root/bin" go install "google.golang.org/grpc/cmd/protoc-gen-go-grpc@v$protoc_grpc"
fi
export TOPOLVM_TOOLS_ROOT="$tools_root"
"$repo_root/infra/topolvm/build.sh" "$build_root/upstream" "$build_root/canonical" build
cp "$proof_root/host_fence_test.go" "$build_root/canonical/internal/lvmd/command/reservation_host_proof_test.go"
cp "$proof_root/service_test.go" "$build_root/canonical/internal/lvmd/reservation_physical_proof_test.go"
mkdir -p "$build_root/bin" "$build_root/evidence"
(
    cd "$build_root/canonical"
    CGO_ENABLED=0 go test -buildvcs=false -p 1 -c -o "$build_root/bin/host-fence" ./internal/lvmd/command
    CGO_ENABLED=0 go test -buildvcs=false -p 1 -c -o "$build_root/bin/service" ./internal/lvmd
)
cp "$build_root/bin/host-fence" "$build_root/canonical/build/reservation-host-proof"

# Test-only mutations remove the two mechanisms whose absence must be detected.
# Each applies to a disposable source assembly; canonical inputs stay untouched.
cp -a "$build_root/canonical" "$build_root/negative"
git -C "$build_root/negative" apply "$proof_root/negative-controls.patch"
(
    cd "$build_root/negative"
    CGO_ENABLED=0 go test -buildvcs=false -p 1 -c -o "$build_root/bin/host-fence-negative" ./internal/lvmd/command
    CGO_ENABLED=0 go test -buildvcs=false -p 1 -c -o "$build_root/bin/service-negative" ./internal/lvmd
)
{
    printf 'repository_commit=%s\n' "$(git -C "$repo_root" rev-parse HEAD)"
    printf 'repository_commit_tree=%s\n' "$(git -C "$repo_root" rev-parse 'HEAD^{tree}')"
    printf 'upstream_commit=%s\n' "$pin"
    printf 'generators=%s\n' "$versions"
    go version
    sha256sum "$repo_root/infra/topolvm/source.patch" "$build_root/bin/"* "$build_root/canonical/build/lvmd" "$build_root/canonical/build/hypertopolvm"
    find "$repo_root/infra/topolvm/overlay" "$proof_root" -type f -print0 | sort -z | xargs -0 sha256sum
} > "$build_root/evidence/source-identity.txt"
(
    cd "$build_root/canonical"
    find . -type f ! -path './build/*' -print0 | sort -z | xargs -0 sha256sum
) > "$build_root/evidence/assembled-files.sha256"
sha256sum "$build_root/evidence/assembled-files.sha256" >> "$build_root/evidence/source-identity.txt"
cp "$repo_root/infra/topolvm/upstream-commit" "$build_root/canonical/upstream-commit"
python3 "$proof_root/provenance.py" "$repo_root" "$build_root/canonical"
