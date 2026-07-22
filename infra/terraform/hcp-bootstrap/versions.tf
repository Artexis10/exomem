terraform {
  required_version = "= 1.15.8"

  required_providers {
    tfe = {
      source  = "hashicorp/tfe"
      version = "= 0.78.0"
    }
  }
}
