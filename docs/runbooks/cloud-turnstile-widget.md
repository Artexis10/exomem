<!-- authority:non-specification -->

# Sign-in Turnstile widget

The Evermatter sign-in on the Substrate web app uses one Cloudflare Turnstile
widget. The foundation root owns it as `cloudflare_turnstile_widget.signin` in
`infra/terraform/foundation/turnstile.tf`. The web app's Turnstile check accepts
exactly the hostnames on the widget, so a missing host breaks sign-in there.

The widget has `prevent_destroy`. Destroying it breaks every sign-in until a new
site key is deployed. Its secret key lives in the remote state as a sensitive
value and is never an output.

## Preconditions

- The foundation plan runs with the HCP Terraform environment from
  [Terraform backend](cloud-terraform-backend.md).
- The root's Cloudflare token, the `cloudflare_api_token` variable, carries
  Account > Turnstile > Edit besides its Tunnel, DNS and Access scopes. Without
  it the plan fails when it reads the widget.

## Change the hostnames

1. Edit `turnstile_hostnames` in `infra/terraform/foundation/turnstile.tf`. List every app host that serves the sign-in form.
2. Run `infra/scripts/plan.sh foundation /run/user/$UID/foundation-turnstile.tfplan`.
3. Review the plan. It must change only `domains` on `cloudflare_turnstile_widget.signin`.
4. Run `infra/scripts/apply_saved_plan.sh foundation /run/user/$UID/foundation-turnstile.tfplan`.

## First plan after adoption

The `import` block adopts the widget that was made by hand. Check these before
the first apply:

1. Read the plan's import line. It names `<account id>/<site key>`.
2. Compare `domains` with the dashboard. Set `turnstile_hostnames` to match, then plan again.
3. Compare `name` and `mode`. The dashboard's values win; edit `turnstile.tf` until the plan shows no change to them.
4. Apply only a plan that shows the import and no change, or a reviewed hostname change.

## Verify

1. Run `terraform state show cloudflare_turnstile_widget.signin`. The output shows `secret` as a sensitive value.
2. Open the sign-in page on each listed host. The Turnstile check completes and sign-in works.
