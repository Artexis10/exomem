# Hosted operations runbooks

Executable runbooks cover backend bootstrap, reviewed deploy, secret
handoff/rotation, cell lifecycle, maintenance, retained-volume rebind,
backup/restore, ordered deletion, node replacement, and break glass. A runbook
may orchestrate the versioned tools under `infra/scripts`; it may not contain a
credential, mutable image tag, tenant content, or destructive default.

The owner canary is the only deployment target until all private-alpha proof
gates in the active OpenSpec change are green.

The machine-readable index is `infra/contracts/runbooks-v1.json`. Every runbook
has explicit preconditions and content-free verification; destructive paths name
their exact approval flag and remain fail-closed by default.
