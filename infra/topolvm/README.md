# TopoLVM reservation overlay

This directory extends [TopoLVM](https://github.com/topolvm/topolvm) v0.41.1 at the commit in `upstream-commit`.
The upstream source uses the Apache License 2.0; `LICENSE` preserves that license.
The source patch and overlay use the same license.

`source.patch` contains changes to existing upstream source. `overlay/` contains new source and tests.
These files are canonical. Generated protobuf, API and CRD files belong in the build directory.

`build.sh` accepts a pinned upstream checkout, an unused output directory and `prepare`, `generate` or `build`.
It exports the pinned commit, applies the patch with Git, copies the overlay, and uses upstream generators and build targets.
The default mode is `build`; `prepare` only assembles source.

Set `TOPOLVM_TOOLS_ROOT` to a directory with the upstream-versioned `bin/` tools and protobuf `include/` directory.
Upstream `versions.mk` and `go.mod` define protoc, protoc-gen-go, protoc-gen-go-grpc and controller-gen versions.
The `generate` mode runs those generators without building binaries.
No mode publishes an image or changes deployment pins.

## Policy and RPC transport

The optional DeviceClass `reservation-policy` has these keys:

- `backup-concurrency`: the number of new backup families that may run together.
- `ordinary-volume-cap-bytes`: the largest permitted ordinary target.
- `lock-directory`: an existing, stable host-mounted directory shared across daemon restarts.

A guarded class must use a thin pool with overprovision ratio 1.
Arbitrary `lvcreate-options` and option classes are unavailable under guarded allocation because they can override tags, sizes or foreground execution.
Without the policy, existing volume behavior remains available. Reserved metadata tags remain provider-owned.

The LVService adds these RPCs:

| RPC | Request | Response |
| --- | --- | --- |
| `BeginBackupFamily` | `FamilyRequest` | `FamilyResponse` |
| `EndBackupFamily` | The same pre-Begin `FamilyRequest` | `FamilyResponse` |
| `GetBackupFamily` | `FamilyRequest` with the source identity | `FamilyResponse` |
| `ReserveLVTarget` | `device_class`, `source_volume`, `physical_source_uuid`, `target_bytes` | `reserved_bytes`, `physical_uuid` |
| `GetPoolBudget` | `device_class` | One physical budget frame |

`FamilyRequest` contains `device_class`, `source_volume`, `physical_source_uuid`, `expected_closed_epoch` and `request_id`.
`FamilyResponse` contains `token`, `source_size_bytes`, `state`, `closed_epoch`, `epoch`, `request_id` and `physical_source_uuid`.
States are the closed protocol `active`, `retiring` and `closed`.
Tokens are opaque and bind the physical source UUID, epoch and request identity.

A budget frame contains `pool_size_bytes`, `ordinary_bytes`, `effective_backup_reserve_bytes`, `outstanding_family_reservation_bytes`, `largest_size_bytes`, `backup_concurrency`, `observed_at`, `refresh_after_seconds` and `allocations`.
Each allocation contains `name`, `physical_uuid`, `pool`, `size_bytes`, `reserved_bytes`, `ordinary` and `tags`.
Only ordinary allocations can offset counted cell commitments. Family children already spend their family's reservation.

## LogicalVolume and CSI bindings

Source requests use `spec.backupFamilyRequest` with `operation`, `physicalSourceUUID`, `expectedClosedEpoch` and `requestID`.
The fixed operations are `Begin` and `End`.
`status.backupFamily` returns `token`, `sourceSizeBytes`, `state`, `closedEpoch`, `epoch`, `requestID` and `physicalSourceUUID`.

An ordinary target uses `spec.reservationTargetBytes` and `spec.reservationTargetPhysicalUUID` in one update.
`status.reservedBytes` retains its accepted target.
`status.reservationTargetResult` echoes `physicalUUID`, `requestedBytes`, `reservedBytes`, `code`, `message` and `observedGeneration`.
A failed result omits the unknown `reservedBytes`.
A matching resource-exhausted result leaves the serving volume status unchanged.
The existing controller backoff retries the retained target.
Family closure runs before a target request and remains reachable during source deletion.
Reservation does not resize the source. A later, smaller request cannot reduce the target.
The caller waits for an acknowledgement of that exact request before rendering a larger claim.
Kubernetes spec and status use nonnegative signed integers; checked conversion preserves unsigned provider accounting.
The node reconciler observes missing physical identities once and reads the provider family when no request is pending.

Snapshot Secret data uses `topolvm.io/backup-family-token` and `topolvm.io/immediate-source-uuid`.
The controller copies these submitted values into immutable child `spec.familyToken` and `spec.immediateSourceUUID`.
A clone copies the token from its specific snapshot and uses that snapshot's `status.physicalUUID`.
It never reads the serving source's current family. Existing opaque volume and snapshot handles do not change.

The existing node publisher writes `reservation.topolvm.io/<deviceClass>` for guarded classes.
Its JSON contains `version: 1`, `observed_at`, `refresh_after_seconds`, `node_uid`, pool size, ordinary bytes, effective reserve, largest size and ordinary `allocations`.
Allocation details contain `name`, `physical_uuid` and `reserved_bytes` only for matching LogicalVolumes on that node.
Unclaimed physical volumes remain included in ordinary bytes. The publisher preserves the provider observation time and advertises the existing heartbeat interval.

## Metadata and fence

One bounded JSON record on the physical source retains the family epoch, state and fixed child identities.
The record uses one LVM tag and one `lvchange` metadata commit.
Ordinary intent tags retain targets; ownership tags enter the same `lvcreate` command as each child.
LVM's [tag contract](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/7/html/logical_volume_manager_administration/lvm_tags) limits each tag to 1024 characters.
Unknown or malformed provider metadata returns a pending error instead of free capacity.

Every guarded transaction separately opens the pool's stable lock inode.
Each command receives that transaction's open description directly through standard input.
The parent closes its description without unlocking it. The lock path is never unlinked or replaced.
The default namespace prefix remains `/usr/bin/nsenter -m -u -i -n -p -t 1 /sbin/lvm`.
Native LVM locks remain enabled.

## Proof limits

The Go tests replace only the LVM backend for provider transactions.
They cover competing claims, cancellation, stale tokens, physical recreation, orphan charges, concurrency reduction, partial commands and source growth.
Separate process tests prove direct descriptor handoff and goroutine serialization.
Non-privileged runs use upstream `SKIP_TESTS_USING_ROOT=1`; they skip root-only loop-device tests.
Controller and node publication tests use the Kubernetes fake client; the upstream cluster suites remain sandbox work.
These tests do not qualify descriptor retention through the actual namespace prefix and host LVM binary.
The disposable infrastructure drill must provide that proof before activation.
