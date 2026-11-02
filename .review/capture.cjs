const {chromium} = require('C:/Users/ROBOT/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
(async()=>{
 const root=path.resolve(__dirname,'..');
 fs.mkdirSync(path.join(root,'assets/projects'),{recursive:true});
 const browser=await chromium.launch({headless:true});
 const captures=[];
 for(const key of ['china','nfc','deport']){
  const context=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1,reducedMotion:'reduce'});
  const page=await context.newPage();
  const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8000/capture/'+key+'/index.html',{waitUntil:'networkidle',timeout:60000});
  await page.evaluate(()=>document.fonts.ready);
  await page.waitForTimeout(1500);
  const file='assets/projects/'+key+'-home.jpg';
  await page.screenshot({path:path.join(root,file),type:'jpeg',quality:90});
  captures.push({project:key,file,source:'Local working interface, original repository served read-only',sourceDirectory:({china:'C:/Workspace/China-history',nfc:'C:/Workspace/NFC',deport:'C:/Workspace/deportation-suite-kazax'})[key],viewport:{width:1440,height:1000},capturedAt:new Date().toISOString(),sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex'),title:await page.title(),pageErrors:errors});
  console.log(key,await page.title(),errors);
  await context.close();
 }
 fs.writeFileSync(path.join(root,'.review/captures.json'),JSON.stringify(captures,null,2));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
