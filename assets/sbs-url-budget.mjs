import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const need = (ok, text) => {if (!ok) throw new Error(text);};
export function normalizedUrl(value) {
  need(typeof value==='string','HTTP(S) URL required');
  const u = new URL(value);
  need(['https:', 'http:'].includes(u.protocol) && !u.username && !u.password,'Public HTTP(S) URL required');
  u.hash = ''; return u.href;
}
const duration = n => Number.isSafeInteger(n) && n>=0;

export class UrlBudget {
  constructor(url, {checkpoint=null, now=()=>Date.now(), save=()=>{}}={}) {
    this.now=now;this.save=save;this.url=normalizedUrl(url);
    const current=now();
    if(checkpoint!==null) {
      const c=checkpoint;
      need(c && typeof c==='object'&&!Array.isArray(c)&&c.version===1&&c.url===this.url,'Invalid checkpoint version/URL');
      need(duration(c.started_at)&&duration(c.saved_at)&&duration(c.elapsed_ms)&&c.started_at<=c.saved_at&&c.saved_at<=current&&c.elapsed_ms===c.saved_at-c.started_at,'Invalid checkpoint clock');
      need(typeof c.captcha_encountered==='boolean'&&typeof c.captcha_active==='boolean'&&typeof c.browser_closed==='boolean'&&(!c.captcha_active||c.captcha_encountered),'Invalid checkpoint flags');
      need(duration(c.captcha_elapsed_ms)&&c.captcha_elapsed_ms<=120000&&c.captcha_elapsed_ms<=c.elapsed_ms&&Number.isInteger(c.captcha_choices)&&c.captcha_choices>=0&&c.captcha_choices<=5&&(!c.captcha_choices||c.captcha_encountered)&&[0,1].includes(c.reloads),'Invalid checkpoint counters');
      need(c.captcha_encountered || c.captcha_elapsed_ms===0,'Invalid checkpoint CAPTCHA history');
      need(typeof c.initial_navigation_started==='boolean','Checkpoint missing navigation history');
      need(typeof c.observed_primary_url==='string'&&normalizedUrl(c.observed_primary_url)===c.observed_primary_url,'Invalid checkpoint primary URL');
      need(Array.isArray(c.internal_pages)&&c.internal_pages.length<=3,'Invalid checkpoint internal pages');
      const origins=[new URL(this.url).origin,new URL(c.observed_primary_url).origin];
      for(const p of c.internal_pages)need(p&&typeof p==='object'&&!Array.isArray(p)&&typeof p.url==='string'&&normalizedUrl(p.url)===p.url&&p.url!==this.url&&origins.includes(new URL(p.url).origin)&&typeof p.purpose==='string'&&p.purpose.trim(),'Invalid checkpoint internal page');
      need(c.initial_navigation_started || c.reloads===0&&c.internal_pages.length===0&&c.observed_primary_url===this.url,'Invalid checkpoint navigation history');
    }
    this.started=checkpoint?.started_at ?? current;
    this.captcha=checkpoint?.captcha_encountered ?? false;
    this.captchaSpent=checkpoint?.captcha_elapsed_ms ?? 0;
    this.captchaStart=checkpoint?.captcha_active ? checkpoint.saved_at : null;
    this.choices=checkpoint?.captcha_choices ?? 0;
    this.internal=structuredClone(checkpoint?.internal_pages ?? []);
    this.primary=checkpoint?.observed_primary_url ?? this.url;
    this.reloaded=checkpoint?.reloads ?? 0;
    this.initialStarted=checkpoint?.initial_navigation_started ?? false;
  }
  elapsed(){return Math.max(0,this.now()-this.started);}
  captchaElapsed(){return this.captchaSpent+(this.captchaStart===null?0:Math.max(0,this.now()-this.captchaStart));}
  remaining(cleanup=false){return Math.max(0,(this.captcha?300000:180000)-this.elapsed()-(cleanup?0:5000));}
  timeout(cap){need(Number.isFinite(cap)&&cap>=0,'Invalid timeout');const r=Math.min(this.remaining(),this.captchaStart===null?Infinity:120000-this.captchaElapsed());need(r>0,'URL/CAPTCHA budget exhausted');return Math.min(cap,r);}
  persist(closed=false){
    const at=this.now();
    const state={version:1,url:this.url,started_at:this.started,saved_at:at,elapsed_ms:at-this.started,captcha_encountered:this.captcha,captcha_elapsed_ms:Math.min(120000,this.captchaSpent+(this.captchaStart===null?0:Math.max(0,at-this.captchaStart))),captcha_active:this.captchaStart!==null,captcha_choices:this.choices,internal_pages:this.internal,observed_primary_url:this.primary,reloads:this.reloaded,initial_navigation_started:this.initialStarted,browser_closed:closed};
    this.save(state);return state;
  }
  beginCaptcha(grant,evidence){
    need(grant?.authorized===true && typeof grant.grant_id==='string' && grant.grant_id && typeof grant.scope==='string' && grant.scope && typeof grant.statement==='string' && grant.statement,'Coordinator CAPTCHA grant required');
    need(typeof evidence==='string' && evidence.trim(),'Observed necessary challenge evidence required');
    if(Array.isArray(grant.urls))need(grant.urls.map(normalizedUrl).includes(this.url),'CAPTCHA grant outside assigned URL');
    need(this.captchaStart===null,'CAPTCHA already active');this.timeout(1);
    need(this.captchaElapsed()<120000,'CAPTCHA budget exhausted');this.captcha=true;this.captchaStart=this.now();this.persist();
  }
  endCaptcha(){need(this.captchaStart!==null,'CAPTCHA is not active');this.captchaSpent=Math.min(120000,this.captchaElapsed());this.captchaStart=null;this.persist();}
  choice(){if(this.captchaStart!==null){this.timeout(1);need(this.choices<5,'CAPTCHA choice limit reached');this.choices++;this.persist();}}
  navigation(value,purpose,observedLinks){
    const url=normalizedUrl(value);this.timeout(1);
    if(url===this.url){if(this.initialStarted){need(this.reloaded<1,'Original URL reload limit reached');this.reloaded++;}else this.initialStarted=true;this.persist();return;}
    need(this.initialStarted,'Visit the assigned original URL first');
    need(typeof purpose==='string'&&purpose.trim(),'Internal navigation needs a missing-fact purpose');
    need(observedLinks.has(url),'Internal URL must be a freshly observed link');
    need([new URL(this.url).origin,new URL(this.primary).origin].includes(new URL(url).origin),'Internal URL outside observed site');
    // Count every attempted visit, including failed requests and repeated links.
    need(this.internal.length<3,'Internal page limit reached');this.internal.push({url,purpose});this.persist();
  }
  watchdog(close){let fired=false;const check=()=>{if(!fired&&(this.remaining()<=0||this.captchaStart!==null&&this.captchaElapsed()>=120000)){fired=true;Promise.resolve().then(close).catch(()=>{});}};const timer=setInterval(check,50);check();return ()=>clearInterval(timer);}
}

