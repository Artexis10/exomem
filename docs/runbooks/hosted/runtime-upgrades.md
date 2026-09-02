<!-- authority:non-specification -->

# Hosted runtime upgrades

Use this controller workflow for every Hosted runtime release. Concrete versions,
digests, commits, cell IDs, and evidence belong in one private operation directory,
not in this runbook. The execution advances only through:

`selected -> trusted -> expanded -> inventoried -> rolling -> drained -> contracted -> promoted -> accepted -> complete`.

The deployment procedure in [`deploy.md`](deploy.md) remains the platform effector.
It consumes a verified lock member; it does not authorize an upgrade by itself.

## Select and trust the target

The target is the ten-field output of `hosted_image_candidate.py verify`. The
Substrate checkout must be clean at the reviewed consumer commit, and every output
path below must name a new file.

```bash
set -euo pipefail
umask 077
operation_dir="${EXOMEM_RUNTIME_UPGRADE_OPERATION_DIR:?set a private operation directory}"
substrate_checkout="${EXOMEM_SUBSTRATE_CHECKOUT:?set the reviewed Substrate checkout}"
mkdir -p "$operation_dir"
operation_dir="$(cd "$operation_dir" && pwd -P)"
target="$operation_dir/target.json"
execution="$operation_dir/execution.json"

infra/scripts/hosted_runtime_upgrade.py create \
  --execution "$execution" \
  --execution-id "${EXOMEM_RUNTIME_UPGRADE_EXECUTION_ID:?set the durable execution ID}" \
  --target "$target" \
  --at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

(cd "$substrate_checkout" && pnpm exec tsx scripts/verify-exomem-hosted-runtime-trust.ts \
  --target "$target" --output "$operation_dir/substrate-trust.json")
infra/scripts/hosted_runtime_upgrade_orchestrator.py prove-trust \
  --execution "$execution" \
  --substrate-trust "$operation_dir/substrate-trust.json" \
  --facts-output "$operation_dir/trusted-facts.json"
execution_sha="$(infra/scripts/hosted_runtime_upgrade.py inspect \
  --execution "$execution" | jq -r .executionSha256)"
infra/scripts/hosted_runtime_upgrade.py advance \
  --execution "$execution" --expected-sha256 "$execution_sha" --phase trusted \
  --facts "$operation_dir/trusted-facts.json" \
  --at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

The trust proof rejects a changed target, dirty/unreviewed consumer commit, missing
pinned consumer, or changed agent/gateway fixture before composition.

## Inventory and compose expand

Create a reviewed runtime catalog containing the exact eight-field fleet identity for
the target, the currently selected prior runtime, and each older runtime that exact
retained evidence proves may still be live. Its closed shape is `{"runtimes": [...]}`.
Derive it from signed targets and retained contracts, never from a mutable tag or an
observed pod's self-assertion. An unknown workload identity makes collection fail
closed; add it only after recovering its exact retained compatibility evidence.

Collect Substrate, provisioner, and Kubernetes authority in one pass:

Normally the collector executes inside the installed provisioner API. For the first
upgrade from a provisioner that predates `exomem-provisioner-fleet-observe`, verify
the incoming provisioner candidate and set
`EXOMEM_PROVISIONER_BOOTSTRAP_IMAGE` to its exact digest-pinned image. This selects a
one-shot, tokenless observer Job that carries no real API bearer or provider signer
and is deleted before its observation is accepted. Never use a mutable tag or an
image that did not pass the signed-candidate checks. Omit the option once the
installed provisioner contains the collector.

The collector shells out to `kubectl`. A k3s node has no bare `kubectl` — it is
`k3s kubectl` — so when running this on the node, put a wrapper on a private path
and name it with `--kubectl` (or `EXOMEM_KUBECTL`). Do not install a system-wide
shim; the operator workspace owns it:

```bash
install -d -m 0700 "$operation_dir/bin"
printf '#!/bin/sh\nexec k3s kubectl "$@"\n' > "$operation_dir/bin/kubectl"
chmod 0700 "$operation_dir/bin/kubectl"
```

```bash
provisioner_observer_args=()
if test -n "${EXOMEM_PROVISIONER_BOOTSTRAP_IMAGE:-}"; then
  provisioner_observer_args+=(
    --provisioner-bootstrap-image "$EXOMEM_PROVISIONER_BOOTSTRAP_IMAGE"
    --timeout-seconds 60
  )
