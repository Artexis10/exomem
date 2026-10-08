# Foundation ownership

Owns fleet servers, their addresses and firewalls, the shared private network,
the bootstrap SSH key, and product Cloudflare Tunnel/DNS/Access resources. It must never
own B2 recovery resources, Kubernetes tenant objects, application data, Paddle,
Neon, or Substrate configuration. Production changes use the `foundation/`
remote-state key and a reviewed saved plan.

Substrate-infra owns the shared control host, address, firewall and database
DNS record. This root consumes its versioned `shared_control` input and publishes
the existing network and bootstrap key through `fleet_dependency`. Schema 2 also
publishes the K3s server's public IPv4 as `database_client_ipv4_cidr`. The shared
database admits direct PostgreSQL only from that `/32`, so a replacement server
node is a new contract version.