function owned(st,type,label){need((type==='dir'?st.isDirectory():st.isFile())&&!st.isSymbolicLink()&&st.uid===process.getuid()&&!(st.mode&0o077),'Unsafe '+label);}
export function openBudget(url, home) {
  need(typeof home==='string'&&path.isAbsolute(home),'SBS task-owned budget directory required');
  owned(fs.lstatSync(home),'dir','SBS job home');
  const dir=path.join(home,'url-visits');
  try{fs.mkdirSync(dir,{mode:0o700});}catch(e){if(e.code!=='EEXIST')throw e;}
  owned(fs.lstatSync(dir),'dir','URL state directory');
  const file=path.join(dir,crypto.createHash('sha256').update(normalizedUrl(url)).digest('hex')+'.json');
  // An exclusive lease covers checkpoint read, browser/GET use and final save.
  // A stale lease is conservative: never steal it or kill an unverified PID.
  const lock=file+'.lock';let fd;
  try{fd=fs.openSync(lock,'wx',0o600);}catch(e){if(e.code==='EEXIST')throw new Error('URL already in use (or stale lease requires coordinator recovery)');throw e;}
  const identity=fs.fstatSync(fd);
  fs.writeFileSync(fd,JSON.stringify({pid:process.pid,nonce:crypto.randomBytes(16).toString('hex')}));fs.closeSync(fd);
  let released=false;
  const release=()=>{if(released)return;released=true;try{const st=fs.lstatSync(lock);if(st.ino===identity.ino&&st.dev===identity.dev)fs.unlinkSync(lock);}catch(e){if(e.code!=='ENOENT')throw e;}process.off('exit',release);};
  process.once('exit',release);
  try {
    let checkpoint=null;
    if(fs.existsSync(file)){const s=fs.lstatSync(file);owned(s,'file','URL checkpoint');need(s.size<262144,'Oversized URL checkpoint');checkpoint=JSON.parse(fs.readFileSync(file,'utf8'));}
    const save=state=>{
      need(!released,'URL lease released');
      const temp=file+'.'+crypto.randomBytes(16).toString('hex')+'.tmp';const writeFd=fs.openSync(temp,'wx',0o600);
      try{fs.writeFileSync(writeFd,JSON.stringify(state));fs.fsyncSync(writeFd);}finally{fs.closeSync(writeFd);}
      try{fs.renameSync(temp,file);}finally{if(fs.existsSync(temp))fs.unlinkSync(temp);}
    };
    const budget=new UrlBudget(url,{checkpoint,save});budget.release=release;
    // Failed cleanup must leave an explicit recovery gate. An exit callback
    // cannot authorize a new lease while the previous browser may still live.
    budget.retain=()=>process.off('exit',release);
    budget.timeout(1);budget.persist();return budget;
  } catch(e){release();throw e;}
}
