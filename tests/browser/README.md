# Browser regressions

These tests serve only the repository's static files. Every `/api/` request is
intercepted by Playwright, and requests to other origins are blocked. They do
not start the application backend, access training, or send email.

```sh
npm ci --prefix tests/browser
cd tests/browser
npx playwright install webkit chromium
npm test
```

Run one viewport during development:

```sh
npm test -- --project=webkit --grep '390x844'
```

The static server binds `127.0.0.1:18341`. Run one test command at a time.
Screenshots are written to `/tmp/arui-mobile-audit/screenshots`; failure traces
and screenshots go to `/tmp/arui-mobile-audit/results`. Tests use populated,
non-sensitive fixtures in `fixtures.js`, including custom endpoint overrides
for failure paths.

The suite covers WebKit and Chromium at 320×568, 375×667, 390×844, 430×932,
and 844×390; desktop checks use 1440×1000. This is browser emulation, not a
physical iPhone. Native iOS keyboard/chrome behavior still needs an on-device
smoke test; viewport-shrink tests exercise the corresponding layout behavior.
