const {test,expect}=require('@playwright/test');
const {installMocks}=require('./fixtures');
test.use({viewport:{width:1440,height:1000},isMobile:false,hasTouch:false});
test('desktop dashboard rail and solo pages retain their layout',async({page})=>{
  const mock=await installMocks(page);await page.goto('/dashboard');
  await expect(page.locator('.rail')).toBeVisible();await expect(page.locator('.fab')).toBeHidden();await expect(page.locator('.rail-mobile-head')).toBeHidden();
  const left=await page.locator('.left').boundingBox(),rail=await page.locator('.rail').boundingBox();expect(left.x+left.width).toBeLessThanOrEqual(rail.x+1);
  await page.evaluate(()=>setView('analysis'));await expect(page.locator('.anav2-panel')).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440);
  expect(mock.errors).toEqual([]);
});
test('desktop scoping keeps all three panels visible',async({page})=>{
  await installMocks(page,{scoping:true});await page.goto('/');await expect(page.locator('#scope-final')).toBeVisible();
  await expect(page.locator('.scope-side')).toBeVisible();await expect(page.locator('.scope-chatcol')).toBeVisible();await expect(page.locator('.scope-mobile-tabs')).toBeHidden();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440);
});
