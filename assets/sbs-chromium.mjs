// Fixed local browser launcher. No evaluation of caller-supplied JavaScript or shell code.
import path from 'node:path';
import {createRequire} from 'node:module';
import {createInterface} from 'node:readline';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {openBudget, normalizedUrl} from './sbs-url-budget.mjs';
const assetDir = path.dirname(fileURLToPath(import.meta.url));
function loadRuntime() {
  const config = JSON.parse(fs.readFileSync(path.join(assetDir, 'runtime-config.json'), 'utf8'));
  if (typeof config.runtime !== 'string' || typeof config.browsers_path !== 'string') throw new Error('runtime-config.json requires runtime and browsers_path');
  const runtime = path.resolve(assetDir, config.runtime);
  // Playwright reads this variable when its module loads.
  process.env.PLAYWRIGHT_BROWSERS_PATH = path.resolve(assetDir, config.browsers_path);
  const require = createRequire(path.join(runtime, 'package.json'));
  return {require, ...require('playwright')};
}
const args = process.argv.slice(2);
let mode, target, request, assignedUrl, headed=false, android=false;
for(let i=0;i<args.length;i++) {
  const a=args[i];
  if(a==='--target64') assignedUrl=decodeUrl(args[++i]);
  else if(a==='--headed') headed=true;
  else if(a==='--android') android=true;
  else if(['--check','--stdio','--server','--send','--send64','--url','--get64'].includes(a) && !mode) {mode=a;if(a==='--url'||a==='--get64'||a==='--send'||a==='--send64') target=args[++i];if(a==='--send'||a==='--send64')request=args[++i];}
  else throw new Error('Usage: sbs-chromium.mjs --check|--server|--stdio|--url URL|--send SESSION JSON|--send64 SESSION BASE64URL [--headed] [--android]');
}
if(!mode) throw new Error('Mode required: --check, --url URL or --stdio');
function publicUrl(s) {
  const u=new URL(s);
  if(!['http:','https:'].includes(u.protocol)||u.username||u.password) throw new Error('Only HTTP(S) URLs without credentials are accepted');
  return u.href;
}
function decodeUrl(encoded) {
  if(typeof encoded!=='string'||!/^[-_A-Za-z0-9]+$/.test(encoded)||encoded.length>32768)throw new Error('Invalid assigned URL encoding');
  const bytes=Buffer.from(encoded,'base64url');
  if(bytes.toString('base64url')!==encoded)throw new Error('Noncanonical assigned URL encoding');
  return publicUrl(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
}
if(mode==='--url') {publicUrl(target);assignedUrl ||= target;}
if(mode==='--get64') assignedUrl=decodeUrl(target);
if(process.env.SBS_JOB_HOME&&['--server','--stdio'].includes(mode)&&!assignedUrl)throw new Error('SBS URL session requires --target64; defaults are forbidden');
const tmpRoot=fs.realpathSync(os.tmpdir());
if(mode==='--send'||mode==='--send64') {
  if(mode==='--send64') {
    if(typeof request!=='string'||request.length>87380||!/^[-_A-Za-z0-9]+$/.test(request)) throw new Error('Invalid base64url request');
    const bytes=Buffer.from(request,'base64url');
    if(bytes.length>65536||bytes.toString('base64url')!==request) throw new Error('Invalid base64url request');
    request=new TextDecoder('utf-8',{fatal:true}).decode(bytes);
  }
  if(!/^sbs-chromium-[a-zA-Z0-9]{6}$/.test(target||''))throw new Error('Invalid browser session');
  const dir=path.join(tmpRoot,target), st=fs.lstatSync(dir);
  if(!st.isDirectory()||st.isSymbolicLink()||st.uid!==process.getuid()||(st.mode&0o077)!==0)throw new Error('Session ownership mismatch');
  const sock=path.join(dir,'control.sock');
  const sockStat=fs.lstatSync(sock);
  if(!sockStat.isSocket()||sockStat.uid!==process.getuid()||(sockStat.mode&0o077)!==0)throw new Error('Browser session socket unavailable');
  if(process.env.SBS_JOB_HOME) {
    const binding=JSON.parse(fs.readFileSync(path.join(dir,'job.json'),'utf8'));
    if(binding.job_home!==process.env.SBS_JOB_HOME)throw new Error('Browser session belongs to another SBS task');
  }
  const parsed=JSON.parse(request);
  await new Promise((resolve,reject)=>{
    const client=net.createConnection(sock);let result='';
    client.setTimeout(60000,()=>client.destroy(new Error('Browser command timed out')));
    client.on('connect',()=>client.write(JSON.stringify(parsed)+'\n'));
    client.on('data',b=>{result+=b.toString();if(result.length>300000)client.destroy(new Error('Oversized browser reply'));});
    client.on('error',reject);
    client.on('end',()=>{
      try {
        const reply=JSON.parse(result.trim());
        if(typeof reply.ok!=='boolean')throw new Error('Malformed browser reply');
        if(!reply.ok)process.exitCode=1;
        process.stdout.write(result,resolve);
      } catch(e) {reject(e);}
    });
  });
  process.exit(process.exitCode || 0);
}
let browser, browserServer, context, controlDir, controlServer, launchPromise, finishing, stopBudgetWatch, hardBudgetTimer, ownedProcess, ownedDetached=false, ownedGroupGone=false;
const connections=new Set();
const budget=assignedUrl&&process.env.SBS_JOB_HOME ? openBudget(assignedUrl,process.env.SBS_JOB_HOME) : null;
if(mode==='--get64') {
  if(!budget)throw new Error('Preliminary GET needs coordinator task context');
  try {
    if(process.env.NODE_TLS_REJECT_UNAUTHORIZED==='0')throw new Error('TLS verification is required');
    const response=await fetch(publicUrl(assignedUrl),{signal:AbortSignal.timeout(budget.timeout(5000))});
    const type=response.headers.get('content-type')||'';let text='';
    if(/^(text\/|application\/(json|xhtml\+xml))/.test(type)) {
      const reader=response.body?.getReader();let size=0,chunks=[];
      if(reader)try{while(size<60000){const {done,value}=await reader.read();if(done)break;const chunk=value.subarray(0,60000-size);size+=chunk.length;chunks.push(Buffer.from(chunk));}}finally{await reader.cancel().catch(()=>{});}
      text=Buffer.concat(chunks).toString('utf8');
    } else await response.body?.cancel();
    process.stdout.write(JSON.stringify({ok:true,http_status:response.status,url:response.url,text,budget:budget.persist(true),tls_verification:true})+'\n');
  } catch(error) {process.stdout.write(JSON.stringify({ok:false,error:String(error),budget:budget.persist(true),tls_verification:true})+'\n');process.exitCode=1;}
  budget.release();process.exit(process.exitCode||0);
}
const emit=x=>process.stdout.write(JSON.stringify(x)+'\n');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const ownedChild=()=>browserServer?.process()||ownedProcess;
const ownedLeaderExited=()=>!ownedChild()||ownedChild().exitCode!==null||ownedChild().signalCode!==null;
function ownedExited(){
  const proc=ownedChild();
  if(!proc)return true;
  if(!ownedDetached||process.platform==='win32')return ownedLeaderExited();
  // A detached group can outlive its leader. Never mistake the leader's exit
  // for cleanup of Chromium's descendants. Once our group is proven absent,
  // never probe or signal this numeric PGID again (it may later be reused).
  if(ownedGroupGone)return true;
  try{process.kill(-proc.pid,0);return false;}catch(e){
    if(e.code==='ESRCH'&&ownedLeaderExited()){ownedGroupGone=true;return true;}
    return false;
  }
}
function killOwned(){const proc=ownedChild();if(proc&&!ownedExited())try{process.kill(ownedDetached&&process.platform!=='win32'?-proc.pid:proc.pid,'SIGKILL');}catch(e){if(e.code!=='ESRCH')throw e;}}
async function waitOwnedExit(ms){const until=Date.now()+ms;while(!ownedExited()&&Date.now()<until)await delay(Math.min(20,until-Date.now()));return ownedExited();}
function resetHardBudget(){if(budget){clearTimeout(hardBudgetTimer);hardBudgetTimer=setTimeout(async()=>{
  // This handle is exclusively the child launched by this session. Never kill
  // processes found through names, executable paths or someone else's PID file.
  try{killOwned();}catch{}
  // Never wait on a stalled Playwright launch/connect/close promise at the
  // hard deadline. Verify this exact child handle after the forced kill.
  if(!ownedExited())await waitOwnedExit(100);
  budget.persist(ownedExited());
  if(ownedExited())budget.release();else budget.retain();
  if(controlDir)fs.rmSync(controlDir,{recursive:true,force:true});
  process.exit(124);
},budget.remaining(true));}}
function finish(){
  return finishing ||= (async()=>{
    stopBudgetWatch?.();process.stdin.pause();
    for(const conn of connections)conn.destroy();
    controlServer?.close();
    // Startup itself has a work-budget timeout. Its rejection performs
    // Playwright's own spawned-process cleanup; a successful late launch is
    // captured here and must be closed before this session can release its lease.
    if(launchPromise)try{await Promise.race([launchPromise,delay(budget?Math.min(1000,budget.remaining(true)):1000)]);}catch{}
    const closing=Promise.allSettled([context?.close(),browser?.close(),browserServer?.close()]);
    let timer;
    await Promise.race([closing,new Promise(resolve=>{timer=setTimeout(resolve,budget?Math.min(2000,budget.remaining(true)):3000);})]);clearTimeout(timer);
    if(!ownedExited()) {
      // Playwright may already consider its leader closed. Signal our exact
      // detached group independently so surviving descendants are included.
      killOwned();browserServer?.kill().catch(()=>{});
      await waitOwnedExit(budget?Math.min(2000,budget.remaining(true)):2000);
    }
    if(!ownedExited()){process.exitCode=1;emit({ok:false,event:'browser_cleanup_failed'});}
    if(budget){budget.persist(ownedExited());if(ownedExited())budget.release();else budget.retain();}
    clearTimeout(hardBudgetTimer);
    if(controlDir)fs.rmSync(controlDir,{recursive:true,force:true});
  })();
}
process.on('SIGTERM',()=>{finish().finally(()=>process.exit(143));});
process.on('SIGINT',()=>{finish().finally(()=>process.exit(130));});
// The watchdog starts before runtime loading, browser launch and connect.
if(budget){resetHardBudget();stopBudgetWatch=budget.watchdog(async()=>{await finish();emit({ok:false,event:'url_budget_exhausted'});process.exit(124);});}
try {
  if(process.env.NODE_TLS_REJECT_UNAUTHORIZED==='0')throw new Error('TLS verification is required');
  const {require,chromium,devices}=loadRuntime();
  const launchOptions={executablePath:chromium.executablePath(),headless:!headed,chromiumSandbox:true,timeout:budget?budget.timeout(30000):30000};
  if(budget) {
    // Capture only our configured browser executable at spawn time. The public
    // BrowserServer handle arrives after startup; the watchdog must also own
    // the process while startup is still pending. No executable-name search or
    // foreign PID is used. Restore spawn immediately when launch settles.
    const childProcess=createRequire(import.meta.url)('node:child_process');
    const originalSpawn=childProcess.spawn;
    const captureSpawn=(command,argv,options)=>{
      const child=originalSpawn(command,argv,options);
      if(command===launchOptions.executablePath){ownedProcess=child;ownedDetached=options?.detached===true;ownedGroupGone=false;if(finishing)killOwned();}
      return child;
    };
    childProcess.spawn=captureSpawn;
    launchPromise=chromium.launchServer(launchOptions).then(server=>{browserServer=server;return server;}).finally(()=>{if(childProcess.spawn===captureSpawn)childProcess.spawn=originalSpawn;});
    await launchPromise;
    if(finishing)throw new Error('URL session is closing');
    browser=await chromium.connect(browserServer.wsEndpoint(),{timeout:budget.timeout(10000)});
  } else browser=await chromium.launch(launchOptions);
  if(android&&!devices['Pixel 7'])throw new Error('Android device descriptor unavailable');
  if(finishing)throw new Error('URL session is closing');
  context=await browser.newContext({...(android?devices['Pixel 7']:{}),locale:'ru-RU',ignoreHTTPSErrors:false,acceptDownloads:false,serviceWorkers:'block'});
  const page=await context.newPage();
  page.setDefaultTimeout(budget?budget.timeout(10000):15000);
  const observedLinks=new Set();let navigationIntent=null,navigationError=null,lastObservation=null,fatalNavigationError=null,trackedDocumentUrl=page.url(),stateUrl=null;
  function sameDocumentNavigation(value){
    if(fatalNavigationError)return;
    try {
      const url=normalizedUrl(value);
      if(url===trackedDocumentUrl){if(value!==url)stateUrl=value;return;}
      const before=new URL(trackedDocumentUrl),after=new URL(url);
      // Maps and similar widgets store UI state in query/hash. No HTTP
      // navigation occurred: a same-origin, same-path state update stays on
      // the current page. A pathname change still needs an observed link.
      if(before.origin===after.origin&&before.pathname===after.pathname){trackedDocumentUrl=url;stateUrl=value;return;}
      if(!navigationIntent)throw new Error('Unrequested same-document page navigation');
      budget.navigation(url,navigationIntent.purpose,navigationIntent.links);
      trackedDocumentUrl=url;stateUrl=null;
    } catch(error){
      // Keep violations sticky, including an unobserved away-and-back pair
      // whose final URL alone would conceal the intermediate page change.
      fatalNavigationError=error;navigationError=error;
      context.close().catch(()=>{});
    }
  }
  async function guardNavigation(){
    if(!budget)return;
    // Drain Chromium events before reading or returning page evidence.
    await delay(0);
    if(fatalNavigationError)throw fatalNavigationError;
    if(navigationError)throw navigationError;
    const current=page.url();
    if(current!==trackedDocumentUrl){
      if(navigationIntent?.allowed.has(normalizedUrl(current)))trackedDocumentUrl=normalizedUrl(current);
      else sameDocumentNavigation(current);
    }
    if(fatalNavigationError)throw fatalNavigationError;
  }
  context.on('page',p=>{if(p!==page)p.close().catch(()=>{});});
  if(budget)await context.route('**/*',async route=>{
    const r=route.request();
    if(!r.isNavigationRequest()){await route.continue();return;}
    if(r.frame()!==page.mainFrame()){
      // Ordinary embedded frames may load; new top-level pages must not bypass
      // this URL session's accounting.
      if(r.frame().parentFrame())await route.continue();else await route.abort('blockedbyclient');return;
    }
    try {
      if(finishing)throw new Error('URL session is closing');
      const url=normalizedUrl(r.url());
      if(!navigationIntent)throw new Error('Navigation requires an explicit operation and missing-fact purpose');
      if(r.redirectedFrom()) {
        // Redirect destinations are validated and counted by the response
        // interceptor below, before Chromium follows the Location header.
        budget.timeout(1);
      } else if(!navigationIntent.counted) {
        budget.navigation(url,navigationIntent.purpose,navigationIntent.links);
        navigationIntent.counted=true;navigationIntent.original=url===budget.url;navigationIntent.url=url;navigationIntent.allowed.add(url);
      } else if(url!==navigationIntent.url)throw new Error('Unexpected additional navigation');
      await route.continue();
    } catch(e){navigationError=e;await route.abort('blockedbyclient');}
  });
  if(budget){
    // Playwright routes only the first request in an HTTP redirect chain. Use
    // Chromium's response-stage interception for every Document redirect;
    // a disallowed Location must be rejected before it contacts the target.
    const cdp=await context.newCDPSession(page);
    const mainFrameId=(await cdp.send('Page.getFrameTree')).frameTree.frame.id;
    cdp.on('Fetch.requestPaused',async event=>{
      try {
        if(event.frameId===mainFrameId&&[301,302,303,307,308].includes(event.responseStatusCode)) {
          const location=event.responseHeaders?.find(h=>h.name.toLowerCase()==='location')?.value;
          if(location){
            const destination=normalizedUrl(new URL(location,event.request.url).href);
            if(!navigationIntent)throw new Error('Redirect requires an explicit navigation operation');
            if(!navigationIntent.original){budget.navigation(destination,navigationIntent.purpose,navigationIntent.links);navigationIntent.allowed.add(destination);}
            else {budget.timeout(1);navigationIntent.allowed.add(destination);}
          }
        }
        await cdp.send('Fetch.continueRequest',{requestId:event.requestId});
      } catch(error){
        if(!finishing)navigationError=error;
        await cdp.send('Fetch.failRequest',{requestId:event.requestId,errorReason:'BlockedByClient'}).catch(()=>{});
      }
    });
    cdp.on('Page.frameNavigated',event=>{if(event.frame.id===mainFrameId){trackedDocumentUrl=event.frame.url;stateUrl=null;}});
    cdp.on('Page.navigatedWithinDocument',event=>{if(event.frameId===mainFrameId)sameDocumentNavigation(event.url);});
    await cdp.send('Page.enable');
    await cdp.send('Fetch.enable',{patterns:[{urlPattern:'*',resourceType:'Document',requestStage:'Response'}]});
  }
  page.on('dialog',d=>d.dismiss());
  async function observe(){
    await guardNavigation();
    const controls=await page.locator('button,input,select,[role=button],a').evaluateAll(nodes=>nodes.slice(0,250).map(e=>({tag:e.tagName,text:(e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder')||'').slice(0,200),id:e.id,href:e.getAttribute('href')})));
    await guardNavigation();
    observedLinks.clear();for(const c of controls)if(c.href)try{observedLinks.add(normalizedUrl(new URL(c.href,page.url()).href));}catch{}
    const title=await page.title(),text=(await page.locator('body').innerText({timeout:budget?budget.timeout(10000):10000})).slice(0,60000);
    await guardNavigation();
    lastObservation={id:crypto.randomBytes(16).toString('hex'),at:Date.now(),url:page.url()};
    return {url:page.url(),title,text,controls,observation_id:lastObservation.id,...(stateUrl?{state_url:stateUrl}: {}),...(budget?{budget:budget.persist()}: {})};
  }
  async function navigate(url,purpose){
    const previous=navigationIntent;
    if(budget){budget.navigation(url,purpose,observedLinks);navigationIntent={url:normalizedUrl(url),purpose,links:new Set(observedLinks),allowed:new Set([normalizedUrl(url)]),counted:true,original:normalizedUrl(url)===budget.url};}
    try {
      const response=await page.goto(publicUrl(url),{waitUntil:'domcontentloaded',timeout:budget?budget.timeout(30000):45000});
      if(navigationError)throw navigationError;
      if(budget&&navigationIntent.original){budget.primary=normalizedUrl(page.url());budget.persist();}
      return response;
    } finally{navigationIntent=previous;}
  }
  const runtimeReport={node:process.version,playwright:require('playwright/package.json').version,chromium:browser.version(),chromium_sandbox:true,tls_verification:true,headless:!headed,android,...(browserServer?{owned_browser_pid:browserServer.process().pid,spawn_process_captured:ownedProcess===browserServer.process()}: {})};
  if(mode==='--check') {
    await page.setContent('<!doctype html><body>SBS_CHROMIUM_ALLOW_OK</body>');
    const readback=await page.locator('body').innerText();
    if(readback!=='SBS_CHROMIUM_ALLOW_OK')throw new Error('Readback mismatch');
    emit({ok:true,...runtimeReport,readback});
  } else if(mode==='--url') {
    const response=await navigate(target);
    emit({ok:true,...runtimeReport,http_status:response?.status()??null,...await observe()});
  } else {
    const operations=['goto','observe','inspect','screenshot','click','click_xy','drag','scroll','fill','press','wait','close','captcha_begin','captcha_end'];
    async function action(c) {
      if(c?.op==='close')return performAction(c);
      if(fatalNavigationError)throw fatalNavigationError;
      const previous=navigationIntent;navigationError=null;
      if(budget)navigationIntent={purpose:c?.purpose,links:new Set(observedLinks),allowed:new Set(),counted:false,original:false};
      try{return await performAction(c);}finally{navigationIntent=previous;navigationError=null;}
    }
    async function performAction(c) {
        if(!c||typeof c!=='object'||Array.isArray(c)||!operations.includes(c.op))throw new Error('Unsupported operation');
        if(finishing)throw new Error('URL session is closing');
        let extra={};
        if(c.op==='close')return {ok:true,event:'closing'};
        if(budget){budget.timeout(1);page.setDefaultTimeout(budget.timeout(10000));if(['click','click_xy','drag','fill','press'].includes(c.op))budget.choice();}
        if(c.op==='captcha_begin') {
          if(!budget)throw new Error('CAPTCHA accounting requires SBS task context');
          const policy=JSON.parse(fs.readFileSync(path.join(process.env.SBS_JOB_HOME,'sbs-task-policy.json'),'utf8'));
          const challenges=page.locator('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="challenges.cloudflare.com"],.g-recaptcha,.h-captcha,[data-sitekey]');
          let challenge=false;
          for(let i=0;i<Math.min(await challenges.count(),20);i++)if(await challenges.nth(i).isVisible()){challenge=true;break;}
          if(!lastObservation||c.observation_id!==lastObservation.id||Date.now()-lastObservation.at>10000||page.url()!==lastObservation.url)throw new Error('CAPTCHA requires a fresh observation_id from this page');
          // Site-specific image/color challenges need a fresh explicit report
          // from the observing agent. Vendor markers are useful evidence but
          // cannot decide whether every possible custom challenge is a CAPTCHA.
          if(!challenge&&c.challenge_observed!==true)throw new Error('Custom CAPTCHA needs an explicit observed-challenge report');
          budget.beginCaptcha(policy.captcha_grant,c.evidence);resetHardBudget();
          return {ok:true,op:c.op,challenge_markers_detected:challenge,...await observe()};
        }
        if(c.op==='captcha_end') {if(!budget)throw new Error('CAPTCHA accounting requires SBS context');budget.endCaptcha();return {ok:true,op:c.op,...await observe()};}
        if(c.op==='goto'){
          const r=await navigate(c.url,c.purpose);extra.http_status=r?.status()??null;}
        else if(c.op==='observe'){}
        else if(c.op==='inspect') {
          const loc=page.locator(c.selector || 'button,input,select,[role=button],a,canvas,iframe');
          extra.elements=await loc.evaluateAll(nodes=>nodes.slice(0,150).map(e=>{const b=e.getBoundingClientRect(),s=getComputedStyle(e);return {tag:e.tagName,id:e.id,role:e.getAttribute('role'),className:e.getAttribute('class'),text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,200),x:b.x,y:b.y,width:b.width,height:b.height,background:s.backgroundColor,display:s.display,visibility:s.visibility};}));
        }
        else if(c.op==='screenshot') {
          const dest=path.resolve(String(c.path||'sbs-page.png'));
          const cwd=fs.realpathSync(process.cwd());
          const parent=fs.realpathSync(path.dirname(dest));
          if(!(parent===cwd||parent.startsWith(cwd+path.sep))||fs.existsSync(dest)&&fs.lstatSync(dest).isSymbolicLink())throw new Error('Screenshot path must be inside current workspace, no symlinks');
          await page.screenshot({path:dest,fullPage:!!c.fullPage,timeout:budget?budget.timeout(15000):15000});extra.screenshot=dest;
        }
        else if(c.op==='scroll') {if(!Number.isFinite(c.x)||!Number.isFinite(c.y))throw new Error('Scroll delta required');await page.mouse.wheel(c.x,c.y);}
        else if(['click','click_xy','press','drag'].includes(c.op)) {
            if(c.op==='click')await page.locator(c.selector).click();
            else if(c.op==='press')await page.locator(c.selector).press(String(c.key));
            else if(c.op==='click_xy'){if(!Number.isFinite(c.x)||!Number.isFinite(c.y))throw new Error('Coordinates required');await page.mouse.click(c.x,c.y);}
            else {
              for(const n of ['fromX','fromY','toX','toY'])if(!Number.isFinite(c[n]))throw new Error('Drag coordinates required');
              await page.mouse.move(c.fromX,c.fromY);await page.mouse.down();try{await page.mouse.move(c.toX,c.toY,{steps:20});}finally{await page.mouse.up();}
            }
            await page.waitForLoadState('domcontentloaded',{timeout:budget?budget.timeout(10000):10000});
            if(navigationError)throw navigationError;
            if(budget&&navigationIntent.original){budget.primary=normalizedUrl(page.url());budget.persist();}
        }
        else if(c.op==='fill')await page.locator(c.selector).fill(String(c.value));
        else if(c.op==='wait'){if(!Number.isInteger(c.ms)||c.ms<0||c.ms>10000)throw new Error('Wait must be 0..10000 ms');await page.waitForTimeout(budget?budget.timeout(c.ms):c.ms);}
        else throw new Error('Unsupported operation');
        if(navigationError)throw navigationError;
        await guardNavigation();
        return {ok:true,op:c.op,...extra,...await observe()};
    }
    if(mode==='--server') {
      controlDir=fs.mkdtempSync(path.join(tmpRoot,'sbs-chromium-'));
      fs.chmodSync(controlDir,0o700);
      if(budget)fs.writeFileSync(path.join(controlDir,'job.json'),JSON.stringify({job_home:process.env.SBS_JOB_HOME}),{mode:0o600});
      const socket=path.join(controlDir,'control.sock');
      await new Promise((resolve,reject)=>{
        let queue=Promise.resolve();
        const server=controlServer=net.createServer(conn=>{
          connections.add(conn);conn.on('close',()=>connections.delete(conn));
          let buf='',accepted=false;
          conn.setTimeout(60000,()=>conn.destroy());
          conn.on('error',()=>{});
          conn.on('data',b=>{
            if(accepted)return;
            buf+=b.toString();if(buf.length>65536){conn.destroy();return;}
            if(!buf.includes('\n'))return;
            accepted=true;clearTimeout(idle);
            queue=queue.then(async()=>{
              let c,result;
              try{c=JSON.parse(buf.split('\n')[0]);result=await action(c);}catch(e){result={ok:false,error:String(e)};}
              conn.end(JSON.stringify(result)+'\n');
              if(c?.op==='close'){clearTimeout(idle);server.close(resolve);}else resetIdle();
            });
          });
        });
        let idle;
        function resetIdle(){clearTimeout(idle);idle=setTimeout(()=>server.close(resolve),300000);}
        server.on('error',reject);
        server.listen(socket,()=>{fs.chmodSync(socket,0o600);resetIdle();emit({ok:true,event:'ready',session:path.basename(controlDir),...runtimeReport,operations});});
      });
    } else {
      emit({ok:true,event:'ready',...runtimeReport,operations});
      const input=createInterface({input:process.stdin,terminal:false});
      for await(const line of input){
        if(!line.trim())continue;
        try{const c=JSON.parse(line);emit(await action(c));if(c.op==='close'){input.close();process.stdin.pause();break;}}catch(e){emit({ok:false,error:String(e)});}
      }
    }
  }
} catch(e){emit({ok:false,error:String(e)});process.exitCode=1;}
finally{await finish();if(controlDir)fs.rmSync(controlDir,{recursive:true,force:true});}
