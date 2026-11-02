const fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
const cli='C:/Users/ROBOT/AppData/Local/npm-cache/_npx/0f94ee7615faf582/node_modules/lighthouse/cli/index.js';
const chrome='C:/Users/ROBOT/AppData/Local/ms-playwright/chromium-1252/chrome-win64/chrome.exe';
let tasks=[['home-mobile','/'],['home-desktop','/','desktop'],['contact-mobile','/?contact=1'],...['ros','mama','archive','china','sklad','deport','nfc'].map(k=>['case-'+k,'/projects/'+k+'.html'])];
if(process.argv.includes('--home-only'))tasks=tasks.slice(0,3);
const results=[];
for(const [name,url,preset] of tasks){
 const output=path.join(root,'.review/lighthouse-'+name);
 const args=[cli,'http://127.0.0.1:8000'+url,'--chrome-flags=--headless --no-sandbox','--only-categories=accessibility,performance','--output=json','--output=html','--output-path='+output,'--quiet'];
 if(preset)args.push('--preset='+preset);
 const run=spawnSync(process.execPath,args,{env:{...process.env,CHROME_PATH:chrome},encoding:'utf8',timeout:90000});
 if(run.status!==0){results.push({name,error:run.stderr||String(run.error)});console.error('FAIL',name,run.stderr);continue;}
 const report=JSON.parse(fs.readFileSync(output+'.report.json'));
 const item={name,url,performance:report.categories.performance.score*100,accessibility:report.categories.accessibility.score*100,LCP:report.audits['largest-contentful-paint'].numericValue,CLS:report.audits['cumulative-layout-shift'].numericValue,TBT:report.audits['total-blocking-time'].numericValue,failedAccessibility:report.categories.accessibility.auditRefs.filter(x=>report.audits[x.id]?.score===0).map(x=>({id:x.id,title:report.audits[x.id].title})),warnings:report.runWarnings};
 results.push(item);fs.writeFileSync(path.join(root,process.argv.includes('--home-only')?'.review/lighthouse-home-final.json':'.review/lighthouse-summary.json'),JSON.stringify(results,null,2));console.log(name,item.performance,item.accessibility,JSON.stringify(item.failedAccessibility));
}
fs.writeFileSync(path.join(root,process.argv.includes('--home-only')?'.review/lighthouse-home-final.json':'.review/lighthouse-summary.json'),JSON.stringify(results,null,2));
process.exitCode=results.some(r=>r.error||r.accessibility<100)?1:0;
