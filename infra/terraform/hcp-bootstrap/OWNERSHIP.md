# HCP Terraform bootstrap ownership

One-time local-state bootstrap for the Exomem Hosted HCP Terraform project and
its three state-only workspaces. Its state is sealed to a separate SOPS/age
escrow artifact and never shares state with the already-applied legacy B2
bootstrap root. Re-running must converge without replacing a workspace.

The `foundation` and `durability` workspaces are production state authorities.
The `backend-proof` workspace is disposable infrastructure state used only for
the real mutual-exclusion and historical-version recovery gate.
