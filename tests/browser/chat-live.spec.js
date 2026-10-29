const { test, expect } = require('@playwright/test');
const { installMocks } = require('./fixtures');
test.use({ viewport: { width: 390, height: 844 } });

function streamFixture() {
  let offset = 0, sequence = 0;
  return {
    get sequence() { return sequence; },
    overrides: {
      '/agent/alive': { alive: true },
      '/agent/raw': request => {
        const seed = new URL(request.url()).searchParams.has('seed');
        const text = seed ? Array.from({length:100}, (_,i) => 'history ' + i).join('\r\n') + '\r\n'
          : 'live update ' + (++sequence) + '\r\n';
        offset += Buffer.byteLength(text);
        return { chunk: Buffer.from(text).toString('base64'), offset, size:offset, seeded:seed, alive:true };
      },
    },
  };
}
async function instrumentTerminal(page) {
  // Exercise the real bundled xterm renderer; retain a handle to inspect its
  // displayed text/scroll position rather than merely counting HTTP requests.
  await page.evaluate(() => {
    window.WebglAddon = undefined;
    const Original = window.Terminal;
    window.Terminal = class extends Original {
      constructor(...args) { super(...args); window.testTerminal = this; }
    };
  });
}
async function openLive(page) {
  await page.getByRole('button', {name:'Open agent conversation', exact:true}).click();
  await page.locator('.rail-tab[data-tab="live"]').click();
  await expect.poll(() => terminalState(page)).toMatchObject({following:true});
}
async function terminalState(page) {
  return page.evaluate(() => {
    const t = window.testTerminal;
    if (!t) return {};
    const b = t.buffer.active;
    return { following: b.viewportY === b.baseY, rows:t.rows,
      last: b.getLine(b.baseY + b.cursorY - 1)?.translateToString().trim() };
  });
}

test('iPhone keyboard resize keeps actual terminal frames advancing', async ({page}) => {
  const stream = streamFixture(); const mock = await installMocks(page, {overrides:stream.overrides});
  await page.goto('/dashboard'); await instrumentTerminal(page); await openLive(page);
  await expect.poll(async () => (await terminalState(page)).last).toMatch(/live update/);
  const before = (await terminalState(page)).last;
  await page.locator('#chatin').focus(); await page.setViewportSize({width:390,height:360});
  await expect.poll(async () => (await terminalState(page)).last).not.toBe(before);
  await expect.poll(async () => (await terminalState(page)).following).toBe(true);
  const shrunk = (await terminalState(page)).last;
  await page.setViewportSize({width:390,height:844});
  await expect.poll(async () => (await terminalState(page)).last).not.toBe(shrunk);
  expect((await terminalState(page)).following).toBe(true);
  expect(mock.errors).toEqual([]);
});

test('reading history is preserved, then Send resumes live frames without terminal Enter', async ({page}, info) => {
  const stream = streamFixture(); const message = {id:'cm-confirmed', role:'researcher', content:'Check the new results', created_at:new Date().toISOString()};
  const mock = await installMocks(page, {overrides:{...stream.overrides, '/agent/send':{ok:true,message}}});
  await page.goto('/dashboard'); await instrumentTerminal(page); await openLive(page);
  await expect.poll(async () => (await terminalState(page)).last).toMatch(/live update/);
  await page.evaluate(() => window.testTerminal.scrollLines(-20));
  const reading = (await terminalState(page)).last, sequence = stream.sequence;
  await page.setViewportSize({width:390,height:360});
  await expect.poll(() => stream.sequence).toBeGreaterThan(sequence + 2);
  expect((await terminalState(page)).following).toBe(false);
  expect((await terminalState(page)).last).toBe(reading);
  await page.locator('#chatin').fill(message.content);
  await page.locator('.composer .row button').click();
  await expect(page.locator('#chat-send-status')).toHaveText('Message accepted by the agent.');
  await expect.poll(async () => (await terminalState(page)).last).not.toBe(reading);
  expect((await terminalState(page)).following).toBe(true);
  expect(mock.requests.filter(r => r.path === '/agent/send')).toHaveLength(1);
  expect(mock.requests.filter(r => r.path === '/agent/keys')).toHaveLength(0);
  expect(await page.evaluate(() => S.chat.filter(m => m.id === 'cm-confirmed').length)).toBe(1);
  expect(mock.errors).toEqual([]);
  if (info.project.name === 'webkit') {
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'/tmp/arui-mobile-audit/screenshots/390-agent-send.png'});
  }
});

test('paper composer sends to author from the activity tab and opens its live reply', async ({page}) => {
  const stream = streamFixture(); const mock = await installMocks(page,{paper:true,overrides:{...stream.overrides,'/paper/author/send':{ok:true,delivery:'queued'}}});
  await page.goto('/write-paper'); await instrumentTerminal(page);
  await page.getByRole('button',{name:'Open agent conversation',exact:true}).click();
  await page.locator('#chatin').fill('Revise the introduction'); await page.locator('.composer .row button').click();
  await expect(page.locator('#chat-send-status')).toHaveText('Queued for the agent.');
  await expect(page.locator('#authorterm-host')).toBeVisible();
  expect(mock.requests.filter(r=>r.path==='/paper/author/send')).toHaveLength(1);
  expect(mock.requests.filter(r=>r.path==='/agent/send')).toHaveLength(0);
  await expect.poll(async () => (await terminalState(page)).last).toMatch(/live update/);
});

