const { test, expect } = require('@playwright/test');
const { installMocks, settings } = require('./fixtures');
const fs = require('node:fs');
test.use({viewport:{width:390,height:844}});
async function inspectDialog(page, label, info) {
  const modal = page.locator('.modal,.arui-modal').last();
  await expect(modal).toBeVisible();
  expect(await modal.evaluate(el => { const r = el.getBoundingClientRect(); return r.x >= 0 && r.right <= innerWidth + 1 && r.y >= 0 && r.bottom <= innerHeight + 1; })).toBe(true);
  for (const button of await modal.locator('button').all()) {
    if (!await button.isVisible() || !await button.isEnabled()) continue;
    await button.scrollIntoViewIfNeeded();
    expect(await button.evaluate(el => {const r=el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}), label + ': ' + await button.textContent()).toBe(true);
  }
  if (info.project.name === 'webkit') {
    fs.mkdirSync('/tmp/arui-mobile-audit/screenshots', {recursive:true});
    await page.screenshot({path:`/tmp/arui-mobile-audit/screenshots/${page.viewportSize().width}-${label}.png`});
  }
}
for (const [width,height] of [[320,568],[390,844],[844,390]]) {
 test.describe(`controls ${width}x${height}`, () => {
  test.use({viewport:{width,height}});
  test('halt success stays halted after dismiss', async ({page},info) => {
    const mock=await installMocks(page); await page.goto('/dashboard');
    await page.getByRole('button',{name:'Halt Research',exact:true}).click();
    await page.getByRole('button',{name:'Yes, halt everything',exact:true}).click();
    await expect(page.locator('#halt-dismiss')).toHaveText('Keep research halted');
    await expect(page.locator('.arui-dlg-hd')).toHaveText('Research halted');
    await inspectDialog(page,'halt-complete',info);
    await page.locator('#halt-dismiss').click();
    expect(mock.requests.filter(r=>r.path==='/research/interrupt')).toHaveLength(1);
    expect(mock.requests.some(r=>r.path.includes('resume'))).toBe(false);
    await expect(page.locator('.mscrim')).toHaveCount(0);
  });
  test('partial halt never claims success or enables resume', async ({page},info) => {
    const mock=await installMocks(page,{overrides:{'/research/interrupt':{httpStatus:409,body:{ok:false,paused:true,killed:[],discarded:0,remaining:['PID 123: external experiment'],errors:['Worker termination could not be verified.'],warnings:['Retry after checking the worker.'],...settings}}}});
    await page.goto('/dashboard'); await page.getByRole('button',{name:'Halt Research',exact:true}).click();
    await page.getByRole('button',{name:'Yes, halt everything',exact:true}).click();
    await expect(page.locator('.arui-dlg-hd')).toHaveText('Halt needs attention');
    await expect(page.locator('#halt-continue')).toBeDisabled(); await expect(page.locator('#halt-restart')).toBeDisabled();
    await expect(page.locator('.halt-report').first()).toContainText('PID 123');
    await inspectDialog(page,'halt-partial',info); await page.locator('#halt-retry').click();
    await expect.poll(()=>mock.requests.filter(r=>r.path==='/research/interrupt').length).toBe(2);
    await page.locator('#halt-dismiss').click(); expect(mock.requests.some(r=>r.path.includes('resume'))).toBe(false);
  });
  test('all modal families fit and their buttons can be reached', async ({page},info) => {
    const mock=await installMocks(page); await page.goto('/analysis'); await expect(page.locator('.anav2-panel')).toBeVisible();
    const scenarios=[
      ['health',()=>openResearchHealth()],
      ['novelty',()=>openNoveltyModal()],
      ['council-review',()=>openCouncilReviewModal()],
      ['archive',()=>openArchive()],
      ['paper-onboard',()=>openPaperOnboard('proposal-test')],
      ['paper-revert',()=>openRevertModal()],
      ['proposal',()=>_showPaperProposalResults({id:'proposal-test',status:'ready',council_responses:{gemini:{recommendation:'proceed_to_paper',novelty:'high',rationale_md:'Controlled results support the central claim. '.repeat(20),claims:[{title:'Replay improves retention',summary:'Evidence over three seeds.'}]},claude:{recommendation:'keep_researching',red_flags:['Add more seeds'],rationale_md:'Please broaden validation.'}}})],
      ['token-errors',()=>{showTokenCheckResults({claude:{ok:false,detail:'Invalid credentials. '.repeat(60)},gemini:{ok:true}},'claude');}],
      ['version-diff',()=>openVersionDiffModal('a',[{id:'a',label:'Initial draft'},{id:'b',label:'Revised draft'}])],
      ['submission',()=>openSubmitHelper()],
      ['purge-old',()=>openPurgeOldRunLogs(document.querySelector('.viewpane'))],
      ['purge-sota',()=>openPurgeKeepSotaOnly(document.querySelector('.viewpane'))],
      ['filter',()=>openFilterModal(document.querySelector('.viewpane'))],
      ['add-panel',()=>openAddPanelModal(document.querySelector('.viewpane'))],
      ['edit-panel',()=>openEditPanelModal(document.querySelector('.viewpane'),{id:'metric',title:'Validation loss',y_keys:['val_loss'],x_key:'step'})],
      ['prompt',()=>{aruiPrompt('Name the new research file', {title:'New file'});}],
    ];
    for(const [label,fn] of scenarios) {
      await page.evaluate(fn); await inspectDialog(page,label,info);
      await page.evaluate(()=>{closeCouncilReviewModal();document.querySelectorAll('.mscrim,.arui-modal-back').forEach(el=>el.remove());});
    }
    expect(mock.errors).toEqual([]);
  });
 });
}
test('Enter on Cancel cancels a destructive dialog',async({page})=>{
  await installMocks(page);await page.goto('/dashboard');
  await page.evaluate(()=>{window.dialogResult='pending';aruiConfirm('Delete the test data?',{danger:true}).then(value=>window.dialogResult=value);});
  await page.locator('.arui-dlg-cancel').focus();await page.keyboard.press('Enter');
  await expect.poll(()=>page.evaluate(()=>window.dialogResult)).toBe(false);
});
test('composer follows a shrinking visual viewport',async({page})=>{
  await page.setViewportSize({width:390,height:844});await installMocks(page);await page.goto('/dashboard');
  await page.getByRole('button',{name:'Open agent conversation',exact:true}).click();
  await page.locator('#chatin').focus();await page.setViewportSize({width:390,height:360});
  const send=page.locator('.composer .row button');await expect(send).toBeInViewport();
  expect(await send.evaluate(el=>{const r=el.getBoundingClientRect();return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));})).toBe(true);
  await expect(page.getByRole('button',{name:'Close agent conversation',exact:true})).toBeInViewport();
});
test('failed conversation send preserves the draft for retry',async({page})=>{
  const mock=await installMocks(page,{overrides:{'/agent/send':{httpStatus:503,body:{detail:'Agent is reconnecting'}}}});
  await page.goto('/dashboard');await page.getByRole('button',{name:'Open agent conversation',exact:true}).click();
  await page.locator('#chatin').fill('Keep this unsent direction');await page.locator('.composer .row button').click();
  await expect(page.locator('.arui-dlg-bd')).toContainText('Agent is reconnecting');await page.locator('.arui-dlg-ok').click();
  await expect(page.locator('#chatin')).toHaveValue('Keep this unsent direction');expect(mock.errors).toEqual([]);
});
test('claim ready toggle persists and shows failures without lying',async({page})=>{
  const override={}; const mock=await installMocks(page,{paper:true,overrides:override});await page.goto('/write-paper');
  await page.locator('.paper-tab[data-t="coverage"]').click();const star=page.locator('.cov-star');
  await star.click();await expect(star).toHaveAttribute('aria-pressed','true');
  expect(mock.requests.some(r=>r.path==='/paper/claims/c1/update'&&r.method==='PUT'&&r.body.ready===true)).toBe(true);
  override['/paper/claims/c1/update']={httpStatus:500,body:{detail:'Database busy'}};
  await star.click();await expect(page.locator('.arui-dlg-bd')).toContainText('Database busy');
  await page.locator('.arui-dlg-ok').click();await expect(star).toHaveAttribute('aria-pressed','true');
});
test('citation notes survive save failure and tab navigation then save',async({page})=>{
  const override={'/paper/citations/replay2026/update':{httpStatus:500,body:{detail:'Database busy'}}};
  const mock=await installMocks(page,{paper:true,overrides:override});await page.goto('/write-paper');
  await page.locator('.paper-tab[data-t="refs"]').click();
  await page.locator('.diff-cell[data-fld="same"]').fill('Both use balanced replay');
  await page.locator('.diff-cell[data-fld="diff"]').fill('Our replay schedule adapts to forgetting');
  await page.locator('#citation-save').click();await expect(page.locator('#citation-save-status')).toContainText('Database busy');
  await page.locator('.paper-tab[data-t="today"]').click();await page.locator('.paper-tab[data-t="refs"]').click();
  await expect(page.locator('.diff-cell[data-fld="same"]')).toHaveValue('Both use balanced replay');
  override['/paper/citations/replay2026/update']={ok:true};await page.locator('#citation-save').click();
  await expect(page.locator('#citation-save-status')).toHaveText('Notes saved');
  expect(mock.requests.some(r=>r.path==='/paper/citations/replay2026/update'&&r.method==='PUT'&&r.body.diff_md==='Our replay schedule adapts to forgetting')).toBe(true);
});
test('menu navigation and reset cancellation work on phones',async({page})=>{
  const mock=await installMocks(page);await page.goto('/dashboard');
  for (const [label,route] of [['Analysis','analysis'],['Files','files'],['Lessons','lessons'],['Write the paper','write-paper'],['System stats','system-stats'],['authorized_keys','authkeys'],['Dashboard','dashboard']]) {
    await page.getByRole('button',{name:'Open navigation',exact:true}).click();
    await page.locator('.menu-item').filter({hasText:label}).click();await expect(page).toHaveURL(new RegExp('/'+route+'$'));
  }
  await page.getByRole('button',{name:'Open navigation',exact:true}).click();await page.locator('.menu-item').filter({hasText:'Settings'}).click();
  await expect(page.locator('.modal-settings')).toBeVisible();await page.locator('.modal-settings .iconbtn').click();
  await page.getByRole('button',{name:'Open navigation',exact:true}).click();await page.locator('.menu-item').filter({hasText:'Archive'}).click();
  await expect(page.locator('#arc-x')).toBeVisible();await page.locator('#arc-x').click();
  await page.getByRole('button',{name:'Open navigation',exact:true}).click();await page.locator('.menu-item').filter({hasText:'Reset'}).click();
  await page.locator('.arui-dlg-cancel').click();expect(mock.requests.some(r=>r.path==='/reset')).toBe(false);
});
test('analysis menus remain in the phone viewport and change selection',async({page})=>{
  await page.setViewportSize({width:320,height:568});await installMocks(page);await page.goto('/analysis');
  await page.locator('.anav2-sort-btn').click();
  const pop=page.locator('.anav2-popover');
  expect(await pop.evaluate(el=>{const r=el.getBoundingClientRect();return r.x>=0&&r.right<=innerWidth&&r.y>=0&&r.bottom<=innerHeight;})).toBe(true);
  await pop.locator('[data-k="name"]').click();await page.locator('.anav2-bulk-btn').click();
  expect(await pop.evaluate(el=>{const r=el.getBoundingClientRect();return r.x>=0&&r.right<=innerWidth&&r.y>=0&&r.bottom<=innerHeight;})).toBe(true);
  await pop.locator('[data-a="all"]').click();await expect(page.locator('.anav2-visualized')).toContainText('12');
  await page.locator('.anav2-clear').click();await expect(page.locator('.anav2-visualized')).toContainText('0');
});
