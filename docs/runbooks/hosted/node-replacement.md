<!-- authority:non-specification -->

# Node resize or replacement

## Preconditions

Use replacement only after a saved plan is reviewed and the retained-volume
registry, external database, static SOPS set, and latest verified backups are
available. The exact replacement address requires `--allow-destructive`.
A change of CPU architecture also needs
[its own precondition](#before-you-change-any-node) first.

```bash
export TF_CLOUD_ORGANIZATION=replace-with-approved-org
export TF_TOKEN_app_terraform_io=read-from-secret-manager
infra/scripts/plan.sh foundation /run/user/$UID/foundation-replacement.tfplan
infra/scripts/apply_saved_plan.sh foundation /run/user/$UID/foundation-replacement.tfplan \
  --allow-destructive hcloud_server.alpha
```

Apply the saved plan, regenerate inventory, converge Ansible twice, apply SOPS
static secrets, reinstall Helm, then run provider-fence rediscovery before any
lifecycle mutation.

## Verify

```bash
fleet_vars_text="$(infra/scripts/active_ansible_vars.py hosted-node)"
mapfile -t fleet_vars <<< "${fleet_vars_text}"
infra/scripts/verify_ansible_convergence.py --inventory infra/ansible/inventory.yml "${fleet_vars[@]}"
kubectl get nodes,pv
```

All acknowledged writes must be present and cells ready inside the recorded
60-minute node-replacement RTO.

## Change the CPU architecture

Every release publishes the Exomem Cloud cell and cellctl images for
`linux/amd64` and `linux/arm64` under one index digest. One cluster runs one
architecture: the fleet server and every agent are all x86, or all Arm64 (a
Hetzner CAX server). Agent types are x86 only, so Terraform refuses agents
beside a CAX fleet server. The k3s role refuses an agent whose architecture
differs from the server's.

### Before you change any node

Every image that the cluster runs must list the target architecture. Today the
Substrate gateway image, `ghcr.io/substrate-systems/substrate-gateway`, is
published for `linux/amd64` only. A move to Arm64 stops here until Substrate
publishes it for `linux/arm64`. Cells that run a release from before
multi-platform publishing also fail this check.

1. On the node, under a break-glass identity, list every image that the
   cluster's workloads name. The list covers the platform chart, its
   dependencies and every cell:

   ```bash
   {
     kubectl get deployments,statefulsets,daemonsets,jobs -A -o jsonpath='{range .items[*]}{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}{end}'
     kubectl get cronjobs -A -o jsonpath='{range .items[*]}{range .spec.jobTemplate.spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.jobTemplate.spec.template.spec.containers[*]}{.image}{"\n"}{end}{end}'
   } | sort -u > cluster-images.txt
   ```

2. Copy `cluster-images.txt` to a machine with Docker Buildx and `jq`, signed
   in to every private registry in the list.
3. Check each image for the target platform. The loop prints each image that
   lacks it:

   ```bash
   target=linux/arm64
   while read -r image; do
     docker buildx imagetools inspect --raw "$image" \
       | jq -e --arg target "$target" 'any(.manifests[]?.platform; .os + "/" + .architecture == $target)' >/dev/null \
       || echo "no $target: $image"
   done < cluster-images.txt
   ```

4. Go on only when the loop prints nothing.

### Rebuild each cell's vector index

The same chunk's bge-m3 int8 vectors differ between architectures (cosine
0.9685–0.987 between a cx33 and a cax21, 1.000000 on either alone, measured
2026-10-10). The recall sidecar records its model, not the architecture, so no
check rebuilds it. After the node change, rebuild each cell's index on the new
architecture before you accept the cell.

Do not use `maintain_memory --mode fix --rebuild-embeddings` for this. Fix mode
also rewrites notes, and it reports success when the rebuild fails.

The recall sidecar is in the cell's derived state,
`/data/host/.local/state/exomem/state/vault-<hash>/`. The pointer file
`.embeddings.active` names the sidecar that serves, `.embeddings.<16 hex>.sqlite`.
A cell from before the pointer existed serves `.embeddings.sqlite`. Each
sidecar can have `-wal` and `-shm` files beside it. The procedure moves all of
them.

For each cell:

1. Stop the cell and start the helper pod, as
   [import steps 5 and 6](../cloud-operator-import.md#5-stop-the-cell) do.
2. Move every `.embeddings.*` file into a dated directory on the cell's
   volume, then delete the helper pod. Backups do not include `/data/.vectors-aside-*`:

   ```bash
   # shellcheck disable=SC2016
   no_runtime_pod && helper sh -euc '
     state=$(ls -d /data/host/.local/state/exomem/state/vault-*)
     aside=/data/.vectors-aside-$(date -u +%F)
     [ "$(printf "%s\n" "$state" | wc -l)" -eq 1 ] || { echo "expected one vault state directory; nothing moved" >&2; exit 1; }
     [ ! -e "$aside" ] || { echo "$aside exists; nothing moved" >&2; exit 1; }
     mkdir -m 700 "$aside"
     mv "$state"/.embeddings.* "$aside"/
     ls -A "$aside"
   ' && kubectl -n "$NS" delete pod owner-restore --wait=true
   ```

3. Start the cell, as [import step 7](../cloud-operator-import.md#7-start-the-cell)
   does. The cell's own initial build recreates the index. Meanwhile recall
   answers from the pages built so far and adds `embeddings` to
   `warming.components`.
4. Read the build's state from the cell's doctor report. Doctor reads the
   sidecars from disk:

   ```bash
   kubectl -n "$NS" exec -i cell-0 -- python3 -I - <<'PY'
   import json, subprocess
   report = json.loads(subprocess.run(["exomem", "doctor", "--json"], capture_output=True, text=True).stdout)
   checks = {check["id"]: check for check in report["checks"]}
   reembed = checks["embeddings.reembed"]
   details = reembed.get("details") or {}
   sidecar = (checks.get("embeddings.sidecar") or {}).get("details") or {}
   print(json.dumps({
       "reembed": reembed["status"],
       "serving": details.get("serving"),
       "building": details.get("building"),
       "pages": details.get("paths_total"),
       "chunks": sidecar.get("vector_count"),
   }, indent=1))
   PY
   ```

   While the build runs, `reembed` is `warn` and `building.paths_done` counts
   up to `pages`. Record the last `pages` value.
5. Repeat step 4 until the build is done: `reembed` is `pass`, `building` is
   null, and `serving.sidecar` names a `.embeddings.<16 hex>.sqlite` file.
   `chunks` must be at least the last `pages` value, because every page with
   text has at least one chunk.
6. Accept the cell with recall, governance status and a governed write. A
   recall answer must carry no `warming` block that names `embeddings`.
7. After cellctl's next scheduled backup of the cell succeeds, delete the
   moved-aside directory. The backup has succeeded when `last_backup_at` on the
   cell's row is later than the move. The runtime never reads the directory, so
   the cell keeps running:

   ```bash
   kubectl -n "$NS" exec cell-0 -- rm -rf -- "/data/.vectors-aside-<date>"
   ```

On 2026-10-10 the move script, the doctor reader and the delete ran as
written against the amd64 cloud image under Docker, on 300-page and 60-page
volumes. The 300-page rebuild took about 50 s and ended at 300 chunks. Doctor
raised the cell's memory from 265 MiB to 728 MiB at most. No real cell has
moved between architectures yet, and the `kubectl` commands above have not run
against a cluster.
