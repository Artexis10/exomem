locals {
  # Public identifier of the existing widget; it ships in the sign-in page.
  turnstile_site_key = "0x4AAAAAAAFUaR73ufpHqlCsr"

  # The set of app hosts that serve the sign-in form: the web app's Turnstile
  # check accepts exactly these. The first saved plan must show no change to
  # the widget other than intended, and this list must match the dashboard's.
  turnstile_hostnames = [
    "substratesystems.io",
    "evermatter.ai",
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
  domains    = local.turnstile_hostnames

  # Destroying it breaks every sign-in until a new site key is redeployed.
  lifecycle {
    prevent_destroy = true
  }
}
