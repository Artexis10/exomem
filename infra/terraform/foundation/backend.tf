terraform {
  backend "s3" {
    key                         = "foundation/terraform.tfstate"
    region                      = "us-east-1"
    use_lockfile                = true
    use_path_style              = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
  }
}
