const {chromium}=require('C:/Users/ROBOT/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright');
const fs=require('node:fs');
(async()=>{
 const b=await chromium.launch();const results=[];
 for(const [key,url] of Object.entries({whatsapp:'https://wa.me/77779409625',telegram:'https://t.me/GLhost27',instagram:'https://www.instagram.com/egorkitsun2/'})){
  const c=await b.newContext();const p=await c.newPage();
  try {const r=await p.goto(url,{waitUntil:'domcontentloaded',timeout:30000});results.push({key,url,status:r?.status(),finalUrl:p.url(),title:await p.title(),checkedAt:new Date().toISOString()});}
  catch(e){results.push({key,url,error:e.message,finalUrl:p.url(),checkedAt:new Date().toISOString()});}
  await c.close();
 }
 fs.writeFileSync('.review/contact-link-checks.json',JSON.stringify(results,null,2));console.log(JSON.stringify(results));await b.close();
})().catch(e=>{console.error(e);process.exit(1)});