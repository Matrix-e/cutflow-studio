'use strict';
const CREATOR_DRAFT='cutflow.creator.draft.v1';
const creator={selected:[],assetLabels:{},uploads:new Map(),uploading:false,busy:false,resultId:null,requestId:null,requestBody:null,uncertain:false};
const creatorReplacement={draft:null};
const creatorAssetResolution={key:'',sequence:0,timer:null,controller:null,status:'idle',results:new Map(),revision:0,signature:null};
const creatorFields=['creatorBrief','creatorTitle','creatorDuration','creatorAspect','creatorPace','creatorOriginalAudio','creatorMusic','creatorOpeningTitle','creatorSubtitles'];
function setDraftFieldValue(id,value){const field=$('#'+id),text=String(value??'');if(field.tagName==='SELECT'&&text&&![...field.options].some(option=>option.value===text)){const option=document.createElement('option');option.value=text;option.textContent='草稿设置：'+text;field.appendChild(option);}field.value=text;}
function creatorInputLocked(){return creator.busy||creator.uncertain;}
function saveCreatorDraft(){
  if(!localPage||typeof localStorage==='undefined')return;
  try{const assetLabels=creatorAssetLabels();localStorage.setItem(CREATOR_DRAFT,JSON.stringify({fields:Object.fromEntries(creatorFields.map(id=>[id,id==='creatorSubtitles'?$('#'+id).checked:$('#'+id).value])),selected:creator.selected.slice(0,12),assetLabels,resultId:creator.resultId,requestId:creator.requestId,requestBody:creator.requestBody,uncertain:creator.uncertain}));}catch{}
}
function loadCreatorDraft(){
  if(!localPage||typeof localStorage==='undefined')return;
  try{const draft=JSON.parse(localStorage.getItem(CREATOR_DRAFT)||'null');if(!draft)return;
    for(const id of creatorFields){if(id==='creatorSubtitles'){if(typeof draft.fields?.[id]==='boolean')$('#'+id).checked=draft.fields[id];}else if(typeof draft.fields?.[id]==='string')setDraftFieldValue(id,draft.fields[id].slice(0,id==='creatorBrief'?3000:160));}
    if(Array.isArray(draft.selected))creator.selected=[...new Set(draft.selected.filter(v=>typeof v==='string'&&v.length<=4200))].slice(0,12);
    creator.assetLabels=Object.fromEntries(creator.selected.filter(key=>typeof draft.assetLabels?.[key]==='string'&&draft.assetLabels[key].trim()).map(key=>[key,draft.assetLabels[key].trim().slice(0,240)]));
    if(typeof draft.resultId==='string'&&/^[a-zA-Z0-9_-]+$/.test(draft.resultId))creator.resultId=draft.resultId;
    if(typeof draft.requestId==='string'&&draft.requestBody&&typeof draft.requestBody==='object'&&draft.uncertain){creator.requestId=draft.requestId;creator.requestBody=draft.requestBody;creator.uncertain=true;}
  }catch{}
}
function creatorFeedback(text,error=false){$('#creatorFeedback').textContent=text;$('#creatorFeedback').className='media-status'+(error?' error':'');}
function creatorCatalogUsable(){return typeof assetCatalogUsable==='function'&&assetCatalogUsable()===true;}
function creatorCatalogComplete(){return typeof assetCatalogCurrent==='function'&&assetCatalogCurrent()===true;}
function creatorLocalImportsTrusted(){return typeof localImports!=='undefined'&&!localImports.error&&typeof localImportAvailable==='function'&&localImportAvailable()&&localImports.token===state.token;}
function creatorResolutionInputs(){
  const imports=creatorLocalImportsTrusted()?localImports.items:[],associations=new Map();
  for(const key of creator.selected.slice(0,12)){
    const receipt=key.startsWith('l:')?imports.find(item=>'l:'+item.id===key&&item.status==='synced'):null;
    const source=key.startsWith('s:')?key.slice(2):receipt?.remote_source;
    if(typeof source==='string'&&source.length>0&&source.length<=4096)associations.set(key,source);
  }
  const sources=[...new Set([...associations.values()].filter(source=>!state.assets.some(asset=>asset.source===source)))].sort();
  return {sources,associations};
}
function creatorResolutionDraftContext(inputs){
  // A confirmed l: → s: identity mapping is the same source, not a new draft.
  const p=typeof production==='undefined'?null:production;
  return JSON.stringify({selected:creator.selected.map(key=>inputs.associations.has(key)?'s:'+inputs.associations.get(key):key),
    fields:creatorFields.map(id=>id==='creatorSubtitles'?$('#'+id).checked:$('#'+id).value),production:p?{mode:p.mode,kind:p.kind,portraitId:p.portraitId,parentId:p.parentId,parentTitle:p.parentTitle,revisionKind:p.revisionKind,referenceIds:p.referenceIds,
      fields:productionFields.map(id=>productionChecks.has(id)?$('#'+id).checked:$('#'+id).value),consent:$('#presenterConsent').checked}:null});
}
function creatorResolutionSnapshot(){
  const inputs=creatorResolutionInputs(),enabled=state.config.asset_resolution_supported===true&&creatorCatalogUsable();
  const key=enabled&&inputs.sources.length?JSON.stringify([state.base,state.token,state.epoch,state.assetCatalog.round,inputs.sources,creatorResolutionDraftContext(inputs)]):'';
  const matching=!!key&&key===creatorAssetResolution.key;
  return {...inputs,key,enabled,status:matching?creatorAssetResolution.status:key?'pending':'idle',results:matching&&creatorAssetResolution.status==='ready'?creatorAssetResolution.results:new Map()};
}
function creatorResolutionPending(){const view=creatorResolutionSnapshot();return !!view.key&&['pending','loading'].includes(view.status);}
function commitCreatorResolution(results,status){
  const signature=JSON.stringify([...results.entries()].sort(([a],[b])=>a.localeCompare(b)).map(([source,result])=>[source,result.status,result.asset||null]));
  if(signature!==creatorAssetResolution.signature){creatorAssetResolution.signature=signature;creatorAssetResolution.revision++;}
  creatorAssetResolution.results=results;creatorAssetResolution.status=status;
}
function scheduleCreatorAssetResolution(){
  const view=creatorResolutionSnapshot();
  if(view.key===creatorAssetResolution.key)return;
  clearTimeout(creatorAssetResolution.timer);creatorAssetResolution.timer=null;
  creatorAssetResolution.controller?.abort();creatorAssetResolution.controller=null;
  creatorAssetResolution.sequence++;creatorAssetResolution.key=view.key;creatorAssetResolution.results=new Map();creatorAssetResolution.status=view.key?'pending':'idle';
  if(!view.key)return;
  const sequence=creatorAssetResolution.sequence;
  // One query per exact directory round and draft context; rendering never retries it.
  creatorAssetResolution.timer=setTimeout(()=>{creatorAssetResolution.timer=null;resolveCreatorAssets(view.key,sequence);},250);
}
function creatorResolutionBatches(sources){
  const batches=[];let batch=[];const encoder=new TextEncoder();
  for(const source of sources){
    const next=[...batch,source];
    if(encoder.encode(JSON.stringify({sources:next})).byteLength>60000&&batch.length){batches.push(batch);batch=[source];}else batch=next;
  }
  if(batch.length)batches.push(batch);return batches;
}
async function resolveCreatorAssets(key,sequence){
  const active=()=>sequence===creatorAssetResolution.sequence&&key===creatorAssetResolution.key&&key===creatorResolutionSnapshot().key;
  if(!active())return;
  const context=connectionContext(),sources=creatorResolutionSnapshot().sources,results=new Map(),controller=new AbortController();
  creatorAssetResolution.controller=controller;creatorAssetResolution.status='loading';
  try{
    renderCreatorMaterials();
    for(const batch of creatorResolutionBatches(sources)){
      if(!active()||!currentContext(context))return;
      const response=await api('/api/assets/resolve',{method:'POST',body:JSON.stringify({sources:batch}),signal:controller.signal},'json',context);
      if(!active()||!currentContext(context))return;
      if(response?.limit!==12||!Array.isArray(response.results)||response.results.length>batch.length)throw new Error('素材核对结果不完整。');
      const seen=new Set();
      for(const entry of response.results){
        if(!entry||!batch.includes(entry.source)||seen.has(entry.source)||!['found','not_found','unconfirmed'].includes(entry.status))throw new Error('素材核对结果不完整。');
        seen.add(entry.source);
        if(entry.status==='found'&&(!entry.asset||entry.asset.source!==entry.source||typeof entry.asset.name!=='string'||(entry.asset.display_name!=null&&typeof entry.asset.display_name!=='string')||!Number.isFinite(entry.asset.size)||entry.asset.size<0))throw new Error('素材信息尚未确认。');
        results.set(entry.source,entry.status==='found'?{status:'found',asset:entry.asset}:{status:entry.status});
      }
      for(const source of batch)if(!seen.has(source))results.set(source,{status:'unconfirmed'});
    }
    if(active())commitCreatorResolution(results,'ready');
  }catch{
    // This is a read. A failed lookup never becomes an uncertain creation/publish.
    if(active())commitCreatorResolution(new Map(),'failed');
  }finally{
    if(creatorAssetResolution.controller===controller)creatorAssetResolution.controller=null;
    if(active()){
      if(creatorAssetResolution.status==='loading')commitCreatorResolution(new Map(),'failed');
      renderCreatorMaterials();
    }
  }
}
function creatorSourceLabel(key){
  if(key.startsWith('l:'))return '未记录原名（导入编号：'+key.slice(2)+')';
  return (key.replace(/^s:/,'').split(/[\\/]/).pop()||'未记录素材名称').replace(/--[a-f0-9]{16}(?=\.[^.]+$|$)/i,'').slice(0,240);
}
function creatorAssetLabels(pool=creatorPool()){
  const previous=creator.assetLabels||{};
  creator.assetLabels=Object.fromEntries(creator.selected.map(key=>{const item=pool.find(asset=>asset.key===key),known=item?.labelKnown?item.name:previous[key];return [key,(typeof known==='string'&&known.trim()?known.trim():creatorSourceLabel(key)).slice(0,240)];}));
  return {...creator.assetLabels};
}
function creatorPool(){
  const usable=creatorCatalogUsable(),resolution=creatorResolutionSnapshot(),assets=new Map(state.assets.map(asset=>[asset.source,asset])),unverified=new Set();
  if(typeof creatorSelectedBrowseAssets==='function')for(const asset of creatorSelectedBrowseAssets())if(!assets.has(asset.source)){assets.set(asset.source,asset);unverified.add(asset.source);}
  for(const [source,result] of resolution.results)if(result.status==='found'){assets.set(source,result.asset);unverified.delete(source);}
  const pool=[...assets.values()].map(asset=>{const ready=usable&&!unverified.has(asset.source),missing=resolution.results.get(asset.source)?.status==='not_found';return {key:'s:'+asset.source,source:asset.source,name:asset.display_name||asset.name||creatorSourceLabel('s:'+asset.source),size:asset.size,ready,labelKnown:usable,availability:ready?'ready':missing?'missing':'pending_catalog',status:ready?'已同步 · 可制作':missing?'当前素材未找到 · 可在此位置替换':unverified.has(asset.source)?'已选入 · 等待核对素材':'素材目录待确认'};});
  const imports=typeof localImports==='undefined'?[]:localImports.items;
  const localTrusted=creatorLocalImportsTrusted();
  for(const item of imports){
    const asset=localTrusted&&typeof item.remote_source==='string'&&item.remote_source?pool.find(a=>a.ready&&a.source===item.remote_source):null,localKey='l:'+item.id;
    if(asset){if(creator.selected.includes(localKey))creator.assetLabels[asset.key]=(asset.name||creator.assetLabels[localKey]||item.name||creatorSourceLabel(asset.key)).slice(0,240);creator.selected=creator.selected.map(k=>k===localKey?asset.key:k);continue;}
    const waiting=localTrusted&&usable&&item.status==='waiting_sync',resolved=resolution.results.get(resolution.associations.get(localKey)),missing=resolved?.status==='not_found';
    pool.unshift({key:localKey,name:item.name||creator.assetLabels[localKey]||creatorSourceLabel(localKey),size:item.size,source:null,ready:false,labelKnown:localTrusted,availability:missing?'missing':waiting?'waiting_sync':'pending_import',status:missing?'当前素材未找到 · 可在此位置替换':!localTrusted?'本机导入记录待确认':!usable?'远端素材目录待确认':item.status==='synced'?'同步记录存在，远端素材待确认':waiting?'已上传 · 等待同步':'导入状态待确认'});
  }
  for(const [id,item] of creator.uploads){if(!imports.some(i=>i.id===id))pool.unshift({key:'l:'+id,name:item.file.name,size:item.file.size,ready:false,labelKnown:true,availability:item.error?'import_error':'uploading',status:item.error?'导入需处理':item.status,retryId:item.error?id:null});}
  creator.selected=[...new Set(creator.selected)];return pool;
}
function creatorSelection(pool=creatorPool()){
  const labels=creatorAssetLabels(pool),resolution=creatorResolutionSnapshot();
  return creator.selected.map(key=>{const item=pool.find(asset=>asset.key===key);if(item)return {...item,name:labels[key]||item.name};const missing=resolution.results.get(resolution.associations.get(key))?.status==='not_found';return {key,name:labels[key]||creatorSourceLabel(key),ready:false,availability:missing?'missing':key.startsWith('l:')?'pending_import':'pending_catalog',status:missing?'当前素材未找到 · 可在此位置替换':key.startsWith('l:')?'导入记录待确认 · 不代表素材已删除':resolution.status==='loading'||resolution.status==='pending'?'正在核对已选素材信息':resolution.status==='failed'?'素材核对暂未完成 · 刷新后再确认':'素材信息待确认 · 目录未列出不代表不存在'};});
}
function creatorMaterialReadiness(selected){
  const unresolved=selected.filter(item=>!item.ready);if(!unresolved.length)return '';
  const counts={missing:0,waiting_sync:0,uploading:0,import_error:0,pending:0};
  for(const item of unresolved){if(Object.hasOwn(counts,item.availability))counts[item.availability]++;else counts.pending++;}
  return [counts.missing?counts.missing+' 段在当前素材库未找到，可在原位置替换':'',counts.pending?counts.pending+' 段素材或导入记录待确认':'',counts.waiting_sync?counts.waiting_sync+' 段等待同步':'',counts.uploading?counts.uploading+' 段正在导入':'',counts.import_error?counts.import_error+' 段导入需处理':''].filter(Boolean).join('；')+'。';
}
function creatorReplacementLocked(){
  const p=typeof production==='undefined'?null:production,idea=typeof ideaAssistant==='undefined'?null:ideaAssistant;
  return creatorInputLocked()||creator.uploading||creatorResolutionPending()||(typeof localImports!=='undefined'&&localImports.busy)||!!(idea&&(idea.uncertain||idea.checking||idea.pending||['queued','running'].includes(idea.job?.status)))||!!(p&&(p.uploading||p.checking||p.uncertain||['queued','running'].includes(p.scriptJob?.status)));
}
let creatorGridSignature=null;
function renderCreatorMaterials(){
  ensureCreatorSearchControls();syncAssetBrowsers();
  scheduleCreatorAssetResolution();
  const previousLabels=JSON.stringify([creator.selected,creator.assetLabels]);
  const pool=creatorPool(),query=$('#creatorAssetSearch').value.trim().toLowerCase(),search=assetBrowsers.main,shown=assetSearchSupported()?[...pool.filter(item=>item.key.startsWith('l:')&&item.name.toLowerCase().includes(query)),...assetBrowserItems(search,pool)]:pool.filter(a=>a.name.toLowerCase().includes(query)),locked=creatorInputLocked(),replacementLocked=creatorReplacementLocked()||!creatorCatalogUsable();
  const gridSignature=JSON.stringify([shown,creator.selected,locked,assetBrowserCanChoose(search)]);
  if(gridSignature!==creatorGridSignature){creatorGridSignature=gridSignature;
  $('#creatorAssetGrid').innerHTML=shown.length?shown.map(item=>`<article class="creator-asset-card ${creator.selected.includes(item.key)?'selected':''}"><label><input type="checkbox" data-creator-select="${esc(item.key)}" ${item.searchAsset?'data-search-source="'+esc(item.source)+'"':''} ${creator.selected.includes(item.key)?'checked':''} ${locked||(item.searchAsset&&!assetBrowserCanChoose(search))?'disabled':''} aria-label="选择素材：${esc(item.name)}"><span><strong>${esc(item.name)}</strong><small>${bytes(item.size||0)} · ${esc(item.status)}</small></span></label>${typeof assetInsightHtml==='function'?assetInsightHtml(item):''}<div class="creator-card-actions">${item.source?'<button type="button" class="text-button" data-preview="'+esc(item.source)+'" '+(!item.ready?'disabled':'')+'>预览</button>':''}${item.retryId?'<button type="button" class="text-button" data-creator-retry="'+esc(item.retryId)+'" '+(locked||creator.uploading?'disabled':'')+'>核对并重试导入</button>':''}</div></article>`).join(''):'<div class="creator-asset-empty">'+(assetSearchSupported()?search.busy||!search.snapshot?'搜索结果尚待读取。':search.total===0?'这轮全库搜索没有匹配素材。':'当前搜索页没有可显示的素材。':query?'已加载列表中没有同名素材。':creatorCatalogComplete()?'还没有视频，点击上方「上传视频」。':'素材目录待确认，你可以先写下想法。')+'</div>';
  }
  if(typeof hydrateAssetInsights==='function')hydrateAssetInsights();
  renderAssetBrowserControls(search,$('#creatorSearchStatus'),$('#creatorSearchMore'));
  const selected=creatorSelection(pool);$('#creatorSelectedCount').textContent='已选 '+selected.length+' 段';
  $('#creatorSelection').innerHTML=selected.length?selected.map((item,index)=>`<li class="creator-selected-item ${item.availability==='missing'?'is-missing':!item.ready?'is-unconfirmed':''}"><span class="creator-number">${index+1}</span><span class="creator-selected-name">${esc(item.name)}<small>${esc(item.ready?'已就绪':item.status)}</small></span><div class="creator-order-buttons"><button type="button" class="creator-replace-button" data-creator-replace="${index}" aria-label="替换第 ${index+1} 段：${esc(item.name)}" ${replacementLocked?'disabled':''}>替换</button><button type="button" data-creator-move="${index}" data-direction="-1" aria-label="上移：${esc(item.name)}" ${index===0||locked?'disabled':''}>↑</button><button type="button" data-creator-move="${index}" data-direction="1" aria-label="下移：${esc(item.name)}" ${index===selected.length-1||locked?'disabled':''}>↓</button><button type="button" data-creator-remove="${esc(item.key)}" aria-label="移除：${esc(item.name)}" ${locked?'disabled':''}>×</button></div></li>`).join(''):'<li class="help">勾选上面的素材，或上传新视频；最多选择 12 段。</li>';
  if(previousLabels!==JSON.stringify([creator.selected,creator.assetLabels]))saveCreatorDraft();
  updateCreatorAccess();renderCreatorResult();
}
function updateCreatorAccess(){
  syncAssetBrowsers();
  scheduleCreatorAssetResolution();
  const selected=creatorSelection(),brief=$('#creatorBrief').value.trim(),waiting=selected.filter(a=>!a.ready).length;
  const supported=state.config.creation_supported===true,locked=creatorInputLocked();
  for(const id of creatorFields)$('#'+id).disabled=locked;
  $('#creatorUploadButton').disabled=!localImportAvailable()||!supported||creator.uploading||locked;
  $('#creatorFileInput').disabled=$('#creatorUploadButton').disabled;
  $('#creatorClearSelection').disabled=locked||!selected.length;
  $('#creatorGenerate').disabled=creator.busy||!state.connected||!supported||(!creator.uncertain&&(!brief||!selected.length||waiting>0||creator.uploading));
  $('#creatorGenerate').textContent=creator.busy?'正在提交制作任务…':creator.uncertain?'核对并继续提交':creator.resultId?'按当前想法再做一版 →':'生成视频 →';
  $('#creatorReadiness').textContent=creator.busy?'任务只提交一次，请稍候。':!supported?'制作服务正在连接或升级，你可以先写下想法。':!state.connected?'等待制作服务连接恢复。':creator.uncertain?'上次提交结果待确认；再次点击会使用相同编号核对，不会重复创建。':!selected.length?'先选素材，再写下你的想法。':waiting?creatorMaterialReadiness(selected):!brief?'素材已就绪，写下你的想法就可以生成。':selected.length+' 段素材已就绪 · 生成后进入成片审核';
  if(typeof updateIdeaAccess==='function')updateIdeaAccess();
  if(typeof updateProductionAccess==='function')updateProductionAccess();
  renderCreatorReplacement();
  if(typeof directorSync==='function')directorSync();
}
function toggleCreatorMaterial(key,on){
  if(creatorInputLocked())return;
  if(on&&!creator.selected.includes(key)){if(creator.selected.length>=12){creatorFeedback('一条作品最多选择 12 段素材。',true);renderCreatorMaterials();return;}creator.selected.push(key);}
  if(!on)creator.selected=creator.selected.filter(k=>k!==key);
  saveCreatorDraft();renderCreatorMaterials();
}
function addSourceToCreator(source){
  if(creatorInputLocked())return;
  showPage('create');toggleCreatorMaterial('s:'+source,true);creatorFeedback('已加入待用素材。继续选择视频，或写下想法后生成。');
}
function openCreator(source){showPage('create');if(source)addSourceToCreator(source);else renderCreatorMaterials();(typeof director!=='undefined'&&director.active?$('#directorGoal'):$('#creatorBrief')).focus?.();}
function creatorOptions(){
  const options={subtitles:$('#creatorSubtitles').checked===true};const duration=$('#creatorDuration').value,aspect=$('#creatorAspect').value,pace=$('#creatorPace').value,voice=$('#creatorOriginalAudio').value,music=$('#creatorMusic').value,title=$('#creatorOpeningTitle').value.trim();
  if(duration)options.target_duration=Number(duration);if(aspect)options.aspect=aspect;if(pace)options.pace=pace;if(voice)options.keep_original_audio=voice==='true';if(music)options.music=music==='true';if(title)options.opening_title=title;
  return options;
}
function ensureCreatorReplacementDialog(){
  if($('#creatorReplacementDialog'))return;
  const dialog=document.createElement('dialog');dialog.id='creatorReplacementDialog';dialog.className='creator-replacement-dialog';
  dialog.innerHTML='<div class="dialog-heading"><div><div class="eyebrow">REPLACE MATERIAL</div><h2>在原位置替换素材</h2></div><button type="button" class="close-button" id="creatorReplacementClose" aria-label="关闭替换窗口">×</button></div><p id="creatorReplacementOriginal" class="creator-replacement-original"></p><p class="help">只替换这一段，顺序、想法、讲稿和设置都保留；不会自动生成新任务。已提交任务不受影响。</p><label class="creator-search">查找替换素材<input id="creatorReplacementSearch" type="search" maxlength="100" placeholder="按视频名称查找" autocomplete="off"></label><div class="creator-search-controls"><p id="creatorReplacementSearchStatus" class="help" role="status" aria-live="polite"></p><button type="button" class="text-button" id="creatorReplacementRefresh">刷新搜索</button><button type="button" class="button subtle" id="creatorReplacementMore" hidden>加载更多素材</button></div><div id="creatorReplacementStatus" class="media-status" role="status" aria-live="polite"></div><div id="creatorReplacementList" class="creator-replacement-list" role="radiogroup" aria-label="选择替换素材"></div><div class="creator-replacement-actions"><button type="button" class="button subtle" id="creatorReplacementCancel">取消</button><button type="button" class="button primary" id="creatorReplacementApply" disabled>确认替换这一段</button></div>';
  document.body.appendChild(dialog);
  const close=()=>{creatorReplacement.draft?.verifyController?.abort();cancelAssetBrowser(assetBrowsers.replacement);creatorReplacement.draft=null;dialog.close();};
  $('#creatorReplacementClose').onclick=close;$('#creatorReplacementCancel').onclick=close;
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  dialog.addEventListener('close',()=>{if(!dialog.open){creatorReplacement.draft?.verifyController?.abort();cancelAssetBrowser(assetBrowsers.replacement);creatorReplacement.draft=null;}});
  $('#creatorReplacementList').addEventListener('change',event=>{const radio=event.target.closest('[data-replacement-key]'),draft=creatorReplacement.draft;if(!radio||!draft||radio.disabled||draft.verifying)return;draft.choice=radio.dataset.replacementKey;renderCreatorReplacement();});
  $('#creatorReplacementApply').onclick=applyCreatorReplacement;
  $('#creatorReplacementSearch').oninput=()=>{const draft=creatorReplacement.draft;if(draft){draft.verifyController?.abort();draft.choice=null;}changeAssetBrowserQuery(assetBrowsers.replacement,$('#creatorReplacementSearch').value);};
  $('#creatorReplacementRefresh').onclick=()=>{if(creatorReplacement.draft?.verifying)return;if(creatorReplacement.draft)creatorReplacement.draft.choice=null;refreshAssetBrowser(assetBrowsers.replacement);};
  $('#creatorReplacementMore').onclick=()=>requestAssetBrowser(assetBrowsers.replacement,true);
}
function creatorReplacementContext(){return typeof variantDraftContext==='function'?variantDraftContext():JSON.stringify({selected:creator.selected,fields:creatorFields.map(id=>id==='creatorSubtitles'?$('#'+id).checked:$('#'+id).value)});}
function openCreatorReplacement(index){
  if(creatorReplacementLocked()||!creatorCatalogUsable())return;
  const selected=creatorSelection(),original=selected[index];if(!Number.isInteger(index)||!original)return;
  ensureCreatorReplacementDialog();
  creatorReplacement.draft={index,key:original.key,name:original.name,choice:null,base:state.base,token:state.token,epoch:state.epoch,revision:state.assetCatalog?.revision,resolutionRevision:creatorAssetResolution.revision,context:creatorReplacementContext(),invalid:false};
  $('#creatorReplacementSearch').value='';resetAssetBrowser(assetBrowsers.replacement,'');if(!$('#creatorReplacementDialog').open)$('#creatorReplacementDialog').showModal();renderCreatorReplacement();
}
function creatorReplacementState(draft,pool){
  if(draft.invalid||state.base!==draft.base||state.token!==draft.token||state.epoch!==draft.epoch||state.assetCatalog?.revision!==draft.revision||creatorAssetResolution.revision!==draft.resolutionRevision||creator.selected[draft.index]!==draft.key||creatorReplacementContext()!==draft.context){draft.invalid=true;return {message:'素材目录、核对结果、连接、原位置或草稿内容已经变化，请关闭窗口后重新选择。',candidates:[],blocked:true};}
  if(creatorResolutionPending())return {message:'正在核对已选素材，确认完成后才能替换。原位置和候选选择保持不变。',candidates:[],blocked:true};
  if(creatorReplacementLocked())return {message:'当前正在上传、生成想法或讲稿、核对提交，暂不能替换。原选择保持不变。',candidates:[],blocked:true};
  if(!creatorCatalogUsable())return {message:'正在确认素材目录，请等待最新读取完成；不会使用未经确认的素材替换。',candidates:[],blocked:true};
  const session=assetBrowsers.replacement,query=$('#creatorReplacementSearch').value.trim().toLowerCase();
  const candidates=(assetSearchSupported()?assetBrowserItems(session,pool):pool.filter(item=>item.ready&&item.name.toLowerCase().includes(query))).filter(item=>item.source&&!creator.selected.includes(item.key));
  if(assetSearchSupported()&&!assetBrowserCanChoose(session))return {message:'请先完成或刷新这轮搜索；原位置和已选素材保留。',candidates,blocked:true};
  return {message:candidates.length?'选择一段素材并确认替换。当前目录外的素材会先核对可用性，再修改草稿。':'这轮已加载结果中没有可替换素材；已选素材不会重复列出，可继续加载或换名称查找。',candidates,blocked:false};
}
function renderCreatorReplacement(){
  syncAssetBrowsers();
  const disabled=creatorReplacementLocked()||!creatorCatalogUsable();document.querySelectorAll('[data-creator-replace]').forEach(button=>{button.disabled=disabled;});
  const draft=creatorReplacement.draft;if(!draft||!$('#creatorReplacementDialog'))return;
  const view=creatorReplacementState(draft,creatorPool());
  if(view.blocked&&draft.verifying)draft.verifyController?.abort();
  $('#creatorReplacementOriginal').textContent='第 '+(draft.index+1)+' 段：'+draft.name;
  $('#creatorReplacementStatus').textContent=draft.verifying?'正在核对替换素材，原草稿尚未改变…':draft.verifyMessage||view.message;
  $('#creatorReplacementList').innerHTML=view.candidates.map(item=>'<label class="creator-replacement-option"><input type="radio" name="creatorReplacement" data-replacement-key="'+esc(item.key)+'" '+(draft.choice===item.key?'checked':'')+' '+(view.blocked||draft.verifying?'disabled':'')+'><span><strong>'+esc(item.name)+'</strong><small>'+esc(bytes(item.size||0))+' · '+(item.ready?'当前已确认':'确认替换前核对')+'</small></span></label>').join('');
  const candidate=view.candidates.find(item=>item.key===draft.choice);
  $('#creatorReplacementApply').disabled=view.blocked||draft.verifying||!candidate||(!candidate.ready&&state.config.asset_resolution_supported!==true);
  $('#creatorReplacementApply').textContent=draft.verifying?'正在核对素材…':'确认替换这一段';
  renderAssetBrowserControls(assetBrowsers.replacement,$('#creatorReplacementSearchStatus'),$('#creatorReplacementMore'));
  $('#creatorReplacementSearch').disabled=!!draft.verifying;$('#creatorReplacementRefresh').disabled=!!draft.verifying||!state.connected;if(draft.verifying)$('#creatorReplacementMore').disabled=true;
}
async function applyCreatorReplacement(){
  const draft=creatorReplacement.draft;if(!draft||draft.verifying)return;
  // Recheck the exact directory revision and original slot at the moment of confirmation.
  const view=creatorReplacementState(draft,creatorPool()),candidate=view.candidates.find(item=>item.key===draft.choice);
  if(view.blocked||!candidate){renderCreatorReplacement();return;}
  draft.verifyMessage='';
  if(!candidate.ready){
    if(state.config.asset_resolution_supported!==true)return;
    const controller=new AbortController(),context=connectionContext(),session=assetBrowsers.replacement,snapshot=session.snapshot,query=session.query;
    draft.verifyController=controller;draft.verifying=true;renderCreatorReplacement();
    try{
      const response=await api('/api/assets/resolve',{method:'POST',body:JSON.stringify({sources:[candidate.source]}),signal:controller.signal},'json',context);
      if(creatorReplacement.draft!==draft||!currentContext(context)||controller.signal.aborted)return;
      const current=creatorReplacementState(draft,creatorPool()),record=response?.results?.[0];
      if(current.blocked||session.snapshot!==snapshot||session.query!==query||!current.candidates.some(item=>item.key===candidate.key))throw new Error('草稿或搜索状态已变化，请重新选择替换素材。');
      if(response?.limit!==12||response.results.length!==1||record?.source!==candidate.source||record.status!=='found'||!validBrowserAsset(record.asset)||record.asset.source!==candidate.source){session.status='stale';session.message='该素材暂未确认可用，请刷新搜索后重新选择。';draft.choice=null;throw new Error('替换素材暂未确认可用，原草稿未改变。');}
      rememberCreatorBrowsedAsset(record.asset);
    }catch{if(creatorReplacement.draft===draft)draft.verifyMessage='替换素材核对未完成，原草稿未改变。请确认搜索状态后再试。';return;}
    finally{draft.verifying=false;draft.verifyController=null;if(creatorReplacement.draft===draft)renderCreatorReplacement();}
    if(creatorReplacement.draft!==draft||!currentContext(context)||controller.signal.aborted||creatorReplacementState(draft,creatorPool()).blocked)return;
  }else if(candidate.searchAsset)rememberCreatorBrowsedAsset(candidate.searchAsset);
  creator.selected[draft.index]=candidate.key;creator.assetLabels[candidate.key]=candidate.name.slice(0,240);
  cancelAssetBrowser(assetBrowsers.replacement);creatorReplacement.draft=null;$('#creatorReplacementDialog').close();saveCreatorDraft();renderCreatorMaterials();
  creatorFeedback('第 '+(draft.index+1)+' 段已替换为“'+candidate.name+'”。其他内容保留，尚未提交制作。');
}
async function uploadCreatorOne(id){
  if(creatorInputLocked())return;
  const item=creator.uploads.get(id);if(!item)return;
  item.error=null;item.status='正在上传 0%';renderCreatorMaterials();
  try{
    // A retained request ID makes an explicit retry safe after an uncertain transfer.
    const result=await uploadLocalFile(item.file,'library',id,(done,total)=>{item.status='正在上传 '+Math.min(100,Math.round(done/total*100))+'%';renderCreatorMaterials();});
    localImports.items=[result.import,...localImports.items.filter(x=>x.id!==id)];creator.uploads.delete(id);renderLocalImports();saveCreatorDraft();
  }catch(error){item.error=error.message;item.status='导入需处理';$('#creatorUploadStatus').textContent=error.message;}
  renderCreatorMaterials();
}
async function uploadCreatorFiles(files){
  if(creatorInputLocked()||creator.uploading||!localImportAvailable()||state.config.creation_supported!==true)return;
  const accepted=[];let rejected=0;
  for(const file of [...files]){
    if(!/\.(mp4|mov|mkv|webm|avi|m4v|mts)$/i.test(file.name)||!Number.isFinite(file.size)||file.size<=0||file.size>localImports.maxBytes){rejected++;continue;}
    if(accepted.some(x=>x.file.name===file.name&&x.file.size===file.size&&x.file.lastModified===file.lastModified))continue;
    if(creator.selected.length+accepted.length>=12){rejected++;continue;}
    accepted.push({id:crypto.randomUUID(),file});
  }
  if(!accepted.length){$('#creatorUploadStatus').textContent='没有可导入的视频：每段不超过 2 GB，一次制作最多选择 12 段。';return;}
  for(const item of accepted){creator.uploads.set(item.id,{file:item.file,status:'等待上传'});creator.selected.push('l:'+item.id);}
  creator.uploading=true;renderCreatorMaterials();saveCreatorDraft();
  $('#creatorUploadStatus').textContent='正在上传 '+accepted.length+' 段视频，完成后会自动同步。'+(rejected?'有 '+rejected+' 个文件因格式、大小或数量限制未导入。':'');
  try{for(const item of accepted)await uploadCreatorOne(item.id);await refreshLocalLibrary(true);}
  finally{creator.uploading=false;$('#creatorFileInput').value='';renderCreatorMaterials();}
  if(![...creator.uploads.values()].some(i=>i.error))$('#creatorUploadStatus').textContent='已上传 '+accepted.length+' 段并加入选择。同步完成后，写好想法即可生成。'+(rejected?'另有 '+rejected+' 个文件未导入。':'');
}
async function retryCreatorUpload(id){
  if(creatorInputLocked()||creator.uploading)return;creator.uploading=true;updateCreatorAccess();
  try{await refreshLocalLibrary(true);if(!localImports.items.some(i=>i.id===id))await uploadCreatorOne(id);else creator.uploads.delete(id);}
  finally{creator.uploading=false;renderCreatorMaterials();saveCreatorDraft();}
}
async function generateCreation(){
  if(typeof productionMode==='function'&&productionMode()!=='edit')return generateProduction();
  if(creator.busy||!state.connected||state.config.creation_supported!==true)return;
  const selected=creatorSelection();
  if(!creator.uncertain&&(!selected.length||selected.some(a=>!a.ready)||!$('#creatorBrief').value.trim())){updateCreatorAccess();return;}
  if(!creator.uncertain){creator.requestId=crypto.randomUUID();creator.requestBody={sources:selected.map(a=>a.source),brief:$('#creatorBrief').value.trim(),title:$('#creatorTitle').value.trim(),options:creatorOptions(),idempotency_key:creator.requestId};if(typeof production!=='undefined'){if(production.parentId){creator.requestBody.parent_job_id=production.parentId;creator.requestBody.revision_kind=production.revisionKind;}if(production.referenceIds?.length)creator.requestBody.reference_ids=[...production.referenceIds];}}
  creator.busy=true;creator.uncertain=true;creatorFeedback('正在把所选素材和想法交给制作任务…');saveCreatorDraft();renderCreatorMaterials();
  try{
    const result=await api('/api/creations',{method:'POST',body:JSON.stringify(creator.requestBody)});
    if(!result.job?.id)throw Object.assign(new Error('任务结果需核实，请使用同一编号重新查看。'),{uncertain:true});
    creator.resultId=result.job.id;creator.uncertain=false;creator.requestBody=null;creator.requestId=null;
    state.jobs=[result.job,...state.jobs.filter(j=>j.id!==result.job.id)];renderJobs();renderCreatorResult();
    creatorFeedback(result.job.status==='withdrawn'?'已找到这次提交的任务，但它已被撤回。可在任务详情中继续编辑草稿。':result.created===false?'已找到这次提交的任务，没有重复创建。':'任务已加入制作队列，完成后可在下方直接查看成片。');
    await refresh(true);
  }catch(error){creator.uncertain=error.uncertain===true;creatorFeedback(error.message,true);}
  finally{creator.busy=false;saveCreatorDraft();renderCreatorMaterials();}
}
function creationPlanHtml(job){
  const plan=job.creation_plan||job.plan?.creation||{},summary=plan.summary||plan.interpretation?.summary||job.plan?.summary;
  const warnings=job.creation_warnings||plan.warnings||[];
  const summaryText=Array.isArray(summary)?summary.join('；'):typeof summary==='string'?summary:'';
  return (summaryText?'<p>'+esc(summaryText)+'</p>':'')+(warnings.length?'<ul>'+warnings.map(w=>'<li>'+esc(w)+'</li>').join('')+'</ul>':'');
}
function renderCreatorResult(){
  const job=state.jobs.find(j=>j.id===creator.resultId),differentMode=job&&typeof productionMode==='function'&&(job.mode||'edit')!==productionMode();$('#creatorResult').hidden=!job||differentMode;if(!job||differentMode)return;
  $('#creatorResult').innerHTML='<strong>'+esc(statusNames[job.status]||job.status)+' · '+esc(job.title)+'</strong><p>'+esc(job.brief||'')+'</p><details><summary>制作说明与检查记录</summary>'+creationPlanHtml(job)+'</details><button class="button dark" type="button" data-review="'+esc(job.id)+'">'+(job.has_video?'查看成片':'查看制作进度')+'</button>';
}
function renderCreationReview(job){
  $('#creationReviewBrief').hidden=!job.brief;
  $('#creationReviewBrief').innerHTML=job.brief?'<strong>你对这条视频的想法</strong><p>'+esc(job.brief)+'</p><details><summary>实际制作方案与所用素材</summary>'+creationPlanHtml(job)+'<ol>'+(job.sources||[]).map(s=>'<li>'+esc(assetName(state.assets.find(a=>a.source===s)||{source:s}))+'</li>').join('')+'</ol></details>':'';
}
$('#creatorNext').onclick=()=>(typeof director!=='undefined'&&director.active?$('#directorGoal'):$('#creatorBrief')).focus?.();
$('#creatorUploadButton').onclick=()=>{if(!creatorInputLocked()&&!creator.uploading)$('#creatorFileInput').click();};
$('#creatorFileInput').onchange=()=>uploadCreatorFiles($('#creatorFileInput').files||[]);
$('#creatorAssetSearch').oninput=()=>changeAssetBrowserQuery(assetBrowsers.main,$('#creatorAssetSearch').value);
$('#creatorGenerate').onclick=generateCreation;
$('#creatorRefreshAssets').onclick=async()=>{const session=assetBrowsers.main;cancelAssetBrowser(session);session.initialized=true;const sequence=session.sequence,context=connectionContext();await Promise.allSettled([refresh(true),refreshLocalLibrary(true)]);if(currentContext(context)&&session.sequence===sequence&&assetSearchSupported())refreshAssetBrowser(session);else renderCreatorMaterials();};
$('#creatorClearSelection').onclick=()=>{if(!creatorInputLocked()){creator.selected=[];saveCreatorDraft();renderCreatorMaterials();}};
for(const id of creatorFields)$('#'+id).addEventListener('input',()=>{saveCreatorDraft();updateCreatorAccess();});
document.addEventListener('change',event=>{const checkbox=event.target.closest('[data-creator-select]');if(checkbox){if(checkbox.dataset.searchSource)selectCreatorSearchResult(checkbox.dataset.searchSource,checkbox.checked);else toggleCreatorMaterial(checkbox.dataset.creatorSelect,checkbox.checked);}});
document.addEventListener('click',event=>{
  const replacement=event.target.closest('[data-creator-replace]');if(replacement&&!replacement.disabled)openCreatorReplacement(Number(replacement.dataset.creatorReplace));
  const add=event.target.closest('[data-add-to-creator]');if(add)addSourceToCreator(add.dataset.addToCreator);
  const sample=event.target.closest('[data-brief-example]');if(sample&&!creator.busy&&!creator.uncertain){$('#creatorBrief').value=sample.dataset.briefExample;saveCreatorDraft();updateCreatorAccess();$('#creatorBrief').focus?.();}
  const remove=event.target.closest('[data-creator-remove]');if(remove)toggleCreatorMaterial(remove.dataset.creatorRemove,false);
  const move=event.target.closest('[data-creator-move]');if(move&&!move.disabled&&!creatorInputLocked()){const from=Number(move.dataset.creatorMove),to=from+Number(move.dataset.direction);if(Number.isInteger(from)&&to>=0&&to<creator.selected.length){[creator.selected[from],creator.selected[to]]=[creator.selected[to],creator.selected[from]];saveCreatorDraft();renderCreatorMaterials();}}
  const retry=event.target.closest('[data-creator-retry]');if(retry)retryCreatorUpload(retry.dataset.creatorRetry);
});
for(const name of ['dragenter','dragover'])$('#creatorDropZone').addEventListener(name,event=>{event.preventDefault();if(!creator.uploading&&!creatorInputLocked())$('#creatorDropZone').classList.add('drag-over');});
for(const name of ['dragleave','drop'])$('#creatorDropZone').addEventListener(name,event=>{event.preventDefault();$('#creatorDropZone').classList.remove('drag-over');});
$('#creatorDropZone').addEventListener('drop',event=>uploadCreatorFiles(event.dataTransfer?.files||[]));
loadCreatorDraft();renderCreatorMaterials();showPage('create');
