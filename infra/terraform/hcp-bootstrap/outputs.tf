output "hcp_project_id" {
  description = "HCP Terraform project containing Hosted state workspaces."
  value       = tfe_project.hosted.id
}

output "foundation_workspace_id" {
  description = "HCP workspace ID for the foundation root."
  value       = tfe_workspace.foundation.id
}

output "durability_workspace_id" {
  description = "HCP workspace ID for the durability root."
  value       = tfe_workspace.durability.id
}

output "backend_proof_workspace_id" {
  description = "HCP workspace ID for disposable locking and rollback proof."
  value       = tfe_workspace.backend_proof.id
}
