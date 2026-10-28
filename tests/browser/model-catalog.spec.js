const { test, expect } = require('@playwright/test');
const { execFileSync } = require('child_process');
const path = require('path');
const { installMocks, settings, scope } = require('./fixtures');

// Read the actual Python catalog, so additions cannot silently miss a role.
const catalog = JSON.parse(execFileSync('python3', ['-c',
  'import json; from backend.app.model_registry import public_registry; print(json.dumps(public_registry()))'],
  { cwd: path.resolve(__dirname, '../..'), encoding: 'utf8' }));
const roles = ['research_agent_model', 'author_agent_model', 'scoping_model',
  'council_gemini_model', 'council_openai_model', 'council_claude_model', 'pi_agent_model'];

test.use({ viewport: { width: 390, height: 844 } });
test('every catalog model is selectable in each supported role with its own effort choices', async ({ page }) => {
  test.setTimeout(120000);
  const mock = await installMocks(page, { overrides: {
    '/models': catalog, '/settings': settings, '/watchdog': { scripts: [], config: {} },
  }});
  await page.goto('/dashboard');
  await expect(page.locator('.hero')).toBeVisible();
  await page.evaluate(() => openSettings());
  await expect(page.locator('#setting-research_agent_model option[value="flint/local"]')).toHaveCount(1);
  for (const role of roles) {
    const select = page.locator('#setting-' + role);
    const eligible = catalog.models.filter(m => !['research_agent_model', 'author_agent_model'].includes(role) || m.agent_cli);
    const choices = await select.locator('option').evaluateAll(options => options.map(o => o.value));
    for (const model of eligible) {
      expect(choices, role + ': ' + model.id).toContain(model.id);
      await select.selectOption(model.id);
      await expect(select).toHaveValue(model.id);
      const effort = page.locator('#setting-' + role.replace(/_model$/, '_effort'));
      if (await effort.count()) {
        const values = await effort.locator('option').evaluateAll(options => options.map(o => o.value));
        expect(values).toEqual(model.efforts.length ? model.efforts : ['']);
        expect(await effort.isDisabled()).toBe(!model.efforts.length);
      }
    }
  }
  expect(mock.errors).toEqual([]);
});

test('failed selected-model assessment is visible and cannot be accepted as a completed proposal', async ({ page }) => {
  await installMocks(page, { overrides: {
    '/paper/proposal/failed-test': { id: 'failed-test', status: 'error', council_responses: {
      _error: 'Selected reviewer gemini-4-argon failed. No substitute model was used.',
    } },
  }});
  await page.goto('/dashboard');
  await expect(page.locator('.hero')).toBeVisible();
  await page.evaluate(() => _watchPaperProposal('failed-test'));
  await expect(page.getByText('Selected reviewer gemini-4-argon failed. No substitute model was used.', { exact: true })).toBeVisible();
  await expect(page.getByText('Council is assessing the project', { exact: true })).toHaveCount(0);
});

test('scoping model failure preserves the draft and never starts another model task', async ({ page }) => {
  const mock = await installMocks(page, { scoping: true, overrides: {
    '/scope/chat': { ...scope, error: 'Selected model gemini-4-argon failed: HTTP 429', messages: [
      { role: 'user', text: 'Use matched seeds' },
      { role: 'agent', text: 'Selected model gemini-4-argon failed: HTTP 429' },
    ] },
  }});
  await page.goto('/dashboard');
  await expect(page.locator('[data-scope-panel="chat"]')).toBeVisible();
  await page.locator('[data-scope-panel="chat"]').click();
  await page.locator('#scope-chatin').fill('Use matched seeds');
  await page.locator('#scope-send').click();
  await expect(page.locator('#scope-chat')).toContainText('Selected model gemini-4-argon failed');
  await expect(page.locator('#scope-chatin')).toHaveValue('Use matched seeds');
  expect(mock.requests.filter(r => r.path === '/scope/finalize')).toEqual([]);
});

test('failed plan update is reported instead of presenting an old plan as a new answer', async ({ page }) => {
  await installMocks(page, { scoping: true, overrides: {
    '/scope/chat': { ...scope, messages: [{ role: 'agent', text: 'Use matched seeds.' }] },
    '/scope/finalize': { ...scope, error: 'Selected model gemini-4-argon failed: HTTP 429' },
  }});
  await page.goto('/dashboard');
  await expect(page.locator('[data-scope-panel="chat"]')).toBeVisible();
  await page.locator('[data-scope-panel="chat"]').click();
  await page.locator('#scope-chatin').fill('Use matched seeds');
  await page.locator('#scope-send').click();
  await page.locator('[data-scope-panel="plan"]').click();
  await expect(page.locator('#scope-sync')).toContainText('Plan update failed: Selected model gemini-4-argon');
  await expect(page.locator('#scope-sync')).toHaveAttribute('role', 'alert');
});

test('incomplete required code review stays visibly failed on iPhone', async ({ page }, testInfo) => {
  await installMocks(page, { overrides: {
    '/council/bless/status': { status: 'error', error: 'Required code review incomplete: gemini-4-argon',
      summary: 'Required code review incomplete: gemini-4-argon', verdicts: {
        gemini: { approved: null, summary: 'Selected model gemini-4-argon failed: HTTP 429' },
        openai: { approved: true, summary: 'Approved' },
      } },
  }});
  await page.goto('/dashboard');
  await expect(page.locator('#bless-inline')).toContainText('Required code review incomplete: gemini-4-argon');
  await expect(page.locator('#statusbar')).toHaveClass(/sb-bad/);
  await page.locator('#bless-inline .sb-alert-tx').click();
  await expect(page.locator('.crv-status')).toHaveText('Review incomplete');
  await expect(page.locator('.crv-fail')).toHaveText('— call failed');
  await expect(page.locator('.crv-summary')).toContainText('gemini-4-argon');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('review-incomplete.png') });
});
