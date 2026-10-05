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

Terraform-created agents keep [node-pool.md](node-pool.md).

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
as [node-pool.md](node-pool.md#add-a-node) describes. `site.yml` refuses a host
it cannot administer over NetBird.

## Choose the private link

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
infra/scripts/ansible_with_sops.sh \
  --inventory infra/ansible/inventory.yml \
  --vars infra/secrets/ansible/k3s-server-token.v1.sops.json \
  --vars infra/secrets/ansible/k3s-agent-token.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-access-key.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-secret-key.v1.sops.json \
  --vars infra/secrets/ansible/tang-keys.v1.sops.json \
  --vars infra/secrets/ansible/recovery-passphrase-exomem-agent-dx1.v1.sops.json
```

Every later `site.yml` run needs the Tang keys and every dedicated host's
passphrase. Each run proves that the escrowed passphrase still opens the array
and that Tang still unlocks it.

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

If the server is rebuilt, `site.yml` reinstalls the escrowed Tang keys, so
every existing binding keeps working and nothing is rebound. If the keys
themselves are lost, escrow a new key set as a new version, update
`--vars` to it, and run `site.yml`. Each agent's role finds that its binding no
longer unlocks and binds again with its passphrase. Then remove the stale
binding:

```bash
ssh exomem-admin@100.64.0.40 'sudo clevis luks list -d /dev/md/exomem-cells'
ssh exomem-admin@100.64.0.40 'sudo clevis luks unbind -d /dev/md/exomem-cells -s <old slot> -f'
```

## Replace a failed disk

`site.yml` reports a degraded array but does not repair it. After the provider
swaps the disk, recreate its partition as above and add it:

```bash
ssh exomem-admin@100.64.0.40 'sudo mdadm --manage /dev/md/exomem-cells --add /dev/disk/by-id/<new disk>-part4'
```

Update `data_disks` in the host list with the new disk's ID.

## Remove the host

Removing an agent that still holds cells needs relocation (task 6.4), which
`remove-agent.yml` does not do yet. Until it does, remove only an agent with no
cells. Run the removal playbook first, as in [node-pool.md](node-pool.md#remove-a-node):

```bash
cd infra/ansible
ansible-playbook --inventory inventory.yml remove-agent.yml -e k3s_remove_node=exomem-agent-dx1
```

Then drop the host from the host list, regenerate the inventory and run
`site.yml`. That revokes its WireGuard peer, its inter-node rules and its Tang
access. Keep its escrowed passphrase until the disks are erased. Before you
hand the server back, destroy the LUKS header on the host. This is
irreversible, so pass the exact array name yourself:

```bash
ssh exomem-admin@100.64.0.40 'sudo cryptsetup close exomem_cells; sudo cryptsetup erase /dev/md/exomem-cells'
```
