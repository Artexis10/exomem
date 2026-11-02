<!-- authority:non-specification -->

# Hosted retirement disposal

## Preconditions

The repository retires Hosted code and secret destinations. It does not revoke provider keys or delete manually managed Kubernetes Secrets.
The inventory contains 29 original secret records with 34 destinations, plus eight collector destinations: 42 destination records in total.
Every row remains pending. No provider credential value or account identifier belongs in disposal evidence.

Use explicit operator approval for each destructive live action. Keep Cloud control, scheduler, artifact broker, CSI, volume encryption and alert delivery intact.
Re-sign the active-secret registry against the kept matrix and selection before the next platform upgrade.
Verify that registry with `infra/scripts/apply_active_sops_secrets.py --verify-only`; a stale signed registry cannot authorize the new matrix.

## Destination inventory

| Scope | Secret record | Destination | Materialized target | State |
|---|---|---|---|---|
| Original retirement | `cloudflare_access_client_id` | `vercel.substrate.production.access.active.client-id` | `EXOMEM_CF_ACCESS_CLIENT_ID` (vercel_env) | Pending operator revocation and absence proof |
| Original retirement | `cloudflare_access_client_id` | `vercel.substrate.production.access.previous.client-id` | `EXOMEM_CF_ACCESS_CLIENT_ID_PREVIOUS` (vercel_env) | Pending operator revocation and absence proof |
| Original retirement | `cloudflare_access_client_secret` | `vercel.substrate.production.access.active.client-secret` | `EXOMEM_CF_ACCESS_CLIENT_SECRET` (vercel_env) | Pending operator revocation and absence proof |
| Original retirement | `cloudflare_access_client_secret` | `vercel.substrate.production.access.previous.client-secret` | `EXOMEM_CF_ACCESS_CLIENT_SECRET_PREVIOUS` (vercel_env) | Pending operator revocation and absence proof |
| Original retirement | `cloudflare_tunnel_token` | `k3s.cloudflared.active` | `exomem-cloudflared-token` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `provisioner_credential` | `k3s.provisioner.active` | `exomem-provisioner-auth` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `provisioner_credential` | `vercel.substrate.production.provisioner.active` | `EXOMEM_PROVISIONER_CREDENTIAL` (vercel_env) | Pending operator revocation and absence proof |
| Original retirement | `hcloud_provisioner_token` | `k3s.provisioner.hcloud-token.active` | `exomem-provisioner-hcloud-token` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `provisioner_database_url` | `k3s.provisioner.database-url.active` | `exomem-provisioner-database` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `provisioner_wrapping_key` | `k3s.provisioner.wrapping-key.active` | `exomem-provisioner-wrapping-key` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `provisioner_wrapping_key` | `escrow.provisioner-wrapping-key.active` | `infra/secrets/escrow/provisioner-wrapping-key.{version}.sops.json` (sops_escrow) | Pending operator revocation and absence proof |
| Original retirement | `provider_recovery_signing_key` | `k3s.provider-recovery-signer.active` | `exomem-provider-recovery-signer` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `provider_recovery_signing_key` | `escrow.provider-recovery-signer.active` | `infra/secrets/escrow/provider-recovery-signer.{version}.sops.json` (sops_escrow) | Pending operator revocation and absence proof |
| Original retirement | `provider_recovery_public_key` | `k3s.provider-recovery-verifier.active` | `exomem-provider-recovery-verifier` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `database_backup_pg_service_file` | `k3s.database-backup.pg-service.active` | `exomem-database-backup-pg-service` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `database_backup_pgpass_file` | `k3s.database-backup.pgpass.active` | `exomem-database-backup-pgpass` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `recovery_upload_key_id` | `k3s.provisioner.recovery-upload-key-id.active` | `exomem-recovery-upload-key-id` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `recovery_upload_key` | `k3s.provisioner.recovery-upload-key.active` | `exomem-recovery-upload-key` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `recovery_restore_key_id` | `k3s.provisioner.recovery-restore-key-id.active` | `exomem-recovery-restore-key-id` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `recovery_restore_key` | `k3s.provisioner.recovery-restore-key.active` | `exomem-recovery-restore-key` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `recovery_delete_key_id` | `k3s.provisioner.recovery-delete-key-id.active` | `exomem-recovery-delete-key-id` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `recovery_delete_key` | `k3s.provisioner.recovery-delete-key.active` | `exomem-recovery-delete-key` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `user_export_upload_key_id` | `k3s.provisioner.user-export-upload-key-id.active` | `exomem-user-export-upload-key-id` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `user_export_upload_key` | `k3s.provisioner.user-export-upload-key.active` | `exomem-user-export-upload-key` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `user_export_restore_key_id` | `k3s.provisioner.user-export-restore-key-id.active` | `exomem-user-export-restore-key-id` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `user_export_restore_key` | `k3s.provisioner.user-export-restore-key.active` | `exomem-user-export-restore-key` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `user_export_delete_key_id` | `k3s.provisioner.user-export-delete-key-id.active` | `exomem-user-export-delete-key-id` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `user_export_delete_key` | `k3s.provisioner.user-export-delete-key.active` | `exomem-user-export-delete-key` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `user_export_delivery_key_id` | `k3s.provisioner.user-export-delivery-key-id.active` | `exomem-user-export-delivery-key-id` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `user_export_delivery_key` | `k3s.provisioner.user-export-delivery-key.active` | `exomem-user-export-delivery-key` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `database_backup_upload_key_id` | `k3s.provisioner.database-backup-upload-key-id.active` | `exomem-database-backup-upload-key-id` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `database_backup_upload_key` | `k3s.provisioner.database-backup-upload-key.active` | `exomem-database-backup-upload-key` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `database_backup_restore_key_id` | `k3s.provisioner.database-backup-restore-key-id.active` | `exomem-database-backup-restore-key-id` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Original retirement | `database_backup_restore_key` | `k3s.provisioner.database-backup-restore-key.active` | `exomem-database-backup-restore-key` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Collector retirement | `capacity_receipt_public_key` | `escrow.capacity-receipt-verifier.active` | `infra/secrets/escrow/capacity-receipt-verifier.{version}.sops.json` (sops_escrow) | Pending operator revocation and absence proof |
| Collector retirement | `capacity_receipt_public_key` | `k3s.capacity-workers.capacity-receipt-verifier.active` | `exomem-platform/exomem-capacity-receipt-verifier` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Collector retirement | `capacity_receipt_signing_key` | `k3s.capacity-collector.capacity-receipt-signer.active` | `exomem-capacity-receipt-signer` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Collector retirement | `economics_receipt_public_key` | `escrow.economics-receipt-operator-verifier.active` | `infra/secrets/escrow/economics-receipt-operator-verifier.{version}.sops.json` (sops_escrow) | Pending operator revocation and absence proof |
| Collector retirement | `economics_receipt_signing_key` | `escrow.economics-receipt-collector.active` | `infra/secrets/escrow/economics-receipt-collector.{version}.sops.json` (sops_escrow) | Pending operator revocation and absence proof |
| Collector retirement | `hcloud_capacity_reader_token` | `k3s.hcloud-capacity-reader.active` | `exomem-hcloud-capacity-reader` (sops_k8s_secret) | Pending operator revocation and absence proof |
| Collector retirement | `rotation_receipt_public_key` | `escrow.rotation-receipt-operator-verifier.active` | `infra/secrets/escrow/rotation-receipt-operator-verifier.{version}.sops.json` (sops_escrow) | Pending operator revocation and absence proof |
| Collector retirement | `rotation_receipt_signing_key` | `escrow.rotation-receipt-collector.active` | `infra/secrets/escrow/rotation-receipt-collector.{version}.sops.json` (sops_escrow) | Pending operator revocation and absence proof |
| Key custody retirement | `cloud_cloud_b2_key_management` | `k3s.cloud.exomem-cloud-b2-key-management.active` | `exomem-cloud/exomem-cloud-b2-key-management` (sops_k8s_secret) | Pending provider key deletion (key custody cutover step 11) and absence proof |

