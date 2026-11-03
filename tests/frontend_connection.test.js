'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
const result=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body,blob:async()=>new Blob(['video'])});
const pending=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};

async function setup(){
  const elements=new Map(),listeners=new Map(),intervals=[],calls=[];let timer=0;
  class Element{
    constructor(id=''){this.id=id;this.className='';this.textContent='';this.innerHTML='';this.hidden=false;this.disabled=false;this.open=false;this.value='';this.dataset={};this.listeners={};this.classList={contains:name=>this.className.split(' ').includes(name),toggle:(name,on)=>{const values=new Set(this.className.split(' ').filter(Boolean));on?values.add(name):values.delete(name);this.className=[...values].join(' ');}};}
    addEventListener(name,fn){this.listeners[name]=fn;}
    before(el){if(el.id)elements.set(el.id,el);}
    after(el){if(el.id)elements.set(el.id,el);}
    pause(){}
    removeAttribute(name){delete this[name];}
    showModal(){this.open=true;}
    close(){const was=this.open;this.open=false;if(was&&this.listeners.close)this.listeners.close();}
    click(){if(this.onclick)this.onclick();}
  }
  for(const [,id]of html.matchAll(/id="([^"]+)"/g))elements.set(id,new Element(id));
  const access=new Element();
  const document={hidden:false,title:'',querySelector:selector=>selector==='.local-access'?access:selector.startsWith('#')?elements.get(selector.slice(1))||null:null,querySelectorAll:()=>[],createElement:()=>new Element(),addEventListener:(name,fn)=>listeners.set(name,fn)};
  const harness={elements,document,listeners,intervals,calls,fetchImpl:async()=>result({},404)};
  const context=vm.createContext({document,location:{hostname:'127.0.0.1',origin:'http://127.0.0.1:8767',hash:'',pathname:'/'},history:{replaceState(){}},URL,URLSearchParams,AbortController,AbortSignal,Blob,Date,console,crypto:{randomUUID:()=> 'request-id'},setTimeout:()=>++timer,clearTimeout(){},setInterval:(fn,ms)=>{intervals.push({fn,ms});return ++timer;},fetch:(url,options={})=>{calls.push({url,options});return harness.fetchImpl(url,options);}});
  vm.runInContext(source+'\nglobalThis.app={state,api,refresh,review,reportError,setConnection,nextEpoch,connectionContext,probeIfVisible,toast,updateReviewActions};',context);
  await tick();
  harness.app=context.app;calls.length=0;
  harness.configure=(connected=true)=>{Object.assign(harness.app.state,{base:'http://127.0.0.1:8767',token:'a'.repeat(32),localGateway:true,jobs:[{id:'j1',title:'Existing',status:'review',source:'source.mp4'}],assets:[{source:'source.mp4',name:'source.mp4',size:10,extension:'.mp4'}],config:{delivery:{enabled:true,configured:true}},selected:{id:'j1',status:'review'},lastSuccessAt:Date.now()});harness.app.setConnection(connected,undefined,connected?'connected':'error');};
  harness.good=(url)=>{const route=new URL(url,'http://127.0.0.1:8767').pathname;return result(route==='/api/status'?{counts:{review:1},worker_running:true,execution_target:'remote_gpu',gpu:{available:true,name:'GPU',memory_mb:24576}}:route==='/api/assets'?{assets:[]} :route==='/api/jobs'?{jobs:[{id:'j1',title:'Fresh',status:'review',source:'source.mp4'}]}:route==='/api/config'?{inbox:'/media',delivery:{enabled:true,configured:true}}:{});};
  return harness;
}

test('review detail 503 immediately invalidates green, retains cached data, disables actions',async()=>{
  const h=await setup();h.configure();h.fetchImpl=async()=>result({detail:'连接暂不可用'},503);
  await h.app.review('j1');
  assert.equal(h.app.state.connected,false);
  assert.match(h.elements.get('connectionPill').innerHTML,/连接异常/);
  assert.equal(h.app.state.jobs[0].title,'Existing');
  assert.equal(h.elements.get('approveButton').disabled,true);
  assert.equal(h.elements.get('publishButton').disabled,true);
  assert.equal(h.elements.get('dataFreshness').hidden,false);
  assert.match(h.elements.get('desktopConnectionStatus').innerHTML,/连接方式：本地自动连接/);
  assert.match(h.elements.get('desktopConnectionStatus').innerHTML,/GPU 连接异常/);
});

