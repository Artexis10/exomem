# Design

The companion migration 0071 makes `storage_gib` a generation-changing desired field.
The controller continues to render it on CSI and retains the existing local `grown_storage_gib` floor.
The Kubernetes adapter reads the bound claim's UID, resource version, status capacity and pending resize conditions.
Existing namespace, binding, class and recorded-volume checks determine whether that observation belongs to the cell.
The controller writes `observed_storage_pvc_uid`, `observed_storage_pvc_version`, `observed_storage_capacity_bytes`, `observed_storage_generation` and `observed_storage_at`.
It reports a satisfied generation only when actual capacity meets the current target and filesystem expansion is complete.
Unknown or conflicting observations clear that receipt; they do not manufacture a zero capacity.
Unchanged receipts do not cause a database write every reconciliation pass.

The fixture imports the companion migration's cell-only columns and trigger, plus the source grants script.
Only the application writes desired size. Only the controller writes the observation fields.
The web service matches the operation's original claim UID and target generation before reporting completion.
Offline decision and real PostgreSQL tests cover delayed growth, pending filesystem expansion and identity conflicts.
Operational resize acceptance and controller rollout remain with the existing deployment owner.
