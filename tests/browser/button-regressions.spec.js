const {test,expect}=require('@playwright/test');
const {installMocks,paperState}=require('./fixtures');
test.use({viewport:{width:390,height:844}});

test('New File refuses to overwrite an existing file',async({page})=>{
  const mock=await installMocks(page,{overrides:{'/files/write':{httpStatus:409,body:{detail:'File already exists'}}}});
  await page.goto('/files');await expect(page.locator('.files-row').first()).toBeVisible();
  await page.locator('#files-new').click();await page.locator('.arui-dlg-in').fill('train.py');await page.locator('.arui-dlg-ok').click();
  await expect(page.locator('.arui-dlg-bd')).toContainText('File already exists');
  const write=mock.requests.find(r=>r.path==='/files/write');expect(write.body).toMatchObject({create_only:true,content:''});
  expect(mock.errors).toEqual([]);
});

for(const action of ['switch tab','edit during save']) {
 test(`File save completion preserves changes when users ${action}`,async({page})=>{
  await installMocks(page);await page.goto('/files');await expect(page.locator('#files-save')).toBeVisible();
  await page.evaluate(()=>{
    const model=value=>({value,version:1,getValue(){return this.value},getVersionId(){return this.version},isDisposed(){return false}});
    const a={path:'/workspace/research/a.py',dirty:true,model:model('first contents')};
    const b={path:'/workspace/research/b.py',dirty:true,model:model('second contents')};
    FilesState.tabs=[a,b];FilesState.file=a.path;FilesState.dirty=true;FilesState.editor={getValue:()=>a.model.value};
    _updateSaveBar();_paintTabs();
  });
  let release,received;
  const gate=new Promise(resolve=>release=resolve);
  await page.route('**/api/files/write',async route=>{received=route.request().postDataJSON();await gate;await route.fulfill({json:{ok:true}});});
  await page.locator('#files-save').click();await expect.poll(()=>received?.content).toBe('first contents');
  await page.evaluate(action=>{
    if(action==='switch tab') {FilesState.file=FilesState.tabs[1].path;FilesState.dirty=true;}
    else {FilesState.tabs[0].model.value='newer unsaved contents';FilesState.tabs[0].model.version++;}
    _updateSaveBar();
  },action);
  release();await expect.poll(()=>page.evaluate(()=>FilesState.tabs[0].saving)).toBe(false);
  const state=await page.evaluate(()=>({first:FilesState.tabs[0].dirty,second:FilesState.tabs[1].dirty,active:FilesState.dirty}));
  expect(state).toEqual({first:action==='edit during save',second:true,active:true});
  await expect(page.locator('#files-save')).toBeEnabled();
 });
}

test('Analysis autosave failure offers retry and preserves an intentionally empty layout',async({page})=>{
  let fail=true;
  const mock=await installMocks(page,{overrides:{'/analysis/panels':request=>request.method()==='GET'
    ?{panels:[{id:'metric',title:'Validation loss',x_key:'step',y_keys:['val_loss']}]}
    :fail?{httpStatus:500,body:{detail:'Storage unavailable'}}:{ok:true}}});
  await page.goto('/analysis');await page.locator('.anav2-rm').click();
  await expect(page.locator('.anav2-save-status')).toContainText('Storage unavailable');
  await expect(page.locator('.anav2-grid-empty')).toBeVisible();
  fail=false;await page.locator('.anav2-save-retry').click();await expect(page.locator('.anav2-save-status')).toHaveText('Layout saved');
  const writes=mock.requests.filter(r=>r.path==='/analysis/panels'&&r.method==='PUT');
  expect(writes).toHaveLength(2);expect(writes.every(r=>Array.isArray(r.body.panels)&&r.body.panels.length===0)).toBe(true);
});

test('Analysis reset is explicit and a failed reset preserves current panels',async({page})=>{
  let fail=true,reset=false;
  const mock=await installMocks(page,{overrides:{'/analysis/panels':request=>{
    if(request.method()==='GET') return {panels:[{id:reset?'default':'custom',title:reset?'Default panel':'My custom panel',x_key:'step',y_keys:['val_loss']}]};
    if(fail)return {httpStatus:500,body:{detail:'Reset unavailable'}};
    reset=request.postDataJSON().reset===true;return {ok:true};
  }}});
  await page.goto('/analysis');await expect(page.locator('.anav2-panel-title')).toHaveText('My custom panel');
  await page.locator('.anav2-reset').click();await page.locator('.arui-dlg-ok').click();
  await expect(page.locator('.anav2-save-status')).toContainText('Reset unavailable');await expect(page.locator('.anav2-panel-title')).toHaveText('My custom panel');
  fail=false;await page.locator('.anav2-reset').click();await page.locator('.arui-dlg-ok').click();
  await expect(page.locator('.anav2-panel-title')).toHaveText('Default panel');
  expect(mock.requests.filter(r=>r.path==='/analysis/panels'&&r.method==='PUT').every(r=>r.body.reset===true&&!Object.hasOwn(r.body,'panels'))).toBe(true);
});

