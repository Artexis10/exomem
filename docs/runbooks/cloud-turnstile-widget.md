<!-- authority:non-specification -->

# Sign-in Turnstile widget

The Evermatter sign-in on the Substrate web app uses one Cloudflare Turnstile
widget. The foundation root owns it as `cloudflare_turnstile_widget.signin` in
`infra/terraform/foundation/turnstile.tf`. The widget refuses a token solved on
a host that is not in its list, so every host that serves the sign-in form must
be listed.

The widget has `prevent_destroy`. Destroying it breaks every sign-in until a new
site key is deployed. Its secret key lives in the remote state as a sensitive
value and is never an output.

## Preconditions

- The foundation plan runs with the HCP Terraform environment from
  [Terraform backend](cloud-terraform-backend.md).
- The root's Cloudflare token, the `cloudflare_api_token` variable, carries
  Account > Turnstile > Edit besides its Tunnel, DNS and Access scopes. With
  read access only, the plan succeeds and the apply fails.

## Change the hostnames

1. Edit `turnstile_hostnames` in `infra/terraform/foundation/turnstile.tf`. Keep the existing entries in their order and append the new host.
2. Run `infra/scripts/plan.sh foundation /run/user/$UID/foundation-turnstile.tfplan`.
3. Review the plan. It updates only `cloudflare_turnstile_widget.signin`. Besides `domains`, it shows `created_on`, `modified_on` and `secret` as known after apply. The provider sends no secret on update and does not rotate it.
4. If the plan shows any other change, stop and fix the configuration.
5. Run `infra/scripts/apply_saved_plan.sh foundation /run/user/$UID/foundation-turnstile.tfplan`.
6. Run the plan again. It reports no changes.

## Stop managing the widget

Use this to hand the widget back to the dashboard. The widget itself stays.

1. If the widget is not yet in state, revert the change that added `turnstile.tf`. Nothing else is needed.
2. Otherwise, in one change, delete the `import` and `resource` blocks from `turnstile.tf`.
3. In the same change, run `terraform -chdir=infra/terraform/foundation state rm cloudflare_turnstile_widget.signin`.
4. Run the plan. It reports no changes.

Never delete only the `resource` block while the widget is in state. The plan
then destroys the production widget. The plan inspector refuses that plan unless
`--allow-destructive` is passed; never pass it here.

## Verify

1. Run `terraform state show cloudflare_turnstile_widget.signin`. The output shows `secret` as a sensitive value.
2. Open the sign-in page on each listed host. The Turnstile check completes and sign-in works.