test('video fetch uses central failure handling',async()=>{
  const h=await setup();h.configure();h.fetchImpl=async url=>url.endsWith('/video')?result({detail:'成片读取暂不可用'},503):result({job:{id:'j1',title:'Review',status:'review',has_video:true,video_url:'/api/jobs/j1/video',plan:{duration:3,clips:[]}}});
  await h.app.review('j1');
  assert.equal(h.app.state.connected,false);
  assert.equal(h.elements.get('approveButton').disabled,true);
  assert.equal(h.app.state.toastKind,'connection');
});

test('business 409 does not disconnect and successful refresh does not clear business toast',async()=>{
  const h=await setup();h.configure();h.fetchImpl=async()=>result({detail:'当前任务状态不能审核'},409);
  try{await h.app.api('/api/jobs/j1/approve',{method:'POST'});}catch(error){h.app.reportError(error);}
  assert.equal(h.app.state.connected,true);assert.equal(h.app.state.toastKind,'business');
  h.fetchImpl=async url=>h.good(url);await h.app.refresh(true);
  assert.equal(h.elements.get('toast').textContent,'当前任务状态不能审核');
});

test('late old successful probe cannot overwrite a newer action failure',async()=>{
  const h=await setup();h.configure();const reads=[];
  h.fetchImpl=(url)=>{if(url.endsWith('/detail'))return Promise.resolve(result({detail:'服务暂不可用'},503));const hold=pending();reads.push({url,...hold});return hold.promise;};
  const probe=h.app.refresh(true);
  await assert.rejects(h.app.api('/api/jobs/detail'));
  for(const read of reads)read.resolve(h.good(read.url));
  await probe;
  assert.equal(h.app.state.connected,false);assert.equal(h.app.state.jobs[0].title,'Existing');
});

test('user disconnect prevents in-flight refresh from repopulating credentials or jobs',async()=>{
  const h=await setup();h.configure();const reads=[];h.fetchImpl=url=>{const hold=pending();reads.push({url,...hold});return hold.promise;};
  const probe=h.app.refresh(true);h.elements.get('disconnectButton').onclick();
  for(const read of reads)read.resolve(h.good(read.url));await probe;
  assert.equal(h.app.state.token,'');assert.equal(h.app.state.connected,false);assert.equal(h.app.state.jobs.length,0);
});

test('periodic disconnected probe and visibility return recover without replaying mutations',async()=>{
  const h=await setup();h.configure();h.fetchImpl=async()=>result({detail:'暂时不可达'},503);
  try{await h.app.api('/api/jobs/j1');}catch(error){h.app.reportError(error);}
  h.fetchImpl=async url=>h.good(url);h.calls.length=0;
  await h.intervals.find(item=>item.ms===8000).fn();
  assert.equal(h.app.state.connected,true);assert.match(h.elements.get('toast').textContent,/连接已恢复/);
  assert.equal(h.calls.length,4);assert.ok(h.calls.every(call=>!call.options.method||call.options.method==='GET'));
  h.calls.length=0;h.document.hidden=true;h.listeners.get('visibilitychange')();await tick();assert.equal(h.calls.length,0);
  h.document.hidden=false;h.listeners.get('visibilitychange')();await tick();assert.equal(h.calls.length,4);
});

test('overlapping probes share one request batch',async()=>{
  const h=await setup();h.configure(false);const reads=[];h.fetchImpl=url=>{const hold=pending();reads.push({url,...hold});return hold.promise;};
  const first=h.app.refresh(true),second=h.app.refresh(true);assert.equal(first,second);assert.equal(reads.length,4);
  for(const read of reads)read.resolve(h.good(read.url));await first;assert.equal(h.app.state.connected,true);
});

