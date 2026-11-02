const {chromium}=require('C:/Users/ROBOT/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root='C:/Workspace/portfolio';
const report={startedAt:new Date().toISOString(),browser:'',checks:[],consoleErrors:[],pageErrors:[],failedRequests:[],liveUrls:[]};
const base=process.env.PORTFOLIO_BASE_URL || 'http://127.0.0.1:8000';
const keys=['ros','mama','archive','china','sklad','deport','nfc'];
let browser;
const check=(name,fn)=>async()=>{try{await fn();report.checks.push({name,passed:true});console.log('PASS',name);}catch(e){report.checks.push({name,passed:false,error:e.message});console.log('FAIL',name,e.message);}};
const tick=()=>new Promise(r=>setTimeout(r,150));
async function closed(page){await page.waitForFunction(()=>!document.querySelector('dialog[open]'));}
async function noOverflow(page){
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Horizontal overflow at '+JSON.stringify(page.viewportSize()));
}
async function focusInside(page,id){
 for(let i=0;i<12;i++){await page.keyboard.press(i<6?'Tab':'Shift+Tab');assert(await page.evaluate(id=>document.getElementById(id).contains(document.activeElement),id),'Focus escaped '+id);}
}
(async()=>{
 browser=await chromium.launch({headless:true});report.browser=browser.version();
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 const page=await context.newPage();
 const monitor=p=>{
  p.on('pageerror',e=>report.pageErrors.push({url:p.url(),message:e.message}));
  p.on('console',m=>{if(m.type()==='error')report.consoleErrors.push({url:p.url(),message:m.text()})});
  p.on('response',r=>{if(r.status()>=400)report.failedRequests.push({url:r.url(),status:r.status()})});
 };
 monitor(page);
 await page.goto(base,{waitUntil:'networkidle'});
 await check('Navigation, anchors and fixed header',async()=>{
  for(const id of ['services','works','about','home']){
   await page.locator('#nav a[href="#'+id+'"]').click();
   await page.waitForFunction(id=>location.hash==='#'+id,id);
   const y=await page.locator('#'+id).evaluate(e=>e.getBoundingClientRect().top);
   assert(y>=70&&y<=120,'Section hidden by header: '+id+' '+y);
  }
 })();
 await check('Contact modal, Escape, focus trap, focus and scroll restoration',async()=>{
  const trigger=page.locator('.nav-cta');await trigger.focus();
  const y=await page.evaluate(()=>scrollY);await trigger.click();
  await page.locator('#contact-dialog').waitFor({state:'visible'});
  await focusInside(page,'contact-dialog');
  assert(await page.locator('body').evaluate(e=>e.classList.contains('modal-open')));
  await page.keyboard.press('Escape');await closed(page);
  await page.waitForFunction(()=>document.activeElement.matches('.nav-cta'));
  assert(Math.abs(await page.evaluate(()=>scrollY)-y)<3);
  assert.equal(await page.locator('#contact-dialog a').count(),5,'All owner-provided contacts must be active');
 })();
 await check('Owner contacts use exact verified URLs and a confirmed telephone number',async()=>{
  await page.locator('.nav-cta').click();
  const expected=['https://wa.me/77779409625','https://t.me/GLhost27','https://www.instagram.com/egorkitsun2/','tel:+77013061220','https://github.com/egorkitsun2'];
  assert.deepEqual(await page.locator('#contact-dialog a').evaluateAll(links=>links.map(a=>a.getAttribute('href'))),expected);
  for(const url of expected){
   const link=page.locator('#contact-dialog a[href="'+url+'"]');
   if(url.startsWith('tel:'))assert.equal(await link.getAttribute('target'),null);
   else {assert.equal(await link.getAttribute('target'),'_blank');assert((await link.getAttribute('rel')).includes('noopener'));}
  }
  assert((await page.locator('#contact-dialog a[href^="tel:"]').textContent()).includes('+7 (701) 306-12-20'));
  await page.keyboard.press('Escape');await closed(page);
 })();
 await check('WhatsApp Telegram and Instagram open their real destinations in new tabs',async()=>{
  const verified=JSON.parse(fs.readFileSync(path.join(__dirname,'contact-link-checks.json')));
  for(const item of verified){
   assert.equal(item.status,200);
   await page.locator('.nav-cta').click();
   const [popup]=await Promise.all([page.waitForEvent('popup'),page.locator('#contact-dialog a[href="'+item.url+'"]').click()]);
   await popup.waitForLoadState('domcontentloaded');
   assert.equal(new URL(popup.url()).origin,new URL(item.finalUrl).origin);
   await popup.close();await page.keyboard.press('Escape');await closed(page);
  }
 })();
 await check('Contact backdrop, inner padding and close button',async()=>{
  await page.locator('.nav-cta').click();
  const box=await page.locator('#contact-dialog').boundingBox();
  await page.mouse.click(box.x+2,box.y+2);
  assert(await page.locator('#contact-dialog').isVisible(),'Inner padding closed modal');
  await page.mouse.click(3,3);await closed(page);
  await page.locator('.nav-cta').click();await page.locator('.contact-close').click();await closed(page);
 })();
 await check('Every project preview and all close paths',async()=>{
  for(const key of keys){
   const trigger=page.locator('[data-project="'+key+'"]');
   await trigger.scrollIntoViewIfNeeded();await trigger.focus();
   const y=await page.evaluate(()=>scrollY);
   await trigger.click();assert(await page.locator('#project-quick-title').textContent());
   assert(await page.locator('#project-quick-note').textContent());
   assert.equal(await page.locator('#project-quick-detail').getAttribute('href'),'projects/'+key+'.html');
   await focusInside(page,'project-quick-dialog');
   if(key==='ros')await page.locator('.project-quick-close').click();
   else if(key==='mama')await page.locator('.project-quick-done').click();
   else if(key==='archive')await page.mouse.click(2,2);
   else await page.keyboard.press('Escape');
   await closed(page);assert(await trigger.evaluate(e=>e===document.activeElement));
   assert(Math.abs(await page.evaluate(()=>scrollY)-y)<3,'Lost scroll: '+key);
  }
 })();
 await check('Browser Back and Forward with project and contact modals',async()=>{
  await page.locator('[data-project="china"]').click();
  await page.goBack();await closed(page);
  await page.goForward();await page.locator('#project-quick-dialog').waitFor({state:'visible'});
  assert((await page.locator('#project-quick-title').textContent()).includes('China'));
  await page.keyboard.press('Escape');await closed(page);
  await page.locator('.nav-cta').click();await page.goBack();await closed(page);
  await page.goForward();await page.locator('#contact-dialog').waitFor({state:'visible'});
  await page.keyboard.press('Escape');await closed(page);
 })();
 await check('Detail case return restores previous preview and scroll',async()=>{
  await page.locator('[data-project="china"]').click();
  const before=await page.evaluate(()=>scrollY);
  await page.locator('#project-quick-detail').click();
  await page.waitForURL('**/projects/china.html');
  await page.locator('[data-return-project]').click();
  await page.waitForURL(url=>!url.pathname.includes('/projects/'));
  await page.locator('#project-quick-dialog').waitFor({state:'visible',timeout:3000});
  assert((await page.locator('#project-quick-title').textContent()).includes('China'));
  await page.keyboard.press('Escape');await closed(page);
  assert(Math.abs(await page.evaluate(()=>scrollY)-before)<4,'Lost prior scroll on return');
 })();
 // Reset independently so a failed modal-return test cannot contaminate subsequent cases.
 await page.goto(base,{waitUntil:'networkidle'});
 await check('Screenshot popup and local image integrity',async()=>{
  await page.locator('[data-project="china"]').click();
  const pending=page.waitForEvent('popup');
  await page.locator('#project-quick-media a').click();
  const popup=await pending;await popup.waitForLoadState();
  assert(popup.url().endsWith('china-owner.png'));await popup.close();
  const valid=await page.locator('#project-quick-media img').evaluate(e=>e.complete&&e.naturalWidth===1857);
  assert(valid);await page.keyboard.press('Escape');await closed(page);
 })();
 await check('Contact and project state survive reload',async()=>{
  await page.locator('.nav-cta').click();await page.reload();
  await page.locator('#contact-dialog').waitFor({state:'visible'});
  await page.keyboard.press('Escape');await closed(page);
  await page.locator('[data-project="ros"]').click();await page.reload();
  await page.locator('#project-quick-dialog').waitFor({state:'visible'});
  await page.keyboard.press('Escape');await closed(page);
 })();
 await check('Verified external GitHub link opens a real new tab',async()=>{
  await page.locator('.nav-cta').click();
  const [popup]=await Promise.all([page.waitForEvent('popup'),page.locator('#contact-dialog a[href="https://github.com/egorkitsun2"]').click()]);
  await popup.waitForLoadState('domcontentloaded');
  assert.equal(popup.url(),'https://github.com/egorkitsun2');
  const status=(await popup.request.get(popup.url())).status();assert.equal(status,200);
  await popup.close();await page.keyboard.press('Escape');await closed(page);
 })();
 await check('Five confirmed published URLs and no private repository links',async()=>{
  report.liveUrls=await page.locator('a.project-live').evaluateAll(links=>links.map(a=>a.href));
  assert.equal(report.liveUrls.length,5);
  assert.equal(await page.locator('a[href*="github.com/egorkitsun2/"]').count(),0);
  assert.equal(await page.locator('[data-live-project] .live-status').count(),0);
 })();
 await check('All five site buttons open verified projects in new tabs',async()=>{
  const verified=JSON.parse(fs.readFileSync(path.join(__dirname,'public-links.json')));
  for(const item of verified){
   await page.locator('[data-project="'+item.key+'"]').click();
   const link=page.locator('#project-quick-live a');
   assert.equal(await link.getAttribute('href'),item.finalUrl);
   assert.equal(await link.getAttribute('target'),'_blank');
   assert((await link.getAttribute('rel')).includes('noopener'));
   const [popup]=await Promise.all([page.waitForEvent('popup'),link.click()]);
   await popup.waitForLoadState('domcontentloaded');
   assert.equal(new URL(popup.url()).origin,new URL(item.finalUrl).origin);
   assert.equal(await popup.title(),item.title);
   await popup.close();await page.keyboard.press('Escape');await closed(page);
  }
 })();
 await check('Modified clicks preserve native link behavior',async()=>{
  const event=await page.locator('[data-project="ros"]').evaluate(el=>{
   const e=new MouseEvent('click',{bubbles:true,cancelable:true,ctrlKey:true,button:0});
   el.dispatchEvent(e);return {prevented:e.defaultPrevented,open:!!document.querySelector('dialog[open]')};
  });assert.equal(event.prevented,false);assert.equal(event.open,false);
 })();
 for(const width of [320,375,390,768,1024,1440,1920]){
  await check('Responsive layout and dialogs at '+width,async()=>{
   await page.setViewportSize({width,height:900});await page.goto(base,{waitUntil:'networkidle'});await noOverflow(page);
   for(const key of keys){
    await page.locator('[data-project="'+key+'"]').click();
    await noOverflow(page);
    const box=await page.locator('#project-quick-dialog').boundingBox();
    assert(box.x>=0&&box.x+box.width<=width+1,'Modal exceeds viewport');
    await focusInside(page,'project-quick-dialog');
    await page.keyboard.press('Escape');await closed(page);
   }
   await page.locator('.nav-cta').click();await noOverflow(page);
   await page.keyboard.press('Escape');await closed(page);
   if(width===390||width===768||width===1440){
    await page.goto(base,{waitUntil:'networkidle'});
    await page.screenshot({path:path.join(__dirname,'portfolio-'+width+'.png'),fullPage:true});
   }
  })();
 }
 await page.setViewportSize({width:390,height:844});await page.goto(base);
 await check('Mobile menu by keyboard, Escape, outside click, navigation',async()=>{
  await page.locator('#menu').focus();await page.keyboard.press('Enter');
  assert.equal(await page.locator('#menu').getAttribute('aria-expanded'),'true');
  await page.keyboard.press('Tab');assert(await page.locator('#nav').evaluate(e=>e.contains(document.activeElement)));
  await page.keyboard.press('Escape');assert.equal(await page.locator('#menu').getAttribute('aria-expanded'),'false');
  assert(await page.locator('#menu').evaluate(e=>e===document.activeElement));
  await page.locator('#menu').click();await page.locator('#nav a[href="#works"]').click();
  assert.equal(await page.locator('#menu').getAttribute('aria-expanded'),'false');
  await page.locator('#menu').click();await page.mouse.click(5,500);
  assert.equal(await page.locator('#menu').getAttribute('aria-expanded'),'false');
 })();
 await check('Reduced motion including runtime preference change',async()=>{
  assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).scrollBehavior),'auto');
  const animations=await page.evaluate(()=>document.getAnimations().filter(a=>a.playState==='running').length);assert.equal(animations,0);
  await page.emulateMedia({reducedMotion:'no-preference'});await page.reload();
  assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).scrollBehavior),'smooth');
  await page.locator('[data-project="ros"]').click();await page.keyboard.press('Escape');await closed(page);
  await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await page.evaluate(()=>document.getAnimations().filter(a=>a.playState==='running').length),0);
 })();
 for(const key of keys){
  await check('Case '+key+' desktop/tablet/phone, contacts and reduced motion',async()=>{
   for(const width of [320,768,1440]){
    await page.setViewportSize({width,height:900});
    await page.goto(base+'/projects/'+key+'.html',{waitUntil:'networkidle'});await noOverflow(page);
    assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).scrollBehavior),'auto');
    assert.equal(await page.locator('h1').count(),1);
    assert.equal(await page.locator('a[href*="github.com/egorkitsun2/"]').count(),0);
    await page.locator('header [data-contact]').click();await focusInside(page,'contact-dialog');
    await page.keyboard.press('Escape');await closed(page);
    for(const image of await page.locator('#case-media img').all()){await image.scrollIntoViewIfNeeded();await image.evaluate(e=>e.decode());}
    const images=await page.locator('#case-media img').evaluateAll(imgs=>imgs.every(e=>e.complete&&e.naturalWidth>0));
    assert(images,'Broken case screenshot');
   }
  })();
 }
 await check('Axe WCAG 2.2 AA checks of every open modal',async()=>{
  await page.setViewportSize({width:390,height:844});await page.goto(base);
  await page.addScriptTag({path:'C:/Users/ROBOT/AppData/Local/npm-cache/_npx/0f94ee7615faf582/node_modules/axe-core/axe.min.js'});
  for(const key of ['contact',...keys]){
   await page.locator(key==='contact'?'.nav-cta':'[data-project="'+key+'"]').click();
   const results=await page.evaluate(async()=>await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']}}));
   assert.deepEqual(results.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)})),[],'Accessibility violations in '+key);
   await page.keyboard.press('Escape');await closed(page);
  }
 })();
 await check('Progressive enhancement without JavaScript',async()=>{
  const nojs=await browser.newContext({javaScriptEnabled:false,viewport:{width:390,height:844}});
  const p=await nojs.newPage();await p.goto(base);
  assert(await p.locator('#nav').isVisible());assert(await p.locator('[data-project="ros"]').isVisible());
  await p.locator('[data-project="ros"]').click();assert(p.url().endsWith('/projects/ros.html'));
  await nojs.close();
 })();
 await context.close();
 report.finishedAt=new Date().toISOString();
 assert.equal(report.consoleErrors.length,0,'Console errors');
 assert.equal(report.pageErrors.length,0,'Page errors');
 assert.equal(report.failedRequests.length,0,'HTTP errors');
})().catch(e=>{report.fatal=e.stack;console.error(e)}).finally(async()=>{
 if(browser)await browser.close();
 fs.writeFileSync(path.join(__dirname,'playwright-results.json'),JSON.stringify(report,null,2));
 const failed=report.checks.filter(x=>!x.passed);
 console.log(JSON.stringify({passed:report.checks.length-failed.length,failed:failed.length,errors:report.pageErrors,fatal:report.fatal}));
 process.exitCode=failed.length||report.fatal?1:0;
});
