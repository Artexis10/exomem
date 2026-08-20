# Tasks — deterministic edge ingress

## Lane W — worker (deploy/cloudflare-ha) — WITHDRAWN 2026-08-20

`deploy/cloudflare-ha/` has been deleted (#550, #581). This change's premise was
that the public hostname is served by **two** competing routing layers, the HA
edge worker and a tunnel-direct connector, and that which one answers is an
accident of DNS bindings and connector restarts. Retiring the worker removes one
of the two layers, so the ambiguity these tasks were written to make
deterministic no longer exists on this deployment.

W1–W4 are therefore withdrawn rather than done. If a two-replica deployment ever
returns, recover the worker from git history and reinstate them — the design in
`design.md` still holds, and the origin-side half in Lane P is already shipped
and unaffected.

**Carry this forward if you do:** `edge_ingress` enforcement fires only when
writer-lease coordination is enabled. With no stamping edge in front of it,
turning coordination on would make the origin refuse every Cloudflare-transited
unsafe-method request. That trade is fail-closed by design, but it is a sharper
edge now that nothing in-tree stamps.


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
