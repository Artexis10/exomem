# Tasks — deterministic edge ingress

## Lane W — worker (deploy/cloudflare-ha)

- [ ] W1. Stamp all proxied requests: extract the request-id/HMAC helper, apply
      it in the read fan-out loop and `proxyMutationRequest` (WebCrypto
      HMAC-SHA256 keyed by `STATE_TOKEN` over the request-id value; headers
      `x-exomem-request-id`, `x-exomem-edge-auth`).
- [ ] W2. `GET /__version` gated by `authorized(request, env.STATE_TOKEN)`;
      payload per design.md Decision 2; `WORKER_GIT_SHA` var with
      `"unlabeled"` fallback; no secrets in payload.
- [ ] W3. Deploy helpers `deploy.ps1` / `deploy.sh` passing
      `--var WORKER_GIT_SHA:<short sha>`; README + wrangler.toml.example
      updated.
- [ ] W4. Tests in `test/worker.test.mjs`: stamp on both paths, HMAC
      correctness, /__version auth gate + shape + secret exclusion +
      unlabeled fallback.

## Lane P — origin (src/exomem) and doctor

- [ ] P1. Edge-stamp verification middleware (new module, e.g.
      `edge_ingress.py`): enforcement predicate per design.md Decision 1;
      installed on the FastMCP streamable-http app and the REST facade;
      `INGRESS_BYPASSED` OpError (403) registered as terminal; content-free
      bypass logging; `EXOMEM_EDGE_STAMP_ENFORCE` kill switch.
- [ ] P2. Doctor `edge-ingress` section per design.md Decision 3 (four
      checks), skipped when coordination disabled.
- [ ] P3. Tests: middleware matrix (enforce/exempt/kill-switch/lease-off),
      terminal classification, doctor checks against stubbed worker-shaped
      and tunnel-shaped endpoints.

## Verification

- [ ] V1. `node --test deploy/cloudflare-ha/test/` green.
- [ ] V2. Lean pytest (lease + new suites) green on Windows.
- [ ] V3. `openspec validate deterministic-edge-ingress` passes.