test('401 reboots local session only on next probe and does not replay the rejected request',async()=>{
  const h=await setup();h.configure();h.fetchImpl=async()=>result({detail:'访问凭据已更新'},401);
  await assert.rejects(h.app.api('/api/jobs/j1'));assert.equal(h.app.state.reauthNeeded,true);
  assert.equal(h.calls.filter(call=>call.url==='/local-session').length,0);
  h.calls.length=0;h.fetchImpl=async url=>url==='/local-session'?result({local_gateway:true,token:'b'.repeat(32)}):h.good(url);
  await h.app.refresh(true);
  assert.equal(h.app.state.token,'b'.repeat(32));assert.equal(h.app.state.connected,true);
  assert.equal(h.calls.filter(call=>call.url==='/local-session').length,1);
  assert.equal(h.calls.filter(call=>call.url.endsWith('/api/jobs/j1')).length,0);
  assert.ok(h.calls.filter(call=>call.url!=='/local-session').every(call=>call.options.headers.Authorization==='Bearer '+'b'.repeat(32)));
});

test('parallel read failure never aborts or repeats an in-flight publish; late result is explicitly uncertain',async()=>{
  const h=await setup();h.configure();const write=pending();let writeSignal;
  h.fetchImpl=(url,options)=>{if(options.method==='POST'){writeSignal=options.signal;return write.promise;}return Promise.resolve(result({detail:'检查失败'},503));};
  const publishing=h.app.api('/api/jobs/j1/publish',{method:'POST'});
  await assert.rejects(h.app.api('/api/status'));
  assert.equal(writeSignal.aborted,false);
  write.resolve(result({published:true}));
  await assert.rejects(publishing,error=>{assert.equal(error.uncertain,true);h.app.reportError(error);return /操作结果需核实/.test(error.message);});
  assert.equal(h.app.state.toastKind,'operation-uncertain');
  assert.equal(h.calls.filter(call=>call.options.method==='POST').length,1);
  h.fetchImpl=async url=>h.good(url);await h.app.refresh(true);
  assert.match(h.elements.get('toast').textContent,/操作结果需核实/);
});

test('network rejection marks connection abnormal without exposing raw fetch details',async()=>{
  const h=await setup();h.configure();h.fetchImpl=async()=>{throw new TypeError('raw upstream url with credentials');};
  await assert.rejects(h.app.api('/api/status'),error=>!error.message.includes('credentials'));
  assert.equal(h.app.state.connected,false);
});

test('failed renewed credentials do not produce an automatic local-session retry loop',async()=>{
  const h=await setup();h.configure();h.fetchImpl=async()=>result({detail:'授权失效'},401);
  await assert.rejects(h.app.api('/api/jobs/j1'));
  h.calls.length=0;h.fetchImpl=async url=>url==='/local-session'?result({local_gateway:true,token:'b'.repeat(32)}):result({detail:'授权仍失效'},401);
  await h.app.refresh(true);await h.app.refresh(true);
  assert.equal(h.calls.filter(call=>call.url==='/local-session').length,1);
  assert.equal(h.app.state.connected,false);
});

test('an old connection error cannot replace the manual-disconnect message',async()=>{
  const h=await setup();h.configure();h.fetchImpl=async()=>result({detail:'过期连接错误'},503);
  let oldError;try{await h.app.api('/api/status');}catch(error){oldError=error;}
  h.elements.get('disconnectButton').onclick();h.app.reportError(oldError);
  assert.match(h.elements.get('toast').textContent,/已断开连接/);
  assert.equal(h.app.state.phase,'idle');
});

test('incomplete 200 status cannot show green or replace cached jobs',async()=>{
  const h=await setup();h.configure(false);h.fetchImpl=async url=>url.endsWith('/api/status')?result({}):h.good(url);
  await h.app.refresh(true);
  assert.equal(h.app.state.connected,false);
  assert.equal(h.app.state.jobs[0].title,'Existing');
});
