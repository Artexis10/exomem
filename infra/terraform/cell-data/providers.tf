# Bare provider: the credentials belong to the cell-data member account, never the platform account.
provider "b2" {
  application_key_id = var.cell_data_b2_application_key_id
  application_key    = var.cell_data_b2_application_key
}
