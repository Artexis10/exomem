<!-- authority:non-specification -->

# Dedicated K3s agents (servers Terraform does not create)

Add, unlock, repair or remove an Exomem Cloud K3s agent on a machine Terraform
does not create: a Hetzner dedicated or auction server, or a server at another
provider (openspec `move-cloud-cells-to-local-storage` D2 and D8). You install
the OS. Ansible does the rest:

- a private link to the cluster: a vSwitch VLAN on Hetzner, WireGuard elsewhere;
- md RAID1 over two data disks or partitions, with LUKS2 on top;
- Clevis bound to Tang on the K3s server, plus an escrowed recovery passphrase;
- the `cells` volume group and its thin pool for TopoLVM;
- a K3s agent that starts only after the storage is unlocked.

Terraform-created agents keep [cloud-node-pool.md](cloud-node-pool.md).

## Preconditions

- The server is bought and reachable in its provider's rescue system or
  installer. Nothing here buys hardware or calls a provider API.
- Exomem Cloud runs TopoLVM with the `thin` device class, or you accept that
  the agent publishes 0 cell slots until it does. The join says which.
- The Tang key set is escrowed, once, before the first dedicated agent. Generate
  it with `jose` and hand it straight to SOPS. It is never written to disk or
  shown:

  ```bash
  repo_root="$(git rev-parse --show-toplevel)"
  matrix="$repo_root/infra/contracts/secret-destinations-v1.json"
  { jose jwk gen -i '{"alg":"ES512"}'; jose jwk gen -i '{"alg":"ECMR"}'; } | jq -cs '{keys: .}' \
    | infra/scripts/secret_handoff.py --matrix "$matrix" --repository-root "$repo_root" \
        --secret dedicated_host_tang_keys --version v1 \
        --destination ansible.hosted-node.tang-keys.active \
        --destination escrow.tang-keys.active --source stdin
  ```

  Escrowing activates nothing. Activate the key set in a reviewed commit that
  adds `"ansible.hosted-node.tang-keys.active": "v1"` to the `destinations` of
  `infra/contracts/active-ansible-selection-v1.json`.

- Each host has its own recovery passphrase, so each host has its own entry in
  `infra/contracts/secret-destinations-v1.json`. Add it in the change that adds
  the host, using the host name with every `-` written as `_` in the variable:

  ```json
  "dedicated_host_recovery_passphrase_exomem_agent_dx1": {
    "sources": [{"kind": "stdin"}],
    "destinations": {
      "ansible.hosted-node.recovery-passphrase-exomem-agent-dx1.active": {
        "kind": "sops_ansible_vars", "slot": "active",
        "target": "infra/secrets/ansible/recovery-passphrase-exomem-agent-dx1.{version}.sops.json",
        "variable": "dedicated_host_recovery_passphrase_exomem_agent_dx1"
      },
      "escrow.recovery-passphrase-exomem-agent-dx1.active": {
        "kind": "sops_escrow", "slot": "active",
        "target": "infra/secrets/escrow/recovery-passphrase-exomem-agent-dx1.{version}.sops.json",
        "secret_key": "passphrase"
      }
    }
  }
  ```

  Then generate and escrow it:

  ```bash
  openssl rand -base64 48 | tr -d '\n' | infra/scripts/secret_handoff.py \
    --matrix "$matrix" --repository-root "$repo_root" \
    --secret dedicated_host_recovery_passphrase_exomem_agent_dx1 --version v1 \
    --destination ansible.hosted-node.recovery-passphrase-exomem-agent-dx1.active \
    --destination escrow.recovery-passphrase-exomem-agent-dx1.active --source stdin
  ```

  In the same change, activate it: add
  `"ansible.hosted-node.recovery-passphrase-exomem-agent-dx1.active": "v1"` to
  `infra/contracts/active-ansible-selection-v1.json`.

## Install the OS

The host runs Ubuntu 24.04 with no swap, and two unformatted data disks or
partitions of equal size.

On Hetzner, boot the rescue system and copy
`infra/ansible/installimage/dedicated-host-autosetup.conf` to `/autosetup`. Fill in
the disks and host name, then run `installimage`. It puts the OS on a small
RAID1 and leaves the rest of both disks unallocated. Still in the rescue
system, add one unformatted partition per disk in the free space:

