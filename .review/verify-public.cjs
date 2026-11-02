const {chromium}=require('C:/Users/ROBOT/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path');
const links={mama:'https://mama-shop.pages.dev/',nfc:'https://onetap-kz.pages.dev/',deport:'https://kazakhstan-deportation-archive.pages.dev/',sklad:'https://mama-shop-reform.pages.dev/',china:'https://china-history-5c2.pages.dev/'};
(async()=>{
 const browser=await chromium.launch({headless:true});const results=[];
 for(const [key,url]of Object.entries(links)){
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  try{
   const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:45000});
   await page.waitForTimeout(1200);
   const item={key,url,status:response.status(),finalUrl:page.url(),title:await page.title(),h1:await page.locator('h1').allTextContents(),pageErrors:errors,checkedAt:new Date().toISOString()};
   results.push(item);console.log(JSON.stringify(item));
  }catch(e){results.push({key,url,error:e.message});console.log(key,e.message);}
  await context.close();
 }
 fs.writeFileSync(path.join(__dirname,'public-links.json'),JSON.stringify(results,null,2));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
