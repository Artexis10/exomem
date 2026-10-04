<!-- authority:non-specification -->

# Shared Substrate control host

The separate control host serves the shared production PostgreSQL database,
including Substrate, Endstate, authentication, billing, and Exomem control
records. It is not an Exomem tenant-workload node.

## Rename without replacing the server

Terraform's `control_db_server_name` names the server, its dedicated primary IP,
and its firewall. The default is `substrate-control-01`. Keep the existing
Terraform resource addresses and the foundation workspace; do not import,
recreate, resize, or migrate state for this rename.

Use current private deployment inputs and inspect a saved foundation plan.
An older input file can omit newer administrator SSH CIDRs: compare the complete
plan with live firewall rules before applying. Diagnose unrelated changes before
proceeding. For this one-off label change, a targeted plan for the three objects
below can leave understood, unrelated drift for its own operational change; do
not use targeting to hide unknown drift or for routine whole-platform deployment.
A rename-only plan must contain
only `name` updates to `hcloud_server.control`, `hcloud_primary_ip.control_db`,
and `hcloud_firewall.control`, with no creation, deletion, or other known changes.
The primary IP's computed assignee fields can become unknown during its name
update; verify the pinned provider does not unassign an unknown assignee and
confirm the actual assignment during read-back.
Apply that exact reviewed plan, then read the provider objects back to verify
their new names and unchanged IDs, IP addresses, and firewall rules.

The Ansible inventory alias does not set the operating-system hostname.
That is a separate host-convergence action. Leave database DNS, connection
strings, TLS certificate lineage, backup paths, and service identities unchanged.
In particular, the existing `exomem-control-db` certificate lineage is a stable
delivery path, not the provider display name.
