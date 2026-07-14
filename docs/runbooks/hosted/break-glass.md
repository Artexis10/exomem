# Break-glass recovery

## Preconditions

Routine operations are exhausted, two maintainers record approval, and a
content-free incident record names operator, reason, ciphertext version, start
time, intended verification, and cleanup owner. Never copy the offline age
identity to the node, Vercel, Terraform state, or repository.

```bash
set +x
umask 077
export SOPS_AGE_KEY_FILE=/secure/offline/exomem-hosted.agekey
export EXOMEM_SECRET_TMPFS_DIR="${XDG_RUNTIME_DIR:?tmpfs runtime directory required}"
```

Use the narrowest escrow artifact through a FIFO or `sops exec-file`. The K3s
server-token escrow is versioned independently from its Ansible artifact.

```bash
sops exec-file infra/secrets/escrow/k3s-server-token.v1.sops.json \
  'python3 -c '\''import json,sys; d=json.load(open(sys.argv[1])); assert set(d)=={"schema_version","secret_name","secret_version","token"}'\'' {}'
```

## Verify

```bash
test -z "$(find "${EXOMEM_SECRET_TMPFS_DIR}" -maxdepth 1 -name '*exomem*' -print -quit)"
unset SOPS_AGE_KEY_FILE EXOMEM_SECRET_TMPFS_DIR
```

Record end time and only the content-free result. Destroy the environment.
