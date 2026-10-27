#!/usr/bin/env bash
set -euo pipefail

[[ $EUID == 0 && ${EXOMEM_RESERVATION_RUNNER:-} == disposable-github-runner ]]

# Each invocation owns one fixture. Cleanup never enumerates or removes other VGs.
binary=$1
test_name=$2
output=$3
export PROOF_ROOT
PROOF_ROOT=$(mktemp -d "$RUNNER_TEMP/reservation-fixture.XXXXXXXX")
PROOF_VG="proof_$(basename "$PROOF_ROOT" | tr -cd '[:alnum:]')"
export PROOF_VG
export PROOF_POOL=pool
PROOF_FENCE_TAG="proof.$(basename "$PROOF_ROOT")"
export PROOF_FENCE_TAG
export LVM_SYSTEM_DIR="$PROOF_ROOT/lvm"
export PROOF_NATIVE_LOCKS="$PROOF_ROOT/native-locks"
export PROOF_PROVIDER_LOCKS="$PROOF_ROOT/provider-locks"
mkdir -p "$LVM_SYSTEM_DIR" "$PROOF_NATIVE_LOCKS" "$PROOF_PROVIDER_LOCKS"
loop_device=
proof_pid=
vg_created=0
pv_created=0
cleanup() {
    result=$?
    trap - EXIT
    set +e
    clean=0
    if [[ -n "$proof_pid" ]]; then
        kill -KILL -- -"$proof_pid" 2>/dev/null
        wait "$proof_pid" 2>/dev/null
    fi
    if [[ -n "$loop_device" ]]; then
        if [[ $vg_created == 1 ]]; then
            /sbin/lvm lvchange --activate n "$PROOF_VG"
            /sbin/lvm vgremove --force --yes "$PROOF_VG" || clean=1
        fi
        if [[ $clean == 0 && $pv_created == 1 ]]; then
            /sbin/lvm pvremove --force --yes "$loop_device" || clean=1
        fi
        if [[ $clean == 0 ]]; then
            losetup --detach "$loop_device" || clean=1
            if losetup "$loop_device" >/dev/null 2>&1; then
                echo "cleanup_failed: owned loop device remains attached: $loop_device"
                clean=1
            fi
        fi
    fi
    if [[ $clean == 0 ]]; then
        rm -rf -- "$PROOF_ROOT"
        echo "cleanup_complete: $PROOF_VG"
    fi
    if [[ $clean != 0 ]]; then
        echo "cleanup_failed: inspect owned fixture $PROOF_ROOT on $loop_device"
        exit 99
    fi
    exit "$result"
}
trap cleanup EXIT
truncate --size 1G "$PROOF_ROOT/disk.img"
loop_device=$(losetup --find --show "$PROOF_ROOT/disk.img")
# These regexes constrain device paths, never user or model meaning.
cat > "$LVM_SYSTEM_DIR/lvm.conf" <<EOF
devices {
    use_devicesfile = 0
    filter = [ "a|^$loop_device\$|", "r|.*|" ]
    global_filter = [ "a|^$loop_device\$|", "r|.*|" ]
}
global {
    wait_for_locks = 1
    locking_dir = "$PROOF_NATIVE_LOCKS"
    use_lvmlockd = 0
}
# The hosted runner runs udevd, as a production host does; disabling LVM's udev sync races its device probes.
activation {
    monitoring = 0
}
EOF
/sbin/lvm pvcreate --yes "$loop_device"
pv_created=1
# The host's udev pvscan must not autoactivate the fixture's LVs while the fixture changes them.
/sbin/lvm vgcreate --setautoactivation n --physicalextentsize 4M "$PROOF_VG" "$loop_device"
vg_created=1
# A metadata spare serves only pool repair, which no proof exercises.
if ! /sbin/lvm lvcreate --yes --type thin-pool --size 512M --poolmetadatasize 16M --poolmetadataspare n \
    --name "$PROOF_POOL" "$PROOF_VG"; then
    # Name what held the device, so a fixture failure is never read as a proof result.
    dmsetup info -c || true
    ps -eo pid,args | grep -E '[p]vscan|[u]disks|[m]ultipath|[l]vm' || true
    exit 1
fi
/sbin/lvm lvcreate --yes --size 4M --name fence "$PROOF_VG"
/sbin/lvm lvmconfig --type current --mergedconfig devices/global_filter global/wait_for_locks global/locking_dir
setsid "$binary" "-test.run=^$test_name\$" -test.v -test.timeout=3m > "$output" 2>&1 &
proof_pid=$!
wait "$proof_pid"
