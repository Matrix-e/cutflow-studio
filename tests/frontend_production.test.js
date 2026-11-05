'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const DRAFT='cutflow.production.draft.v1';
const CREATOR_DRAFT='cutflow.creator.draft.v1';
const plain=value=>JSON.parse(JSON.stringify(value));
const pending=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};

function setup({storage=new Map(),localPage=true,voices=true,connected=true,mode='brand_promo'}={}){
  const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
  const elements=new Map(),listeners=new Map(),timers=new Map();
  class Element{
    constructor(id){this.id=id;this.value='';this.textContent='';this.innerHTML='';this.disabled=false;this.hidden=false;this.checked=false;this.dataset={};this.listeners=new Map();const values=new Set();this.classList={add:c=>values.add(c),remove:c=>values.delete(c),contains:c=>values.has(c),toggle:(c,on)=>on?values.add(c):values.delete(c)};}
    addEventListener(name,fn){if(!this.listeners.has(name))this.listeners.set(name,[]);this.listeners.get(name).push(fn);}
    setAttribute(name,value){this[name]=value;}
    closest(){return this.parent||(this.parent=new Element(this.id+'-label'));}
    emit(name){for(const fn of this.listeners.get(name)||[])fn({target:this});}
    focus(){} scrollIntoView(){} click(){if(!this.disabled)return this.onclick?.();}
  }
  for(const [,id]of html.matchAll(/id="([^"]+)"/g))elements.set(id,new Element(id));
  for(const id of ['creatorSearchStatus','creatorSearchMore'])elements.set(id,new Element(id));
  for(const [id,value]of Object.entries({productionSubtype:'salon_promo',productionStyle:'professional',productionAspect:'9:16',productionDuration:'45',productionSourceAudio:'mute'}))elements.get(id).value=value;
  elements.get('productionSubtitles').checked=true;elements.get('productionMusic').checked=true;
  const directory=[{id:'serena',name:'温和女声',language:'zh-CN'},{id:'uncle_fu',name:'沉稳男声',language:'zh-CN'}];
  const state={base:'http://127.0.0.1:8767',token:'test-only',epoch:1,connected,assets:[],jobs:[],config:{creation_supported:true,production:{modes:Object.fromEntries(['edit','vlog','story','brand_promo','narrated_promo','talking_head'].map(mode=>[mode,{ready:true}])),voices:voices?directory:[],script_supported:true}}};
  const h={elements,state,storage,timers,calls:[],directory,localImports:{items:[],maxBytes:2*1024**3}};
  h.apiImpl=async(route,options={})=>{
    if(route==='/api/presenters')return {presenters:[]};
    if(route.startsWith('/api/assembly-plans'))return {assembly:{id:options.body?JSON.parse(options.body).request_id:h.app.storyAssembly.id,status:'queued',stage:'queued'}};
    if(route.startsWith('/api/scripts'))return {script:{id:options.body?JSON.parse(options.body).request_id:h.app.production.scriptId,status:'queued'}};
    return {job:{id:'finished-job',title:'成片',status:'queued'},created:true};
  };
  let serial=0,timerSerial=0;
  const document={querySelector:selector=>elements.get(selector.slice(1))||null,querySelectorAll:()=>[],addEventListener(name,fn){if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push(fn);}};
  const context=vm.createContext({document,state,localPage,localImports:h.localImports,console,Date,URL,Blob,
    $:selector=>document.querySelector(selector),
    esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    connectionContext:()=>({base:state.base,token:state.token,epoch:state.epoch}),currentContext:c=>c.base===state.base&&c.token===state.token&&c.epoch===state.epoch,
    bytes:value=>String(value),assetName:item=>item.name||item.source,statusNames:{queued:'排队中'},
    localImportAvailable:()=>true,showPage(){},renderLocalImports(){},renderJobs(){},refresh:async()=>{},refreshLocalLibrary:async()=>{},
    assetCatalogUsable:()=>state.connected,assetCatalogCurrent:()=>state.connected,
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    crypto:{randomUUID:()=> '30000000-0000-4000-8000-'+String(++serial).padStart(12,'0')},
    api:async(route,options={},format)=>{h.calls.push({route,options,format,body:typeof options.body==='string'?JSON.parse(options.body):options.body});return h.apiImpl(route,options,format);},
    setTimeout:(fn,ms)=>{const id=++timerSerial;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id)
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/asset-browser.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/creator.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/production.js'),'utf8')+'\nglobalThis.productionApp={renderCreatorMaterials,production,creator,writeProductionScript,pollProductionScript,adoptProductionScript,generateProduction,generateCreation,updateProductionAccess,setProductionMode,saveProductionDraft,loadPresenterCatalog,importPresenter,renderPresenterCatalog,selectedPresenterReady};',context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/story-workflow.js'),'utf8')+'\nObject.assign(globalThis.productionApp,{storyAssembly,requestStoryAssembly,pollStoryAssembly,storyPlanReady,updateStoryWorkflow,saveStoryAssembly,generateStoryProduction});',context);
  h.app=context.productionApp;if(mode)h.app.setProductionMode(mode);
  h.field=(id,value)=>{elements.get(id).value=value;elements.get(id).emit('input');};
  h.ready=()=>{state.assets=[{source:'event.mp4',name:'活动片段',size:1024}];h.app.creator.selected=['s:event.mp4'];h.field('creatorBrief','准备一场真实的 AI 沙龙邀请');h.app.updateProductionAccess();};
  h.resolveScript=async(text='欢迎来到这次沙龙，一起讨论真实的问题。')=>{h.apiImpl=async()=>({script:{id:h.app.production.scriptId,status:'succeeded',result:{spoken_script:text,assumptions:['活动信息仍待确认。']}}});await h.app.pollProductionScript();};
  return h;
}

