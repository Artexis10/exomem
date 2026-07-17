# Browser acceptance

This is an opt-in development lane, not a Python or runtime dependency. Start an
Exomem service from the checkout, then run:

```sh
cd tests/browser
npm install
npx playwright install chromium
STUDIO_BASE_URL=http://127.0.0.1:8765 npm test
```

The browser loads the real packaged shell and intercepts only `/api/*` with
deterministic fixtures. Python route/integration tests separately exercise the
real authenticated REST leaves and governed writes.

## Hosted live-transfer drill

`live-transfer.spec.mjs` is a separate, mutating canary gate for OpenSpec task
12.3. It never intercepts routes or substitutes fixtures. It obtains fresh
tickets from the real Substrate owner session, then sends browser bodies through
the reviewed Cloudflare transfer hostname. Regular `npm test` excludes it.

Run it only against the disposable owner canary. A successful run preserves its
uploaded Evidence artifacts so the later product-scoped deletion drill can prove
their removal. The storage-state file is a private operator artifact and must not
be committed. The gate requires it to be a regular non-symlink file outside the
repository and, on POSIX systems, rejects group/other access. Create it
interactively after the owner invite/session flow, for example:

```sh
npx playwright codegen \
  --save-storage=/secure/exomem-owner.storage-state.json \
  https://substratesystems.io/exomem
chmod 600 /secure/exomem-owner.storage-state.json
```

Close the codegen browser only after the Exomem owner page is authenticated.
Then run the live gate:

```sh
EXOMEM_LIVE_ENABLED=1 \
EXOMEM_LIVE_BASE_URL=https://substratesystems.io \
EXOMEM_LIVE_TRANSFER_HOST=transfer.substratesystems.io \
EXOMEM_LIVE_STORAGE_STATE=/secure/exomem-owner.storage-state.json \
npm run test:hosted-live
```

The 90 MiB upload becomes the default large-download fixture in the same serial
run. `EXOMEM_LIVE_DOWNLOAD_PATH` may instead name a reviewed pre-existing canary
artifact; set `EXOMEM_LIVE_DOWNLOAD_MIN_BYTES` when its minimum expected size is
not 5 MiB (lower values are rejected). The gate fails—rather than skips—when
enabled without its required origin, transfer host, or private storage state.
Grant-bearing traces are disabled for the live project.
