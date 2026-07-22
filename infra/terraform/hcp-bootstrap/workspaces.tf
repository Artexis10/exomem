resource "tfe_project" "hosted" {
  organization = var.hcp_organization
  name         = "Exomem Hosted"
  description  = "State coordination for the isolated Exomem Hosted infrastructure roots."
}

resource "tfe_workspace" "foundation" {
  name                  = "exomem-hosted-foundation"
  organization          = var.hcp_organization
  project_id            = tfe_project.hosted.id
  description           = "Local-execution state authority for Hetzner and Cloudflare foundation resources."
  terraform_version     = var.terraform_version
  queue_all_runs        = false
  file_triggers_enabled = false
  allow_destroy_plan    = false
  force_delete          = false
}

resource "tfe_workspace_settings" "foundation" {
  workspace_id         = tfe_workspace.foundation.id
  execution_mode       = "local"
  global_remote_state  = false
  project_remote_state = false
  auto_apply           = false
  assessments_enabled  = false
}

resource "tfe_workspace" "durability" {
  name                  = "exomem-hosted-durability"
  organization          = var.hcp_organization
  project_id            = tfe_project.hosted.id
  description           = "Local-execution state authority for B2 durability resources."
  terraform_version     = var.terraform_version
  queue_all_runs        = false
  file_triggers_enabled = false
  allow_destroy_plan    = false
  force_delete          = false
}

resource "tfe_workspace_settings" "durability" {
  workspace_id         = tfe_workspace.durability.id
  execution_mode       = "local"
  global_remote_state  = false
  project_remote_state = false
  auto_apply           = false
  assessments_enabled  = false
}

resource "tfe_workspace" "backend_proof" {
  name                  = "exomem-hosted-backend-proof"
  organization          = var.hcp_organization
  project_id            = tfe_project.hosted.id
  description           = "Disposable local-execution workspace for lock and state-version recovery proofs."
  terraform_version     = var.terraform_version
  queue_all_runs        = false
  file_triggers_enabled = false
  allow_destroy_plan    = false
  force_delete          = false
}

resource "tfe_workspace_settings" "backend_proof" {
  workspace_id         = tfe_workspace.backend_proof.id
  execution_mode       = "local"
  global_remote_state  = false
  project_remote_state = false
  auto_apply           = false
  assessments_enabled  = false
}
