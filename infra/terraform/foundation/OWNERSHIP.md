# Foundation ownership

Owns fleet servers, their addresses and firewalls, the shared private network,
the bootstrap SSH key, and product Cloudflare Tunnel/DNS/Access resources. It must never
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
