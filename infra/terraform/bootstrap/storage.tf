resource "b2_bucket" "terraform_state" {
  bucket_name = var.state_bucket_name
  bucket_type = "allPrivate"

  default_server_side_encryption {
    mode      = "SSE-B2"
    algorithm = "AES256"
  }

  lifecycle_rules {
    file_name_prefix             = ""
    days_from_hiding_to_deleting = 30
  }

  lifecycle {
    prevent_destroy = true
  }
}

locals {
  state_capabilities = ["deleteFiles", "listBuckets", "listFiles", "readFiles", "writeFiles"]
}

resource "b2_application_key" "foundation" {
  key_name     = "exomem-terraform-foundation"
  bucket_ids   = [b2_bucket.terraform_state.bucket_id]
  name_prefix  = "foundation/"
  capabilities = local.state_capabilities

  lifecycle {
    prevent_destroy = true
  }
}

resource "b2_application_key" "durability" {
  key_name     = "exomem-terraform-durability"
  bucket_ids   = [b2_bucket.terraform_state.bucket_id]
  name_prefix  = "durability/"
  capabilities = local.state_capabilities

  lifecycle {
    prevent_destroy = true
  }
}
