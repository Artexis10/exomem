#!/usr/bin/env bash
set -euo pipefail

proof_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
build_root=${1:?provide the completed build directory}
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted && ${RUNNER_OS:-} == Linux ]]
[[ $EUID == 0 && -d "$RUNNER_TEMP" && -x "$build_root/bin/host-fence" ]]
export EXOMEM_RESERVATION_RUNNER=disposable-github-runner
export PROOF_EVIDENCE="$build_root/evidence"
mkdir -p "$PROOF_EVIDENCE"
exec > >(tee "$PROOF_EVIDENCE/runner.log") 2>&1
phase=host-negative-control
trap 'result=$?; if [[ $result != 0 ]]; then printf "host_binary_proof=failed\nphase=%s\nexit=%d\nfull_qualification=false\n" "$phase" "$result" > "$PROOF_EVIDENCE/result.txt"; fi' EXIT
uname -a
cat /etc/os-release
dpkg-query --show lvm2 util-linux thin-provisioning-tools
/sbin/lvm version
/usr/bin/nsenter --version
sha256sum /usr/bin/nsenter /sbin/lvm

# A failed negative control must name the realistic defect, not fixture startup.
check_negative() {
    binary=$1
    test_name=$2
    expected=$3
    output="$PROOF_EVIDENCE/$test_name-red.log"
    result=0
    bash "$proof_root/fixture.sh" "$binary" "$test_name" "$output" || result=$?
    cat "$output"
    [[ $result != 0 && $result != 99 ]]
    grep -F -- "$expected" "$output"
}
check_negative "$build_root/bin/host-fence-negative" TestReservationHostLVMParentDeath 'host LVM fd0 does not retain provider lock inode'
phase=service-negative-control
check_negative "$build_root/bin/service-negative" TestReservationPhysicalRestart 'accepted target disappeared across provider SIGKILL'
for proof in host-fence service; do
    phase="$proof-canonical"
    if [[ $proof == host-fence ]]; then
        test_name=TestReservationHostLVMParentDeath
    else
        test_name=TestReservationPhysicalRestart
    fi
    output="$PROOF_EVIDENCE/$test_name-green.log"
    result=0
    bash "$proof_root/fixture.sh" "$build_root/bin/$proof" "$test_name" "$output" || result=$?
    cat "$output"
    [[ $result == 0 ]]
done
printf '%s\n' 'host_binary_proof=passed' 'csi_serving_readiness=not_tested' 'full_qualification=false' > "$PROOF_EVIDENCE/result.txt"