test('references and original work are persisted before script request; new reference invalidates adoption',async()=>{
  const h=setup();h.ready();const parent='b'.repeat(32),reference='c'.repeat(32);
  Object.assign(h.app.production,{parentId:parent,parentTitle:'原作品',revisionKind:'rewrite',referenceIds:[reference]});
  await h.app.writeProductionScript();assert.equal(h.calls[0].body.rewrite_source_id,parent);assert.deepEqual(plain(h.calls[0].body.reference_ids),[reference]);
  await h.resolveScript();assert.equal(h.elements.get('productionAdoptScript').disabled,false);
  h.app.production.referenceIds=[];h.app.updateProductionAccess();assert.equal(h.elements.get('productionAdoptScript').disabled,true);
  const saved=JSON.parse(h.storage.get(DRAFT));assert.equal(saved.parentId,parent);assert.equal(saved.revisionKind,'rewrite');
});

test('new version creates a child with lineage and references without rewriting or retrying its parent',async()=>{
  const h=setup();h.ready();h.app.setProductionMode('brand_promo');h.field('productionScript','为汽车租赁同行准备一场交流，欢迎了解这次沙龙。');
  Object.assign(h.app.production,{parentId:'b'.repeat(32),revisionKind:'rewrite',referenceIds:['c'.repeat(32)]});
  await h.app.generateProduction();const request=h.calls.find(c=>c.route==='/api/creations');
  assert.equal(request.body.parent_job_id,'b'.repeat(32));assert.equal(request.body.revision_kind,'rewrite');assert.deepEqual(plain(request.body.reference_ids),['c'.repeat(32)]);
  assert.ok(!h.calls.some(c=>c.route.includes('/retry')||c.options.method==='PUT'||c.options.method==='PATCH'));
});

test('restoring a new-version draft retains its lineage and references without any submission',()=>{
  const h=setup();h.ready();Object.assign(h.app.production,{parentId:'d'.repeat(32),parentTitle:'被保留的原作品',revisionKind:'presenter',referenceIds:['e'.repeat(32)]});h.app.saveProductionDraft();
  const restored=setup({storage:h.storage});assert.equal(restored.app.production.parentId,'d'.repeat(32));assert.equal(restored.app.production.parentTitle,'被保留的原作品');assert.deepEqual(plain(restored.app.production.referenceIds),['e'.repeat(32)]);assert.equal(restored.calls.length,0);
});

test('script POST is persisted before network resolves; double click submits once',async()=>{
  const h=setup(),response=pending();h.ready();h.field('productionScript','原有独白。');h.apiImpl=()=>response.promise;
  const first=h.app.writeProductionScript();await h.app.writeProductionScript();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].route,'/api/scripts');
  const saved=JSON.parse(h.storage.get(DRAFT));
  assert.equal(saved.scriptBody.request_id,h.app.production.scriptId);assert.equal(saved.uncertain,true);
  assert.equal(saved.fields.productionScript,'原有独白。');
  response.resolve({script:{id:h.app.production.scriptId,status:'queued'}});await first;
  assert.equal(h.app.production.uncertain,false);assert.ok([...h.timers.values()].some(t=>t.ms===2500));
});

