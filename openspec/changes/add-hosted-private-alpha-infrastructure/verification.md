# Verification evidence

Evidence is appended as implementation gates close. A checked task means the result below was reproduced or explicitly inherited from the reviewed branch baseline; known red baselines remain named until their later task closes them.

## 2026-07-14 — trustworthy baseline

### Exomem `main`

- Frontmatter cache freshness fix merged through PR #230 at `80eb668` after independent review.
- Fresh lean suite: 2,233 passed, 19 optional platform/model skips.
- The fix hashes current page bytes before cache reuse, so same-size/same-mtime external edits cannot serve stale frontmatter.

### Exomem PR #227

- Refreshed branch head: `d925642` (`feat/hosted-multi-tenant-service`), mergeable and pushed.
- Semantic merge preserves current-main content-hash freshness, operation-scoped writer fencing, hosted mutation serialization, idempotency isolation, privacy, transfers, and workers.
- Independent review found and then approved the invocation-level hosted admission fix: read-only `connect_memory` modes remain usable during mutation-authority outage/quiescence; write and unknown modes fail closed.
- Fresh full lean suite: 2,392 passed, 19 optional platform/model skips. Focused hosted/lifecycle/lease suite, Ruff correctness/scoped checks, latency gate, installed-wheel product E2E, capability generation, package/image checks, and strict OpenSpec validation passed.
- Every GitHub check on the recorded head passed, including Python 3.11/3.13, retrieval golden gate, Docker smoke, installed-wheel E2E, package, lint/types, onboarding, capabilities, and OpenSpec.

### Substrate PR #32

- Refreshed branch head: `21bb0f1` (`feat/exomem-hosted-service`), mergeable and pushed.
- Reproduced failed Vercel deployment: three minute/hour Exomem jobs in `vercel.json` violated the Vercel Hobby once-per-day cron limit.
- Fixed by keeping the authenticated handlers on Vercel while moving cadence ownership to the versioned K3s schedule contract. The contract pins origin, paths/cadence, dedicated least-privilege bearer and two-version receiver overlap, redirect denial, timeouts/deadlines, non-overlap, retries/history/TTL, content-free metrics, and alerts. Global `CRON_SECRET` is not exposed to K3s.
- Fresh unit/contract suite: 451 passed across 102 suites; TypeScript (`npx tsc --noEmit`), Prettier, strict OpenSpec (16 items), production Next build, and diff checks passed. Independent code and cross-repository artifact reviews approved the head.
- Replacement Vercel deployment check passed on `21bb0f1`, proving the original deployment failure is closed.
- Real PostgreSQL integration/migration baseline inherited unchanged from reviewed `52dabed`: 22 tests across five suites through migration 0021. This session had no `DATABASE_URL`, so the local production-only migration launcher correctly skipped; static migration serialization/transaction tests and the production build passed.
- Lint baseline is explicitly red: `npm run lint` invokes removed Next 16 `next lint` behavior and exits with `Invalid project directory .../lint`; the repository currently has no ESLint dependency/config. This is not hidden as success and must be repaired before Substrate task 3.10 is checked.