test('a hung terminal request times out and live frames recover without reload', async ({page}) => {
  const stream = streamFixture(); await installMocks(page,{overrides:stream.overrides});
  let held = false;
  await page.route('**/api/agent/raw?**', async route => {
    if (!held && !new URL(route.request().url()).searchParams.has('seed')) { held = true; return; }
    await route.fallback();
  });
  await page.goto('/dashboard'); await instrumentTerminal(page); await openLive(page);
  await expect.poll(() => held).toBe(true);
  await expect.poll(async () => (await terminalState(page)).last, {timeout:16000}).toMatch(/live update/);
  const last = (await terminalState(page)).last;
  await expect.poll(async () => (await terminalState(page)).last).not.toBe(last);
});

test('leaving a terminal aborts its pending fetch and cannot restart a hidden polling loop', async ({page}) => {
  const stream = streamFixture(); const mock = await installMocks(page,{overrides:stream.overrides});
  let held = null, count = 0;
  await page.route('**/api/agent/raw?**', async route => {
    count++;
    if (!new URL(route.request().url()).searchParams.has('seed')) { held = route; return; }
    await route.fallback();
  });
  await page.goto('/dashboard'); await instrumentTerminal(page); await openLive(page);
  await expect.poll(() => !!held).toBe(true);
  await page.locator('.rail-tab[data-tab="feed"]').click();
  const stoppedCount = count;
  await held.fulfill({contentType:'application/json',body:JSON.stringify({chunk:Buffer.from('late frame\r\n').toString('base64'),offset:1000,size:1000})}).catch(()=>{});
  await page.waitForTimeout(1000);
  expect(count).toBe(stoppedCount); expect(mock.errors).toEqual([]);
});

test('plain terminal fallback keeps updating when xterm is unavailable', async ({page}) => {
  let frame = 0;
  await installMocks(page,{overrides:{'/agent/alive':{alive:true},'/agent/raw':()=>({seeded:true,alive:true,chunk:Buffer.from('fallback update '+(++frame)).toString('base64')})}});
  await page.goto('/dashboard'); await page.evaluate(()=>{window.Terminal=undefined;});
  await page.getByRole('button',{name:'Open agent conversation',exact:true}).click();
  await page.locator('.rail-tab[data-tab="live"]').click();
  const pre=page.locator('#term-host pre'); await expect(pre).toContainText('fallback update 1');
  await expect(pre).toContainText('fallback update 2');
});

test('missed chat events recover on phone resume and server echoes do not duplicate messages', async ({page}) => {
  const messages=[]; const mock=await installMocks(page,{overrides:{'/chat':()=>messages}});
  await page.addInitScript(()=>{
    window.testStreams=[];
    window.EventSource=class {
      constructor(url){this.url=url;this.listeners={};window.testStreams.push(this);}
      addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);}
      close(){}
      emit(type,data){for(const fn of this.listeners[type]||[])fn({data:JSON.stringify(data)});}
    };
  });
  await page.goto('/dashboard');await page.getByRole('button',{name:'Open agent conversation',exact:true}).click();
  const message={id:'cm-offline',role:'researcher',content:'Message sent from another device',created_at:new Date().toISOString()};
  messages.push(message);
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await expect(page.locator('#feed')).toContainText(message.content);
  await page.evaluate(m=>window.testStreams.find(s=>s.url.endsWith('/chat')).emit('chat',m),message);
  expect(await page.evaluate(()=>S.chat.filter(m=>m.id==='cm-offline').length)).toBe(1);
  expect(await page.locator('#feed').getByText(message.content,{exact:true}).count()).toBe(1);
  expect(mock.errors).toEqual([]);
});

test('recovered messages stay chronological after newer live events and preserve the reading position', async ({page}) => {
  await installMocks(page); await page.goto('/dashboard');
  await page.getByRole('button',{name:'Open agent conversation',exact:true}).click();
  const result = await page.evaluate(() => {
    const feed=document.getElementById('feed');
    feed.scrollTop=200;
    const anchor=[...feed.children].find(row=>row.getBoundingClientRect().bottom>feed.getBoundingClientRect().top);
    const before=anchor.getBoundingClientRect().top;
    acceptChatMessage({id:'newer',role:'researcher',content:'Newer live message',created_at:'2026-10-29T20:05:00Z'});
    acceptChatMessage({id:'older',role:'researcher',content:'Recovered older message',created_at:'2026-10-29T20:04:00Z'});
    return {order:S.chat.map(m=>m.id),rows:[...feed.querySelectorAll('.bub')].map(e=>e.textContent),moved:Math.abs(anchor.getBoundingClientRect().top-before)};
  });
  expect(result.order).toEqual(['older','newer']);
  expect(result.rows).toEqual(['Recovered older message','Newer live message']);
  expect(result.moved).toBeLessThan(2);
});