fi
infra/scripts/hosted_fleet_inventory.py collect \
  --substrate-endpoint "${EXOMEM_SUBSTRATE_FLEET_ENDPOINT:?set the operator endpoint}" \
  --substrate-token-file "${EXOMEM_SUBSTRATE_OPERATOR_TOKEN_FILE:?set the private token file}" \
  --runtime-catalog "$operation_dir/runtime-catalog.json" \
  --target "$target" --observed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --kubectl "$operation_dir/bin/kubectl" \
  "${provisioner_observer_args[@]}" \
  --inventory-output "$operation_dir/inventory-before-expand.json" \
  --facts-output "$operation_dir/inventory-before-expand-facts.json"
```

Resolve every reported ghost, missing binding, divergent identity, stale capacity
claim, active assignment, or unfinished operation before continuing. Then provide
immutable source commits and contract digests for exactly the inventory's
`legacyRuntimes`. Missing, duplicate, mutable, or extra descriptor units are refused.
An empty fleet uses an explicit empty `units` list rather than omitting the proof.

```bash
infra/scripts/hosted_runtime_upgrade_orchestrator.py derive-legacy \
  --inventory "$operation_dir/inventory-before-expand.json" \
  --descriptors "$operation_dir/runtime-descriptors.json" \
  --authority-output "$operation_dir/authoritative-legacy-release-set.json" \
  --catalog-output "$operation_dir/legacy-catalog.json"
```

Compose the pair with `hosted_composition_lock.py`, using the derived legacy files,
the exact signed runtime/provisioner inputs, and both coupled trust inputs:

```text
--runtime-upgrade <runtime-upgrade.json>=<sha256>
--substrate-trust <substrate-trust.json>=<sha256>
```

The composer requires exact agreement on target, Substrate consumer commit,
trust-report digest, and all pinned sites. The signed runtime closes at its own
candidate source commit; the provisioner closes through the platform composition
commit. Never widen the runtime closure to a later source revision merely to make an
older immutable release appear current. Verify the pair with
`verify_hosted_release.py` and deploy only its `expand` member through `deploy.md`.

Collect a second inventory with the same runtime catalog. Expansion is accepted only
when the fleet projection is byte-identical apart from observation timestamps and
source-observation hashes:

```bash
infra/scripts/hosted_runtime_upgrade_orchestrator.py prove-adoption \
  --execution "$execution" --pair "$operation_dir/deployment-lock-pair.json" \
  --before-inventory "$operation_dir/inventory-before-expand.json" \
  --after-inventory "$operation_dir/inventory-after-expand.json" \
  --exomem-commit "${EXOMEM_RUNTIME_UPGRADE_COMMIT:?set the reviewed Exomem commit}" \
  --evidence-output "$operation_dir/expand-adoption-proof.json" \
  --facts-output "$operation_dir/expanded-facts.json"
```

Advance to `expanded` with `expanded-facts.json`, then to `inventoried` with
`inventory-before-expand-facts.json`. Use the current execution SHA as the optimistic
fence on every `advance` command.

## Canary and sequential rollforward

Create the rollout plan before advancing to `rolling`. A fleet with legacy cells
requires one explicit canary. A dependency-free fleet omits `--canary-cell` and
records a no-op.

For the state-root transition, the reviewed target must declare the migration
workload for every cell. Do not accept `runtime-drained` as zero-pod evidence:
the provider observation must show no tenant runtime pod before the target-image
Job starts. Once that Job reports state migration complete, **never roll back to the old image after state migration**. Any fingerprint, health, route, or
readiness failure after that checkpoint keeps the routes closed and recovers
forward with the target image; the old image no longer has write authority for
the migrated volume.

```bash
rollout_args=()
if test -n "${EXOMEM_RUNTIME_CANARY_CELL_ID:-}"; then
  rollout_args+=(--canary-cell "$EXOMEM_RUNTIME_CANARY_CELL_ID")