test('read-only polling and unadopted script never overwrite user text or create video',async()=>{
  const h=setup();h.ready();h.field('productionScript','我自己写的独白。');await h.app.writeProductionScript();await h.resolveScript();
  assert.equal(h.elements.get('productionScript').value,'我自己写的独白。');
  assert.equal(h.elements.get('productionAdoptScript').disabled,false);
  assert.equal(h.calls.filter(c=>c.options.method==='POST').length,1);
  assert.ok(!h.calls.some(c=>c.route==='/api/creations'));
  h.app.adoptProductionScript();assert.match(h.elements.get('productionScript').value,/欢迎来到/);
});

test('changed idea makes suggestion non-adoptable and preserves manual script',async()=>{
  const h=setup();h.ready();await h.app.writeProductionScript();await h.resolveScript();h.field('productionScript','最新人工独白。');h.field('creatorBrief','改成活动回顾。');
  assert.equal(h.elements.get('productionAdoptScript').disabled,true);h.app.adoptProductionScript();
  assert.equal(h.elements.get('productionScript').value,'最新人工独白。');
});

test('uncertain script request resumes GET first using persisted ID without automatic POST',async()=>{
  const h=setup();h.ready();h.apiImpl=async()=>{throw Object.assign(new Error('请求结果未知'),{uncertain:true,connectivity:true,httpStatus:503});};await h.app.writeProductionScript();
  const id=h.app.production.scriptId;assert.equal(h.app.production.uncertain,true);
  const restored=setup({storage:h.storage});restored.field('creatorBrief','准备一场真实的 AI 沙龙邀请');
  assert.equal(restored.calls.length,0);assert.equal(restored.app.production.scriptId,id);
  restored.apiImpl=async()=>({script:{id,status:'queued'}});await restored.app.writeProductionScript();
  assert.equal(restored.calls.length,1);assert.equal(restored.calls[0].route,'/api/scripts/'+id);assert.equal(restored.calls[0].options.method,undefined);
});

test('explicit retry after missing request resends identical body and ID',async()=>{
  const h=setup();h.ready();h.apiImpl=async()=>{throw Object.assign(new Error('unknown'),{uncertain:true});};await h.app.writeProductionScript();
  const initial=plain(h.calls[0].body);h.calls.length=0;
  h.apiImpl=async(route,options)=>{if(!options.method)throw Object.assign(new Error('missing'),{httpStatus:404});return {script:{id:initial.request_id,status:'queued'}};};
  await h.app.writeProductionScript();assert.equal(h.calls.length,2);assert.deepEqual(plain(h.calls[1].body),initial);
});

test('definitive script validation error unlocks a corrected request instead of replaying old text',async()=>{
  const h=setup();h.ready();h.apiImpl=async()=>{throw Object.assign(new Error('资料无效'),{httpStatus:422});};await h.app.writeProductionScript();
  const old=h.app.production.scriptId;assert.equal(h.app.production.uncertain,false);
  h.field('creatorBrief','修正后的想法');h.apiImpl=async(route,options)=>({script:{id:JSON.parse(options.body).request_id,status:'queued'}});await h.app.writeProductionScript();
  assert.notEqual(h.app.production.scriptId,old);assert.equal(h.calls.at(-1).body.brief,'修正后的想法');
});

test('failed script retry is explicit and stays on the same script ID',async()=>{
  const h=setup();h.ready();await h.app.writeProductionScript();const id=h.app.production.scriptId;
  h.apiImpl=async()=>({script:{id,status:'failed',error:'模型本次未完成'}});await h.app.pollProductionScript();
  assert.equal(h.calls.filter(c=>c.route.endsWith('/retry')).length,0);
  h.apiImpl=async(route)=>({script:{id,status:route.endsWith('/retry')?'queued':'failed'}});await h.app.writeProductionScript();
  assert.equal(h.calls.at(-1).route,'/api/scripts/'+id+'/retry');assert.equal(h.calls.at(-1).options.method,'POST');
});

test('disconnection blocks script and video writes while retaining both drafts',async()=>{
  const h=setup();h.ready();h.field('productionScript','等待连接恢复。');h.state.connected=false;h.app.updateProductionAccess();
  await h.app.writeProductionScript();await h.app.generateProduction();
  assert.equal(h.calls.length,0);assert.equal(h.elements.get('creatorGenerate').disabled,true);assert.equal(h.elements.get('productionScript').value,'等待连接恢复。');
});

