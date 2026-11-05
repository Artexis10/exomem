# Mocked provider proof covers configuration, never live retention enforcement.
mock_provider "b2" {}
mock_provider "random" {}
variables {
  b2_application_key_id = "mock-only"
  b2_application_key    = "mock-only"
}
run "disabled_creates_no_record_or_credentials" {
  command = plan
  assert {
    condition     = length(b2_bucket.access_record) == 0 && length(b2_application_key.access_record_node) == 0 && length(b2_application_key.access_record_reader) == 0 && length(b2_bucket_notification_rules.access_record) == 0
    error_message = "Access recording must remain disabled by default."
  }
}
run "record_keys_cannot_read_or_erase_and_versions_lock_for_one_year" {
  command = plan
  variables {
    access_record_enabled  = true
    access_record_node_ids = ["node-a", "node-b"]
  }
  assert {
    condition     = one(one(b2_bucket.access_record[0].file_lock_configuration).default_retention).mode == "compliance" && one(one(one(b2_bucket.access_record[0].file_lock_configuration).default_retention).period).duration >= 365
    error_message = "Received versions require at least one year of compliance retention."
  }
  assert {
    condition     = alltrue([for key in b2_application_key.access_record_node : toset(key.capabilities) == toset(["writeFiles"])])
    error_message = "Nodes must receive writeFiles only."
  }
}
