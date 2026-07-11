# One connector, two Exomem replicas

This optional Cloudflare Worker keeps a single public MCP/OAuth URL in front of
desktop and laptop replicas. A SQLite Durable Object stores only:

- writer identity, expiry, and fencing counter;
- opaque OAuth records encrypted by Exomem before upload.

Vault files and search results are proxied, never stored at the edge. Syncthing,
Obsidian Sync, a NAS, or any other replication mechanism remains a separate
operator choice.

Copy `wrangler.toml.example` to `wrangler.toml`, set both private origin hostnames
and the stable route, then deploy:

```powershell
npx wrangler login
npx wrangler secret put STATE_TOKEN
npx wrangler deploy
```

Configure both replicas with the same stable `EXOMEM_BASE_URL`, GitHub OAuth app,
`EXOMEM_JWT_SIGNING_KEY`, state URL/token, vault ID, and lease token. Give each a
different replica ID; set `EXOMEM_WRITER_LEASE_PREFERRED=1` only on the preferred
desktop. See `docs/deployment.md` for the complete environment block and takeover
test.