test('narrated generation captures script and options once, and ambiguous retry uses same ID',async()=>{
  const h=setup();h.ready();h.app.setProductionMode('brand_promo');h.field('productionScript','完整朗读的讲稿。');h.apiImpl=async()=>{throw Object.assign(new Error('结果待核实'),{uncertain:true});};
  await h.app.generateProduction();const original=plain(h.calls[0].body);
  assert.equal(original.mode,'brand_promo');assert.equal(original.spoken_script,'完整朗读的讲稿。');assert.equal(original.visual_fill,'reject');
  assert.deepEqual(original.sources,['event.mp4']);assert.equal(h.app.creator.uncertain,true);
  assert.equal(JSON.parse(h.storage.get(CREATOR_DRAFT)).requestBody.idempotency_key,original.idempotency_key);
  h.apiImpl=async()=>({job:{id:'job-one',status:'queued'},created:false});await h.app.generateProduction();
  assert.deepEqual(plain(h.calls[1].body),original);assert.equal(h.app.creator.uncertain,false);
});

test('missing spoken script starts script draft only, never a video task',async()=>{
  const h=setup();h.ready();h.app.setProductionMode('brand_promo');await h.app.generateProduction();assert.equal(h.calls.length,1);assert.equal(h.calls[0].route,'/api/scripts');
});

test('old uncertain edit draft restores edit mode and original request shape',async()=>{
  const body={sources:['legacy.mp4'],brief:'保留原声',options:{},idempotency_key:'legacy-key'};
  const storage=new Map([[CREATOR_DRAFT,JSON.stringify({fields:{creatorBrief:'保留原声'},selected:['s:legacy.mp4'],requestId:'legacy-key',requestBody:body,uncertain:true})]]);
  const h=setup({storage});assert.equal(h.app.production.mode,'edit');await h.app.generateCreation();
  assert.deepEqual(plain(h.calls[0].body),body);assert.equal(h.calls[0].body.mode,undefined);
});

test('presenter selection can generate without videos but requires ready matching portrait',async()=>{
  const h=setup();h.app.production.catalogAt=Date.now();h.app.production.catalog=[{id:'host',kind:'public',ready:true}];h.app.production.portraitId='host';
  h.app.setProductionMode('talking_head');h.field('creatorBrief','主持人邀请参与沙龙');h.field('productionScript','欢迎带着问题来交流。');
  assert.equal(h.elements.get('creatorGenerate').disabled,false);await h.app.generateProduction();
  assert.deepEqual(plain(h.calls[0].body.sources),[]);assert.equal(h.calls[0].body.presenter.portrait_id,'host');
  assert.equal(h.calls[0].body.presenter.consent_confirmed,true);
});

test('unavailable portrait or unconfirmed personal authorization blocks generation',async()=>{
  const h=setup();h.app.production.catalogAt=Date.now();h.app.production.catalog=[{id:'mine',kind:'personal',ready:true}];h.app.production.kind='personal';h.app.production.portraitId='mine';
  h.app.setProductionMode('talking_head');h.field('creatorBrief','人物介绍');h.field('productionScript','介绍讲稿。');
  assert.equal(h.elements.get('creatorGenerate').disabled,true);await h.app.generateProduction();assert.equal(h.calls.length,0);
  h.elements.get('presenterConsent').checked=true;h.app.updateProductionAccess();assert.equal(h.elements.get('creatorGenerate').disabled,false);
  h.app.production.catalog[0].ready=false;h.app.updateProductionAccess();await h.app.generateProduction();assert.equal(h.calls.length,0);
});

test('uncertain portrait upload retains UUID for selecting same file again',async()=>{
  const h=setup();h.elements.get('presenterConsent').checked=true;const file={name:'人物.jpg',type:'image/jpeg',size:1024,lastModified:123};
  h.apiImpl=async()=>{throw Object.assign(new Error('保存结果未知'),{uncertain:true});};await h.app.importPresenter(file);
  const first=h.calls[0];assert.equal(first.options.body,file);assert.match(first.route,/consent_confirmed=true/);
  assert.ok(h.app.production.portraitUpload);assert.match(h.elements.get('presenterStatus').textContent,/同一张照片/);
  h.apiImpl=async(route)=>route==='/api/presenters'?{presenters:[{id:'saved',kind:'personal',ready:true}]}:{presenter:{id:'saved'}};
  await h.app.importPresenter({...file});assert.equal(h.calls[1].route,first.route);assert.equal(h.app.production.portraitUpload,null);
});

