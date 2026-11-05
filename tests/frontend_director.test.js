'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const key='cutflow.director.v1',plain=x=>JSON.parse(JSON.stringify(x));
const uuid='10000000-0000-4000-8000-000000000001';
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {resolve,promise};}
function setup(storage=new Map()){
  const elements=new Map(),listeners=new Map(),timers=new Map(),calls=[],created=[],blobs=[];let serial=0;
  class El{constructor(id){this.id=id;this.value='';this.checked=false;this.hidden=false;this.disabled=false;this.textContent='';this.innerHTML='';this.dataset={};this.events={};this.parentElement=null;}addEventListener(e,fn){this.events[e]=fn;}appendChild(el){el.parentElement=this;}insertBefore(el){el.parentElement=this;}closest(){return elements.get('layout');}querySelectorAll(){return [];}focus(){}scrollIntoView(){}pause(){}load(){}removeAttribute(){}click(){this.clicked=true;return this.onclick?.();}}
  class MediaURL extends URL{static createObjectURL(value){blobs.push(value);return 'blob:cutflow/'+blobs.length;}static revokeObjectURL(){}}
  const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');for(const [,id]of html.matchAll(/id="([^"]+)"/g))elements.set(id,new El(id));elements.set('layout',new El('layout'));
  for(const [id,v]of Object.entries({directorMode:'auto',directorDuration:'30',directorAspect:'9:16'}))elements.get(id).value=v;elements.get('directorSubtitles').checked=true;elements.get('directorMusic').checked=true;
  const state={base:'http://127.0.0.1:8767',epoch:1,token:'test',connected:false,config:{director_supported:true}},creator={uploading:false},h={state,creator,elements,timers,calls,created,blobs,storage,selection:[]};
  h.impl=async(route,opts={})=>{if(route==='/api/productions'&&!opts.method)return {productions:[]};if(!opts.method)throw Object.assign(new Error('not found'),{httpStatus:404});return {production:{id:JSON.parse(opts.body).request_id,status:'queued',stage:'preflight',events:[],artifacts:[]}};};
  const ctx=vm.createContext({state,creator,console,Date,Map,Set,JSON,Array,Number,Object,String,RegExp,URL:MediaURL,localPage:true,location:{origin:state.base},
    $:s=>elements.get(s.slice(1)),esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),date:s=>String(s||''),
    document:{addEventListener:(e,fn)=>listeners.set(e,fn),createElement:tag=>{const element=new El(tag);created.push(element);return element;}},
    creatorSelection:()=>h.selection,creatorMaterialReadiness:()=> '素材尚未同步',creatorInputLocked:()=>false,
    connectionContext:()=>({...state}),currentContext:c=>c.base===state.base&&c.epoch===state.epoch,
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},crypto:{randomUUID:()=> '10000000-0000-4000-8000-'+String(++serial).padStart(12,'0')},
    setTimeout:(fn,ms)=>{const id=timers.size+1;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),canStreamMedia:()=>h.streaming===true,setMediaSource(){},
    api:async(route,opts={},format,scope)=>{calls.push({route,opts,body:opts.body?JSON.parse(opts.body):null,format});return h.impl(route,opts,format,scope);}
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8').split('\n').find(line=>line.startsWith('function friendlySourceName(')),ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/director.js'),'utf8')+'\nglobalThis.app={director,submitDirector,refreshDirector,directorSync,acceptDirector,directorArtifactPath,directorPlanHtml,directorIssues,renderDirector,setDirectorActive,checkDirector,loadDirector,saveDirector,downloadDirector};',ctx);
  h.app=ctx.app;state.connected=true;h.app.director.lastRead=Date.now();h.field=(id,value)=>{elements.get(id).value=value;elements.get(id).events.input?.();};return h;
}
test('one submit persists UUID/body before network returns and double clicks do not duplicate',async()=>{
  const h=setup(),wait=deferred();h.selection=[{source:'视频一.mp4',ready:true}];h.field('directorGoal','整理现场的一个发现');h.impl=()=>wait.promise;
  const submitting=h.app.submitDirector();await h.app.submitDirector();const saved=JSON.parse(h.storage.get(key));assert.equal(saved.pending.id,uuid);assert.deepEqual(saved.pending.body.sources,['视频一.mp4']);assert.equal(saved.pending.body.goal,'整理现场的一个发现');assert.equal(h.calls.length,1);
  wait.resolve({production:{id:uuid,status:'queued',stage:'preflight',events:[]}});await submitting;assert.equal(h.app.director.pending,null);assert.equal(h.app.director.records.length,1);
});
test('brand preference is saved and sent with the one production request',async()=>{
  const h=setup();h.field('directorBrandUsage','required');await h.app.submitDirector();
  assert.equal(h.calls.find(c=>c.opts.method==='POST').body.options.brand_usage,'required');
  const restored=setup(h.storage);restored.app.loadDirector();
  assert.equal(restored.elements.get('directorBrandUsage').value,'required');
});
test('only actually cited brand facts appear, and screenshot is named as a real page',()=>{
  const h=setup();const text=h.app.directorPlanHtml({theme:'托管入口',scenes:[{kind:'brand_screenshot',brand_asset:{name:'托管问答首页'},evidence_ids:['B0-0','F2'],spoken_text:'打开托管问答',source_in:0,source_out:8}],brand_facts:[{id:'F2',text:'提供问答入口',source:'本机页面 <script>'},{id:'F9',text:'未引用内容'}]});
  assert.match(text,/本片采用的企业知识/);assert.match(text,/托管问答首页/);assert.match(text,/提供问答入口/);
  assert.doesNotMatch(text,/未引用内容/);assert.match(text,/&lt;script&gt;/);
});
test('missing receipt is sent once and blank goal receives the documented default',async()=>{
  const h=setup();await h.app.submitDirector();assert.equal(h.calls.filter(c=>c.opts.method==='POST').length,1);const body=h.calls.find(c=>c.opts.method==='POST').body;assert.match(body.goal,/素材/);assert.deepEqual(body.sources,[]);assert.equal(body.options.subtitles,true);assert.equal(body.options.music,true);
});
test('uncertain submission survives reload and GET recovery does not resubmit',async()=>{
  const h=setup();h.impl=async()=>{throw Object.assign(new Error('timeout'),{uncertain:true});};await h.app.submitDirector();assert.ok(h.app.director.pending);
  const next=setup(h.storage);next.impl=async()=>({production:{id:uuid,status:'running',stage:'planning',events:[]}});await next.app.submitDirector();assert.equal(next.calls.length,1);assert.equal(next.calls[0].route,'/api/productions/'+uuid);assert.equal(next.calls[0].opts.method,undefined);assert.equal(next.app.director.pending,null);
});
test('retry of missing receipt reuses identical body even after editing the current draft',async()=>{
  const h=setup();h.field('directorGoal','第一版目标');h.impl=async()=>{throw Object.assign(new Error('timeout'),{uncertain:true});};await h.app.submitDirector();const body=plain(h.app.director.pending.body);h.field('directorGoal','尚未提交的新目标');h.calls.length=0;
  h.impl=async(route,opts)=>{if(!opts.method)throw Object.assign(new Error('missing'),{httpStatus:404});return {production:{id:uuid,status:'queued',stage:'preflight',events:[]}};};await h.app.submitDirector();assert.deepEqual(h.calls.find(c=>c.opts.method==='POST').body,body);assert.equal(h.elements.get('directorGoal').value,'尚未提交的新目标');
});
test('polling never rewrites goal/settings or posts, and unchanged detail is not repainted',async()=>{
  const h=setup();h.field('directorGoal','我正在输入');const r={id:uuid,status:'running',stage:'planning',events:[{stage:'planning',message:'选择主题'}]};h.impl=async route=>route==='/api/productions'?{productions:[r]}:{production:r};await h.app.refreshDirector(true);const detail=h.elements.get('directorTaskDetail').innerHTML;h.elements.get('directorTaskDetail').innerHTML=detail+'<!-- retained focus -->';await h.app.refreshDirector(true);
  assert.equal(h.elements.get('directorGoal').value,'我正在输入');assert.ok(h.elements.get('directorTaskDetail').innerHTML.endsWith('<!-- retained focus -->'));assert.ok(h.calls.every(c=>!c.opts.method));
});
test('unsynced selection blocks submission before any request',async()=>{const h=setup();h.selection=[{source:'waiting.mp4',ready:false}];await h.app.submitDirector();assert.equal(h.calls.length,0);assert.equal(h.app.director.pending,null);});
test('revision is a new immutable request, not a patch to the original',async()=>{
  const h=setup();h.app.director.records=[{id:'20000000-0000-4000-8000-000000000002',status:'completed'}];h.app.director.selected=h.app.director.records[0].id;h.field('directorRevision','保留更多现场原声');await h.app.submitDirector('revise');const sent=h.calls.find(c=>c.opts.method==='POST');assert.match(sent.route,/\/revise$/);assert.equal(sent.body.instruction,'保留更多现场原声');assert.equal(sent.body.request_id,uuid);assert.ok(h.app.director.records.some(r=>r.id.startsWith('2000')));
});
test('blocked record shows all issues and cannot expose delivery as success',()=>{
  const h=setup();h.app.acceptDirector({id:uuid,status:'blocked',stage:'preflight',error:{message:'服务不可用',details:['视觉服务未启动']},preflight:{blockers:[{message:'存储不足',solution:'扩容或释放空间'}]},artifacts:[{kind:'video',url:'/api/productions/'+uuid+'/video'}]});
  const text=h.elements.get('directorTaskDetail').innerHTML;assert.match(text,/服务不可用/);assert.match(text,/视觉服务未启动/);assert.match(text,/存储不足/);assert.equal(h.elements.get('directorMedia').hidden,true);assert.doesNotMatch(text,/成片已就绪/);
});
test('artifact addresses are restricted to the selected authenticated production',()=>{
  const h=setup(),r={id:uuid,status:'completed'};assert.equal(h.app.directorArtifactPath(r,{url:'/api/productions/'+uuid+'/video'}),'/api/productions/'+uuid+'/video');for(const url of ['https://evil.example/file','/api/productions/other/video','/api/productions/'+uuid+'/artifacts/../../private','javascript:alert(1)'])assert.equal(h.app.directorArtifactPath(r,{url}),null);
});
test('content scheme renders escaped source evidence and actual source times',()=>{
  const h=setup();const html=h.app.directorPlanHtml({theme:'<script>fake</script>',viewpoint:'观点',audience_value:'方法',actual_script:'原话',scenes:[{role:'展开',text:'原话',source:'现场.mp4',source_in:9.5,source_out:18}]});assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);assert.match(html,/0:09–0:18/);
});
test('service switch never restores a pending request from another workspace',()=>{
  const storage=new Map([[key,JSON.stringify({base:'http://another',pending:{id:uuid,path:'/api/productions',body:{request_id:uuid}},fields:{directorGoal:'other secret'}})]]);const h=setup(storage);assert.equal(h.app.director.pending,null);assert.equal(h.elements.get('directorGoal').value,'');
});
test('failed retry cap stops exposing another retry control',()=>{const h=setup();h.app.acceptDirector({id:uuid,status:'failed',stage:'quality',attempts:3,error:{message:'检查未通过'}});assert.doesNotMatch(h.elements.get('directorTaskDetail').innerHTML,/data-director-retry/);});
test('actual service scene schema shows structure, source names and visual grounding',()=>{
  const h=setup();const html=h.app.directorPlanHtml({theme:'主题',structure:{opening:'提出问题',development:'现场对照',ending:'回应问题'},scenes:[{purpose:'现场细节',spoken_text:'真实原话',source_index:0,source_in:2,source_out:6,visual_description:'门口步行',evidence_ids:['V0-0','S0-1'],relation:'时间推进'}]},['library/走访.mp4']);
  for(const value of ['开头：提出问题','展开：现场对照','结尾：回应问题','真实原话','走访.mp4','门口步行','时间推进'])assert.ok(html.includes(value),value);
  assert.doesNotMatch(html,/V0-0|S0-1|素材索引/);
});
test('reference-only structure uses existing scene meaning and keeps the stored plan untouched',()=>{
  const h=setup(),plan={theme:'现场观察',structure:{opening:['T2-0-3'],development:['V2-1','T2-1-4'],ending:['T2-2-5']},evidence:['V2-0','T2-0-3'],
    scenes:[{purpose:'引出现场问题',source_index:0,source_in:1,source_out:4,evidence_ids:['V2-0']},
      {purpose:'展示细节',relation:'用现场细节解释问题',source_index:0,source_in:5,source_out:8,evidence_ids:['T2-1-4']},
      {purpose:'回应开头的问题',source_index:0,source_in:9,source_out:12,evidence_ids:['T2-2-5']}]};
  const before=JSON.stringify(plan),html=h.app.directorPlanHtml(plan,['工作台导入/library/private/video(2)--77f7ac56d19284f5.mp4']);
  for(const value of ['开头：引出现场问题','展开：用现场细节解释问题','结尾：回应开头的问题','video(2).mp4','0:01–0:04'])assert.ok(html.includes(value),value);
  assert.doesNotMatch(html,/T2-0-3|V2-1|T2-1-4|T2-2-5|V2-0|77f7ac56d19284f5|工作台导入|素材索引/);
  assert.equal(JSON.stringify(plan),before);
});
test('natural-language structure stays intact and source labels reuse normal sanitization',()=>{
  const h=setup(),structure={opening:'说明为什么要去现场',development:'先观察，再讨论',ending:'不替未知结果作保证'};
  const html=h.app.directorPlanHtml({structure,scenes:[{source:'资料\\<现场>--abcdef0123456789.mp4',source_in:3,source_out:7,evidence_id:'V3-0',evidence_ids:['V3-0']}]});
  for(const text of Object.values(structure))assert.ok(html.includes(text));
  assert.match(html,/&lt;现场&gt;\.mp4 · 0:03–0:07/);assert.doesNotMatch(html,/V3-0|abcdef0123456789|<现场>/);
});
test('bare source evidence identifiers never substitute for a readable material name',()=>{
  const h=setup(),html=h.app.directorPlanHtml({structure:['T0-0-1','V0-1'],scenes:[{source:'V0-1',evidence_id:'V0-1',source_index:0,source_in:2,source_out:3}]},['library/真实素材--aabbccddeeff1122.mp4']);
  assert.match(html,/真实素材\.mp4 · 0:02–0:03/);assert.doesNotMatch(html,/T0-0-1|V0-1|aabbccddeeff1122|开头：undefined/);
});
test('blocked work with a saved plan can be revised without pretending video exists',async()=>{
  const h=setup();h.app.acceptDirector({id:'20000000-0000-4000-8000-000000000002',status:'blocked',stage:'planning',can_revise:true,content_plan:{theme:'旧主题'},error:{message:'画面不足'}});h.field('directorRevision','缩小主题，只保留现场问题');h.app.directorSync();
  assert.equal(h.elements.get('directorRevisionPanel').hidden,false);assert.equal(h.elements.get('directorMedia').hidden,true);assert.equal(h.elements.get('directorRevise').disabled,false);await h.app.submitDirector('revise');assert.ok(h.calls.some(c=>c.route.endsWith('/revise')));
});
test('unadvertised new service does not poll on routine legacy refresh',()=>{const h=setup();h.state.config.director_supported=false;h.app.director.lastRead=0;h.app.directorSync();assert.equal(h.calls.length,0);assert.equal(h.elements.get('directorStart').disabled,true);assert.equal(h.elements.get('directorCheck').disabled,true);});
test('second click after acknowledged submit reuses work rather than creating another UUID',async()=>{
  const h=setup();await h.app.submitDirector();const r=plain(h.app.director.records[0]);h.calls.length=0;h.impl=async route=>route==='/api/productions'?{productions:[r]}:{production:r};await h.app.submitDirector();assert.ok(h.calls.every(c=>!c.opts.method));assert.equal(h.app.director.selected,r.id);
  const restored=setup(h.storage);restored.impl=h.impl;await restored.app.submitDirector();assert.ok(restored.calls.every(c=>!c.opts.method));
});
test('editing the goal after acknowledged submit creates an intentional new request',async()=>{
  const h=setup();await h.app.submitDirector();h.calls.length=0;h.field('directorGoal','新目标：说明现场细节');await h.app.submitDirector();const post=h.calls.find(c=>c.opts.method==='POST');assert.ok(post);assert.notEqual(post.body.request_id,uuid);
});
test('buttons and direct handler cannot submit against an old backend',async()=>{const h=setup();h.state.config.director_supported=false;h.app.directorSync();await h.app.submitDirector();await h.app.checkDirector();assert.equal(h.calls.length,0);assert.equal(h.elements.get('directorStart').disabled,true);assert.match(h.elements.get('directorMessage').textContent,/原有制作方式/);});
test('completed label alone never exposes incomplete or unchecked delivery',()=>{
  const h=setup();h.app.acceptDirector({id:uuid,status:'completed',stage:'completed',artifacts:[{kind:'video',url:'/api/productions/'+uuid+'/video'}],quality:{passed:false}});
  assert.match(h.elements.get('directorTaskDetail').innerHTML,/交付信息待核对/);assert.equal(h.elements.get('directorMedia').hidden,true);assert.doesNotMatch(h.elements.get('directorTaskDetail').innerHTML,/成片已就绪/);
});
test('server explicit retry and revision restrictions are obeyed',async()=>{
  const h=setup();h.app.acceptDirector({id:uuid,status:'failed',stage:'quality',attempts:1,can_retry:false,can_revise:false,error:{message:'需要恢复服务'}});h.field('directorRevision','再剪一次');h.app.directorSync();assert.equal(h.elements.get('directorRevisionPanel').hidden,true);assert.doesNotMatch(h.elements.get('directorTaskDetail').innerHTML,/data-director-retry/);await h.app.submitDirector('retry');await h.app.submitDirector('revise');assert.equal(h.calls.length,0);
});
test('a saved selected work older than the first list page remains recoverable',async()=>{
  const h=setup();h.app.director.selected=uuid;const old={id:uuid,status:'running',stage:'planning'},recent={id:'20000000-0000-4000-8000-000000000002',status:'queued',stage:'queued'};h.impl=async route=>route==='/api/productions'?{productions:[recent]}:{production:old};await h.app.refreshDirector(true);assert.equal(h.app.director.selected,uuid);assert.ok(h.app.director.records.some(r=>r.id===uuid));
});
test('failed planning retains completed prerequisites and marks the failing stage',()=>{
  const h=setup();h.app.acceptDirector({id:uuid,status:'failed',stage:'failed',events:[{stage:'preflight'},{stage:'understanding'},{stage:'planning'},{stage:'failed'}],error:{message:'选题尚未通过'}});
  const html=h.elements.get('directorTaskDetail').innerHTML;
  assert.match(html,/<li class="done">检查制作条件<b>已完成/);
  assert.match(html,/<li class="done">理解素材<b>已完成/);
  assert.match(html,/<li class="issue">策划内容<b>此环节未完成/);
  assert.match(html,/<li class="">脚本与分镜<b>等待前序步骤/);
});
test('blocked preflight never marks prerequisites or downstream work complete',()=>{
  const h=setup();h.app.acceptDirector({id:uuid,status:'blocked',stage:'blocked',events:[{stage:'preflight'},{stage:'blocked'}],error:{message:'服务离线'}});
  const html=h.elements.get('directorTaskDetail').innerHTML;
  assert.match(html,/<li class="issue">检查制作条件<b>需要补充条件/);
  assert.doesNotMatch(html,/<li class="done">/);
});
test('voice preparation belongs to sound/rendering and its failure preserves previous steps',()=>{
  const h=setup(),events=[{stage:'scripting'},{stage:'editing'},{stage:'preparing_voice',message:'准备独白与声音'}];
  h.app.acceptDirector({id:uuid,status:'running',stage:'preparing_voice',stage_label:'准备独白与声音',events});
  assert.match(h.elements.get('directorTaskDetail').innerHTML,/<li class="active">声音与渲染<b>准备独白与声音/);
  assert.match(h.elements.get('directorTaskDetail').innerHTML,/<li class="">选材与剪辑<b>等待声音确定后完成剪辑/);
  assert.doesNotMatch(h.elements.get('directorTaskDetail').innerHTML,/preparing_voice/);
  h.app.acceptDirector({id:uuid,status:'failed',stage:'failed',events:[...events,{stage:'failed'}],error:{message:'独白准备失败'}});
  assert.match(h.elements.get('directorTaskDetail').innerHTML,/<li class="issue">声音与渲染<b>此环节未完成/);
  assert.match(h.elements.get('directorTaskDetail').innerHTML,/<li class="done">脚本与分镜<b>已完成/);
});
test('queued retry and repair preserve truthful progress without claiming a passed quality check',()=>{
  const h=setup();h.app.acceptDirector({id:uuid,status:'queued',stage:'queued',events:[{stage:'preflight'},{stage:'understanding'},{stage:'planning'},{stage:'queued'}],error:{message:'稍后重试'}});
  assert.match(h.elements.get('directorTaskDetail').innerHTML,/<li class="paused">策划内容<b>等待重试/);
  h.app.acceptDirector({id:uuid,status:'failed',stage:'failed',events:[{stage:'rendering'},{stage:'quality'},{stage:'repair'},{stage:'failed'}],error:{message:'修复未通过'}});
  const html=h.elements.get('directorTaskDetail').innerHTML;
  assert.match(html,/<li class="done">声音与渲染<b>已完成/);
  assert.match(html,/<li class="issue">检查成片<b>此环节未完成/);
  assert.doesNotMatch(html,/<li class="done">检查成片/);
});
function completedRecord(){return {id:uuid,status:'completed',stage:'completed',quality:{passed:true},can_revise:true,artifacts:['video','subtitles','cover','copy','record','package'].map(kind=>({kind,name:kind+'.file',url:'/api/productions/'+uuid+(kind==='video'?'/video':'/artifacts/'+kind)}))};}
test('all six delivered artifacts appear and use authenticated Blob downloads',async()=>{
  const h=setup();h.streaming=true;h.app.acceptDirector(completedRecord());h.impl=async()=>new Blob(['actual response body']);
  const html=h.elements.get('directorDownloads').innerHTML;assert.equal(h.elements.get('directorMedia').hidden,false);
  for(const kind of ['video','subtitles','cover','copy','record','package']){
    assert.ok(html.includes('data-director-download="'+kind+'"'));await h.app.downloadDirector(kind);
    const call=h.calls.at(-1),link=h.created.at(-1);assert.equal(call.format,'blob');assert.equal(call.opts.method,undefined);assert.match(call.route,new RegExp('^/api/productions/'+uuid+'/'));assert.equal(link.download,kind+'.file');assert.equal(link.clicked,true);assert.match(link.href,/^blob:cutflow\//);
  }
  assert.equal(h.calls.length,6);assert.equal(h.blobs.length,6);
});
test('missing any deliverable kind keeps all downloads closed',async()=>{
  for(const missing of ['video','subtitles','cover','copy','record','package']){
    const h=setup(),record=completedRecord();record.artifacts=record.artifacts.filter(item=>item.kind!==missing);h.app.acceptDirector(record);await h.app.downloadDirector('video');
    assert.equal(h.elements.get('directorMedia').hidden,true,missing);assert.equal(h.calls.length,0,missing);
  }
});
test('one sentence revision survives uncertain response and a repeated click reuses its UUID',async()=>{
  const h=setup(),original=completedRecord();original.id='20000000-0000-4000-8000-000000000002';h.app.director.records=[original];h.app.director.selected=original.id;h.field('directorRevision','开头更直接，保留现场原声');
  h.impl=async(route,opts)=>{if(!opts.method)throw Object.assign(new Error('missing'),{httpStatus:404});throw new Error('timeout');};await h.app.submitDirector('revise');const saved=JSON.parse(h.storage.get(key)).pending;
  assert.equal(saved.kind,'revise');assert.equal(saved.body.instruction,'开头更直接，保留现场原声');assert.equal(saved.parent_id,original.id);
  h.calls.length=0;h.impl=async()=>({production:{id:saved.id,parent_id:original.id,status:'queued',stage:'preflight',events:[]}});await h.app.submitDirector('revise');
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].opts.method,undefined);assert.equal(h.app.director.pending,null);assert.ok(h.app.director.records.includes(original));
});