test('Filter blank Apply, Clear all, and date comparisons have their displayed meaning',async({page})=>{
  await installMocks(page);await page.goto('/analysis');await expect(page.locator('.anav2-table tbody tr')).toHaveCount(12);
  await page.locator('.anav2-filter-btn').click();await page.locator('#fm-apply').click();await expect(page.locator('.anav2-table tbody tr')).toHaveCount(12);
  await page.locator('.anav2-filter-btn').click();await page.locator('#fm-hide-crashed').check();await page.locator('#fm-clear').click();
  await expect(page.locator('#fm-hide-crashed')).not.toBeChecked();await page.locator('#fm-apply').click();
  await page.locator('.anav2-filter-btn').click();await page.locator('.fm-field').selectOption('started_at');await page.locator('.fm-op').selectOption('>');await page.locator('.fm-val').fill('2026-10-27T09:00:00Z');await page.locator('#fm-apply').click();
  await expect(page.locator('.anav2-table tbody tr')).toHaveCount(12);
  await page.locator('.anav2-filter-btn').click();await page.locator('.fm-val').fill('2026-10-27T11:00:00Z');await page.locator('#fm-apply').click();
  await expect(page.locator('.anav2-table tbody tr')).toHaveCount(0);
});

test('Share revocation and clipboard failures never claim success',async({page})=>{
  await page.addInitScript(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(new Error('Clipboard permission denied'))}}));
  const mock=await installMocks(page,{paper:true,overrides:{'/paper/share/token':request=>request.method()==='POST'?{ok:true,token:'fixture-token',url:'/p/fixture-token'}:{httpStatus:500,body:{detail:'Revocation unavailable'}}}});
  await page.goto('/write-paper');await page.locator('.paper-tab[data-t="share"]').click();await page.locator('#share-gen').click();
  await expect(page.locator('#share-url')).toHaveValue(/fixture-token/);
  await page.locator('#share-copy').click();await expect(page.locator('#share-status')).toContainText('Could not copy automatically');
  await page.locator('#share-revoke').click();await page.locator('.arui-dlg-ok').click();
  await expect(page.locator('#share-status')).toContainText('Revocation unavailable');await expect(page.locator('#share-url')).toHaveValue(/fixture-token/);await expect(page.locator('#share-revoke')).toBeEnabled();
  await page.goto('/authkeys');await page.locator('.ak-copy-pub').click();await expect(page.locator('.arui-dlg-bd')).toContainText('Clipboard permission denied');await expect(page.locator('.ak-copy-pub')).toHaveText('Copy public key');
  expect(mock.errors).toEqual([]);
});

test('Reviewer simulation reports missing prerequisites and permits retry',async({page})=>{
  const mock=await installMocks(page,{paper:true,overrides:{'/paper/reviewer_sim/run':{httpStatus:400,body:{detail:'Configure a council reviewer first'}}}});
  await page.goto('/write-paper');await page.locator('.paper-tab[data-t="versions"]').click();await page.locator('#ver-revsim').click();await page.locator('.arui-dlg-ok').click();
  await expect(page.locator('#ver-revsim-result')).toContainText('Configure a council reviewer first');await expect(page.locator('#ver-revsim')).toBeEnabled();expect(mock.errors).toEqual([]);
});

test('A single pinned version can diff against HEAD and recover from errors',async({page})=>{
  const overrides={'/paper/versions/v1/diff':{httpStatus:500,body:{detail:'Git diff unavailable'}}};
  await installMocks(page,{paper:true,overrides});await page.goto('/write-paper');await expect(page.locator('.paper-tabs')).toBeVisible();
  await page.evaluate(()=>openVersionDiffModal('v1',[{id:'v1',label:'Only pinned draft'}]));
  await expect(page.locator('#vd-against')).toHaveValue('');await page.locator('#vd-go').click();await expect(page.locator('#vd-out')).toContainText('Git diff unavailable');await expect(page.locator('#vd-go')).toBeEnabled();
  overrides['/paper/versions/v1/diff']={ok:true,files:[]};await page.locator('#vd-go').click();await expect(page.locator('#vd-out')).toContainText('No changes');
});