test('restored voice preference survives initially empty server capabilities',()=>{
  const storage=new Map([[DRAFT,JSON.stringify({mode:'brand_promo',fields:{productionVoice:'uncle_fu'}})]]);
  const h=setup({storage,voices:false});assert.equal(h.elements.get('productionVoice').value,'');h.state.config.production.voices=h.directory;h.app.updateProductionAccess();
  assert.equal(h.elements.get('productionVoice').value,'uncle_fu');
});

test('drafts never persist tokens or require storage for remote page',async()=>{
  const h=setup({localPage:false});h.ready();await h.app.writeProductionScript();assert.equal(h.storage.size,0);
});


test('unchanged material refresh does not replace cards or interrupt their DOM state',()=>{
 const h=setup();h.ready();h.app.renderCreatorMaterials();
 const grid=h.elements.get('creatorAssetGrid');let html=grid.innerHTML,writes=0;
 Object.defineProperty(grid,'innerHTML',{get:()=>html,set:value=>{html=value;writes++;}});
 h.app.renderCreatorMaterials();h.app.renderCreatorMaterials();
 assert.equal(writes,0);
 h.state.assets.push({source:'second.mp4',name:'新素材',size:200});h.app.renderCreatorMaterials();
 assert.equal(writes,1);
});
test('Vlog supplemental toggles are preserved and sent, never leak into narrated modes',async()=>{
 const h=setup();h.state.config.production.modes.vlog={ready:true};h.state.config.production.supplements={visuals:true,dialogue:true};h.ready();h.app.setProductionMode('vlog');
 for(const id of ['productionSupplementVisuals','productionGeneratedDialogue']){h.elements.get(id).checked=true;h.elements.get(id).emit('change');}
 h.app.saveProductionDraft();const saved=JSON.parse(h.storage.get(DRAFT));assert.equal(saved.fields.productionGeneratedDialogue,true);
 await h.app.generateProduction();const call=h.calls.find(c=>c.route==='/api/creations');
 assert.equal(call.body.options.allow_supplemental_visuals,true);assert.equal(call.body.options.allow_generated_dialogue,true);
});


test('story is the default workflow and writes an editable script before automatic selection',async()=>{
 const h=setup({mode:null});assert.equal(h.app.production.mode,'story');h.field('creatorBrief','');
 assert.equal(h.elements.get('productionScriptPanel').hidden,false);
 assert.equal(h.elements.get('productionWriteScript').disabled,false);
 await h.app.writeProductionScript();assert.equal(h.calls[0].body.mode,'story');assert.deepEqual(plain(h.calls[0].body.sources),[]);
 await h.resolveScript('先核对车辆交接记录，再讲清责任。');
 assert.equal(h.elements.get('productionScript').value,'先核对车辆交接记录，再讲清责任。');
 assert.equal(h.elements.get('productionScriptPreview').hidden,true);
 assert.ok(!h.calls.some(c=>c.route==='/api/creations'||c.route==='/api/assembly-plans'));
});
test('Vlog subtitle choice stays visible, persists and is sent',async()=>{
 const h=setup();h.ready();h.app.setProductionMode('vlog');h.elements.get('productionSubtitles').checked=false;
 h.app.saveProductionDraft();h.app.updateProductionAccess();
 assert.equal(h.elements.get('productionSubtitles').closest().hidden,false);
 assert.equal(JSON.parse(h.storage.get(DRAFT)).fields.productionSubtitles,false);
 await h.app.generateProduction();assert.equal(h.calls.find(c=>c.route==='/api/creations').body.options.subtitles,false);
});


test('story creation preserves references and makes an independent remix of a presenter parent',async()=>{
 const h=setup({mode:'story'});h.ready();h.field('productionScript','先确认交车记录。');Object.assign(h.app.production,{parentId:'b'.repeat(32),revisionKind:'presenter',referenceIds:['c'.repeat(32)]});
 await h.app.generateProduction();const request=h.calls.find(c=>c.route==='/api/assembly-plans').body;
 assert.equal(request.auto_render,true);assert.equal(request.parent_job_id,'b'.repeat(32));assert.equal(request.revision_kind,'remix');assert.deepEqual(plain(request.reference_ids),['c'.repeat(32)]);
});

