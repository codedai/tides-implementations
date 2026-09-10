/** Explicitly authorized one-off UI force test: Nov20 route29 global, no other mutation. */
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
const args = process.argv.slice(2), option = (key, fallback) => args.includes(key) ? args[args.indexOf(key)+1] : fallback;
const origin = option('--origin','http://127.0.0.1:8780'), sourceRun = option('--validated-all-mode-run',null), output = resolve(option('--output','outputs/experiment_05/acceptance/browser-authorized-force'));
if (!args.includes('--authorize-nov20-global-force-once') || !sourceRun || !/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(origin)) throw new Error('Requires explicit one-off Nov20/global force authorization, a validated all-mode source run, and localhost origin');
await mkdir(output,{recursive:true,mode:0o700});
const checks=[],errors=[],requests=[],blocked=[],events=new Map(); let browser,socket,cdp,jobId,result,failure,postCount=0;
const check=(ok,name,details)=>{checks.push({name,passed:Boolean(ok),...(details===undefined?{}:{details})});if(!ok)throw new Error(name);};
const json=async path=>{const response=await fetch(origin+path);if(!response.ok)throw new Error(`${response.status}: ${path}`);return response.json();};
class CDP {
  constructor(ws){this.socket=ws;this.id=0;this.pending=new Map();this.handlers=new Map();ws.addEventListener('message',({data})=>{const m=JSON.parse(data);if(m.id){const p=this.pending.get(m.id);this.pending.delete(m.id);if(p)m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}else for(const f of this.handlers.get(m.method)||[])Promise.resolve(f(m.params)).catch(e=>errors.push(String(e)));});}
  on(name,fn){this.handlers.set(name,[...(this.handlers.get(name)||[]),fn]);}
  send(method,params={}){const id=++this.id,p=new Promise((resolve,reject)=>this.pending.set(id,{resolve,reject}));this.socket.send(JSON.stringify({id,method,params}));return p;}
  async evaluate(expression){const r=await this.send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
}
async function waitFor(expression,timeout=120000){const begin=Date.now();while(Date.now()-begin<timeout){if(await cdp.evaluate(expression))return;await delay(200);}throw new Error('Timeout: '+expression);}
async function screenshot(name){const r=await cdp.send('Page.captureScreenshot',{format:'png',fromSurface:true});await writeFile(join(output,name),Buffer.from(r.data,'base64'),{mode:0o600});}
try {
  const source=await json(`/api/runs/${sourceRun}/summary`);
  check(source.status==='complete' && source.summary.validation_ok===true && source.run_manifest.validation_ok===true && source.summary.service_date==='2023-11-20' && source.summary.route_id==='29' && JSON.stringify(source.summary.match_modes)==='["global","early","middle","late"]','source is validated Nov20 route29 all-mode result');
  const initialRuns=(await json('/api/runs')).runs.map(row=>row.run_id).sort();
  check(!initialRuns.some(id=>id.includes('/')),'initial run inventory is well-formed');
  const profile=await mkdtemp(join(tmpdir(),'experiment05-force-chrome-'));
  browser=spawn('google-chrome',['--headless=new','--no-sandbox','--disable-dev-shm-usage','--disable-background-networking','--disable-component-update','--disable-domain-reliability','--disable-extensions','--disable-sync','--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication','--no-first-run','--no-default-browser-check','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost, EXCLUDE 127.0.0.1',`--user-data-dir=${profile}`,'about:blank'],{stdio:['ignore','ignore','pipe']});
  const endpoint=await new Promise((resolve,reject)=>{let log='';const timer=setTimeout(()=>reject(new Error(log)),15000);browser.on('error',reject);browser.stderr.on('data',chunk=>{log+=chunk;const match=log.match(/DevTools listening on (ws:\/\/[^\s]+)/);if(match){clearTimeout(timer);resolve(match[1]);}});});
  const pages=await fetch(endpoint.replace(/^ws:/,'http:').split('/devtools/')[0]+'/json/list').then(r=>r.json());socket=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});cdp=new CDP(socket);
  cdp.on('Runtime.exceptionThrown',error=>errors.push(error));
  cdp.on('Network.requestWillBeSent',({request})=>requests.push({method:request.method,url:request.url,...(request.postData?{postData:request.postData}:{})}));
  cdp.on('Fetch.requestPaused',({requestId,request})=>{
    let allowed=request.method==='GET' && (/^(about:|data:|blob:)/.test(request.url)||new URL(request.url).origin===origin);
    if(request.method==='POST' && request.url===origin+'/api/runs' && postCount===0){try{const body=JSON.parse(request.postData);allowed=body.service_date==='2023-11-20'&&body.route_id==='29'&&body.match_mode==='global'&&body.force===true&&Object.keys(body).length===4;}catch{allowed=false;}if(allowed)postCount++;}
    if(!allowed)blocked.push({method:request.method,url:request.url});return cdp.send(allowed?'Fetch.continueRequest':'Fetch.failRequest',{requestId,...(allowed?{}:{errorReason:'BlockedByClient'})});
  });
  for(const method of ['Page.enable','Runtime.enable','Network.enable'])await cdp.send(method);
  await cdp.send('Fetch.enable',{patterns:[{urlPattern:'*'}]});await cdp.send('Emulation.setDeviceMetricsOverride',{width:1500,height:1100,deviceScaleFactor:1,mobile:false});await cdp.send('Page.navigate',{url:origin});
  await waitFor("document.querySelector('#service-date')?.options.length>1");
  await cdp.evaluate("(() => {for(const [id,value]of [['service-date','2023-11-20'],['match-mode','global']]){const el=document.getElementById(id);el.value=value;el.dispatchEvent(new Event('change',{bubbles:true}));}document.querySelector('#force').checked=true;})()");
  check(await cdp.evaluate("document.querySelector('#force').checked && document.querySelector('#match-mode').value==='global' && document.querySelector('#service-date').value==='2023-11-20' && !document.querySelector('#run-button').disabled"),'exact authorized controls selected');
  await screenshot('authorized-controls-before-submit.png');
  await cdp.evaluate("document.querySelector('#run-button').click()");
  await waitFor("/^[a-f0-9]{32}/.test(document.querySelector('#job-id').textContent)");jobId=await cdp.evaluate("document.querySelector('#job-id').textContent.slice(0,32)");
  const started=Date.now();let lastProgress='';
  while(Date.now()-started<5400000){
    const job=await json(`/api/jobs/${jobId}`);for(const event of job.progress||[])events.set(JSON.stringify(event),event);
    const latest=job.progress?.at(-1), progress=JSON.stringify({job_id:jobId,state:job.state,stage:latest?.stage,event_state:latest?.state,completed:latest?.completed,total:latest?.total});
    if(progress!==lastProgress){process.stdout.write(progress+'\n');lastProgress=progress;}
    if(['complete','failed'].includes(job.state)){check(job.state==='complete','authorized web force run completed',job.error);result=job;break;}await delay(10000);
  }
  check(Boolean(result),'authorized job completed before timeout');check(result.result.cached===false,'force run does not reuse cache');
  const target=await json(`/api/runs/${result.result.run_id}/summary`);
  check(target.status==='complete'&&target.summary.validation_ok===true&&target.run_manifest.validation_ok===true,'new force output passes pipeline validation');
  check(target.summary.service_date==='2023-11-20'&&target.summary.route_id==='29'&&JSON.stringify(target.summary.match_modes)==='["global"]','new force output contains global only');
  check(target.run_manifest.pipeline_version===source.run_manifest.pipeline_version && target.run_manifest.config_hash===source.run_manifest.config_hash && JSON.stringify(target.run_manifest.input_hashes)===JSON.stringify(source.run_manifest.input_hashes),'force and all-mode use same code/config/raw inputs');
  check(Object.keys(target.run_manifest.stages).length===10&&Object.values(target.run_manifest.stages).every(stage=>stage.status==='complete'),'all ten raw-to-export stages actually completed');
  check(target.run_id!==sourceRun&&!initialRuns.includes(target.run_id)&&target.run_id.startsWith('run-2023-11-20-'),'force created a new run without overwriting source');
  const newRuns=(await json('/api/runs')).runs.map(row=>row.run_id).filter(id=>!initialRuns.includes(id));check(JSON.stringify(newRuns)==JSON.stringify([target.run_id]),'exactly one new run created by this acceptance');
  await waitFor(`document.querySelector('#result-run-id').textContent===${JSON.stringify(target.run_id)}&&document.querySelectorAll('.mode-card').length===1`);
  check(await cdp.evaluate("document.querySelector('.mode-card').dataset.mode==='global'"),'actual UI renders one global result card');
  await cdp.evaluate("document.querySelector('#result-panel').scrollIntoView({block:'start'})");await screenshot('authorized-force-global-results.png');
  result={job_id:jobId,run_id:target.run_id,source_all_mode_run:sourceRun,cached:false,run_manifest:target.run_manifest,summary:target.summary};
  check(postCount===1&&requests.filter(request=>request.method==='POST').length===1,'exactly one bounded force POST');check(errors.length===0&&blocked.length===0,'no JavaScript errors or unauthorized requests',{errors,blocked});
}catch(error){failure=String(error.stack||error);}
finally{
  if(cdp){try{await cdp.send('Browser.close');}catch{}socket.close();}if(browser)browser.kill('SIGTERM');
  await writeFile(join(output,'browser-authorized-force.json'),JSON.stringify({schema:'experiment05-one-off-force-acceptance-v1',status:failure?'FAIL':'PASS',authorization:{service_date:'2023-11-20',route_id:'29',match_mode:'global',force:true,maximum_post_count:1},checks,errors,blocked,requests,progress:[...events.values()],result,failure:failure||null,created_at:new Date().toISOString()},null,2)+'\n',{mode:0o600});
  process.stdout.write(JSON.stringify({status:failure?'FAIL':'PASS',checks:checks.length,job_id:jobId,run_id:result?.run_id,output})+'\n');
}
if(failure){process.stderr.write(failure+'\n');process.exitCode=1;}
