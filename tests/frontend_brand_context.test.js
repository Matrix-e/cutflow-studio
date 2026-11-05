'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
function deferred(){let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};}
function setup(){
  const elements=new Map(),state={base:'https://studio-a.example',token:'secret',epoch:1,connected:true};
  for(const [,id] of fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').matchAll(/id="([^"]+)"/g))elements.set(id,{value:'',textContent:'',disabled:false,innerHTML:'',listeners:{},addEventListener(name,fn){this.listeners[name]=fn;},click(){},scrollIntoView(){}});
  const h={elements,state,calls:[]};h.impl=async()=>({revision:1,culture:'先替客户把事情想清楚',facts:'已验证业务事实',advantages:'',miniapp:'',creative_rules:'从真实问题切入',screenshots:[]});
  h.crypto={randomUUID:()=> '00000000-0000-4000-8000-000000000001'};
  const context=vm.createContext({state,console,Map,JSON,URL:{createObjectURL:()=> 'blob:local-image',revokeObjectURL(){}},crypto:h.crypto,
    $:selector=>elements.get(selector.slice(1)),esc:value=>String(value).replace(/[<>"']/g,''),document:{addEventListener(){}},showPage(){},
    connectionContext:()=>({...state}),currentContext:c=>c.base===state.base&&c.token===state.token&&c.epoch===state.epoch,
    api:async(route,options={},format,scope)=>{h.calls.push({route,options,format,scope});return h.impl(route,options,format,scope);}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/brand-context.js'),'utf8')+'\nglobalThis.brandApp={loadBrandContext,saveBrandContext,uploadBrandScreenshot,brandContextUI,brandButtons,openBrandEvidence,saveBrandEvidence,editBrandEvidence,brandEvidenceReady,renderBrandImages};',context);
  h.app=context.brandApp;h.edit=(id,value)=>{elements.get(id).value=value;elements.get(id).listeners.input();};return h;
}
const submit={preventDefault(){}};

test('brand knowledge loads once and a dirty manual reload never overwrites input',async()=>{
  const h=setup();await h.app.loadBrandContext();h.edit('brandFacts','我刚补充的事实');
  await h.app.loadBrandContext();assert.equal(h.calls.length,1);assert.equal(h.elements.get('brandFacts').value,'我刚补充的事实');
});
test('loading locks inputs until the returned revision is available',async()=>{
  const h=setup(),pending=deferred();h.impl=()=>pending.promise;const loading=h.app.loadBrandContext();
  assert.equal(h.elements.get('brandFacts').disabled,true);assert.equal(h.elements.get('brandContextSave').disabled,true);
  pending.resolve({revision:3,culture:'',facts:'真实事实',advantages:'',miniapp:'',creative_rules:'',screenshots:[]});await loading;
  assert.equal(h.elements.get('brandFacts').disabled,false);assert.equal(h.elements.get('brandContextSave').disabled,false);
});
test('save keeps changes made while submission is pending',async()=>{
  const h=setup();await h.app.loadBrandContext();h.edit('brandFacts','已提交事实');const pending=deferred();h.impl=()=>pending.promise;
  const saving=h.app.saveBrandContext(submit);h.edit('brandFacts','提交后新写的事实');pending.resolve({revision:2});await saving;
  assert.equal(h.app.brandContextUI.dirty,true);assert.equal(h.elements.get('brandFacts').value,'提交后新写的事实');
  assert.equal(JSON.parse(h.calls[1].options.body).facts,'已提交事实');
});
test('changed connection target cannot save using the prior servers revision',async()=>{
  const h=setup();await h.app.loadBrandContext();h.state.base='https://studio-b.example';h.edit('brandFacts','原项目内容');
  await h.app.saveBrandContext(submit);h.app.brandButtons();assert.equal(h.calls.length,1);assert.equal(h.elements.get('brandContextSave').disabled,true);
});
test('screenshot upload rejects unsupported or oversized content before sending',async()=>{
  const h=setup();await h.app.loadBrandContext();await h.app.uploadBrandScreenshot({type:'image/svg+xml',size:100});
  await h.app.uploadBrandScreenshot({type:'image/png',size:8*1024*1024+1});assert.equal(h.calls.length,1);
});
test('uploaded screenshot is retrieved through the authenticated image endpoint',async()=>{
  const h=setup();await h.app.loadBrandContext();h.impl=async(route,options)=>options.method==='POST'?{screenshot:{id:'shot1',name:'页面.png',url:'/api/brand-context/screenshots/shot1/image'}}:{};
  await h.app.uploadBrandScreenshot({type:'image/png',size:100,name:'页面.png',lastModified:1});
  assert.ok(h.calls.some(c=>c.route==='/api/brand-context/screenshots/shot1/image'&&c.format==='blob'&&c.scope.token==='secret'));
});

test('culture summary uses a short complete statement without repeating technical knowledge',async()=>{
 const h=setup();const culture='先替客户把事情想清楚。';
 h.impl=async()=>({revision:1,culture,facts:'本地实现与业务资料。'.repeat(40),advantages:'优势资料。',miniapp:'',creative_rules:'',screenshots:[],summary:'本地实现，不应显示。'.repeat(40)});
 await h.app.loadBrandContext();assert.equal(h.elements.get('brandCultureSummary').textContent,culture);
 const longCulture=culture+'后续完整的文化内容。'.repeat(30);h.impl=async()=>({revision:2,culture:longCulture,facts:'',advantages:'',miniapp:'',creative_rules:'',screenshots:[]});
 await h.app.loadBrandContext();assert.equal(h.elements.get('brandCultureSummary').textContent,culture);assert.equal(h.elements.get('brandCulture').value,longCulture);
});

test('culture summary never cuts a long sentence and distinguishes saved knowledge from empty setup',async()=>{
 const cases=[{culture:'完整但很长的句子'.repeat(30)+'。',facts:'',expected:'已保存企业文化，写稿时自动使用。'},{culture:'',facts:'业务事实'.repeat(200),expected:'已关联品牌知识，写稿时自动使用。'},{culture:'',facts:'',expected:'保存企业文化与业务资料，后续自动沿用。'}];
 for(const item of cases){const h=setup();h.impl=async()=>({revision:1,culture:item.culture,facts:item.facts,advantages:'',miniapp:'',creative_rules:'默认导演规则',screenshots:[]});await h.app.loadBrandContext();assert.equal(h.elements.get('brandCultureSummary').textContent,item.expected);assert.ok(h.elements.get('brandCultureSummary').textContent.length<=160);}
});

function screenshot(){return {id:'a'.repeat(32),name:'真实汽车助理首页',url:'/api/brand-context/screenshots/'+'a'.repeat(32),sha256:'b'.repeat(64)};}
async function evidenceSetup(){
 const h=setup(),item=screenshot();
 h.impl=async(route,options,format)=>format==='blob'?{}:{revision:1,culture:'',facts:'托管问答入口',advantages:'',miniapp:'',creative_rules:'',screenshots:[item],confirmed_facts:[{id:'assistant-entry',text:'提供托管问答入口',source:'已核对小程序页面'},{id:'unverified',text:'待核对内容',status:'needs_review'}]};
 await h.app.loadBrandContext();await Promise.resolve();h.app.openBrandEvidence(item.id);
 Object.assign(h.app.brandContextUI.evidenceDraft,{page_route:'托管 → 汽车助理',source_reference:'我在本机打开 Super Ecar 截图',purpose:'介绍咨询入口',fact_ids:['assistant-entry'],confirmed:true});
 return {h,item};
}

test('screenshot association lists confirmed knowledge and sends normal authenticated evidence update',async()=>{
 const {h,item}=await evidenceSetup();
 assert.match(h.elements.get('brandScreenshots').innerHTML,/提供托管问答入口/);
 assert.doesNotMatch(h.elements.get('brandScreenshots').innerHTML,/待核对内容/);
 h.impl=async(route,options)=>({screenshot:{...item,...JSON.parse(options.body).evidence,evidence_revision:1}});
 await h.app.saveBrandEvidence(submit);
 const call=h.calls.find(x=>x.options.method==='PUT'),body=JSON.parse(call.options.body);
 assert.equal(call.route,item.url+'/evidence');assert.equal(call.scope.token,'secret');
 assert.equal(body.expected_sha256,item.sha256);assert.deepEqual(body.evidence.fact_ids,['assistant-entry']);
 assert.equal(body.evidence.kind,'miniapp_screenshot');assert.equal(body.evidence.capture_mode,'local_miniapp');
 assert.match(body.evidence.verification_scope,/不证明线上可用性/);
 assert.equal(h.app.brandContextUI.evidenceDraft,null);assert.match(h.elements.get('brandScreenshots').innerHTML,/已关联知识/);
});

test('screenshot requires actual preview, user review and current fact membership before association',async()=>{
 for(const change of [{confirmed:false},{fact_ids:[]},{fact_ids:['unverified']},{purpose:''}]){
  const {h}=await evidenceSetup();Object.assign(h.app.brandContextUI.evidenceDraft,change);
  await h.app.saveBrandEvidence(submit);assert.equal(h.calls.filter(x=>x.options.method==='PUT').length,0);
 }
 const {h}=await evidenceSetup();h.app.brandContextUI.imageUrls.clear();await h.app.saveBrandEvidence(submit);
 assert.equal(h.calls.filter(x=>x.options.method==='PUT').length,0);assert.match(h.elements.get('brandContextStatus').textContent,/截图显示/);
});

test('evidence save blocks duplicate clicks and preserves draft after a network error',async()=>{
 const {h}=await evidenceSetup(),pending=deferred();h.impl=()=>pending.promise;
 const save=h.app.saveBrandEvidence(submit);await h.app.saveBrandEvidence(submit);
 assert.equal(h.calls.filter(x=>x.options.method==='PUT').length,1);
 pending.resolve({screenshot:{id:'wrong'}});await save;
 assert.equal(h.app.brandContextUI.evidenceDraft.purpose,'介绍咨询入口');
 assert.match(h.elements.get('brandContextStatus').textContent,/当前填写内容已保留/);
});

test('evidence draft blocks reload and cannot cross a changed connection',async()=>{
 const {h,item}=await evidenceSetup(),before=h.calls.length;await h.app.loadBrandContext();assert.equal(h.calls.length,before);
 h.app.openBrandEvidence(item.id);assert.equal(h.app.brandContextUI.evidenceDraft.purpose,'介绍咨询入口');
 h.state.base='https://other.example';await h.app.saveBrandEvidence(submit);assert.equal(h.calls.length,before);
});

test('pending image load never replaces the evidence editor or typed text',async()=>{
 const h=setup(),image=deferred(),item=screenshot();h.impl=async(route,options,format)=>format==='blob'?image.promise:{revision:1,culture:'',facts:'',advantages:'',miniapp:'',creative_rules:'',screenshots:[item],confirmed_facts:[]};
 await h.app.loadBrandContext();h.app.openBrandEvidence(item.id);
 h.app.editBrandEvidence({target:{dataset:{brandField:'purpose'},value:'刚输入，不应被图片加载覆盖'}});
 const markup=h.elements.get('brandScreenshots').innerHTML;image.resolve({});await Promise.resolve();await Promise.resolve();
 assert.equal(h.elements.get('brandScreenshots').innerHTML,markup);
 assert.equal(h.app.brandContextUI.evidenceDraft.purpose,'刚输入，不应被图片加载覆盖');
});

test('new upload does not claim ready before the user has associated evidence',async()=>{
 const h=setup();await h.app.loadBrandContext();h.impl=async(route,options)=>options.method==='POST'?{screenshot:screenshot()}:{};
 await h.app.uploadBrandScreenshot({type:'image/png',size:100,name:'页面.png',lastModified:2});
 assert.match(h.elements.get('brandContextStatus').textContent,/核对对应知识和用途/);
 assert.doesNotMatch(h.elements.get('brandContextStatus').textContent,/制作可自动选用/);
});

test('legacy screenshot gets its exact preview hash without reupload or changing its identity',async()=>{
 const h=setup(),item={...screenshot(),sha256:null};let hashed;
 h.crypto.subtle={digest:async(algorithm,bytes)=>{hashed={algorithm,bytes};return Uint8Array.from({length:32},()=>17).buffer;}};
 const bytes=new Uint8Array([1,2,3]).buffer;
 h.impl=async(route,options,format)=>format==='blob'?{arrayBuffer:async()=>bytes}:{revision:1,culture:'',facts:'',advantages:'',miniapp:'',creative_rules:'',screenshots:[item],confirmed_facts:[]};
 await h.app.loadBrandContext();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(hashed.algorithm,'SHA-256');assert.equal(hashed.bytes,bytes);
 assert.equal(h.app.brandContextUI.images[0].id,item.id);assert.equal(h.app.brandContextUI.images[0].sha256,'11'.repeat(32));
 assert.equal(h.calls.some(call=>call.options.method==='POST'),false);
});
