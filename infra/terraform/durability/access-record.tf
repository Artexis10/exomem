# Prepared only. Compliance retention and notification enablement need live proof.
resource "b2_bucket" "access_record" {
  count       = var.access_record_enabled ? 1 : 0
  bucket_name = "${var.bucket_prefix}-access-${random_id.bucket_suffix.hex}"
  bucket_type = "allPrivate"

  default_server_side_encryption {
    mode      = "SSE-B2"
    algorithm = "AES256"
  }
  file_lock_configuration {
    is_file_lock_enabled = true
    default_retention {
      mode = "compliance"
      period {
        duration = var.access_record_retention_days
        unit     = "days"
      }
    }
  }
  lifecycle {
    prevent_destroy = true
  }
}

# writeFiles can add or hide versions. Readers always request an exact version.
resource "b2_application_key" "access_record_node" {
  for_each     = var.access_record_enabled ? var.access_record_node_ids : toset([])
  key_name     = "access-record-${each.key}"
  bucket_ids   = [b2_bucket.access_record[0].bucket_id]
  name_prefix  = "access/${each.key}/"
  capabilities = ["writeFiles"]
}

resource "b2_application_key" "access_record_reader" {
  count        = var.access_record_enabled ? 1 : 0
  key_name     = "access-record-reader"
  bucket_ids   = [b2_bucket.access_record[0].bucket_id]
  name_prefix  = "access/"
  capabilities = ["readFiles", "readFileRetentions"]
}

resource "b2_bucket_notification_rules" "access_record" {
  count     = var.access_record_enabled && var.access_record_notifications_enabled ? 1 : 0
  bucket_id = b2_bucket.access_record[0].bucket_id
  notification_rules {
    name               = "access-record-receipts"
    event_types        = ["b2:ObjectCreated:Upload", "b2:ObjectCreated:MultipartUpload"]
    object_name_prefix = "access/"
    is_enabled         = true
    target_configuration {
      target_type                = "webhook"
      url                        = var.access_record_notification_url
      hmac_sha256_signing_secret = var.access_record_notification_secret
    }
  }
}
