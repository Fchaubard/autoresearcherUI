const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: '.', testMatch: '*.spec.js', timeout: 45000,
  expect: { timeout: 6000 }, fullyParallel: true, workers: 3,
  outputDir: '/tmp/arui-mobile-audit/results',
  reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:18341', screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  projects: [{ name: 'webkit', use: { browserName: 'webkit', isMobile: true, hasTouch: true } },
             { name: 'chromium', use: { browserName: 'chromium', isMobile: true, hasTouch: true } }],
  webServer: { command: 'node server.js', url: 'http://127.0.0.1:18341', reuseExistingServer: false }
});
