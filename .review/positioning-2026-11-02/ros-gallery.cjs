const {chromium}=require('C:/Users/ROBOT/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const report={checks:[],errors:[],httpErrors:[],startedAt:new Date().toISOString()};
const root='C:/Workspace/portfolio';let browser;
const test=async(name,fn)=>{try{await fn();report.checks.push({name,passed:true});console.log('PASS',name);}catch(e){report.checks.push({name,passed:false,error:e.stack});console.log('FAIL',name,e.message);}};
(async()=>{
 browser=await chromium.launch();
 const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 page.on('pageerror',e=>report.errors.push(e.message));
 page.on('console',m=>{if(m.type()==='error')report.errors.push(m.text());});
 page.on('response',r=>{if(r.status()>=400)report.httpErrors.push({url:r.url(),status:r.status()});});
 await page.goto('http://127.0.0.1:8010',{waitUntil:'networkidle'});
 const carousel=page.locator('.ros-carousel');
 const index=()=>carousel.getAttribute('data-slide');
 const closed=()=>page.waitForFunction(()=>!document.querySelector('dialog[open]'));
 await test('Original illustration preserved and owner screenshot unchanged',async()=>{
  const hash=file=>crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex');
  assert.equal(hash('screenshots_for_projects/ROS_FyCweYWtOT.png'),hash('assets/projects/ros-interface.png'));
  assert.equal(await carousel.locator('.ros-window').count(),1);
  assert.equal(await index(),'0');assert.equal(await carousel.locator('.ros-slide').count(),2);
  assert(await carousel.locator('.ros-window').textContent());
 });
 await test('Manual controls, arrow keys and slide accessibility',async()=>{
  await carousel.locator('[data-carousel-next]').click();assert.equal(await index(),'1');
  assert.equal(await carousel.locator('.ros-slide').first().getAttribute('aria-hidden'),'true');
  assert(await carousel.locator('.ros-slide').first().evaluate(e=>e.inert));
  await page.keyboard.press('ArrowRight');assert.equal(await index(),'0');
  await page.keyboard.press('End');assert.equal(await index(),'1');
  await page.keyboard.press('Home');assert.equal(await index(),'0');
  await carousel.locator('[data-carousel-prev]').click();assert.equal(await index(),'1');
  assert.equal(await page.locator('dialog[open]').count(),0);
 });
 await test('Details gallery displays real original and restores focus and main slide',async()=>{
  await page.locator('#project-ros').click();
  assert.equal(await page.locator('#project-quick-media h3').textContent(),'Галерея программы');
  const img=page.locator('#project-quick-media img');
  await img.scrollIntoViewIfNeeded();await img.evaluate(e=>e.decode());
  assert.equal(await img.evaluate(e=>e.naturalWidth),1920);
  const [popup]=await Promise.all([page.waitForEvent('popup'),page.locator('#project-quick-media a').click()]);
  await popup.waitForLoadState();assert(popup.url().endsWith('/ros-interface.png'));await popup.close();
  await page.keyboard.press('Escape');await closed();
  await page.waitForFunction(()=>document.activeElement.id==='project-ros');
  assert.equal(await index(),'1');
 });
 await test('Technical case contains the same program gallery',async()=>{
  await page.goto('http://127.0.0.1:8010/projects/ros.html',{waitUntil:'networkidle'});
  const img=page.locator('#case-media img');await img.scrollIntoViewIfNeeded();await img.evaluate(e=>e.decode());
  assert.equal(await img.evaluate(e=>e.naturalWidth),1920);
  assert.equal(await page.locator('#case-media h3').textContent(),'Галерея программы');
 });
 for(const width of [320,390,768,1440]){
  await test('Responsive carousel and gallery '+width,async()=>{
   await page.setViewportSize({width,height:900});await page.goto('http://127.0.0.1:8010',{waitUntil:'networkidle'});
   await carousel.locator('[data-carousel-next]').click();
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   for(const button of await carousel.locator('button:visible').all()){
    const b=await button.boundingBox();assert(b.width>=44&&b.height>=44);assert(b.x>=0&&b.x+b.width<=width);
   }
   assert.equal(await carousel.locator('img').evaluate(e=>getComputedStyle(e).objectFit),'contain');
   if(width===390||width===1440)await carousel.screenshot({path:path.join(__dirname,'ros-carousel-'+width+'.png')});
   await page.locator('#project-ros').click();
   await page.locator('#project-quick-media img').scrollIntoViewIfNeeded();
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   if(width===390)await page.screenshot({path:path.join(__dirname,'ros-gallery-mobile.png')});
   await page.keyboard.press('Escape');await closed();
  });
 }
 await page.setViewportSize({width:1440,height:1000});
 await page.emulateMedia({reducedMotion:'no-preference'});
 await page.clock.install();
 await page.goto('http://127.0.0.1:8010',{waitUntil:'networkidle'});
 await carousel.scrollIntoViewIfNeeded();
 await page.mouse.move(1,1);await page.locator('#project-ros').focus();await page.waitForTimeout(200);
 await test('Automatic rotation loops without changing card geometry',async()=>{
  const before=await carousel.boundingBox();
  await page.clock.fastForward(6500);assert.equal(await index(),'1');
  await page.clock.fastForward(6500);assert.equal(await index(),'0');
  const after=await carousel.boundingBox();assert.equal(before.width,after.width);assert.equal(before.height,after.height);
 });
 await test('Explicit pause and resume',async()=>{
  await carousel.locator('[data-carousel-pause]').click();await page.mouse.move(1,1);await page.locator('#project-ros').focus();
  const start=await index();await page.clock.fastForward(13000);assert.equal(await index(),start);
  assert.equal(await carousel.locator('[data-carousel-pause]').textContent(),'Продолжить');
  await carousel.locator('[data-carousel-pause]').click();await page.mouse.move(1,1);await page.locator('#project-ros').focus();await page.waitForTimeout(100);
  await page.clock.fastForward(6500);assert.notEqual(await index(),start);
 });
 await test('Hover and keyboard focus suspend rotation',async()=>{
  await carousel.hover();const start=await index();await page.clock.fastForward(7000);assert.equal(await index(),start);
  await page.mouse.move(1,1);await carousel.locator('[data-carousel-next]').focus();
  await page.clock.fastForward(7000);assert.equal(await index(),start);
 });
 await test('Open modal and offscreen card suspend rotation',async()=>{
  await page.locator('#project-ros').click();const start=await index();
  await page.clock.fastForward(7000);assert.equal(await index(),start);
  await page.keyboard.press('Escape');await closed();
  await page.locator('#home').scrollIntoViewIfNeeded();await page.waitForTimeout(150);
  await page.clock.fastForward(7000);assert.equal(await index(),start);
 });
 await test('Reduced motion stops automatic changes while manual navigation works',async()=>{
  await carousel.scrollIntoViewIfNeeded();await page.mouse.move(1,1);
  await page.emulateMedia({reducedMotion:'reduce'});const start=await index();
  await page.clock.fastForward(13000);assert.equal(await index(),start);
  assert(await carousel.locator('[data-carousel-pause]').isHidden());
  await carousel.locator('[data-carousel-next]').click();assert.notEqual(await index(),start);
  assert.equal(await carousel.locator('.ros-slide').last().evaluate(e=>getComputedStyle(e).transitionDuration),'0s');
 });
 await test('Axe audit of visible main carousel and ROS modal gallery',async()=>{
  await page.addScriptTag({path:'C:/Users/ROBOT/AppData/Local/npm-cache/_npx/0f94ee7615faf582/node_modules/axe-core/axe.min.js'});
  for(const modal of [false,true]){
   if(modal)await page.locator('#project-ros').click();
   const v=await page.evaluate(async()=>{const r=await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']}});return r.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)}));});
   assert.deepEqual(v,[]);
   if(modal){await page.keyboard.press('Escape');await closed();}
  }
 });
 await test('No JavaScript retains the original illustration and case navigation',async()=>{
  const c=await browser.newContext({javaScriptEnabled:false,viewport:{width:390,height:844}});
  const p=await c.newPage();await p.goto('http://127.0.0.1:8010');
  await p.locator('.ros-window').scrollIntoViewIfNeeded();assert(await p.locator('.ros-window').isVisible());
  assert(await p.locator('.ros-carousel-controls').isHidden());
  await p.locator('#project-ros').click();assert(p.url().endsWith('/projects/ros.html'));await c.close();
 });
 assert.deepEqual(report.errors,[]);assert.deepEqual(report.httpErrors,[]);
})().catch(e=>{report.fatal=e.stack;console.error(e)}).finally(async()=>{
 if(browser)await browser.close();report.finishedAt=new Date().toISOString();
 fs.writeFileSync(path.join(__dirname,'ros-gallery-results.json'),JSON.stringify(report,null,2));
 console.log(JSON.stringify({passed:report.checks.filter(c=>c.passed).length,failed:report.checks.filter(c=>!c.passed).length,errors:report.errors,fatal:report.fatal}));
 process.exitCode=report.fatal||report.checks.some(c=>!c.passed)?1:0;
});