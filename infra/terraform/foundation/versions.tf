terraform {
  required_version = "= 1.15.8"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "= 5.22.0"
    }
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "= 1.66.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "= 2.9.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "= 3.9.0"
    }
  }
}
