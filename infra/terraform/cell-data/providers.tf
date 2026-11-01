# Bare provider: the credentials belong to the cell-data member account, never the platform account.
provider "b2" {
  application_key_id = var.b2_application_key_id
  application_key    = var.b2_application_key
}
