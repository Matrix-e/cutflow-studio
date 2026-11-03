'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const plain = value => JSON.parse(JSON.stringify(value));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup() {
  const elements = new Map();
  class Element {
    constructor(id) {
      Object.assign(this, {id, value:'', textContent:'', innerHTML:'', hidden:false, disabled:false,
        checked:false, open:false, dataset:{}, closes:0});
      this.classList = {toggle(){}};
    }
    showModal(){ this.open = true; }
    close(){ this.open = false; this.closes++; }
    focus(){} scrollIntoView(){} querySelectorAll(){ return []; }
  }
  for (const [, id] of fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8').matchAll(/id="([^"]+)"/g))
    elements.set(id, new Element(id));
  const learn = ['opening','structure','cta','tone','visual_format','music','subtitles','notes']
    .map(value => ({value, checked:true}));
  const production = {mode:'brand_promo', kind:'public', portraitId:null, referenceIds:[],
    parentId:null, parentTitle:null, revisionKind:null, checking:false, uncertain:false, scriptJob:null};
  const creator = {selected:[], resultId:null, busy:false, uncertain:false};
  const state = {connected:true, jobs:[], selected:null, config:{}};
  const h = {elements, production, creator, state, calls:[], toasts:[], saved:[], pages:[]};
  h.apiImpl = async route => route.startsWith('/api/knowledge?') ? {items:[]} : {};
  const fields = ['productionSubtype','productionScript','productionVoice','productionStyle',
    'productionAspect','productionDuration','productionMusic','productionSubtitles','productionLoop',
    'productionSourceAudio','productionBrand','productionDate','productionPlace','productionCTA','productionOpeningTitle'];
  const checks = new Set(['productionMusic','productionSubtitles','productionLoop']);
  const context = vm.createContext({
    console, production, creator, state, productionFields:fields, productionChecks:checks,
    $:selector => elements.get(selector.slice(1)),
    document:{addEventListener(){},querySelectorAll(selector){
      if (selector === '.learn-fields input') return learn;
      if (selector === '.learn-fields input:checked') return learn.filter(e => e.checked);
      return [];
    }},
    esc:value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    isTechnicalTest:job => job.job_kind === 'technical_test',
    creatorInputLocked:() => creator.busy || creator.uncertain,
    productionContext:() => JSON.stringify({mode:production.mode, brief:elements.get('creatorBrief').value,
      sources:creator.selected, parentId:production.parentId, referenceIds:production.referenceIds}),
    connectionContext:() => 'connection-1',currentContext:context => context === 'connection-1',
    api:async (route, options={}) => {
      const call = {route, options, body:options.body ? JSON.parse(options.body) : undefined};
      h.calls.push(call); return h.apiImpl(route, options, call.body);
    },
    toast:(message, error) => h.toasts.push({message, error}),
    saveProductionDraft:() => h.saved.push({type:'production', parentId:production.parentId}),
    saveCreatorDraft:() => h.saved.push({type:'creator', script:elements.get('productionScript').value}),
    showPage:page => h.pages.push(page),
    updateProductionAccess(){}, renderCreatorMaterials(){}, renderCreatorResult(){}, scriptMessage(){},
    loadPresenterCatalog:async()=>{}, writeProductionScript:async()=>{},clearTimeout(){}
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/library.js'), 'utf8') +
    '\nglobalThis.libraryApp={knowledgeUI,prepareWorkVariant,applyWorkDraft,collectWork,saveKnowledge,referenceWork,renderKnowledge};', context);
  h.app = context.libraryApp;
  h.field = (id, value) => { elements.get(id).value = value; };
  return h;
}
function draft(id) {
  return {parent_job_id:id, title:'作品 '+id, sources:[], draft:{mode:'talking_head', brief:'原想法 '+id,
    spoken_script:'原讲稿 '+id, subtype:'salon_promo', brand:{name:'品牌 '+id}, voice_id:'serena',
    visual_fill:'reject', options:{aspect:'9:16',music:true,subtitles:true,voice_style:'professional',source_audio:'mute',opening_title:'片头 '+id},
    presenter:{kind:'public',portrait_id:'portrait-'+id,consent_confirmed:true}}};
}
function item(id, revision=1, notes='已有标注') {
  return {id, revision, title:'作品 '+id, notes, favorite:true, tags:['沙龙'], learn_fields:['opening','notes'],
    snapshot:{mode:'brand_promo',spoken_script:'欢迎交流。',options:{}}, learning_profile:{opening:{text:'欢迎交流。'}}};
}

test('latest variant request wins even when an older draft arrives last', async () => {
  const h=setup(), a=deferred(), b=deferred();
  h.apiImpl=route=>route.includes('/A/')?a.promise:b.promise;
  const first=h.app.prepareWorkVariant('A','presenter'), second=h.app.prepareWorkVariant('B','presenter');
  b.resolve(draft('B')); await second;
  assert.equal(h.production.parentId,'B');
  h.field('productionScript','用户正在编辑 B');
  a.resolve(draft('A')); await first;
  assert.equal(h.production.parentId,'B');
  assert.equal(h.elements.get('productionScript').value,'用户正在编辑 B');
  assert.equal(h.elements.get('productionOpeningTitle').value,'片头 B');
  assert.equal(h.production.portraitId,null,'换人物必须重新选择人物');
  assert.ok(!h.saved.some(saved=>saved.parentId==='A'));
});

test('editing the current draft while a variant loads prevents destructive replacement', async () => {
  const h=setup(), response=deferred();h.apiImpl=()=>response.promise;
  h.field('productionScript','已写好的独白');
  const loading=h.app.prepareWorkVariant('A','presenter');
  h.field('productionScript','等待时补充的新句子');
  response.resolve(draft('A'));await loading;
  assert.equal(h.elements.get('productionScript').value,'等待时补充的新句子');
  assert.equal(h.production.parentId,null);assert.equal(h.saved.length,0);
  assert.ok(h.toasts.some(t=>t.message.includes('已编辑当前草稿')));
});

test('a video submission begun during variant loading is never replaced', async () => {
  const h=setup(), response=deferred();h.apiImpl=()=>response.promise;
  const loading=h.app.prepareWorkVariant('A','rewrite');h.creator.uncertain=true;
  response.resolve(draft('A'));await loading;
  assert.equal(h.app.knowledgeUI.variant,null);
  assert.equal(h.elements.get('variantDialog').open,false);assert.equal(h.saved.length,0);
});

test('annotation conflict keeps user input and only rebases after explicit action', async () => {
  const h=setup(), latest=item('A',2,'其他窗口已经保存的文字');
  h.app.knowledgeUI.editing='A';h.app.knowledgeUI.revision=1;
  h.field('knowledgeTitle','本地名称');h.field('knowledgeNotes','本地待保留的标注');
  h.field('knowledgeTags','汽车，沙龙');h.elements.get('knowledgeFavorite').checked=true;
  let puts=0;
  h.apiImpl=async(route, options, body)=>{
    if(options.method==='PUT'){
      puts++;
      if(puts===1)throw Object.assign(new Error('版本已经更新'),{httpStatus:409});
      assert.equal(body.expected_revision,2);assert.equal(body.notes,'本地待保留的标注');
      return {item:{...latest,...body,revision:3},created:false};
    }
    return route.includes('?')?{items:[]}:{item:latest};
  };
  await h.app.saveKnowledge({preventDefault(){}});
  assert.equal(puts,1,'发生冲突后不能自动写入覆盖最新版本');
  assert.equal(h.elements.get('knowledgeNotes').value,'本地待保留的标注');
  assert.equal(h.app.knowledgeUI.revision,1);assert.equal(h.elements.get('knowledgeConflict').hidden,false);
  assert.match(h.elements.get('knowledgeConflictText').textContent,/其他窗口已经保存的文字/);
  h.elements.get('knowledgeRebase').onclick();await tick();
  assert.equal(puts,2);assert.equal(h.app.knowledgeUI.revision,3);
  assert.equal(h.elements.get('knowledgeConflict').hidden,true);
});

test('double save submits one PUT; uncertain replay retains the same job and revision', async () => {
  const h=setup(), response=deferred();h.app.knowledgeUI.editing='A';h.app.knowledgeUI.revision=0;
  h.field('knowledgeTitle','新收藏');h.field('knowledgeNotes','值得学习的开场');
  h.apiImpl=()=>response.promise;
  const first=h.app.saveKnowledge({preventDefault(){}});
  await h.app.saveKnowledge({preventDefault(){}});
  assert.equal(h.calls.length,1);
  response.reject(Object.assign(new Error('结果待核对'),{uncertain:true}));await first;
  assert.equal(h.app.knowledgeUI.revision,0);assert.equal(h.elements.get('knowledgeNotes').value,'值得学习的开场');
  h.apiImpl=async(route,options)=>options.method==='PUT'?{item:item('A'),created:false}:{items:[]};
  await h.app.saveKnowledge({preventDefault(){}});
  const puts=h.calls.filter(call=>call.options.method==='PUT');
  assert.equal(puts.length,2);assert.equal(puts[0].route,puts[1].route);
  assert.deepEqual(plain(puts[0].body),plain(puts[1].body));
  assert.ok(!h.calls.some(call=>/\/api\/(creations|jobs|scripts)/.test(call.route)));
});

test('late annotation save never closes or alters a newer editing session, including A to B to A', async t => {
  for (const result of ['success','conflict']) for (const returnToA of [false,true]) {
    await t.test(result+(returnToA?' A-B-A':' A-B'), async () => {
      const h=setup(), pending=deferred();let version=1;
      h.apiImpl=async(route,options)=>{
        if(options.method==='PUT')return pending.promise;
        if(route.includes('?'))return {items:[]};
        return {item:item(route.split('/').at(-1),version)};
      };
      await h.app.collectWork('A');h.field('knowledgeNotes','A 的待保存文字');
      const saving=h.app.saveKnowledge({preventDefault(){}});
      h.elements.get('knowledgeDialog').close();version=6;
      await h.app.collectWork('B');
      if(returnToA){h.elements.get('knowledgeDialog').close();await h.app.collectWork('A');}
      h.field('knowledgeNotes','当前窗口的人工编辑');const closes=h.elements.get('knowledgeDialog').closes;
      if(result==='success')pending.resolve({item:item('A',2),created:false});
      else pending.reject(Object.assign(new Error('版本冲突'),{httpStatus:409}));
      await saving;
      assert.equal(h.app.knowledgeUI.revision,6);
      assert.equal(h.elements.get('knowledgeDialog').open,true);
      assert.equal(h.elements.get('knowledgeDialog').closes,closes);
      assert.equal(h.elements.get('knowledgeNotes').value,'当前窗口的人工编辑');
      assert.equal(h.elements.get('knowledgeSave').disabled,false);
      assert.equal(h.app.knowledgeUI.conflict,null);
      assert.equal(h.app.knowledgeUI.editing,returnToA?'A':'B');
    });
  }
});

test('stored library text renders escaped and a nonfavorite cannot be selected as reference', () => {
  const h=setup(), saved=item('A');saved.title='<img src=x onerror=alert(1)>';saved.notes='<script>overwrite()</script>';
  saved.tags=['<b>标签</b>'];saved.favorite=false;h.app.knowledgeUI.items=[saved];h.app.knowledgeUI.loaded=true;
  h.app.renderKnowledge();const html=h.elements.get('knowledgeCards').innerHTML;
  assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>'));
  h.app.referenceWork('A');assert.deepEqual(plain(h.production.referenceIds),[]);assert.equal(h.calls.length,0);
});
