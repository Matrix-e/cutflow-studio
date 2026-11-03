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

function setup({storage=new Map(),localPage=true,voices=true,connected=true}={}){
  const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
  const elements=new Map(),listeners=new Map(),timers=new Map();
  class Element{
    constructor(id){this.id=id;this.value='';this.textContent='';this.innerHTML='';this.disabled=false;this.hidden=false;this.checked=false;this.dataset={};this.listeners=new Map();const values=new Set();this.classList={add:c=>values.add(c),remove:c=>values.delete(c),contains:c=>values.has(c),toggle:(c,on)=>on?values.add(c):values.delete(c)};}
    addEventListener(name,fn){if(!this.listeners.has(name))this.listeners.set(name,[]);this.listeners.get(name).push(fn);}
    setAttribute(name,value){this[name]=value;}
    emit(name){for(const fn of this.listeners.get(name)||[])fn({target:this});}
    focus(){} scrollIntoView(){} click(){if(!this.disabled)return this.onclick?.();}
  }
  for(const [,id]of html.matchAll(/id="([^"]+)"/g))elements.set(id,new Element(id));
  for(const [id,value]of Object.entries({productionSubtype:'salon_promo',productionStyle:'professional',productionAspect:'9:16',productionDuration:'45',productionSourceAudio:'mute'}))elements.get(id).value=value;
  elements.get('productionSubtitles').checked=true;elements.get('productionMusic').checked=true;
  const directory=[{id:'serena',name:'温和女声',language:'zh-CN'},{id:'uncle_fu',name:'沉稳男声',language:'zh-CN'}];
  const state={connected,assets:[],jobs:[],config:{creation_supported:true,production:{modes:Object.fromEntries(['edit','brand_promo','narrated_promo','talking_head'].map(mode=>[mode,{ready:true}])),voices:voices?directory:[],script_supported:true}}};
  const h={elements,state,storage,timers,calls:[],directory,localImports:{items:[],maxBytes:2*1024**3}};
  h.apiImpl=async(route,options={})=>{
    if(route==='/api/presenters')return {presenters:[]};
    if(route.startsWith('/api/scripts'))return {script:{id:options.body?JSON.parse(options.body).request_id:h.app.production.scriptId,status:'queued'}};
    return {job:{id:'finished-job',title:'成片',status:'queued'},created:true};
  };
  let serial=0,timerSerial=0;
  const document={querySelector:selector=>elements.get(selector.slice(1))||null,querySelectorAll:()=>[],addEventListener(name,fn){if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push(fn);}};
  const context=vm.createContext({document,state,localPage,localImports:h.localImports,console,Date,URL,Blob,
    $:selector=>document.querySelector(selector),
    esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    bytes:value=>String(value),assetName:item=>item.name||item.source,statusNames:{queued:'排队中'},
    localImportAvailable:()=>true,showPage(){},renderLocalImports(){},renderJobs(){},refresh:async()=>{},refreshLocalLibrary:async()=>{},
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    crypto:{randomUUID:()=> '30000000-0000-4000-8000-'+String(++serial).padStart(12,'0')},
    api:async(route,options={},format)=>{h.calls.push({route,options,format,body:typeof options.body==='string'?JSON.parse(options.body):options.body});return h.apiImpl(route,options,format);},
    setTimeout:(fn,ms)=>{const id=++timerSerial;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id)
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/creator.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/production.js'),'utf8')+'\nglobalThis.productionApp={production,creator,writeProductionScript,pollProductionScript,adoptProductionScript,generateProduction,generateCreation,updateProductionAccess,setProductionMode,saveProductionDraft,loadPresenterCatalog,importPresenter,renderPresenterCatalog,selectedPresenterReady};',context);
  h.app=context.productionApp;
  h.field=(id,value)=>{elements.get(id).value=value;elements.get(id).emit('input');};
  h.ready=()=>{state.assets=[{source:'event.mp4',name:'活动片段',size:1024}];h.app.creator.selected=['s:event.mp4'];h.field('creatorBrief','准备一场真实的 AI 沙龙邀请');h.app.updateProductionAccess();};
  h.resolveScript=async(text='欢迎来到这次沙龙，一起讨论真实的问题。')=>{h.apiImpl=async()=>({script:{id:h.app.production.scriptId,status:'succeeded',result:{spoken_script:text,assumptions:['活动信息仍待确认。']}}});await h.app.pollProductionScript();};
  return h;
}

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
  const h=setup();h.ready();h.field('productionScript','完整朗读的讲稿。');h.apiImpl=async()=>{throw Object.assign(new Error('结果待核实'),{uncertain:true});};
  await h.app.generateProduction();const original=plain(h.calls[0].body);
  assert.equal(original.mode,'brand_promo');assert.equal(original.spoken_script,'完整朗读的讲稿。');assert.equal(original.visual_fill,'reject');
  assert.deepEqual(original.sources,['event.mp4']);assert.equal(h.app.creator.uncertain,true);
  assert.equal(JSON.parse(h.storage.get(CREATOR_DRAFT)).requestBody.idempotency_key,original.idempotency_key);
  h.apiImpl=async()=>({job:{id:'job-one',status:'queued'},created:false});await h.app.generateProduction();
  assert.deepEqual(plain(h.calls[1].body),original);assert.equal(h.app.creator.uncertain,false);
});

test('missing spoken script starts script draft only, never a video task',async()=>{
  const h=setup();h.ready();await h.app.generateProduction();assert.equal(h.calls.length,1);assert.equal(h.calls[0].route,'/api/scripts');
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
