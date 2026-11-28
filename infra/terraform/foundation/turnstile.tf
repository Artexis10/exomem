locals {
  # Public identifier of the existing widget; it ships in the sign-in page.
  turnstile_site_key = "0x4AAAAAAFUaR73ufpHqICsr"

  # The hosts the widget accepts, in the order Cloudflare returns them. The
  # widget refuses a token solved on any other host, so a host that serves the
  # sign-in form must be listed. This is the adopted dashboard list.
  turnstile_hostnames = [
    "127.0.0.1",
    "localhost",
    "substratesystems.io",
  ]
}

# The widget for the Evermatter sign-in on the Substrate web app. Its secret is
# a sensitive attribute held in remote state and is never an output. The block
# adopts the widget made by hand; it is inert once the widget is in state.
import {
  to = cloudflare_turnstile_widget.signin
  id = "${var.cloudflare_account_id}/${local.turnstile_site_key}"
}

resource "cloudflare_turnstile_widget" "signin" {
  account_id = var.cloudflare_account_id
  name       = "substratesystems.io"
  mode       = "managed"
  region     = "world"
  domains    = local.turnstile_hostnames

  # The adopted widget's live settings, set so that a plan shows no change.
  bot_fight_mode  = false
  clearance_level = "no_clearance"
  ephemeral_id    = false
  offlabel        = false

  # Destroying it breaks every sign-in until a new site key is redeployed.
  lifecycle {
    prevent_destroy = true
  }
}
