removed {
  from = b2_bucket.control_db_pgbackrest
  lifecycle { destroy = false }
}

removed {
  from = b2_application_key.control_db_pgbackrest
  lifecycle { destroy = false }
}
