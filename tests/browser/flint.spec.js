const { test, expect } = require('@playwright/test');
const { installMocks, settings } = require('./fixtures');
test.use({ viewport: { width: 390, height: 844 } });
const initial = { ...settings, research_agent_model: 'claude-sonnet-4-6', research_agent_effort: 'high' };
const stopped = {status:'not_installed',detail:'Choose Flint to install and start it automatically.'};
async function open(page) {
  await page.goto('/dashboard'); await expect(page.locator('.hero')).toBeVisible();
  await page.evaluate(() => openSettings());
  await expect(page.locator('#setting-research_agent_model option[value="flint/local"]')).toHaveCount(1);
}

test('selecting Flint prepares it automatically and saves a pending handoff without manual connection fields', async ({page}) => {
  let state = stopped;
  const mock = await installMocks(page, {overrides:{
    '/settings': request => request.method() === 'PUT' ? {status:'ok',saved:true,pending_agents:['agent'],pending_reasons:{agent:'model_starting'}} : initial,
    '/flint/status': () => state,
    '/flint/prepare': () => (state = {status:'downloading',detail:'Downloading the official Flint model (26 GB, once).',progress:.25}),
  }});
  await open(page);
  await expect(page.locator('#setting-flint_base_url')).toBeHidden();
  await expect(page.locator('#setting-flint_token')).toBeHidden();
  await page.locator('#setting-research_agent_model').selectOption('flint/local');
  await expect(page.locator('.flint-setup-status')).toContainText('Downloading');
  await expect(page.locator('.flint-setup progress')).toHaveAttribute('value','0.25');
  expect(mock.requests.filter(r => r.path === '/flint/prepare')).toHaveLength(1);
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('.set-status')).toContainText('current agent stays running');
  const saved = mock.requests.find(r => r.path === '/settings' && r.method === 'PUT');
  expect(saved.body.settings.flint_mode).toBe('managed');
  expect(saved.body.settings.research_agent_model).toBe('flint/local');
  expect(mock.requests.filter(r => r.path === '/research/interrupt')).toEqual([]);
  expect(mock.errors).toEqual([]);
});

test('setup failure is actionable and stop failure never claims the service stopped', async ({page}) => {
  let attempts = 0;
  await installMocks(page,{overrides:{'/settings':initial,'/flint/status':stopped,
    '/flint/prepare': () => ++attempts === 1 ? {httpStatus:503,body:{detail:'Insufficient disk space'}} : {status:'ready',device:'cpu',detail:'Flint is ready on CPU (4 threads). Responses are slower.'},
    '/flint/stop': {httpStatus:500,body:{detail:'Process could not be stopped'}},
  }});
  await open(page);
  await page.locator('#setting-research_agent_model').selectOption('flint/local');
  await expect(page.locator('.flint-setup-status')).toContainText('Insufficient disk space');
  await page.getByRole('button',{name:'Prepare Flint',exact:true}).click();
  await expect(page.locator('.flint-setup-status')).toContainText('CPU');
  await page.getByRole('button',{name:'Stop Flint',exact:true}).click();
  await expect(page.locator('.flint-setup-status')).toContainText('Could not stop Flint');
  await expect(page.getByRole('button',{name:'Stop Flint',exact:true})).toBeEnabled();
});

test('advanced external server mode preserves an existing custom endpoint', async ({page}) => {
  await installMocks(page,{overrides:{'/settings':{...initial,flint_base_url:'http://localhost:8090/v1',flint_model:'flint-custom'},'/flint/status':stopped}});
  await open(page);
  await expect(page.locator('#setting-flint_mode')).toHaveValue('external');
  await expect(page.locator('#setting-flint_base_url')).toBeVisible();
  await expect(page.locator('#setting-flint_base_url')).toHaveValue('http://localhost:8090/v1');
  await expect(page.locator('.flint-setup')).toBeHidden();
  await page.locator('#setting-flint_mode').selectOption('managed');
  await expect(page.locator('#setting-flint_base_url')).toBeHidden();
  await expect(page.locator('.flint-setup')).toBeVisible();
});

test('save confirmations distinguish conversation continuation and saved-state handoff', async ({page}) => {
  let saves = 0;
  await installMocks(page,{overrides:{'/settings': request => request.method() === 'PUT'
    ? {status:'ok',saved:true,applied_agents:['agent'],context_preservation:{agent:++saves === 1 ? 'native_conversation' : 'workspace_state_only'}} : initial,
    '/flint/status':stopped}});
  await open(page);
  await page.locator('#setting-research_agent_effort').selectOption('medium');
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('.set-status')).toContainText('Research conversation resumed');
  await page.getByRole('button',{name:'Saved ✓',exact:true}).click();
  await expect(page.locator('.set-status')).toContainText('Research continued from saved project state');
  await expect(page.locator('.set-status')).not.toContainText('conversation resumed');
});

test('managed setup remains visible and usable on a narrow iPhone onboarding form', async ({page}) => {
  await page.setViewportSize({width:320,height:568});
  await installMocks(page,{onboarding:true,overrides:{'/flint/status':stopped,'/flint/prepare':{status:'waiting_for_resources',detail:'Waiting for an idle GPU or available RAM. Running experiments stay in place.'}}});
  await page.goto('/');
  await expect(page.locator('#setting-research_agent_model option[value="flint/local"]')).toHaveCount(1);
  await page.locator('#setting-research_agent_model').selectOption('flint/local');
  await expect(page.locator('.flint-setup-status')).toContainText('Waiting for an idle GPU');
  const stop = page.getByRole('button',{name:'Stop Flint',exact:true});
  await stop.scrollIntoViewIfNeeded();
  const box = await stop.boundingBox();
  expect(box.width).toBeGreaterThanOrEqual(44); expect(box.height).toBeGreaterThanOrEqual(44);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
  expect(overflow).toBe(false);
});
