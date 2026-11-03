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

async function setup(withImports=false){
  const elements=new Map(),listeners=new Map(),intervals=[],calls=[],created=[],uploads=[];let timer=0;
  class Element{
    constructor(id=''){this.id=id;this.className='';this.textContent='';this.innerHTML='';this.hidden=false;this.disabled=false;this.open=false;this.value='';this.dataset={};this.style={};this.listeners={};this.currentTime=0;this.classList={contains:name=>this.className.split(' ').includes(name),toggle:(name,on)=>{const values=new Set(this.className.split(' ').filter(Boolean));on?values.add(name):values.delete(name);this.className=[...values].join(' ');},add:name=>this.classList.toggle(name,true),remove:name=>this.classList.toggle(name,false)};}
    addEventListener(name,fn){this.listeners[name]=fn;}
    removeEventListener(name,fn){if(this.listeners[name]===fn)delete this.listeners[name];}
    before(el){if(el.id)elements.set(el.id,el);}
    after(el){if(el.id)elements.set(el.id,el);}
    pause(){}
    removeAttribute(name){delete this[name];}
    showModal(){this.open=true;}
    close(){const was=this.open;this.open=false;if(was&&this.listeners.close)this.listeners.close();}
    click(){this.clicked=true;if(this.onclick)this.onclick();}
  }
  for(const [,id]of html.matchAll(/id="([^"]+)"/g))elements.set(id,new Element(id));
  const access=new Element();
  const document={hidden:false,title:'',querySelector:selector=>selector==='.local-access'?access:selector.startsWith('#')?elements.get(selector.slice(1))||null:null,querySelectorAll:()=>[],createElement:()=>{const el=new Element();created.push(el);return el;},addEventListener:(name,fn)=>listeners.set(name,fn)};
  const harness={elements,document,listeners,intervals,calls,created,uploads,fetchImpl:async()=>result({},404)};
  class FakeXHR{constructor(){this.upload={};this.headers={};uploads.push(this);}open(method,url){this.method=method;this.url=url;}setRequestHeader(key,value){this.headers[key]=value;}send(body){this.body=body;}reply(body,status=200){this.response=body;this.status=status;this.onload();}}
  const context=vm.createContext({document,location:{hostname:'127.0.0.1',origin:'http://127.0.0.1:8767',hash:'',pathname:'/'},history:{replaceState(){}},URL,URLSearchParams,AbortController,AbortSignal,Blob,Date,console,XMLHttpRequest:FakeXHR,crypto:{randomUUID:()=> 'request-id'},setTimeout:()=>++timer,clearTimeout(){},setInterval:(fn,ms)=>{intervals.push({fn,ms});return ++timer;},fetch:(url,options={})=>{calls.push({url,options});return harness.fetchImpl(url,options);}});
  vm.runInContext(source+'\nglobalThis.app={state,api,refresh,review,reportError,setConnection,nextEpoch,connectionContext,probeIfVisible,toast,updateReviewActions,renderJobs,refreshTechnicalTests,selectJobFilter,previewSource,sourcePreview,safeVideoFilename,downloadReviewedVideo,newJob,presets,updatePresetHelp,renderAssets};',context);
  if(withImports){vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/local-imports.js'),'utf8')+'\nObject.assign(app,{localImports,refreshLocalLibrary,localRequest,startLocalImport,selectImportFile,renderLocalImports,updateLocalImportAccess});',context);}
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

const diagnosticJob=()=>({id:'qa1',title:'old-colorbar-render-name',source:'synthetic-qa.mp4',job_kind:'technical_test',diagnostic_reason:'合成彩条与测试音，仅验证链路。',status:'review',has_video:false,plan:{duration:3,clips:[]},result:{qc:{passed:true}}});

test('technical tests are excluded from production overview and counts',async()=>{
  const h=await setup();h.configure();
  h.fetchImpl=async url=>url.endsWith('/api/status')?result({worker_running:true,execution_target:'remote_gpu',counts:{},technical_test_count:2}):url.endsWith('/api/jobs')?result({jobs:[diagnosticJob()]}):h.good(url);
  await h.app.refresh(true);
  assert.equal(h.app.state.jobs.length,0);
  assert.equal(h.elements.get('statReview').textContent,0);
  assert.equal(h.elements.get('jobBadge').textContent,0);
  assert.equal(h.elements.get('technicalTestCount').textContent,2);
  assert.match(h.elements.get('recentJobs').innerHTML,/还没有正式成片。/);
  assert.doesNotMatch(h.elements.get('recentJobs').innerHTML,/old-colorbar-render-name/);
  assert.equal(h.calls.filter(call=>call.url.includes('kind=technical_test')).length,0);
});

test('technical filter loads a separate read-only list and never labels tests as waiting editorial review',async()=>{
  const h=await setup();h.configure();h.app.state.jobs=[];h.app.state.technicalTestCount=2;h.app.renderJobs();
  h.elements.get('statReview').textContent=0;
  h.fetchImpl=async url=>{assert.ok(url.endsWith('/api/jobs?kind=technical_test'));return result({jobs:[diagnosticJob()]});};
  await h.app.selectJobFilter('technical_test');
  assert.equal(h.app.state.jobs.length,0);assert.equal(h.app.state.technicalTests.length,1);
  assert.match(h.elements.get('jobsList').innerHTML,/技术自测（非成片）/);
  assert.match(h.elements.get('jobsList').innerHTML,/自测通过/);
  assert.doesNotMatch(h.elements.get('jobsList').innerHTML,/等待审核/);
  assert.doesNotMatch(h.elements.get('recentJobs').innerHTML,/彩条/);
  assert.equal(h.elements.get('statReview').textContent,0);
  await h.app.selectJobFilter('all');
  assert.match(h.elements.get('jobsList').innerHTML,/还没有正式成片。/);
});

test('diagnostic detail prominently identifies a test and blocks approve and publish even when invoked directly',async()=>{
  const h=await setup();h.configure();h.fetchImpl=async()=>result({job:diagnosticJob()});
  await h.app.review('qa1');
  assert.equal(h.elements.get('technicalTestNotice').hidden,false);
  assert.match(html,/测试图案，仅用于链路检查，不是商业视频/);
  assert.equal(h.elements.get('reviewTitle').textContent,'GPU 链路自测（彩条测试片）');
  assert.equal(h.elements.get('reviewActions').hidden,true);
  assert.equal(h.elements.get('reviewActions').style.display,'none');
  assert.equal(h.elements.get('approveButton').disabled,true);assert.equal(h.elements.get('publishButton').disabled,true);
  assert.doesNotMatch(h.elements.get('reviewDetails').innerHTML,/等待审核|预期成片/);
  assert.match(h.elements.get('reviewDetails').innerHTML,/synthetic-qa.mp4/);
  h.calls.length=0;await h.elements.get('approveButton').onclick();
  h.app.state.selected.status='approved';await h.elements.get('publishButton').onclick();assert.equal(h.calls.length,0);
});

test('production detail restores ordinary review controls after a technical test',async()=>{
  const h=await setup();h.configure();h.fetchImpl=async()=>result({job:diagnosticJob()});await h.app.review('qa1');
  h.fetchImpl=async()=>result({job:{id:'real1',title:'真实访谈',source:'interview.mp4',job_kind:'production',status:'review',has_video:false}});
  await h.app.review('real1');
  assert.equal(h.elements.get('technicalTestNotice').hidden,true);
  assert.equal(h.elements.get('reviewActions').hidden,false);assert.equal(h.elements.get('approveButton').disabled,false);
  assert.equal(h.elements.get('reviewTitle').textContent,'真实访谈');
});

test('late technical query cannot repopulate records after disconnect',async()=>{
  const h=await setup();h.configure();const hold=pending();h.fetchImpl=()=>hold.promise;
  const load=h.app.selectJobFilter('technical_test');h.elements.get('disconnectButton').onclick();
  hold.resolve(result({jobs:[diagnosticJob()]}));await load;
  assert.equal(h.app.state.technicalTests.length,0);assert.equal(h.app.state.technicalTestCount,0);
});

test('refresh closes an open production review when it is reclassified and never reloads its video',async()=>{
  const h=await setup();h.configure();
  const original={id:'qa1',title:'Old production label',source:'source.mp4',job_kind:'production',status:'review',has_video:true,video_url:'/api/jobs/qa1/video'};
  h.fetchImpl=async url=>url.endsWith('/video')?result({}):result({job:original});
  await h.app.review('qa1');
  assert.equal(h.elements.get('reviewDialog').open,true);
  const blob=h.app.state.videoBlob;
  assert.ok(blob);
  h.calls.length=0;
  h.fetchImpl=async url=>url.endsWith('/api/jobs')?result({jobs:[{...original,title:'Latest production fields',diagnostic_reason:null}]}):h.good(url);
  await h.app.refresh(true);
  assert.equal(h.app.state.selected.title,'Latest production fields');
  assert.equal(h.app.state.selected.job_kind,'production');
  assert.equal(h.app.state.videoBlob,blob);
  assert.equal(h.calls.length,4);
  h.calls.length=0;
  h.fetchImpl=async url=>url.endsWith('/api/jobs')?result({jobs:[]}):url.endsWith('/api/status')?result({worker_running:true,execution_target:'remote_gpu',counts:{},technical_test_count:1}):h.good(url);
  await h.app.refresh(true);
  assert.equal(h.elements.get('reviewDialog').open,false);
  assert.equal(h.app.state.selected,null);
  assert.equal(h.app.state.videoBlob,null);
  assert.equal(h.elements.get('reviewVideo').src,undefined);
  assert.equal(h.elements.get('approveButton').disabled,true);
  assert.equal(h.elements.get('publishButton').disabled,true);
  assert.match(h.elements.get('toast').textContent,/任务列表已更新，请重新打开记录查看/);
  assert.equal(h.app.state.connected,true);
  assert.equal(h.calls.length,4);
  assert.ok(h.calls.every(call=>!call.url.endsWith('/video')&&!call.url.endsWith('/api/jobs/qa1')));
});

test('preserve preset keeps all time and forces original aspect even if the field is changed',async()=>{
  const h=await setup();h.configure();h.app.state.assets[0].suggested_preset='preserve';h.app.newJob('source.mp4');
  assert.equal(h.elements.get('editPreset').value,'preserve');assert.equal(h.elements.get('aspect').disabled,true);
  assert.match(h.elements.get('presetHelp').textContent,/不裁剪停顿/);h.elements.get('aspect').value='9:16';
  h.fetchImpl=async(url,options)=>options.method==='POST'?result({}):h.good(url);
  await h.elements.get('jobForm').onsubmit({preventDefault(){}});
  const body=JSON.parse(h.calls.find(call=>call.options.method==='POST').options.body);
  assert.equal(body.settings.remove_silence,false);assert.equal(body.settings.normalize_audio,true);assert.equal(body.settings.target_aspect,'original');
});

test('local streaming previews source immediately without fetching a complete blob',async()=>{
  const h=await setup();h.configure();h.app.state.mediaStreaming=true;
  await h.app.previewSource('课程/第 1 课.mp4');
  assert.equal(h.calls.length,0);assert.equal(h.app.sourcePreview.blob,null);
  const video=h.elements.get('sourceVideo');assert.match(video.src,/\/api\/assets\/video\?source=/);
  assert.match(h.elements.get('sourceVideoStatus').textContent,/正在读取/);
  video.currentSrc=video.src;video.listeners.loadedmetadata();assert.match(h.elements.get('sourceVideoStatus').textContent,/已加载/);
  h.elements.get('sourceDialog').close();assert.equal(video.src,undefined);assert.equal(h.app.sourcePreview.source,null);
});

test('streaming source events from a prior video cannot overwrite a newer source or a closed dialog',async()=>{
  const h=await setup();h.configure();h.app.state.mediaStreaming=true;await h.app.previewSource('one.mp4');
  const video=h.elements.get('sourceVideo'),late=video.listeners.error;
  await h.app.previewSource('two.mp4');late();assert.doesNotMatch(h.elements.get('sourceVideoStatus').textContent,/无法播放/);
  const afterClose=video.listeners.canplay;h.elements.get('sourceDialog').close();h.elements.get('sourceVideoStatus').textContent='Closed';afterClose();assert.equal(h.elements.get('sourceVideoStatus').textContent,'Closed');
});

test('ordinary streamed output downloads safely without approval or any mutation and keeps play position on detail refresh',async()=>{
  const h=await setup();h.configure();h.app.state.mediaStreaming=true;
  const job={id:'j1',title:'品牌:样片/最终?',job_kind:'production',status:'review',has_video:true,video_url:'/api/jobs/j1/video'};
  h.fetchImpl=async()=>result({job});await h.app.review('j1');
  assert.equal(h.calls.length,1);assert.equal(h.app.state.videoBlob,null);assert.equal(h.elements.get('downloadVideoButton').disabled,false);
  h.elements.get('reviewVideo').currentTime=6.5;const url=h.elements.get('reviewVideo').src;
  h.app.downloadReviewedVideo();const anchor=h.created.find(el=>el.download);assert.equal(anchor.download,'品牌_样片_最终_.mp4');assert.equal(anchor.href,url);
  assert.equal(h.calls.length,1);await h.app.review('j1');assert.equal(h.elements.get('reviewVideo').currentTime,6.5);assert.equal(h.elements.get('reviewVideo').src,url);
  assert.ok(h.calls.every(call=>!call.options.method));h.elements.get('reviewDialog').close();assert.equal(h.app.state.videoSource,null);
});

test('technical videos never expose a finished-video download even through direct handler invocation',async()=>{
  const h=await setup();h.configure();h.app.state.mediaStreaming=true;h.fetchImpl=async()=>result({job:{...diagnosticJob(),has_video:true,video_url:'/api/jobs/qa1/video'}});
  await h.app.review('qa1');assert.equal(h.elements.get('downloadVideoButton').hidden,true);h.app.downloadReviewedVideo();assert.equal(h.created.filter(el=>el.download).length,0);
  assert.match(h.elements.get('reviewHelp').textContent,/不能作为成片下载/);
});

test('retry uses backend can_retry, submits once and never retries a delivery-blocked failure',async()=>{
  const h=await setup();h.configure();h.app.state.selected={id:'j1',status:'failed',can_retry:false};h.app.updateReviewActions();assert.equal(h.elements.get('retryJobButton').hidden,true);
  await h.elements.get('retryJobButton').onclick();assert.equal(h.calls.length,0);
  h.app.state.selected.can_retry=true;h.fetchImpl=async(url,options)=>options.method==='POST'?result({}):h.good(url);await h.elements.get('retryJobButton').onclick();
  const writes=h.calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,1);assert.match(writes[0].url,/\/retry$/);assert.ok(h.calls.every(call=>!call.url.endsWith('/publish')));
});

const library=()=>({inbox:'C:/素材投递箱',imports:[],max_bytes:2147483648});
const receipt=()=>({id:'request-id',name:'品牌样片.mp4',mode:'preserve',size:100,source:'待同步/品牌样片.mp4',remote_source:null,status:'waiting_sync',created_at:1});

test('local import works while GPU is offline and sends the original File object with progress',async()=>{
  const h=await setup(true);h.fetchImpl=async()=>result(library());h.configure(false);await tick();
  const file={name:'品牌样片.mp4',size:100,arrayBuffer(){throw new Error('must never buffer the whole file');}};
  h.app.selectImportFile(file);h.elements.get('importMode').value='preserve';const uploading=h.app.startLocalImport();
  assert.equal(h.uploads.length,1);const xhr=h.uploads[0];assert.equal(xhr.body,file);assert.match(xhr.url,/mode=preserve/);assert.equal(xhr.headers.Authorization,'Bearer '+'a'.repeat(32));
  xhr.upload.onprogress({lengthComputable:true,loaded:50,total:100});assert.equal(h.elements.get('importProgress').value,50);
  h.fetchImpl=async()=>result({...library(),imports:[receipt()]});xhr.reply({import:receipt(),duplicate:false});await uploading;
  assert.equal(h.app.state.connected,false);assert.match(h.elements.get('localImportsList').innerHTML,/已导入本机 · 等待同步/);assert.equal(h.uploads.length,1);
  assert.ok(h.calls.every(call=>!call.url.includes('/api/jobs')));
});

test('an uncertain upload is not replayed, keeps the id for explicit retry, and does not mark GPU disconnected',async()=>{
  const h=await setup(true);h.fetchImpl=async()=>result(library());h.configure();await tick();h.app.selectImportFile({name:'one.mp4',size:100});
  const id=h.app.localImports.requestId;const first=h.app.startLocalImport();h.uploads[0].onerror();await first;
  assert.equal(h.app.localImports.requestId,id);assert.equal(h.uploads.length,1);assert.equal(h.app.state.connected,true);assert.match(h.elements.get('importFeedback').textContent,/不会自动重复上传/);
  const retry=h.app.startLocalImport();assert.equal(new URL(h.uploads[1].url,'http://localhost').searchParams.get('id'),id);h.uploads[1].reply({import:receipt(),duplicate:true});await retry;
});

test('oversized files are rejected before upload and local operations use only fixed same-origin paths',async()=>{
  const h=await setup(true);h.fetchImpl=async()=>result(library());h.configure(false);await tick();h.app.selectImportFile({name:'huge.mp4',size:2147483649});await h.app.startLocalImport();assert.equal(h.uploads.length,0);assert.match(h.elements.get('importFeedback').textContent,/超过单文件上限/);
  h.calls.length=0;h.fetchImpl=async()=>result({opened:true});await h.elements.get('openInboxButton').onclick();assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/local-api/open-folder');assert.equal(h.calls[0].options.body,undefined);
});

test('local receipt maps to an existing production job without automatically creating another job',async()=>{
  const h=await setup(true);h.fetchImpl=async()=>result({...library(),imports:[{...receipt(),status:'synced',remote_source:'source.mp4'}]});h.configure();await tick();
  assert.match(h.elements.get('localImportsList').innerHTML,/等待审核/);assert.match(h.elements.get('localImportsList').innerHTML,/data-import-job="j1"/);assert.ok(h.calls.every(call=>!call.options.method||call.options.method==='GET'));
});

test('native media failure probes one byte once and invalidates green on an actual HTTP 503',async()=>{
  const h=await setup();h.configure();h.app.state.mediaStreaming=true;await h.app.previewSource('one.mp4');h.fetchImpl=async()=>result({detail:'媒体读取暂不可用'},503);
  const video=h.elements.get('sourceVideo');video.currentSrc=video.src;video.listeners.error();video.listeners.error();await tick();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].options.headers.Range,'bytes=0-0');assert.equal(h.app.state.connected,false);assert.ok(h.calls.every(call=>!call.options.method));
});

