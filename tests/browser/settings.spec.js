const { test, expect } = require('@playwright/test');
const { installMocks, settings } = require('./fixtures');
const models = [
  { id: 'gpt-5.6-sol', provider: 'openai', label: 'GPT 5.6 Sol', agent_cli: true, efforts: ['low', 'medium', 'high'] },
  { id: 'gemini-4-argon', provider: 'gemini', label: 'Gemini 4 Argon', agent_cli: true, efforts: ['low', 'high'] },
  { id: 'gemini-3.1-flash-lite', provider: 'gemini', label: 'Gemini 3.1 Flash Lite', agent_cli: true, efforts: ['low', 'high'] },
  ...['nemotron-3-ultra-free','nemotron-3.5-lightning-free','nemotron-3.5-super-vl-free','big-pickle','glm-5.3-flash-free','deepseek-v4.1-flash-free','beam-free','flint-40b-free','mimo-v2.6-flash-free'].map(id => ({id: 'opencode/' + id, provider: 'opencode', label: id, free: true, agent_cli: true, efforts: []})),
  { id: 'flint/local', provider: 'flint', label: 'Local Flint', agent_cli: true, efforts: [] }
];
const roleFields = ['research_agent_model','author_agent_model','scoping_model','council_gemini_model','council_openai_model','council_claude_model','pi_agent_model'];
const initial = { ...settings, research_agent_model: 'gpt-5.6-sol', author_agent_model: 'gemini-4-argon', research_agent_effort: 'high', openai_token: '••••••••' };
async function open(page) {
  await page.goto('/dashboard'); await expect(page.locator('.hero')).toBeVisible();
  await page.evaluate(() => { openSettings(); });
  await expect(page.locator('#setting-research_agent_model option[value="flint/local"]')).toHaveCount(1);
}
test.use({ viewport: { width: 390, height: 844 } });

test('all new models appear in all seven roles and save while research is active', async ({ page }) => {
  const mock = await installMocks(page, { overrides: {
    '/models': { models },
    '/settings': request => request.method() === 'PUT' ? {status:'ok',saved:true,applied_agents:['agent'],pending_agents:[]} : initial,
    '/watchdog': {scripts:[],config:{}},
  }});
  await open(page);
  for (const field of roleFields) {
    const values = await page.locator('#setting-' + field + ' option').evaluateAll(options => options.map(o => o.value));
    for (const model of models) expect(values).toContain(model.id);
  }
  await expect(page.locator('#setting-author_agent_model')).toHaveValue('gemini-4-argon');
  await page.locator('#setting-research_agent_model').selectOption('opencode/nemotron-3.5-lightning-free');
  await expect(page.locator('#setting-research_agent_effort')).toBeDisabled();
  await expect(page.locator('#setting-research_agent_effort')).toHaveValue('');
  await page.locator('#setting-purpose').fill('Edited while experiments run');
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('.set-status')).toContainText('Research continued from saved project state');
  const saved = mock.requests.find(r => r.path === '/settings' && r.method === 'PUT');
  expect(saved.body.settings.research_agent_model).toBe('opencode/nemotron-3.5-lightning-free');
  expect(saved.body.settings.purpose).toBe('Edited while experiments run');
  expect(saved.body.settings.openai_token).toBe('••••••••');
  expect(mock.requests.filter(r => r.path.includes('interrupt') || r.path === '/reset')).toEqual([]);
  expect(mock.errors).toEqual([]);
});

test('plain-text server error preserves settings drafts and retry succeeds', async ({ page }) => {
  const mock = await installMocks(page, {overrides:{'/models':{models},'/settings':initial}});
  let saves = 0;
  await page.route('**/api/settings', async route => {
    if (route.request().method() !== 'PUT') return route.fallback();
    saves++;
    return saves === 1 ? route.fulfill({status:500,contentType:'text/plain',body:'Internal Server Error'})
      : route.fulfill({contentType:'application/json',body:JSON.stringify({status:'ok',saved:true,pending_agents:['agent'],pending_reasons:{agent:'research_paused'}})});
  });
  await open(page); await page.locator('#setting-purpose').fill('Keep my unsaved research notes');
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('.set-status')).toContainText('Server error (500)');
  await expect(page.locator('.set-status')).not.toContainText('Unexpected token');
  await expect(page.locator('#setting-purpose')).toHaveValue('Keep my unsaved research notes');
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('.set-status')).toContainText('when research resumes');
  expect(saves).toBe(2); expect(mock.errors).toEqual([]);
});

test('partial apply has an actionable retry without losing edits', async ({page}) => {
  let saves = 0;
  const mock = await installMocks(page,{overrides:{'/models':{models},'/settings':request => {
    if (request.method() !== 'PUT') return initial;
    saves++;
    return saves === 1 ? {status:'partial',saved:true,warnings:['Settings saved, but the agent could not restart. Retry Save.']}
      : {status:'ok',saved:true,applied_agents:['agent']};
  }}});
  await open(page); await page.locator('#setting-purpose').fill('Preserved after partial apply');
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('.set-status')).toContainText('could not restart');
  await expect(page.locator('#setting-purpose')).toHaveValue('Preserved after partial apply');
  await page.getByRole('button',{name:'Retry applying settings',exact:true}).click();
  await expect(page.locator('.set-status')).toContainText('Research continued from saved project state');
  expect(saves).toBe(2); expect(mock.errors).toEqual([]);
});

test('email pause failure is visible and does not falsely change status', async ({page}) => {
  await installMocks(page,{overrides:{'/models':{models},'/settings':initial,'/emails/resume':{httpStatus:503,body:{detail:'Email setting could not be saved'}}}});
  await open(page);
  const button=page.getByRole('button',{name:'▶ Resume all emails',exact:true});
  await button.click();
  await expect(page.locator('.set-email-hint').first()).toContainText('could not be saved');
  await expect(button).toBeEnabled();
});
