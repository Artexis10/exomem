# Foundation ownership

Owns fleet servers, their addresses and firewalls, the shared private network,
the bootstrap SSH key, product Cloudflare Tunnel/DNS/Access resources, and the
Evermatter sign-in Turnstile widget (`turnstile.tf`). It must never
own B2 recovery resources, Kubernetes tenant objects, application data, Paddle,
Neon, or Substrate configuration. Production changes use the `foundation/`
remote-state key and a reviewed saved plan.

Substrate-infra owns the shared control host, address, firewall and database
DNS record. The control host is not on this root's private network. This root
consumes the versioned `shared_control` input (schema 2) and publishes
`fleet_dependency` (schema 3). That output carries only the K3s server's public
IPv4 as `database_client_ipv4_cidr`. The shared database admits direct
PostgreSQL only from that `/32`. Only a new Primary IP
changes it: `hcloud_primary_ip.node` has `prevent_destroy` and
`auto_delete = false`, so replacing the server keeps the address.

The sign-in widget is `cloudflare_turnstile_widget.signin`. It has
`prevent_destroy`, and its hostnames are the app hosts that serve the sign-in
form. Its secret stays in remote state and in no output. Change it through
`docs/runbooks/cloud-turnstile-widget.md`. A staging widget is not owned here.