test('media error verification cancels a successful response body without reading or buffering video',async()=>{
  const h=await setup();h.configure();let cancelled=false;
  h.fetchImpl=async()=>({ok:true,status:200,body:{cancel:async()=>{cancelled=true;}},blob:async()=>{throw new Error('must not buffer video');},json:async()=>{throw new Error('must not parse video');}});
  assert.equal(await h.app.api('/api/assets/video?source=one.mp4',{headers:{Range:'bytes=0-0'}},'headers'),true);assert.equal(cancelled,true);assert.equal(h.app.state.connected,true);
});

test('choosing an invalid replacement file cannot accidentally import the previous selection',async()=>{
  const h=await setup(true);h.fetchImpl=async()=>result(library());h.configure(false);await tick();h.app.selectImportFile({name:'valid.mp4',size:100});h.app.selectImportFile({name:'not-a-video.txt',size:100});await h.app.startLocalImport();
  assert.equal(h.uploads.length,0);assert.equal(h.app.localImports.file,null);assert.equal(h.elements.get('startImportButton').disabled,true);
});

test('MTS files are available in the picker and accepted by client validation',async()=>{
  const h=await setup(true);h.fetchImpl=async()=>result(library());h.configure(false);await tick();
  const file={name:'课程摄影机.MTS',size:1234};h.app.selectImportFile(file);
  assert.equal(h.app.localImports.file,file);assert.equal(h.elements.get('startImportButton').disabled,false);
  assert.match(html,/accept="[^"]*\.mts/);assert.match(html,/M4V、MTS/);
});

test('synced material copy distinguishes automatic processing from an optional new edit version',async()=>{
  const h=await setup();h.configure();h.app.renderAssets();
  assert.match(html,/同步后自动处理；也可预览原片或另建版本/);
  assert.match(html,/用这段素材另建剪辑版本/);
  assert.match(h.elements.get('assetsList').innerHTML,/另建剪辑版本/);
  assert.match(html,/id="newJobButton">＋ 新建剪辑/);
  assert.match(html,/id="newJobButton2">＋ 新建剪辑/);
  assert.equal(h.calls.length,0);
});