function assemblyReady(h,extra={}){
 const body=h.app.storyAssembly.body;
 return {id:h.app.storyAssembly.id,status:'ready',stage:'ready',result:{script:body.spoken_script,script_sha256:'a'.repeat(64),summary:'先讲交车问题，再用记录说明。',sources:body.sources.length?body.sources:['auto-selected.mp4'],shots:[{source:body.sources[0]||'auto-selected.mp4',source_in:2,source_out:8,timeline_start:0,timeline_end:6,narration:body.spoken_script,selection_basis:'原声讨论了车辆交接。',score:86}]},...extra};
}
async function readyAssembly(h){await h.app.requestStoryAssembly(false);h.apiImpl=async()=>({assembly:assemblyReady(h)});await h.app.pollStoryAssembly();}

test('story auto-select accepts no selected assets and sends only supported defaults',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','交车前先把责任说清楚。');h.elements.get('productionSubtitles').checked=false;h.app.updateProductionAccess();assert.equal(h.elements.get('creatorGenerate').disabled,false);
 await h.app.generateProduction();const request=h.calls.find(c=>c.route==='/api/assembly-plans');assert.deepEqual(plain(request.body.sources),[]);assert.equal(request.body.auto_render,true);assert.equal(request.body.spoken_script,'交车前先把责任说清楚。');assert.equal(request.body.options.subtitles,false);assert.deepEqual(Object.keys(request.body.options).sort(),['aspect','music','opening_title','subtitles','target_duration']);assert.ok(!h.calls.some(c=>c.route==='/api/creations'));
});

