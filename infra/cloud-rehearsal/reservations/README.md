# Reservation host proof

The `Cloud rehearsal` workflow runs this proof only for the dispatched `local-storage-drill` mode.
The host proof precedes the cluster drill on a disposable Ubuntu 24.04 GitHub runner.
This prerequisite prevents a cluster pass from hiding a broken host fence.
A fixture failure costs the maintainer a delayed drill and another disposable run.
It needs no credentials and contacts no production service.
It creates one sparse loop disk and one random volume group for each proof.
Its LVM configuration accepts only that loop device and retains native file locking.
Cleanup removes only that fixture's volume group, loop mapping and scratch directory.

`build.sh` uses the canonical provider assembler and its pinned upstream commit.
It reads generator versions from upstream `versions.mk` and `go.mod`.
It builds both shipped provider binaries from the archive with `-buildvcs=false`.
It copies these proof tests into the disposable assembly and compiles static test binaries with `CGO_ENABLED=0`.
`TOPOLVM_TOOLS_ROOT` can select an existing bundle with those exact generator versions.

The host proof blocks an actual LVM command on its native VG lock.
The [LVM file-lock implementation](https://github.com/lvmteam/lvm2/blob/main/lib/locking/file_locking.c) defines that lock path.
Kernel process, descriptor and lock observations identify the waiting host binary.
The test kills only the provider parent and verifies that the surviving LVM child retains its fence.
A competing transaction waits until that child exits after completing its physical metadata mutation.
The default namespace prefix and host LVM executable remain unchanged.
`PROOF_LVM_BINARY` selects only the path used to verify the host executable's inode.
It defaults to `/sbin/lvm`; a provider pod can use `/proc/1/root/sbin/lvm` for the same host binary.

The service proof uses real gRPC requests, separate provider processes and real LVM metadata.
It covers target persistence, physical orphan charges, competing ordinary allocations, cancellation epochs and stale family tokens.
An open snapshot device interrupts physical cleanup after token revocation.
The proof restarts the service with zero concurrency and checks the retained retiring charge before cleanup.
It also deletes and recreates the source to check its physical UUID boundary.

Two disposable source mutations must first fail with their expected invariant messages.
They remove descriptor handoff and target persistence, respectively.
The unmodified provider must then pass both tests.
Artifacts retain source pins, source hashes, binary hashes, host tool identities, budget frames and red/green output.
Missing observations remain failures; fixture errors cannot satisfy a negative control.
The `local-reservation-provider` artifact supplies the exact proved binaries and generated CRD to the cluster job.
It includes `build/reservation-host-proof` for the same fence proof inside the provider pod.
That job receives its download root through `RESERVATION_PROVIDER_ROOT`.

This proves the host binary boundary only.
It does not qualify CSI bindings, serving readiness, two online backup cycles or production activation.
The existing cluster rehearsal must supply those proofs.