fi
infra/scripts/hosted_runtime_upgrade_orchestrator.py begin-rollout \
  --execution "$execution" \
  --inventory "$operation_dir/inventory-before-expand.json" \
  "${rollout_args[@]}" \
  --evidence-output "$operation_dir/rollout-plan.json" \
  --facts-output "$operation_dir/rolling-facts.json"
```

Advance to `rolling` with `rolling-facts.json`. For a non-empty rollout, `next-cell`
returns only the explicit canary, an already active cell to resume, or the next stable
cell ID:

```bash
infra/scripts/hosted_runtime_upgrade_orchestrator.py next-cell \
  --execution "$execution" --rollout-plan "$operation_dir/rollout-plan.json"
```

Create the Substrate assignment and fenced rollforward operation for that one cell,
execute the provisioner's rollforward, and save its bounded result as
`cell-outcome.json`. Record both the start and terminal checkpoint; never start a later
cell before the prior one is `complete`.

```bash
infra/scripts/hosted_runtime_upgrade_orchestrator.py record-cell \
  --execution "$execution" \
  --expected-sha256 "$(infra/scripts/hosted_runtime_upgrade.py inspect \
    --execution "$execution" | jq -r .executionSha256)" \
  --facts "$operation_dir/cell-outcome.json" \
  --at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Completion requires the same cell, binding, assignment, operation, volume, exact
target identity, and equal pre/post canonical vault fingerprint. The first failed or
post-record recovery-required checkpoint stops selection and changes the next safe
action to `hold_expand_and_recover`. Fingerprints come from the fixed
`exomem-provisioner-vault-fingerprint` command in the pinned provisioner image, not
from the tenant runtime; admission permits only that image, command, restricted
identity, bounded resources, and read-only canonical-vault mount.

## Drain and contract

After all cells are complete or no-op, collect a fresh inventory. Contract is blocked
until every authority reports zero legacy runtimes, active assignments, and unfinished
operations and the original pair lineage is unchanged.

```bash
infra/scripts/hosted_runtime_upgrade_orchestrator.py prove-contract \
  --execution "$execution" --pair "$operation_dir/deployment-lock-pair.json" \
  --inventory "$operation_dir/inventory-before-contract.json" \
  --evidence-output "$operation_dir/zero-legacy-proof.json" \
  --facts-output "$operation_dir/drained-facts.json"
```

Advance to `drained`, deploy and reverify only the pair's `contract` member, collect
`inventory-after-contract.json`, and advance to `contracted` with the contract
deployment evidence digest.

## Promote and accept

Run the free gate before creating reviewer authority or starting its clock:

```bash
infra/scripts/hosted_runtime_upgrade_orchestrator.py promotion-preflight \
  --execution "$execution" \
  --inventory "$operation_dir/inventory-after-contract.json" \
  --output "$operation_dir/promotion-preflight.json"
```

Only then run the existing prepare/run reviewer boundary. Import both Claude and
OpenAI evidence chains within their real assignment validity and promote. A
non-reviewer personal account must then prove exact target identity, OAuth, bootstrap,
recall, governed write/read-back, refresh, reconnect, and authority/capacity/unfinished
operation leak checks. Keep the accepted personal tenant live, collect the final fleet
inventory, and record its digest before advancing through `promoted`, `accepted`, and
`complete`.

## Stop and recovery

At any interruption, inspect the execution and obey its recovery action:

```bash
infra/scripts/hosted_runtime_upgrade.py recover --execution "$execution"
```

Before any target cell is recorded, recovery may restore the prior platform lock.
After a target observation or preservation record exists, keep expand active and use
the explicit recovery path. Never delete, relabel, or automatically downgrade an
ordinary tenant or its volume.