test('one-click story assembly carries the entered work title without invalidating the visual plan',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','交车前先把责任说清楚。');h.field('creatorTitle','  托管交车三件事  ');await h.app.generateProduction();
 const request=h.calls.find(c=>c.route==='/api/assembly-plans').body;assert.equal(request.auto_render,true);assert.equal(request.title,'托管交车三件事');
 h.apiImpl=async()=>({assembly:assemblyReady(h,{error:'精剪稍后继续'})});await h.app.pollStoryAssembly();assert.equal(h.app.storyPlanReady(),true);h.field('creatorTitle','新的作品名');assert.equal(h.app.storyPlanReady(),true);
});
test('manual story scope sends exact sources and unresolved material blocks assembly',async()=>{
 const h=setup({mode:'story'});h.ready();h.field('productionScript','先核对交接。');await h.app.requestStoryAssembly(false);assert.deepEqual(plain(h.calls[0].body.sources),['event.mp4']);assert.equal(h.calls[0].body.auto_render,false);
 const blocked=setup({mode:'story'});blocked.field('productionScript','先核对交接。');blocked.app.creator.selected=['s:missing.mp4'];await blocked.app.requestStoryAssembly(false);assert.equal(blocked.calls.length,0);
});
test('assembly polling preserves edits and escapes its preview without creating video',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','<script>不能成为HTML</script>。');await readyAssembly(h);assert.equal(h.app.storyPlanReady(),true);assert.ok(h.elements.get('storyAssemblyPreview').innerHTML.includes('&lt;script&gt;'));assert.ok(!h.calls.some(c=>c.route==='/api/creations'));
 h.field('productionScript','我修改了最后一句。');await h.app.pollStoryAssembly();assert.equal(h.elements.get('productionScript').value,'我修改了最后一句。');assert.equal(h.app.storyPlanReady(),false);
});
test('ready story uses exact plan and hash; confirmed receipt prevents duplicate rendering',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','脚本正文。');h.elements.get('productionSubtitles').checked=false;await readyAssembly(h);const id=h.app.storyAssembly.id;h.apiImpl=async()=>({job:{id:'render-one',status:'queued'},created:true});await h.app.generateProduction();const body=h.calls.find(c=>c.route==='/api/creations').body;
 assert.equal(body.assembly_plan_id,id);assert.equal(body.expected_script_sha256,'a'.repeat(64));assert.equal(body.spoken_script,'脚本正文。');assert.deepEqual(plain(body.sources),['auto-selected.mp4']);assert.equal(body.options.subtitles,false);assert.equal(h.app.storyAssembly.record.job_id,'render-one');const count=h.calls.length;await h.app.generateProduction();assert.equal(h.calls.length,count);
});
test('script, selection and option changes invalidate ready plans',async()=>{
 for(const field of ['productionScript','productionAspect','productionSubtitles','selection']){const h=setup({mode:'story'});h.field('productionScript','脚本正文。');await readyAssembly(h);
 if(field==='productionSubtitles')h.elements.get(field).checked=false;else if(field==='selection'){h.state.assets=[{source:'other.mp4',name:'新素材',size:200}];h.app.creator.selected=['s:other.mp4'];}else h.field(field,field==='productionScript'?'另一份脚本。':'16:9');h.app.updateProductionAccess();assert.equal(h.app.storyPlanReady(),false,field);
 h.apiImpl=async(route,options)=>({assembly:{id:JSON.parse(options.body).request_id,status:'queued'}});await h.app.generateProduction();assert.ok(!h.calls.some(c=>c.route==='/api/creations'),field);assert.equal(h.calls.at(-1).body.auto_render,true);}
});
test('uncertain assembly is saved before POST and retry reuses its ID and body',async()=>{
 const h=setup({mode:'story'}),response=pending();h.field('productionScript','脚本正文。');h.apiImpl=()=>response.promise;const first=h.app.requestStoryAssembly(true);await h.app.requestStoryAssembly(true);assert.equal(h.calls.length,1);const saved=JSON.parse(h.storage.get('cutflow.story.assembly.v1'));assert.equal(saved.body.spoken_script,'脚本正文。');assert.equal(saved.uncertain,true);response.reject(Object.assign(new Error('网络中断'),{uncertain:true}));await first;
 const original=plain(h.calls[0].body);h.field('productionScript','稍后修改的脚本。');h.apiImpl=async(route,options)=>{if(!options.method)throw Object.assign(new Error('未找到'),{httpStatus:404});return {assembly:{id:original.request_id,status:'queued'}};};await h.app.requestStoryAssembly(true);assert.equal(h.calls.at(-2).options.method,undefined);assert.deepEqual(plain(h.calls.at(-1).body),original);assert.equal(h.elements.get('productionScript').value,'稍后修改的脚本。');
});
test('restored uncertain assembly reconciles with GET and never automatically re-posts',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','脚本正文。');h.apiImpl=async()=>{throw Object.assign(new Error('断开'),{uncertain:true});};await h.app.requestStoryAssembly(true);const restored=setup({mode:'story',storage:h.storage});assert.equal(restored.calls.length,0);restored.apiImpl=async()=>({assembly:{id:h.app.storyAssembly.id,status:'queued'}});await restored.app.requestStoryAssembly(true);assert.equal(restored.calls.length,1);assert.equal(restored.calls[0].route,'/api/assembly-plans/'+h.app.storyAssembly.id);assert.equal(restored.calls[0].options.method,undefined);
});
test('auto rendering follows durable assembly job receipt without second creation',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','脚本正文。');await h.app.requestStoryAssembly(true);h.apiImpl=async route=>route.startsWith('/api/jobs/')?{job:{id:'continued-job',status:'queued'}}:{assembly:assemblyReady(h,{job_id:'continued-job'})};await h.app.pollStoryAssembly();await new Promise(resolve=>setImmediate(resolve));assert.equal(h.app.creator.resultId,'continued-job');assert.ok(!h.calls.some(c=>c.route==='/api/creations'));
});
test('story script response preserves typing done during generation',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','原稿。');await h.app.writeProductionScript();h.field('productionScript','等候时亲手改的文案。');await h.resolveScript('生成的新稿。');assert.equal(h.elements.get('productionScript').value,'等候时亲手改的文案。');assert.equal(h.elements.get('productionScriptPreview').hidden,false);
});
test('connection switch cannot replay the previous projects uncertain assembly',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','脚本正文。');h.apiImpl=async()=>{throw Object.assign(new Error('断开'),{uncertain:true});};await h.app.requestStoryAssembly(true);h.state.base='http://other-studio';h.app.updateStoryWorkflow();assert.equal(h.app.storyAssembly.id,null);assert.equal(h.app.storyAssembly.uncertain,false);
});