test('Scaffold and citation approval failures restore the buttons',async({page})=>{
  const state=structuredClone(paperState);state.citations.push({key:'new2026',title:'New discovered reference',authors:'A Researcher',year:2026});
  const mock=await installMocks(page,{paper:true,overrides:{'/paper/state':state,'/paper/scaffold':{ok:false,detail:'No proposal is available'},'/paper/citations/new2026/approve':{httpStatus:500,body:{detail:'Citation database busy'}}}});
  await page.goto('/write-paper');await page.locator('.paper-tab[data-t="plan"]').click();await page.locator('#plan-rescaffold').click();
  await expect(page.locator('.arui-dlg-hd')).toHaveText('Scaffold failed');await expect(page.locator('.arui-dlg-bd')).toContainText('No proposal is available');await page.locator('.arui-dlg-ok').click();await expect(page.locator('#plan-rescaffold')).toBeEnabled();
  await page.locator('.paper-tab[data-t="refs"]').click();await page.locator('.rw-approve').click();await expect(page.locator('.arui-dlg-bd')).toContainText('Citation database busy');await page.locator('.arui-dlg-ok').click();await expect(page.locator('.rw-approve')).toBeEnabled();expect(mock.errors).toEqual([]);
});

for(const status of ['reverted','already_in_research','partial']) {
 test(`Return to research handles ${status} and reports continuing experiments`,async({page})=>{
  let restored=false;
  const mock=await installMocks(page,{paper:true,overrides:{
    '/mode':()=>({mode:restored?'research':'paper'}),
    '/paper/revert':()=>{restored=true;return {ok:status!=='partial',status,mode:'research',research_restarted:status==='reverted',paused:false,paper_runs_continuing:2,
      detail:status==='partial'?'Research mode was restored, but the research agent could not restart. Check its provider settings.':'Research mode restored.'};}
  }});
  await page.goto('/write-paper');await expect(page.locator('.paper-tabs')).toBeVisible();await page.evaluate(()=>openRevertModal());
  await expect(page.locator('.modal-sub').last()).toContainText('finish naturally');
  await page.locator('#rv-reason').fill('Revise the experiment objective.');await page.locator('#rv-go').click();
  await expect(page).toHaveURL(/\/dashboard$/);await expect(page.locator('.arui-dlg-bd')).toContainText('2 existing paper experiments continue to completion');
  if(status==='partial') {await expect(page.locator('.arui-dlg-hd')).toHaveText('Research restart incomplete');await expect(page.locator('.arui-dlg-bd')).toContainText('could not restart');}
  else await expect(page.locator('.arui-dlg-hd')).toHaveText('Research mode restored');
  expect(await page.evaluate(()=>S.mode.mode)).toBe('research');expect(mock.errors).toEqual([]);
 });
}

test('Failed return to research keeps the reason and permits retry',async({page})=>{
 const mock=await installMocks(page,{paper:true,overrides:{'/paper/revert':{httpStatus:400,body:{detail:'Research workspace is missing'}}}});
 await page.goto('/write-paper');await expect(page.locator('.paper-tabs')).toBeVisible();await page.evaluate(()=>openRevertModal());
 await page.locator('#rv-reason').fill('Revise the experiment objective.');await page.locator('#rv-go').click();
 await expect(page.locator('.arui-dlg-bd')).toContainText('Research workspace is missing');await page.locator('.arui-dlg-ok').click();
 await expect(page.locator('#rv-reason')).toHaveValue('Revise the experiment objective.');await expect(page.locator('#rv-go')).toBeEnabled();
 await expect(page).toHaveURL(/\/write-paper$/);expect(await page.evaluate(()=>S.mode.mode)).toBe('paper');expect(mock.errors).toEqual([]);
});

test('pin version reports a missing commit and allows retry', async ({page})=>{
  const overrides={'/paper/versions/pin':{httpStatus:400,body:{detail:'Commit the paper sources before pinning.'}}};
  const mock=await installMocks(page,{paper:true,overrides});
  await page.goto('/write-paper');await page.locator('.paper-tab[data-t="versions"]').click();
  await page.locator('#ver-pin').click();await page.locator('.arui-dlg-ok').click();
  await expect(page.locator('.arui-dlg-bd')).toContainText('Commit the paper sources');
  await page.locator('.arui-dlg-ok').click();await expect(page.locator('#ver-pin')).toBeEnabled();
  overrides['/paper/versions/pin']={id:'new-version',label:'v0-draft',sha:'a'.repeat(40)};
  await page.locator('#ver-pin').click();await page.locator('.arui-dlg-ok').click();
  await expect.poll(()=>mock.requests.filter(r=>r.path==='/paper/versions/pin').length).toBe(2);
  await expect(page.locator('.arui-dlg-bd')).toHaveCount(0);expect(mock.errors).toEqual([]);
});

test('GPU activity reflects telemetry even when experiments are untracked',async({page})=>{
  await installMocks(page,{overrides:{'/runs':[], '/gpus':[{index:0,util_pct:99,vram_used_mb:1000},{index:1,util_pct:0,vram_used_mb:32}]}});
  await page.goto('/dashboard');
  const card=page.locator('.stat').filter({has:page.locator('.k',{hasText:'GPUs active'})});
  await expect(card.locator('.v')).toHaveText('1');
  await expect(card.locator('.d')).toHaveText('0 tracked runs');
});
