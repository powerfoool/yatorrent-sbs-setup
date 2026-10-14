// One explicit browser operation, bounded automatic Document transitions.
// This module never grants a new URL visit or resets its parent URL budget.
export const NAVIGATION_LIMITS = Object.freeze({milliseconds:30000, transitions:10, quietMilliseconds:750});
const normalize=value=>{const u=new URL(value);if(!['http:','https:'].includes(u.protocol)||u.username||u.password)throw new Error('Only credential-free HTTP(S) navigation is supported');u.hash='';return u.href;};
const safe=value=>{const u=new URL(value);return {origin:u.origin,path:u.pathname,query_keys:[...new Set(u.searchParams.keys())]};};
const samePath=(a,b)=>{const x=new URL(a),y=new URL(b);return x.origin===y.origin&&x.pathname===y.pathname;};

export class NavigationTransaction {
  constructor(url,{original,allowedOrigins,remaining,now=Date.now}={}){
    this.url=normalize(url);this.original=!!original;this.allowedOrigins=new Set(allowedOrigins);
    this.now=now;this.started=now();this.deadline=this.started+Math.min(NAVIGATION_LIMITS.milliseconds,remaining);
    this.lastActivity=this.started;this.records=[];this.ready=false;this.done=false;this.failure=null;this.finalUrl=null;
  }
  reject(message){
    this.failure=message;
    throw Object.assign(new Error(message),{source:'runtime_navigation_guard'});
  }
  timeout(){
    if(this.done)this.reject('Navigation transaction is closed');
    const remaining=this.deadline-this.now();
    if(remaining<=0)this.reject('Automatic navigation deadline exceeded');
    return remaining;
  }
  accept({id,url,method='GET',initiator='unknown'}){
    this.timeout();const value=normalize(url);
    if(!this.records.length&&value!==this.url)this.reject('First document differs from the explicit navigation URL');
    if(!this.original&&!this.allowedOrigins.has(new URL(value).origin))this.reject('Internal redirect outside observed site');
    if(this.records.length>=NAVIGATION_LIMITS.transitions+1)this.reject('Automatic navigation transition limit exceeded');
    const previous=this.records.at(-1);
    this.records.push({id,url:value,method,initiator,kind:previous&&[301,302,303,307,308].includes(previous.status)?'http_redirect':'browser_document',status:null,elapsed_ms:this.now()-this.started});
    this.lastActivity=this.now();this.ready=false;
  }
  response(id,url,status){
    const record=this.records.findLast(r=>r.id===id)||this.records.findLast(r=>r.url===normalize(url));
    if(record){record.status=status;this.lastActivity=this.now();}
  }
  committed(){if(!this.done){this.ready=false;this.lastActivity=this.now();}}
  domReady(){if(!this.done){this.ready=true;this.lastActivity=this.now();}}
  sameDocument(url){
    const ready=this.ready;this.accept({id:'same-document-'+this.records.length,url,method:'SAME_DOCUMENT',initiator:'script'});
    this.records.at(-1).status=this.records.at(-2)?.status??200;this.ready=ready;
  }
  settled(url){
    this.timeout();const last=this.records.at(-1);
    return !!last&&this.ready&&last.status!==null&&!([301,302,303,307,308].includes(last.status))&&samePath(last.url,url)&&this.now()-this.lastActivity>=NAVIGATION_LIMITS.quietMilliseconds;
  }
  finish(url){this.finalUrl=normalize(url);this.done=true;return this.snapshot();}
  snapshot(){return {automatic_transitions:Math.max(0,this.records.length-1),elapsed_ms:this.now()-this.started,
    completed:this.done&&!this.failure,error_source:this.failure?'runtime_navigation_guard':null,error:this.failure,
    requested:safe(this.url),final:this.finalUrl?safe(this.finalUrl):null,
    final_http_status:this.records.at(-1)?.status??null,
    hops:this.records.map(({url,method,initiator,kind,status,elapsed_ms})=>({url:safe(url),method,initiator,kind,status,elapsed_ms}))};}
}