test('ready plan carries its bound script ID into the render request',async()=>{
 const h=setup({mode:'story'});await h.app.writeProductionScript();await h.resolveScript('可编辑的正式脚本。');const scriptId=h.app.production.scriptId;
 h.apiImpl=async(route,options)=>({assembly:{id:options.body?JSON.parse(options.body).request_id:h.app.storyAssembly.id,status:'queued'}});await readyAssembly(h);
 assert.equal(h.app.storyAssembly.body.script_id,scriptId);h.apiImpl=async()=>({job:{id:'script-linked-render',status:'queued'}});await h.app.generateProduction();assert.equal(h.calls.find(c=>c.route==='/api/creations').body.script_id,scriptId);
});
test('auto render handoff error leaves a ready plan available for manual render retry',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','脚本正文。');await h.app.requestStoryAssembly(true);h.apiImpl=async()=>({assembly:assemblyReady(h,{error:'队列暂未提交'})});await h.app.pollStoryAssembly();
 assert.equal(h.app.storyAssembly.timer,null);assert.equal(h.elements.get('creatorGenerate').disabled,false);assert.match(h.elements.get('storyAssemblyStatus').textContent,/编排已完成/);
 h.apiImpl=async()=>({job:{id:'retried-handoff',status:'queued'}});await h.app.generateProduction();assert.equal(h.calls.filter(c=>c.route==='/api/creations').length,1);assert.equal(h.app.creator.resultId,'retried-handoff');
});
test('pending script prevents repeat submission and uncertain poll remains recoverable',async()=>{
 const h=setup({mode:'story'});await h.app.writeProductionScript();assert.equal(h.elements.get('productionWriteScript').disabled,true);
 h.apiImpl=async()=>{throw Object.assign(new Error('断开'),{uncertain:true});};await h.app.pollProductionScript();assert.equal(h.elements.get('productionWriteScript').disabled,false);
});
test('uncertain assembly polling remains clickable even after editing away the script',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','脚本正文。');await h.app.requestStoryAssembly(true);h.apiImpl=async()=>{throw Object.assign(new Error('断开'),{uncertain:true});};await h.app.pollStoryAssembly();h.field('productionScript','');
 assert.equal(h.elements.get('storyPlanButton').disabled,false);assert.equal(h.elements.get('creatorGenerate').disabled,false);
});

test('story puts optional material scope after the workflow and preserves user expansion across refreshes',()=>{
 const h=setup(),material=h.elements.get('productionMaterialPanel'),script=h.elements.get('productionScriptPanel');
 const layout=script.closest(),brief={id:'brief'},presenter=h.elements.get('productionPresenter');
 layout.children=[presenter,material,brief];
 Object.defineProperty(material,'nextElementSibling',{get(){return layout.children[layout.children.indexOf(material)+1]||null;}});
 layout.appendChild=element=>{layout.children=layout.children.filter(child=>child!==element);layout.children.push(element);};
 layout.insertBefore=(element,before)=>{layout.children=layout.children.filter(child=>child!==element);layout.children.splice(layout.children.indexOf(before),0,element);};
 script.closest=selector=>selector==='.creator-layout'?layout:brief;
 h.field('productionScript','保留正在编辑的稿件。');h.app.setProductionMode('story');
 assert.deepEqual(layout.children.map(child=>child.id),['productionPresenter','brief','productionMaterialPanel']);assert.equal(material.open,false);
 h.elements.get('storyChooseMaterials').click();assert.equal(material.open,true);h.app.updateProductionAccess();assert.equal(material.open,true);assert.equal(h.elements.get('productionScript').value,'保留正在编辑的稿件。');
 h.app.setProductionMode('brand_promo');assert.deepEqual(layout.children.map(child=>child.id),['productionPresenter','productionMaterialPanel','brief']);assert.equal(material.open,true);
 h.app.setProductionMode('talking_head');assert.equal(material.hidden,true);h.app.setProductionMode('story');assert.equal(material.hidden,false);assert.equal(material.open,false);
});

test('assembly preview uses the selected-shot thumbnail and readable selection note only',async()=>{
 const h=setup({mode:'story'});h.field('productionScript','脚本正文。');await h.app.requestStoryAssembly(false);
 const result=assemblyReady(h);Object.assign(result.result.shots[0],{thumbnail:'data:image/jpeg;base64,YWJj',selection_basis:'semantic_overlap',selection_note:'此处解释<交车>，与台词匹配。'});
 h.apiImpl=async()=>({assembly:result});await h.app.pollStoryAssembly();let html=h.elements.get('storyAssemblyPreview').innerHTML;
 assert.match(html,/src="data:image\/jpeg;base64,YWJj"/);assert.match(html,/此处解释&lt;交车&gt;/);assert.ok(!html.includes('semantic_overlap'));
 result.result.shots[0].thumbnail='javascript:alert(1)';await h.app.pollStoryAssembly();html=h.elements.get('storyAssemblyPreview').innerHTML;
 assert.ok(!html.includes('<img'));assert.ok(!html.includes('javascript:'));assert.match(html,/<span>▷<\/span>/);
});
