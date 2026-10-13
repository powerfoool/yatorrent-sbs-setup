// Fixed local browser launcher. No evaluation of caller-supplied JavaScript or shell code.
import path from 'node:path';
import {createRequire} from 'node:module';
import {createInterface} from 'node:readline';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
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
let mode, target, request, headed=false, android=false;
for(let i=0;i<args.length;i++) {
  const a=args[i];
  if(a==='--headed') headed=true;
  else if(a==='--android') android=true;
  else if(['--check','--stdio','--server','--send','--send64','--url'].includes(a) && !mode) {mode=a;if(a==='--url'||a==='--send'||a==='--send64') target=args[++i];if(a==='--send'||a==='--send64')request=args[++i];}
  else throw new Error('Usage: sbs-chromium.mjs --check|--server|--stdio|--url URL|--send SESSION JSON|--send64 SESSION BASE64URL [--headed] [--android]');
}
if(!mode) throw new Error('Mode required: --check, --url URL or --stdio');
function publicUrl(s) {
  const u=new URL(s);
  if(!['http:','https:'].includes(u.protocol)||u.username||u.password) throw new Error('Only HTTP(S) URLs without credentials are accepted');
  return u.href;
}
if(mode==='--url') publicUrl(target);
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
let browser, context, controlDir, finishing;
const emit=x=>process.stdout.write(JSON.stringify(x)+'\n');
function finish(){
  return finishing ||= (async()=>{
    await Promise.allSettled([context?.close(),browser?.close()]);
    if(controlDir)fs.rmSync(controlDir,{recursive:true,force:true});
  })();
}
process.on('SIGTERM',()=>{finish().finally(()=>process.exit(143));});
process.on('SIGINT',()=>{finish().finally(()=>process.exit(130));});
try {
  const {require,chromium,devices}=loadRuntime();
  browser=await chromium.launch({executablePath:chromium.executablePath(),headless:!headed,chromiumSandbox:true,timeout:30000});
  if(android&&!devices['Pixel 7'])throw new Error('Android device descriptor unavailable');
  context=await browser.newContext({...(android?devices['Pixel 7']:{}),locale:'ru-RU',ignoreHTTPSErrors:false,acceptDownloads:false});
  const page=await context.newPage();
  page.setDefaultTimeout(15000);
  page.on('dialog',d=>d.dismiss());
  async function observe(){
    return {url:page.url(),title:await page.title(),text:(await page.locator('body').innerText()).slice(0,60000),controls:await page.locator('button,input,select,[role=button],a').evaluateAll(nodes=>nodes.slice(0,250).map(e=>({tag:e.tagName,text:(e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder')||'').slice(0,200),id:e.id,href:e.getAttribute('href')})))};
  }
  const runtimeReport={node:process.version,playwright:require('playwright/package.json').version,chromium:browser.version(),chromium_sandbox:true,tls_verification:true,headless:!headed,android};
  if(mode==='--check') {
    await page.setContent('<!doctype html><body>SBS_CHROMIUM_ALLOW_OK</body>');
    const readback=await page.locator('body').innerText();
    if(readback!=='SBS_CHROMIUM_ALLOW_OK')throw new Error('Readback mismatch');
    emit({ok:true,...runtimeReport,readback});
  } else if(mode==='--url') {
    const response=await page.goto(publicUrl(target),{waitUntil:'domcontentloaded',timeout:45000});
    emit({ok:true,...runtimeReport,http_status:response?.status()??null,...await observe()});
  } else {
    const operations=['goto','observe','inspect','screenshot','click','click_xy','drag','scroll','fill','press','wait','close'];
    async function action(c) {
        if(!c||typeof c!=='object'||Array.isArray(c)||!operations.includes(c.op))throw new Error('Unsupported operation');
        let extra={};
        if(c.op==='close')return {ok:true,event:'closing'};
        if(c.op==='goto'){const r=await page.goto(publicUrl(c.url),{waitUntil:'domcontentloaded',timeout:45000});extra.http_status=r?.status()??null;}
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
          await page.screenshot({path:dest,fullPage:!!c.fullPage,timeout:15000});extra.screenshot=dest;
        }
        else if(c.op==='drag') {
          for(const n of ['fromX','fromY','toX','toY'])if(!Number.isFinite(c[n]))throw new Error('Drag coordinates required');
          await page.mouse.move(c.fromX,c.fromY);await page.mouse.down();
          try{await page.mouse.move(c.toX,c.toY,{steps:20});}finally{await page.mouse.up();}
        }
        else if(c.op==='scroll') {if(!Number.isFinite(c.x)||!Number.isFinite(c.y))throw new Error('Scroll delta required');await page.mouse.wheel(c.x,c.y);}
        else if(c.op==='click')await page.locator(c.selector).click();
        else if(c.op==='click_xy'){if(!Number.isFinite(c.x)||!Number.isFinite(c.y))throw new Error('Coordinates required');await page.mouse.click(c.x,c.y);}
        else if(c.op==='fill')await page.locator(c.selector).fill(String(c.value));
        else if(c.op==='press')await page.locator(c.selector).press(String(c.key));
        else if(c.op==='wait'){if(!Number.isInteger(c.ms)||c.ms<0||c.ms>10000)throw new Error('Wait must be 0..10000 ms');await page.waitForTimeout(c.ms);}
        else throw new Error('Unsupported operation');
        return {ok:true,op:c.op,...extra,...await observe()};
    }
    if(mode==='--server') {
      controlDir=fs.mkdtempSync(path.join(tmpRoot,'sbs-chromium-'));
      fs.chmodSync(controlDir,0o700);
      const socket=path.join(controlDir,'control.sock');
      await new Promise((resolve,reject)=>{
        let queue=Promise.resolve();
        const server=net.createServer(conn=>{
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
        try{const c=JSON.parse(line);emit(await action(c));if(c.op==='close')break;}catch(e){emit({ok:false,error:String(e)});}
      }
    }
  }
} catch(e){emit({ok:false,error:String(e)});process.exitCode=1;}
finally{await finish();if(controlDir)fs.rmSync(controlDir,{recursive:true,force:true});}
