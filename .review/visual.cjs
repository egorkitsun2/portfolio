const {chromium}=require('C:/Users/ROBOT/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright');
(async()=>{
 const browser=await chromium.launch();
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 await page.goto('http://127.0.0.1:8000',{waitUntil:'networkidle'});
 await page.screenshot({path:'.review/desktop-home.png'});
 await page.locator('#nav a[href="#works"]').click();await page.waitForTimeout(1300);
 await page.screenshot({path:'.review/desktop-works.png'});
 await page.setViewportSize({width:390,height:844});
 await page.locator('[data-project="mama"]').click();await page.waitForTimeout(250);
 await page.screenshot({path:'.review/mobile-project.png'});
 await page.keyboard.press('Escape');await page.waitForTimeout(250);
 await page.locator('.nav-cta').click();await page.waitForTimeout(250);
 await page.screenshot({path:'.review/mobile-contact.png'});
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
