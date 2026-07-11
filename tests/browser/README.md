# Review Studio browser acceptance

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
