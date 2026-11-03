'use strict';

// Named drafts contain form values only. Task identities and credentials stay out.
const workDrafts = (() => {
  const KEY = 'cutflow.work.drafts.v1', LIMIT = 30;
  const creatorLimits = {creatorBrief:3000,creatorTitle:160,creatorDuration:80,creatorAspect:80,
    creatorPace:80,creatorOriginalAudio:80,creatorMusic:80,creatorOpeningTitle:160};
  const productionLimits = {productionSubtype:80,productionScript:1600,productionVoice:160,
    productionStyle:80,productionAspect:80,productionDuration:80,productionSourceAudio:80,
    productionBrand:120,productionDate:80,productionPlace:160,productionCTA:240,productionOpeningTitle:160};
  const checkFields = ['productionMusic','productionSubtitles','productionLoop'];
  const modes = new Set(['edit','talking_head','narrated_promo','brand_promo']);
  const labels = {edit:'原声剪辑',talking_head:'人物口播',narrated_promo:'独白宣传',brand_promo:'企业／沙龙宣传'};
  let dialog, list, status, search, archived, savePanel, nameInput, summary;
  const available = () => typeof localPage !== 'undefined' && localPage &&
    ['localhost','127.0.0.1','[::1]','::1'].includes(location.hostname);
  const field = id => document.getElementById(id);
  function error(message){ return new Error(message); }
  function text(value, limit, fallback='') {
    if (value == null) return fallback;
    if (typeof value !== 'string' || value.length > limit) throw error('草稿内容超出可保存范围，当前输入仍然保留。');
    return value;
  }
  function jobId(value) {
    if (value == null || value === '') return null;
    if (typeof value !== 'string' || !/^[a-f0-9]{32}$/.test(value)) throw error('草稿中的作品关联无效，未覆盖当前内容。');
    return value;
  }
  function cleanSnapshot(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw error('这份草稿内容不完整，未覆盖当前内容。');
    const source = value.creator || {}, p = value.production || {}, creatorValues = {}, productionValues = {};
    for (const [id, max] of Object.entries(creatorLimits)) creatorValues[id] = text(source.fields?.[id], max);
    for (const [id, max] of Object.entries(productionLimits)) productionValues[id] = text(p.fields?.[id], max);
    for (const id of checkFields) {
      if (p.fields?.[id] != null && typeof p.fields[id] !== 'boolean') throw error('草稿的选项格式无效。');
      productionValues[id] = p.fields?.[id] === true;
    }
    if (!Array.isArray(source.selected) || source.selected.length > 12 || source.selected.some(s =>
      typeof s !== 'string' || s.length > 4202 || !/^[sl]:./.test(s))) throw error('草稿的素材清单无效，未覆盖当前内容。');
    if (!modes.has(p.mode) || !['public','personal'].includes(p.kind)) throw error('草稿的制作方式无效。');
    const portrait = p.portraitId == null ? null : text(p.portraitId,100);
    if (portrait && !/^[a-zA-Z0-9_-]+$/.test(portrait)) throw error('草稿的人物编号无效。');
    if (!Array.isArray(p.referenceIds) || p.referenceIds.length > 3) throw error('草稿的参考作品清单无效。');
    const refs = p.referenceIds.map(jobId);
    if (refs.some(id => !id)) throw error('草稿的参考作品编号无效。');
    const parent = jobId(p.parentId), revision = p.revisionKind == null ? null : p.revisionKind;
    if (parent && !['rewrite','presenter','remix'].includes(revision) || !parent && revision != null)
      throw error('草稿的新版本关联不完整。');
    return {creator:{selected:[...new Set(source.selected)],fields:creatorValues},production:{
      mode:p.mode,kind:p.kind,portraitId:portrait,preferredVoice:p.preferredVoice == null ? null : text(p.preferredVoice,160),
      voiceExplicit:p.voiceExplicit === true,fields:productionValues,referenceIds:[...new Set(refs)],
      parentId:parent,parentTitle:parent ? text(p.parentTitle,160) : null,revisionKind:revision}};
  }
  function capture() {
    const c = {}, p = {};
    for (const id of Object.keys(creatorLimits)) c[id] = field(id)?.value || '';
    for (const id of Object.keys(productionLimits)) p[id] = field(id)?.value || '';
    for (const id of checkFields) p[id] = field(id)?.checked === true;
    return cleanSnapshot({creator:{selected:[...creator.selected],fields:c},production:{
      mode:production.mode,kind:production.kind,portraitId:production.portraitId,
      preferredVoice:production.preferredVoice,voiceExplicit:production.voiceExplicit,fields:p,
      referenceIds:[...(production.referenceIds || [])],parentId:production.parentId,
      parentTitle:production.parentTitle,revisionKind:production.revisionKind}});
  }
  function hasContent(snapshot) {
    const c = snapshot.creator, p = snapshot.production;
    return c.selected.length || p.portraitId || p.parentId || p.referenceIds.length ||
      ['creatorBrief','creatorTitle','creatorOpeningTitle'].some(id => c.fields[id].trim()) ||
      ['productionScript','productionBrand','productionCTA','productionDate','productionPlace','productionOpeningTitle'].some(id => p.fields[id].trim());
  }
  function defaultName(snapshot) {
    const name = snapshot.creator.fields.creatorBrief.trim() || snapshot.production.parentTitle ||
      snapshot.production.fields.productionScript.trim() || snapshot.creator.fields.creatorTitle.trim();
    return Array.from(name || labels[snapshot.production.mode]+'草稿').slice(0,30).join('');
  }
  function requireLocal() {
    if (!available()) throw error('多份草稿只保存在本机工作台，请从本机入口打开。');
  }
  function read() {
    requireLocal();let raw;
    try { raw = localStorage.getItem(KEY); } catch { throw error('浏览器未允许本机保存草稿，当前输入未改变。'); }
    if (!raw) return {raw:null,drafts:[]};
    let value;
    try { value = JSON.parse(raw); } catch { throw error('本机草稿记录无法读取，现有记录和当前输入都未改动。'); }
    if (value?.schema !== 1 || !Array.isArray(value.drafts) || value.drafts.length > LIMIT)
      throw error('本机草稿格式暂不支持，现有记录和当前输入都未改动。');
    const ids = new Set();
    const drafts = value.drafts.map(d => {
      if (!d || typeof d.id !== 'string' || !/^[a-f0-9-]{36}$/i.test(d.id) || ids.has(d.id) ||
        typeof d.archived !== 'boolean' || typeof d.created_at !== 'string' || typeof d.updated_at !== 'string')
        throw error('本机草稿记录不完整，未覆盖已有记录。');
      ids.add(d.id);
      return {id:d.id,name:text(d.name,80),archived:d.archived,created_at:text(d.created_at,40),
        updated_at:text(d.updated_at,40),snapshot:cleanSnapshot(d.snapshot)};
    });
    return {raw,drafts};
  }
  function write(book) {
    try {
      if (localStorage.getItem(KEY) !== book.raw) throw error('另一个窗口刚更新了草稿，请重新打开草稿列表再操作。');
      localStorage.setItem(KEY,JSON.stringify({schema:1,drafts:book.drafts}));
    } catch (e) {
      if (e.message?.includes('另一个窗口')) throw e;
      throw error('本机保存失败，可能是浏览器空间不足或禁止存储。当前输入仍在，请先保留好文字再继续。');
    }
  }
  function store(snapshot, name, automatic) {
    const book = read(), key = JSON.stringify(snapshot);
    const same = book.drafts.find(d => JSON.stringify(d.snapshot) === key && (automatic || d.name === name));
    if (same) return {draft:same,reused:true};
    if (book.drafts.length >= LIMIT) throw error('已保存 30 份草稿（含归档）。当前内容未覆盖，请先保留文字；本次无法另存或切换到其他草稿。');
    const now = new Date().toISOString(), draft = {id:crypto.randomUUID(),name,archived:false,
      created_at:now,updated_at:now,snapshot};
    book.drafts.unshift(draft);write(book);return {draft,reused:false};
  }
  function requireIdle() {
    if (creatorInputLocked() || production.checking || production.uncertain ||
      ['queued','running'].includes(production.scriptJob?.status))
      throw error('当前制作或讲稿任务仍需核对，完成后再恢复或切换草稿。当前输入已保留。');
    if (creator.uploading || production.uploading)
      throw error('素材或人物照片正在上传，完成后再恢复或切换草稿。');
  }
  function stash() {
    // Remote pages keep their existing workflow; named storage is local only.
    if(!available())return null;
    requireIdle();const snapshot = capture();
    if (!hasContent(snapshot)) return null;
    return store(snapshot,defaultName(snapshot),true);
  }
  function message(value, failure=false) {
    status.textContent = value;status.classList.toggle('is-error',failure);
  }
  function button(label, action, className='button subtle') {
    const b=document.createElement('button');b.type='button';b.className=className;b.textContent=label;
    b.addEventListener('click',()=>{try { action(); } catch(e) { message(e.message,true); }});return b;
  }
  function changeArchive(id) {
    const book=read(), draft=book.drafts.find(d=>d.id===id);
    if(!draft)throw error('这份草稿已变化，请重新打开列表。');
    draft.archived=!draft.archived;draft.updated_at=new Date().toISOString();write(book);
    render();message(draft.archived?'已归档。归档不会删除文字或释放名额。':'已移回草稿列表。');
  }
  function restore(id) {
    requireIdle();const book=read(), record=book.drafts.find(d=>d.id===id);
    if(!record)throw error('这份草稿已变化，请重新打开列表。');
    const snapshot=cleanSnapshot(record.snapshot), saved=stash();
    // Everything that can reject the switch runs before changing the current form.
    clearTimeout(production.pollTimer);production.pollTimer=null;production.scriptId=null;
    production.scriptBody=null;production.scriptJob=null;production.scriptContext=null;
    production.checking=false;production.uncertain=false;
    creator.requestId=null;creator.requestBody=null;creator.resultId=null;creator.uncertain=false;
    creator.selected=[...snapshot.creator.selected];
    for(const [id,value] of Object.entries(snapshot.creator.fields))if(field(id))setDraftFieldValue(id,value);
    const p=snapshot.production;
    for(const key of ['mode','kind','portraitId','preferredVoice','voiceExplicit','parentId','parentTitle','revisionKind'])production[key]=p[key];
    production.referenceIds=[...p.referenceIds];
    for(const [id,value] of Object.entries(p.fields))if(field(id)){
      if(checkFields.includes(id))field(id).checked=value;else setDraftFieldValue(id,value);
    }
    if(field('presenterConsent'))field('presenterConsent').checked=false;
    if(field('productionScriptPreview'))field('productionScriptPreview').hidden=true;
    for(const id of ['productionScriptSuggestion','productionScriptAssumptions'])if(field(id))field(id).textContent='';
    if(typeof knowledgeUI!=='undefined'){knowledgeUI.variant=null;knowledgeUI.variantRequest++;}
    document.querySelectorAll('[data-presenter-kind]').forEach(e=>e.classList.toggle('active',e.dataset.presenterKind===production.kind));
    saveCreatorDraft();saveProductionDraft();
    dialog.close();showPage('create');renderCreatorMaterials();renderWorkContext();
    if(typeof renderPresenterCatalog==='function')renderPresenterCatalog();
    updateProductionAccess();renderCreatorResult();
    scriptMessage('已恢复草稿，讲稿可继续编辑；这次不会自动生成或发送。');
    const note=saved?'切换前的内容已保存在'+(saved.draft.archived?'归档':'草稿列表')+'。':'当前空白内容无需备份。';
    toast('已恢复“'+record.name+'”。'+note+(production.kind==='personal'?' 使用本人人物前请重新确认授权。':''));
  }
  function render() {
    const book=read(), query=search.value.trim().toLocaleLowerCase();list.replaceChildren();
    summary.textContent='本机已保存 '+book.drafts.length+' / '+LIMIT+' 份 · 归档也计入数量';
    const shown=book.drafts.filter(d=>d.archived===archived.checked&&(!query||
      [d.name,d.snapshot.creator.fields.creatorBrief,d.snapshot.production.fields.productionScript].join(' ').toLocaleLowerCase().includes(query)));
    for(const draft of shown){
      const card=document.createElement('article');card.className='work-draft-card';
      const header=document.createElement('div');header.className='work-draft-card-heading';
      const name=document.createElement('h3');name.textContent=draft.name;header.append(name);
      const mode=document.createElement('span');mode.className='work-draft-mode';mode.textContent=labels[draft.snapshot.production.mode];header.append(mode);card.append(header);
      const detail=document.createElement('p');detail.className='work-draft-description';
      const p=draft.snapshot.production,c=draft.snapshot.creator;
      detail.textContent=Array.from(c.fields.creatorBrief||p.fields.productionScript||'已保存素材选择与制作设置。').slice(0,120).join('');card.append(detail);
      const metadata=document.createElement('p');metadata.className='help';
      const date=new Date(draft.updated_at);metadata.textContent=(Number.isNaN(date.getTime())?'':date.toLocaleString('zh-CN'))+
        ' · '+c.selected.length+' 段素材'+(p.parentId?' · 原作品的新版本':'')+(p.referenceIds.length?' · '+p.referenceIds.length+' 份参考':'');card.append(metadata);
      const actions=document.createElement('div');actions.className='work-draft-actions';
      actions.append(button('恢复到制作台',()=>restore(draft.id),'button primary'));
      actions.append(button(draft.archived?'移回草稿':'归档',()=>changeArchive(draft.id)));card.append(actions);list.append(card);
    }
    if(!shown.length){const empty=document.createElement('p');empty.className='work-drafts-empty';
      empty.textContent=query?'没有找到匹配的草稿。':archived.checked?'还没有归档草稿。':'还没有保存草稿。写好想法后，可以在这里命名保存。';list.append(empty);}
  }
  function ensureDialog() {
    if(dialog)return;
    dialog=document.createElement('dialog');dialog.id='workDraftsDialog';dialog.className='work-drafts-dialog';
    dialog.innerHTML='<div class="work-drafts-heading"><div><div class="eyebrow">LOCAL DRAFTS</div><h2>我的创作草稿</h2></div><button type="button" class="close-button" data-drafts-close aria-label="关闭草稿">×</button></div>'+
      '<p class="help">只保存在这台电脑的当前浏览器中。草稿保存文字、素材选择与设置，不复制视频，也不会自动制作或发布。</p>'+
      '<form class="work-draft-save" hidden><label>草稿名称<input maxlength="80" required autocomplete="off" placeholder="为这次创作取个名字"></label><div class="work-draft-actions"><button class="button primary" type="submit">另存这份草稿</button><button class="button subtle" type="button" data-drafts-cancel>暂不保存</button></div></form>'+
      '<div class="work-drafts-tools"><label>搜索草稿<input type="search" placeholder="名称、想法或讲稿" maxlength="200"></label><label class="work-drafts-archive"><input type="checkbox">查看归档</label></div>'+
      '<p class="help" data-drafts-summary></p><p class="work-drafts-status" role="status" aria-live="polite"></p><div class="work-drafts-list"></div>';
    document.body.append(dialog);
    list=dialog.querySelector('.work-drafts-list');status=dialog.querySelector('.work-drafts-status');
    search=dialog.querySelector('input[type="search"]');archived=dialog.querySelector('input[type="checkbox"]');
    savePanel=dialog.querySelector('form');nameInput=savePanel.querySelector('input');summary=dialog.querySelector('[data-drafts-summary]');
    dialog.querySelector('[data-drafts-close]').addEventListener('click',()=>dialog.close());
    dialog.querySelector('[data-drafts-cancel]').addEventListener('click',()=>{savePanel.hidden=true;message('当前输入保持不变。');});
    const refresh=()=>{try{render();}catch(e){message(e.message,true);}};
    search.addEventListener('input',refresh);archived.addEventListener('change',refresh);
    savePanel.addEventListener('submit',event=>{
      event.preventDefault();try{
        const snapshot=capture(),name=nameInput.value.trim();
        if(!hasContent(snapshot))throw error('先写下想法、讲稿或选择素材，再保存草稿。');
        if(!name||name.length>80)throw error('请填写 1–80 个字的草稿名称。');
        const result=store(snapshot,name,false);savePanel.hidden=true;archived.checked=result.draft.archived;search.value='';render();
        message(result.reused?'同名、同内容的草稿已保存，没有重复占用名额。':'已保存。你可以继续修改当前内容，下次再另存新草稿。');
      }catch(e){message(e.message,true);}
    });
    window.addEventListener('storage',event=>{if(event.key===KEY&&dialog.open){try{render();message('另一个窗口更新了草稿列表，当前名称输入未改变。');}catch(e){message(e.message,true);}}});
  }
  function open(save=false) {
    requireLocal();ensureDialog();savePanel.hidden=!save;message('');
    if(save)nameInput.value=defaultName(capture());
    render();if(!dialog.open)dialog.showModal();if(save){nameInput.focus();nameInput.select();}else search.focus();
  }
  function bind() {
    for(const [id,save] of [['saveWorkDraftButton',true],['openWorkDraftsButton',false]]){
      const node=field(id);if(!node)continue;node.hidden=!available();
      node.addEventListener('click',()=>{try{open(save);}catch(e){toast(e.message,true);}});
    }
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',bind,{once:true});else bind();
  return {open,stash};
})();

function openWorkDrafts(){ return workDrafts.open(false); }
function saveNamedWorkDraft(){ return workDrafts.open(true); }
function stashBeforeWorkVariant(){ return workDrafts.stash(); }
