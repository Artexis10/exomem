<!-- authority:non-specification -->

# Shared Substrate control host

The shared production database serves authentication, billing and several
products. Substrate infrastructure owns its host configuration and recovery.
Exomem owns its application migrations and consumes the published
`shared_control` dependency. It receives no shared Terraform state or apply
permission.

The [shared handover runbook](https://github.com/substrate-systems/substrate-infra/blob/main/docs/control-host-handover.md)
holds the operating procedure. Exomem's OpenSpec change
`separate-shared-substrate-control-infrastructure` governs the current handover.
The source extraction alone does not prove that remote state ownership moved.

The first handover preserves resource IDs, names, addresses, credentials,
certificate lineage, backup history and the existing private network.
The provider-project relocation follows its separate rehearsal and window.

The provider rename proposed in #1513 remains unapplied. Existing names that
contain `exomem` identify live resources and managed paths. Any later rename
needs a reviewed plan from the shared owner, with unchanged IDs and addresses.
The existing `exomem-control-db` certificate lineage remains a delivery path.
