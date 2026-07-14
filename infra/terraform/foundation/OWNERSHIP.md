# Foundation ownership

Owns only the dedicated Hetzner server, primary IP, private network, firewall,
SSH key references, and Cloudflare Tunnel/DNS/Access resources. It must never
own B2 recovery resources, Kubernetes tenant objects, application data, Paddle,
Neon, or Substrate configuration. Production changes use the `foundation/`
remote-state key and a reviewed saved plan.