## BWS inventory

Each entry below requires its own operator go. The eight deletions remain pending.
Retained ciphertext removal, consumer retirement and missing local readers do not grant that go.

These logical names come from the retirement handoff. Their current BWS identities are unverified.
Read the current private binding metadata before deletion; never infer a rename or identity from these names.
Audit the actual binding after each authorized deletion.

| Logical name | State |
|---|---|
| `EXOMEM_CF_ACCESS_CLIENT_ID` | Pending current binding check and separate operator go |
| `EXOMEM_CF_ACCESS_CLIENT_SECRET` | Pending current binding check and separate operator go |
| `EXOMEM_PROVISIONER_CREDENTIAL` | Pending current binding check and separate operator go |
| `EXOMEM_PROVISIONER_ENDPOINT` | Pending current binding check and separate operator go |
| `EXOMEM_HOSTED_CONTRACT_IMPORT_SECRET` | Pending current binding check and separate operator go |
| `EXOMEM_HOSTED_PROMOTION_SECRET` | Pending current binding check and separate operator go |
| `EXOMEM_HOSTED_TRANSFER_HOST` | Pending current binding check and separate operator go |
| `EXOMEM_HOSTED_PROVISIONER_DATABASE_URL` | Pending current binding check and separate operator go |

## Disposal sequence

1. Verify that no kept consumer uses each retired destination, and retain its content-free evidence.
2. With explicit operator approval, revoke each provider credential, and prove rejection or provider absence.
3. With separate operator go, delete each obsolete BWS entry, and record its absence.
4. With explicit operator approval, remove orphan Secrets and collector or export ConfigMaps, and prove absence.
5. With data-disposal approval, dispose of Hosted tenant namespaces, retained volumes and backups, and record exact absence proofs.
6. Review a saved Terraform disposal plan for retired tunnel, Access, DNS and durability resources, with kept resources unchanged.
7. With explicit operator approval, apply that disposal plan, and verify its resource actions.

Never delete shared cluster snapshots to claim immediate tenant erasure.
Keep their retention residual visible under [cell deletion](cloud-cell-deletion.md).
A provider key can serve several destinations; record its revocation once and link every affected destination to that evidence.
A Secret can hold several keys; prove the complete materialized target is obsolete before removing it.

## Verify

```bash
infra/scripts/validate_sops_ciphertext.py
```

The repository check verifies kept ciphertext shape. It does not inspect production revocation or data disposal.
Record each pending action's time, target class and exact provider or Kubernetes absence result outside public artifacts.
Mark a destination complete only when its required revocation and materialized-resource checks pass.
