const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { installMocks, paperState } = require('./fixtures');
const sizes = [[320,568], [375,667], [390,844], [430,932], [844,390]];
async function noOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
}
async function screenshot(page, label, testInfo) {
  if (testInfo.project.name !== 'webkit') return;
  const dir = '/tmp/arui-mobile-audit/screenshots'; fs.mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: path.join(dir, `${page.viewportSize().width}-${label}.png`) });
}
async function hitTarget(locator) {
  await locator.scrollIntoViewIfNeeded();
  expect(await locator.evaluate(el => {
    const b = el.getBoundingClientRect();
    return b.width > 0 && b.height > 0 && b.x >= 0 && b.right <= innerWidth + 1 &&
      el.contains(document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2));
  })).toBe(true);
}
for (const [width, height] of sizes) {
  test.describe(`${width}x${height}`, () => {
    test.use({ viewport: { width, height } });
    test('all navigation pages fit the screen', async ({ page }, info) => {
      const mock = await installMocks(page);
      for (const [route, ready] of [['dashboard','.hero'],['analysis','.anav2-panel'],['files','.files-row'],['lessons','.lessons-wrap'],['write-paper','#paper-cta'],['system-stats','.sys-maint'],['authkeys','.ak-addbtn']]) {
        await page.goto('/' + route); await expect(page.locator(ready).first()).toBeVisible();
        await noOverflow(page); await screenshot(page, route, info);
        await hitTarget(page.getByRole('button', { name: 'Open navigation', exact: true }));
      }
      expect(mock.errors).toEqual([]);
    });
    test('conversation submit is unobstructed and closes independently', async ({ page }, info) => {
      const mock = await installMocks(page); await page.goto('/dashboard');
      await page.getByRole('button', { name: 'Open agent conversation', exact: true }).click();
      await expect(page.locator('.fab')).toBeHidden();
      const send = page.locator('.composer .row button');
      await hitTarget(send); await page.locator('#chatin').fill('what is the current status?');
      await send.click();
      await expect.poll(() => mock.requests.some(r => r.path === '/agent/send' && r.body.text === 'what is the current status?')).toBe(true);
      await screenshot(page, 'conversation', info);
      for (const tab of ['Summary', 'Research agent', 'Sessions', 'Activity feed']) {
        await page.getByRole('button', { name: tab, exact: true }).click(); await noOverflow(page);
      }
      await page.getByRole('button', { name: 'Close agent conversation', exact: true }).click();
      await expect(page.locator('.fab')).toBeVisible();
      expect(mock.errors).toEqual([]);
    });
    test('scoping plan, review, discussion and confirm are accessible', async ({ page }, info) => {
      const mock = await installMocks(page, { scoping: true }); await page.goto('/');
      await expect(page.locator('#scope-final')).toBeVisible(); await noOverflow(page);
      await screenshot(page, 'scope-plan', info);
      await hitTarget(page.locator('#scope-confirm'));
      await screenshot(page, 'scope-confirm', info);
      await page.getByRole('button', { name: 'Review status', exact: true }).click();
      await expect(page.locator('.scope-side')).toBeVisible(); await screenshot(page, 'scope-review', info);
      await page.getByRole('button', { name: 'Discuss', exact: true }).click();
      await page.locator('#scope-chatin').fill('Use matched seeds'); await hitTarget(page.locator('#scope-send'));
      await page.locator('#scope-send').click(); await screenshot(page, 'scope-discussion', info);
      await page.getByRole('button', { name: 'Research plan', exact: true }).click();
      await expect(page.locator('#scope-final')).toBeVisible();
      await page.locator('#scope-final').fill('Keep my edited direction');
      await page.locator('#scope-confirm').click();
      await expect.poll(() => mock.requests.some(r => r.path === '/scope/confirm' && r.body.final_direction === 'Keep my edited direction')).toBe(true);
      await expect(page.locator('.boot-wrap')).toBeVisible(); await screenshot(page, 'agent-boot', info);
      expect(mock.errors).toEqual([]);
    });
    test('onboarding and login fit and editable controls avoid focus zoom', async ({ page }, info) => {
      await installMocks(page, { onboarding: true }); await page.goto('/');
      await expect(page.locator('.onb-start')).toBeAttached(); await noOverflow(page);
      await screenshot(page, 'onboarding', info);
      await hitTarget(page.locator('.onb-start'));
      expect(await page.locator('.onb-in').first().evaluate(el => parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(16);
      await page.evaluate(() => renderLogin()); await hitTarget(page.locator('#login-go')); await screenshot(page, 'login', info);
    });
    test('settings and long dialogs scroll without clipping actions', async ({ page }, info) => {
      await installMocks(page); await page.goto('/dashboard'); await expect(page.locator('.hero')).toBeVisible();
      await page.evaluate(() => { openSettings(); }); await expect(page.locator('.modal-settings .onb-in').first()).toBeAttached();
      await noOverflow(page); await screenshot(page, 'settings-top', info);
      const save = page.locator('.modal-settings .modal-actions button').first(); await hitTarget(save);
      await screenshot(page, 'settings-save', info);
      await page.evaluate(() => document.querySelectorAll('.mscrim').forEach(el => el.remove()));
      await page.evaluate(() => { aruiAlert('Long error explanation '.repeat(120), { title: 'Validation error' }); });
      await hitTarget(page.locator('.arui-dlg-ok')); await noOverflow(page); await screenshot(page, 'long-alert', info);
      await page.locator('.arui-dlg-ok').click();
      await page.evaluate(() => { openFilterModal(document.querySelector('.viewpane') || document.querySelector('.left')); });
      await expect(page.locator('.modal-filter')).toBeVisible(); await noOverflow(page); await screenshot(page, 'filter', info);
    });
    test('paper tabs and operator approval fit', async ({ page }, info) => {
      const mock = await installMocks(page, { paper: true }); await page.goto('/write-paper');
      await expect(page.locator('.paper-tabs')).toBeVisible();
      for (const tab of ['today','coverage','plan','path','refs','versions','share']) {
        await page.locator(`.paper-tab[data-t="${tab}"]`).click();
        await page.locator('#paper-tab-body').scrollIntoViewIfNeeded();
        await noOverflow(page); await screenshot(page, 'paper-' + tab, info);
      }
      await page.evaluate(state => {
        PaperState.status = { meta: { ...state.meta, ablations: Array.from({length:20}, (_,i) => ({id:'ablation-'+i,dataset:'benchmark',variant:'replay',seeds:3})) }, progress: { ablations: { proposed:20,est_hours:10 } }, gate:{plan:{note:'Review these experiments before approving.'}} };
        openApprovalModal(document.querySelector('.viewpane'));
      }, paperState);
      await expect(page.locator('.modal-approval')).toBeVisible();
      await hitTarget(page.locator('.approval-actions button').last()); await noOverflow(page); await screenshot(page, 'paper-approval', info);
      expect(mock.errors).toEqual([]);
    });
  });
}
module.exports = { hitTarget, noOverflow };
