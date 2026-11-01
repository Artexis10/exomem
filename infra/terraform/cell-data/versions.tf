terraform {
  required_version = "= 1.15.8"

  required_providers {
    b2 = {
      source  = "Backblaze/b2"
      version = "= 0.12.1"
    }
    random = {
      source  = "hashicorp/random"
      version = "= 3.9.0"
    }
  }
}