```bash
sgdisk --new=0:0:0 --typecode=0:fd00 --change-name=0:exomem-cells /dev/nvme0n1
sgdisk --new=0:0:0 --typecode=0:fd00 --change-name=0:exomem-cells /dev/nvme1n1
ls -l /dev/disk/by-id/ | grep -- '-part4'
```

On a server with separate data disks, list only the OS disks in the autosetup
and skip `sgdisk`. At another provider, use its installer the same way: OS on
its own disks or partitions, and two data disks or partitions left
unformatted.

Name data disks by `/dev/disk/by-id/`, because kernel names can change between
boots. The role refuses a data disk that holds a filesystem, a partition table
or another array. It refuses one that something holds or mounts in every case.
If a disk carries old signatures you mean to destroy, list it under `wipe` for
the bootstrap run, then remove that entry.

Enroll the host in NetBird with substrate-infra's `ansible/administration.yml`,
as [cloud-node-pool.md](cloud-node-pool.md#add-a-node) describes. `site.yml` refuses a host
it cannot administer over NetBird.

## Choose the private link

Precondition for either link: every node runs flannel at a `k3s_flannel_mtu`
of 1400, so VXLAN packets are 1400 bytes. A vSwitch carries at most 1400, and
the role's WireGuard interface 1420. Until the MTU is set, flannel takes the
Cloud interface's 1450, and a dedicated agent never receives those packets. The
role refuses a vSwitch or WireGuard host until the MTU is set, so roll it out
first, as in [Roll out the flannel MTU](#roll-out-the-flannel-mtu).

- **Hetzner dedicated: vSwitch.** Create the vSwitch in Robot, attach the
  server, and set the foundation variable. Plan and apply as usual. This adds
  one subnet and changes nothing else:

  ```bash
  # terraform.tfvars (foundation)
  #   vswitch = { id = 54321, vlan_id = 4000, subnet_cidr = "10.50.2.0/24" }
  infra/scripts/plan.sh foundation /run/user/$UID/foundation-vswitch.tfplan
  infra/scripts/apply_saved_plan.sh foundation /run/user/$UID/foundation-vswitch.tfplan
  ```

  Give the host an address in that subnet. It must not be the gateway, which
  is the subnet's first address.
- **Other provider: WireGuard.** Pick a private address outside the Hetzner
  network, for example from `10.51.0.0/24`. Every K3s node peers with the host
  over UDP 51820, and the inter-node firewall admits it on `wgexomem`.

## Roll out the flannel MTU

Run this once per cluster, before the first dedicated join, in an operator
window. The k3s role leaves `k3s_flannel_mtu` unset, and installs no flannel
configuration while it is unset. The rollout is a reviewed commit that sets
`k3s_flannel_mtu: 1400` in `infra/ansible/roles/k3s/defaults/main.yml`. 1400 is
`k3s_vswitch_mtu`, the smallest dedicated link.

A node's first `site.yml` run with the value installs the flannel
configuration. It adds `flannel-conf` to the node's K3s configuration, and
`cluster-cidr` on the server. It then recreates `flannel.1`: it stops K3s,
deletes the device and starts K3s. A plain restart would keep the old MTU. Cell
pods keep running while K3s is stopped. The run fails unless `flannel.1` and
`FLANNEL_MTU` in `/run/flannel/subnet.env` both read 1350, which is 1400 less
VXLAN's 50 bytes.

1. Check that every node's pod range is inside the role's `k3s_cluster_cidr`, `10.42.0.0/16`: `kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name} {.spec.podCIDR}{"\n"}{end}'`. If a range is outside it, stop: the role's value does not match the cluster.
2. Do not start a platform deploy or a cell image release until the last step is done.
3. Merge the reviewed commit only now: after the merge, every `site.yml` run, the deploy gate included, rolls the MTU out on each node it reaches.
4. Converge the server, with the inventory and variables of [Add the host](#add-the-host): `infra/scripts/ansible_with_sops.sh --inventory infra/ansible/inventory.yml "${fleet_vars[@]}" -- --limit exomem-alpha`.
5. If the cluster has agents, converge one canary agent, then each other agent, one at a time. End the same command with `-- --limit 'exomem-alpha,<agent>'`: an agent's run needs the server's facts from the same run, and the server reports no change.
6. On each node, read the MTU: `ssh exomem-admin@<node> 'cat /sys/class/net/flannel.1/mtu; grep FLANNEL_MTU /run/flannel/subnet.env'`.
7. If the cluster has agents, restart the pods that run on the pod network, so that they take the new MTU. In each of `kube-system`, `exomem-platform`, `exomem-cloud` and `exomem-edge`, run `kubectl rollout restart deployment --namespace <namespace>`, then the same for `daemonset` and `statefulset`. Then restart each cell in turn: `kubectl --namespace exo-cell-<id> delete pod cell-0`.

Expected result: each run reports "Require flannel.1 and new pods at the
configured MTU" as passed, and step 6 prints `1350` and `FLANNEL_MTU=1350` on
every node. A pod that started before its node's run keeps the old MTU until it
restarts, which matters only for traffic to a pod on another node. With the
server as the only node, which is production until an agent token is
selected, no pod traffic crosses nodes, so step 7 has nothing to do.

If a run fails at that check, run the same command again. Every run reads each
node's `flannel.1`, and recreates it while it runs at another MTU.

To undo while no dedicated node is joined, set `k3s_flannel_mtu: 1450`, the
Cloud network's MTU, in a reviewed commit, and repeat steps 4 to 7. Flannel
then sends 1450-byte packets again, as it did with the value unset. The role
refuses 1450 once a vSwitch or WireGuard host is in the inventory, and it
refuses to unset the value once it has applied one.

## Add the host

Describe the host in a private JSON file. It is never committed:

```json
{
  "exomem-agent-dx1": {
    "ipv4": "203.0.113.40",
    "admin_address": "100.64.0.40",
    "private_ip": "10.50.2.40",
    "link": "vswitch",
    "data_disks": [
      "/dev/disk/by-id/nvme-SAMSUNG_A-part4",
      "/dev/disk/by-id/nvme-SAMSUNG_B-part4"
    ]
  }
}
```

Then generate the inventory and converge every node. Never pass `--limit`:
every node must learn the new peer, and the join checks that it did.

```bash
terraform -chdir=infra/terraform/foundation output -json > /run/user/$UID/foundation.json
chmod 0600 /run/user/$UID/foundation.json
infra/scripts/generate_ansible_inventory.py /run/user/$UID/foundation.json \
  infra/ansible/inventory.yml --user exomem-admin \
  --admin-addresses "${EXOMEM_ADMIN_ADDRESSES:?private NetBird address map required}" \
  --dedicated-hosts "${EXOMEM_DEDICATED_HOSTS:?private dedicated host list required}"
fleet_vars_text="$(infra/scripts/active_ansible_vars.py hosted-node)"
mapfile -t fleet_vars <<< "${fleet_vars_text}"
infra/scripts/ansible_with_sops.sh --inventory infra/ansible/inventory.yml "${fleet_vars[@]}"
```

`active_ansible_vars.py` passes the version of each hosted-node Ansible
variable that `infra/contracts/active-ansible-selection-v1.json` selects: the
tokens, the etcd keys, the Tang keys and each dedicated host's passphrase. It
refuses when a selected version's file is missing. A version that is escrowed
but not selected is never passed.

The first run fixes the thin pool's geometry: 64 KiB chunks, and 64 bytes of
metadata per chunk computed from the pool's size. Above about 15.8 TiB of pool,
the metadata stops at LVM's 15.81 GiB limit; set a larger
`dedicated_host_thin_chunk_kib` in `group_vars/hosted_nodes.yml` before that run.

Every later `site.yml` run needs the Tang keys and every dedicated host's
passphrase. Each run proves that the escrowed passphrase still opens the array
and that Tang still unlocks it.

If this run is the first with an agent token selected, it restarts the K3s
server with that token. Run steps 4 to 6 of
[Rotate the agent token](#rotate-the-agent-token) straight after it, before
anything else restarts the server, with step 6's first-selection credentials.

A WireGuard host's public key goes in the host list, so that a run limited to
the server can configure the host as a peer without reaching it. After the
host's first join, and after each rotation of its key:

1. Copy the key from the run's output line `Record <key> as exomem-agent-dx1's wireguard_public_key`.
2. Add `"wireguard_public_key": "<key>"` to the host's entry in the host list.
3. Regenerate the inventory as above.

Expected result: the next run prints no `Record` line for the host. If the
host's own key no longer matches its record, the run stops before it configures
any peer, and prints the host's key to record.

## Verify

```bash
ssh exomem-admin@100.64.0.40 'cat /proc/mdstat; sudo cryptsetup status exomem_cells; sudo clevis luks list -d /dev/md/exomem-cells; sudo vgs cells; sudo lvs cells'
ssh exomem-admin@100.64.0.40 'systemctl is-active exomem-cells-ready.service k3s-agent.service'
kubectl get node exomem-agent-dx1 -o jsonpath='{.metadata.annotations.capacity\.topolvm\.io/thin}'
```

The array is a clean or resyncing RAID1, `exomem_cells` is an active LUKS2
mapping, Clevis lists one `tang` binding to the server's private address, and
`cells/pool0` is a thin pool. The node annotation is a positive byte count.
Then reboot the host once and check that it comes back with K3s running and no
operator action.

## Unlock by hand

A rebooting agent waits for Tang. If Tang is down, the host still boots to
NetBird, and K3s waits in `exomem-cells-ready.service`. Unlock with the
escrowed passphrase. It travels from SOPS to the host over SSH and is never
shown:

```bash
sops decrypt --extract '["passphrase"]' \
  infra/secrets/escrow/recovery-passphrase-exomem-agent-dx1.v1.sops.json \
  | ssh exomem-admin@100.64.0.40 'sudo cryptsetup open --key-file=- /dev/md/exomem-cells exomem_cells'
```

K3s starts by itself once the volume group appears. A `site.yml` run does the
same unlock.

## Tang lost or replaced

If the server is rebuilt, `site.yml` reinstalls the escrowed Tang keys. Every
existing binding keeps working, and nothing is rebound.

If the Tang keys themselves are lost:

1. Escrow a new key set as the next version, as in Preconditions, with `--version v2`.
2. In a reviewed commit, set `ansible.hosted-node.tang-keys.active` to `v2` in `infra/contracts/active-ansible-selection-v1.json`.
3. Run `site.yml` as in Add the host.
4. On each dedicated host, list the bindings: `sudo clevis luks list -d /dev/md/exomem-cells`.
5. Remove the binding the role replaced: `sudo clevis luks unbind -d /dev/md/exomem-cells -s <old slot> -f`.

Expected result: each host lists one `tang` binding, and `site.yml` reports no
change. In step 3, each host's role finds that its binding no longer unlocks,
and binds again with its passphrase.

## Rotate the Tang keys

Tang keeps a key it no longer advertises as a hidden `.jwk` file, and still
answers recovery requests with it. Old bindings therefore keep unlocking until
you delete those files.

1. Escrow a new key set as the next version, as in Preconditions, with `--version v2`.
2. In a reviewed commit, set `ansible.hosted-node.tang-keys.active` to `v2` in `infra/contracts/active-ansible-selection-v1.json`.
3. Run `site.yml` as in Add the host. The server now advertises the v2 keys.
4. Compute the new signing key's thumbprint on your machine. Only the thumbprint is printed:

   ```bash
   thp=$(sops decrypt --extract '["jwks"]' infra/secrets/escrow/tang-keys.v2.sops.json \
     | jq -c '.keys[] | select(.alg == "ES512")' | jose jwk thp -i- -a S256)
   ```

5. On each dedicated host, note the current Tang slot: `sudo clevis luks list -d /dev/md/exomem-cells`.
6. Bind a new slot pinned to the new key. The passphrase travels from SOPS over SSH:

   ```bash
   sops decrypt --extract '["passphrase"]' \
     infra/secrets/escrow/recovery-passphrase-exomem-agent-dx1.v1.sops.json \
     | ssh exomem-admin@100.64.0.40 \
       "sudo clevis luks bind -f -d /dev/md/exomem-cells -k - tang '{\"url\":\"http://10.50.1.10:7500\",\"thp\":\"$thp\"}'"
   ```

7. Remove the slot you noted in step 5: `sudo clevis luks unbind -d /dev/md/exomem-cells -s <old slot> -f`.
8. When every host is rebound, delete the hidden keys on the server: `sudo find /var/lib/tang -name '.*.jwk' -delete`.
9. Run `site.yml` again.

Expected result: step 9 reports no change on any dedicated host. A host you
missed fails its Tang test in step 9, and binds again from its passphrase.
Until then, it needs a manual unlock after a reboot. If the key directory is
not `/var/lib/tang`, `systemctl cat tangd@.service` shows it.

## Replace a failed disk

`site.yml` reports a degraded array but does not repair it. Nothing alerts on
it yet (OpenSpec task 5.5). After the provider swaps the disk, run these steps
on the host. `<new>` and `<surviving>` are whole disks, for example
`/dev/nvme1n1` and `/dev/nvme0n1`.

1. Identify the new disk and the surviving disk: `ls -l /dev/disk/by-id/` and `cat /proc/mdstat`.
2. Copy the surviving disk's partition table onto the new disk. The `--replicate` value is the target:
   `sudo sgdisk --replicate=/dev/<new> /dev/<surviving>`.
3. Give the copy its own GUIDs: `sudo sgdisk --randomize-guids /dev/<new>`.
4. For each OS array in `/proc/mdstat` that lost a member, add the new disk's matching partition:
   `sudo mdadm --manage /dev/<md array> --add /dev/<new partition>`.
5. Add the new data partition to the cell array:
   `sudo mdadm --manage /dev/md/exomem-cells --add /dev/disk/by-id/<new disk>-part4`.
6. If `/proc/mdstat` shows no array for the EFI system partition, copy that partition. The `of=` value is the new one:
   `sudo dd if=/dev/<surviving ESP> of=/dev/<new ESP> bs=4M`.
7. Install the boot loader on the new disk. On UEFI, run `sudo dpkg-reconfigure grub-efi-amd64` and select both ESPs.
   On BIOS boot, run `sudo grub-install /dev/<new>`.
8. Replace the old disk's ID in `data_disks` in the host list, and regenerate the inventory.
9. If the host joins over WireGuard, delete its key with `sudo rm /etc/wireguard/wgexomem.key`. Then run `site.yml` without `--limit`.
10. If the host list records the host's WireGuard key, step 9 stops and prints the new key. Record it as in [Add the host](#add-the-host), and run `site.yml` again.

Expected result: `cat /proc/mdstat` shows every array rebuilding, then `[UU]`.
After the last `site.yml` run, every peer holds the host's new public key and the tunnel is up.
The removed disk held the old WireGuard key and the agent token
(`/etc/rancher/k3s/config.yaml`) unencrypted. Unless the provider confirms
that it destroyed the disk, [rotate the agent token](#rotate-the-agent-token).
The rotation does not revoke the agent's kubelet client certificate, which the
same disk held: see [What a lost disk still exposes](#what-a-lost-disk-still-exposes).

## Remove the host

Removing an agent that still holds cells needs relocation (task 6.4), which
`remove-agent.yml` does not do yet. Until it does, remove only an agent with no
cells.

1. Run the removal playbook, as in [cloud-node-pool.md](cloud-node-pool.md#remove-a-node):

   ```bash
   cd infra/ansible
   ansible-playbook --inventory inventory.yml remove-agent.yml -e k3s_remove_node=exomem-agent-dx1
   ```

2. Destroy the cell array's LUKS header. Step 1 already closed the storage. This is irreversible, so type the confirmation yourself:

   ```bash
   ssh -t exomem-admin@100.64.0.40 'sudo cryptsetup erase /dev/md/exomem-cells'
   ```

3. Remove the host from the host list, regenerate the inventory, and run `site.yml` without `--limit`.
4. Boot the server into the provider's rescue system.
5. If the disks are NVMe, wipe each whole disk: `nvme format --ses=1 /dev/nvme0n1`, then `/dev/nvme1n1`.
6. If they are not NVMe, wipe each whole disk with `blkdiscard -f /dev/<disk>`.
7. Remove the host's peer from NetBird.
8. In one reviewed commit, remove the host's passphrase entry from the secret matrix and from `infra/contracts/active-ansible-selection-v1.json`, and delete its SOPS files.

Expected result: after step 3, every node has dropped the host's WireGuard
peer, inter-node rules and Tang rule. If it was the last WireGuard host, every
node has also stopped `wg-quick@wgexomem` and removed its configuration and
key. After step 5, `nvme format` has completed on both disks. The WireGuard
key and the agent token were only on those disks.
[Rotate the agent token](#rotate-the-agent-token) unless the wipe is confirmed:
`blkdiscard` and an empty `blkid --probe` do not prove erasure. The rotation does
not revoke the agent's kubelet client certificate, which the same disks held:
see [What a lost disk still exposes](#what-a-lost-disk-still-exposes).

## Rotate the agent token

Rotate `k3s_agent_token` when a disk that held it leaves your custody unwiped.
`k3s token rotate` cannot do it: K3s leaves the agent token alone while
`agent-token` is configured. The rotation changes the configured token and
restarts the server.

That restart leaves etcd with the old copy of `server/cred/passwd`, because
K3s saves its bootstrap data at start only when etcd holds none. The server's
next start would then stop with `cred/passwd newer than datastore`. Step 4 saves
the data again. The `Cloud k3s-link rehearsal` workflow rehearses the K3s side of these steps
on a disposable cluster.

1. Escrow the next version, here v2, to both destinations. Keep v1 for the undo:

   ```bash
   openssl rand -base64 48 | infra/scripts/secret_handoff.py \
     --matrix "$matrix" --repository-root "$repo_root" \
     --secret k3s_agent_token --version v2 \
     --destination ansible.hosted-node.k3s-agent-token.active \
     --destination escrow.k3s-agent-token.active --source stdin
   ```

2. In a reviewed commit, set `ansible.hosted-node.k3s-agent-token.active` to `v2` in `infra/contracts/active-ansible-selection-v1.json`.
3. Run `site.yml` as in [Add the host](#add-the-host). The server restarts with v2, then each agent restarts with it.
4. Save the bootstrap data again on the server. Do not replace the helper with a typed `k3s token rotate`: an empty or wrong `--new-token` rotates the server token. Run `ssh exomem-admin@<server> 'sudo /usr/local/sbin/exomem-k3s-resave-bootstrap'`.
5. Restart the server: `ssh exomem-admin@<server> 'sudo systemctl restart k3s && sudo k3s kubectl get --raw=/readyz'`.
6. Present the old credential, then the new token, each alone, to the server. Each travels from SOPS over SSH, and `curl` reads it from stdin. Set `escrow` to each escrow file in turn, and run the command:
   - for a rotation from v1 to v2, the old credential is `infra/secrets/escrow/k3s-agent-token.v1.sops.json`, and the new token is `k3s-agent-token.v2.sops.json`;
   - for the first selection of an agent token, the old credential is the selected server token's escrow file, for example `k3s-server-token.v1.sops.json`, because agents used the server token until an agent token was set. The new token is `k3s-agent-token.v1.sops.json`.

   ```bash
   escrow=infra/secrets/escrow/k3s-agent-token.v1.sops.json
   sops decrypt --extract '["token"]' "$escrow" \
     | ssh exomem-admin@<server> 'read -r token; printf "user = \"node:%s\"\n" "$token" \
       | sudo curl -sS -o /dev/null -w "%{http_code}\n" -K - \
         --cacert /var/lib/rancher/k3s/server/tls/server-ca.crt https://127.0.0.1:6443/v1-k3s/readyz'
   ```

Expected result: step 4 prints `bootstrap data saved again; the server token is
unchanged`. Step 5 prints `ok`, and `kubectl get nodes` shows every node Ready.
Step 6 prints `401` for the old credential and `200` for the new token. Repeat
it if it prints `503`, which means the server is still starting. A Ready agent
does not prove the new token, because a joined agent also authenticates with
its kubelet client certificate.

If step 4 reports that the server token changed, do not restart K3s. With the
old token in its configuration, the server would no longer find its bootstrap
data in etcd. While the server runs, rotate the token back to the escrowed
server token, then run step 4 again:

```bash
sops decrypt --extract '["token"]' infra/secrets/escrow/k3s-server-token.v1.sops.json \
  | ssh exomem-admin@<server> 'read -r token; test -n "$token" && sudo k3s token rotate --new-token "$token"'
```

If step 4 reports that `k3s token rotate` failed and the token is unchanged,
run step 4 again.

If the server stops with `newer than datastore` before step 4, remove the file
it names: `sudo rm /var/lib/rancher/k3s/server/cred/passwd`. Then run
`sudo systemctl restart k3s`, and continue with step 4.

To undo, select v1 again in a reviewed commit and repeat steps 3 to 6, with the
expected codes swapped.

### What a lost disk still exposes

The rotation closes the agent token, not the other credentials on the disk.
The disk also held the agent's kubelet client certificate and key, under
`/var/lib/rancher/k3s/agent/`, and its node password,
`/etc/rancher/node/password`. K3s keeps accepting that certificate after the
rotation. With it, a caller can get new kube-proxy and k3s-controller client
certificates from the server. While the host's Node still exists, the
certificate and the node password also get a new kubelet certificate.

Expiry is therefore no remedy: until a removed host's certificate expires, it
can get other new certificates, and a host still in the cluster renews its own. Only a
rotation of the cluster's client CA closes the exposure, and this runbook does
not cover it yet. The host firewall admits port 6443 only from the other
inventoried nodes' private addresses, which limits who can use these
credentials.
