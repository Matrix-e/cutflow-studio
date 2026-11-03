'use strict';
const PRODUCTION_DRAFT='cutflow.production.draft.v1';
const production={mode:'brand_promo',kind:'public',portraitId:null,catalog:[],catalogAt:0,catalogLoading:false,images:new Map(),uploading:false,portraitUpload:null,preferredVoice:null,voiceExplicit:false,scriptId:null,scriptBody:null,scriptJob:null,checking:false,uncertain:false,pollTimer:null,scriptContext:null,parentId:null,parentTitle:null,revisionKind:null,referenceIds:[]};
const productionFields=['productionSubtype','productionScript','productionVoice','productionStyle','productionAspect','productionDuration','productionMusic','productionSubtitles','productionLoop','productionSourceAudio','productionBrand','productionDate','productionPlace','productionCTA','productionOpeningTitle'];
const productionChecks=new Set(['productionMusic','productionSubtitles','productionLoop']);
function productionMode(){return production.mode;}
function selectedPresenterReady(){return production.catalog.some(p=>p.id===production.portraitId&&p.kind===production.kind&&p.ready===true)&&(production.kind==='public'||$('#presenterConsent').checked);}
function scriptRequestUncertain(error){return error.uncertain===true||error.connectivity===true||error.stale===true||!Number.isInteger(error.httpStatus)||error.httpStatus>=500;}
function productionBrand(){return {name:$('#productionBrand').value.trim(),cta:$('#productionCTA').value.trim(),event_date:$('#productionDate').value.trim(),event_place:$('#productionPlace').value.trim()};}
function productionContext(){return JSON.stringify({mode:production.mode,subtype:$('#productionSubtype').value,brief:$('#creatorBrief').value,brand:productionBrand(),sources:creator.selected,duration:$('#productionDuration').value,parentId:production.parentId,referenceIds:production.referenceIds});}
function productionOptions(){return {...($('#productionOpeningTitle').value.trim()?{opening_title:$('#productionOpeningTitle').value.trim()}:{}),aspect:$('#productionAspect').value,target_duration:Number($('#productionDuration').value),music:$('#productionMusic').checked,subtitles:$('#productionSubtitles').checked,voice_style:$('#productionStyle').value,source_audio:$('#productionSourceAudio').value};}
function saveProductionDraft(){
  if(!localPage||typeof localStorage==='undefined')return;
  try{localStorage.setItem(PRODUCTION_DRAFT,JSON.stringify({mode:production.mode,kind:production.kind,portraitId:production.portraitId,voiceExplicit:production.voiceExplicit,fields:Object.fromEntries(productionFields.map(id=>[id,productionChecks.has(id)?$('#'+id).checked:$('#'+id).value])),scriptId:production.scriptId,scriptBody:production.scriptBody,scriptJob:production.scriptJob,uncertain:production.uncertain,scriptContext:production.scriptContext,parentId:production.parentId,parentTitle:production.parentTitle,revisionKind:production.revisionKind,referenceIds:production.referenceIds}));}catch{}
}
function loadProductionDraft(){
  if(!localPage||typeof localStorage==='undefined')return;
  try{const d=JSON.parse(localStorage.getItem(PRODUCTION_DRAFT)||'null');if(d){
    if(typeof d.parentId==='string'&&/^[a-f0-9]{32}$/.test(d.parentId)&&['rewrite','presenter','remix'].includes(d.revisionKind)){production.parentId=d.parentId;production.parentTitle=String(d.parentTitle||'已有作品').slice(0,160);production.revisionKind=d.revisionKind;}
    production.referenceIds=Array.isArray(d.referenceIds)?[...new Set(d.referenceIds.filter(id=>typeof id==='string'&&/^[a-f0-9]{32}$/.test(id)))].slice(0,3):[];
    if(['edit','brand_promo','narrated_promo','talking_head'].includes(d.mode))production.mode=d.mode;
    if(['public','personal'].includes(d.kind))production.kind=d.kind;
    if(typeof d.portraitId==='string'&&/^[a-zA-Z0-9_-]{1,100}$/.test(d.portraitId))production.portraitId=d.portraitId;
    for(const id of productionFields){const e=$('#'+id),v=d.fields?.[id];if(productionChecks.has(id)&&typeof v==='boolean')e.checked=v;else if(typeof v==='string')setDraftFieldValue(id,v.slice(0,id==='productionScript'?1500:200));}
    if(typeof d.fields?.productionVoice==='string')production.preferredVoice=d.fields.productionVoice;
    production.voiceExplicit=d.voiceExplicit===true;
    if(typeof d.scriptId==='string'&&/^[a-f0-9-]{36}$/i.test(d.scriptId)&&d.scriptBody?.request_id===d.scriptId){production.scriptId=d.scriptId;production.scriptBody=d.scriptBody;production.uncertain=d.uncertain===true;if(d.scriptJob?.id===d.scriptId)production.scriptJob=d.scriptJob;if(typeof d.scriptContext==='string')production.scriptContext=d.scriptContext;}
  }
  if(creator.uncertain)production.mode=creator.requestBody?.mode||'edit';
  }catch{}
}
function setProductionMode(mode){
  if(creatorInputLocked()||!['edit','brand_promo','narrated_promo','talking_head'].includes(mode))return;
  production.mode=mode;saveProductionDraft();updateCreatorAccess();renderCreatorResult();
}
function updateProductionAccess(){
  const mode=production.mode,isEdit=mode==='edit',isPerson=mode==='talking_head',locked=creatorInputLocked(),caps=state.config.production||{},cap=caps.modes?.[mode];
  for(const el of document.querySelectorAll('[data-production-mode]')){el.classList.toggle('active',el.dataset.productionMode===mode);el.setAttribute('aria-pressed',String(el.dataset.productionMode===mode));el.disabled=locked;}
  $('#productionPresenter').hidden=!isPerson;$('#productionMaterials').hidden=isPerson;
  $('#productionSubtypeField').hidden=mode!=='brand_promo';$('#productionScriptPanel').hidden=isEdit;$('#productionOptions').hidden=isEdit;$('#creatorOptions').hidden=!isEdit;
  $('#productionLoopField').hidden=isPerson;$('#productionSourceAudioField').hidden=isPerson;
  $('#productionScope').textContent=isEdit?'按所选素材顺序剪辑，保留或关闭原声，可调整标题、时长和音乐。':isPerson?'正面静图口型同步：完整朗读讲稿，身体保持静态。选择照片中的服装与背景。':'按照所选画面顺序制作，加入完整中文独白、字幕和轻音乐。画面不足时会提示你补充素材。';
  $('#creatorBrief').placeholder=isEdit?'例如：做一条30秒竖屏视频，保留原声，清理长停顿。':mode==='brand_promo'?'例如：宣传一场面向汽车租赁老板的 AI 沙龙，突出真实案例交流，让大家愿意报名。':isPerson?'例如：由一位亲切的主持人介绍我们的汽车托管服务，开头提出车主最关心的问题。':'例如：用这些素材介绍汽车托管的交接流程，专业、清楚，让车主放心。';
  const voices=Array.isArray(caps.voices)?caps.voices:[],voice=$('#productionVoice'),old=voice.value||production.preferredVoice;
  const signature=JSON.stringify(voices.map(v=>[v.id,v.name]));
  if(voice.dataset.signature!==signature){voice.dataset.signature=signature;voice.innerHTML=voices.length?voices.map(v=>'<option value="'+esc(v.id)+'">'+esc(v.name)+'</option>').join(''):'<option value="">配音服务正在准备</option>';voice.value=voices.some(v=>v.id===old)?old:(voices[0]?.id||'');if(voice.value)production.preferredVoice=voice.value;}
  for(const id of productionFields)$('#'+id).disabled=locked;
  $('#presenterConsent').disabled=locked||production.uploading;
  $('#presenterUploadButton').disabled=locked||production.uploading||!state.connected||!$('#presenterConsent').checked;
  $('#productionModeStatus').textContent=!state.connected?'正在连接租用 GPU，草稿已保留。':isEdit?'保留现有原声剪辑流程。':cap?.ready?'租用 GPU 制作 · 先确认讲稿，再生成成片':cap?.reason||'此模式的生成服务正在准备，暂不能生成成片。';
  const pending=['queued','running'].includes(production.scriptJob?.status);
  $('#productionWriteScript').disabled=locked||production.checking||!state.connected||caps.script_supported!==true||!$('#creatorBrief').value.trim();
  $('#productionWriteScript').textContent=production.checking?'正在核对讲稿…':pending?'查看讲稿进度…':production.uncertain?'核对上次讲稿':production.scriptJob?.status==='failed'?'重试生成讲稿':'✦ 按想法写讲稿';
  $('#productionAdoptScript').disabled=locked||production.scriptJob?.status!=='succeeded'||production.scriptContext!==productionContext();
  if(!isEdit){
    const selected=creatorSelection(),script=$('#productionScript').value.trim(),ready=isPerson?selectedPresenterReady()&&!production.uploading:selected.length>0&&selected.every(a=>a.ready)&&!creator.uploading;
    const tooLong=script.length>(isPerson?450:1500);
    $('#creatorGenerate').disabled=creator.busy||!state.connected||(!creator.uncertain&&(!cap?.ready||!ready||tooLong||!$('#creatorBrief').value.trim()||!voice.value||(!script&&(!caps.script_supported||pending||production.checking))));
    $('#creatorGenerate').textContent=creator.busy?'正在提交制作任务…':creator.uncertain?'核对并继续提交':!script?'先生成独白讲稿 →':'生成带声音的视频 →';
    $('#creatorReadiness').textContent=creator.uncertain?'提交结果待核对，再次点击会查找同一任务。':tooLong?'当前讲稿有 '+script.length+' 字，'+(isPerson?'人物口播最多 450 字，请先精简讲稿。':'单条最多 1500 字，请先精简讲稿。'):!cap?.ready?cap?.reason||'生成服务正在准备。':!ready?isPerson?'先选择可用的人物形象。':creatorMaterialReadiness(selected)||'先选择已经同步完成的视频素材。':!script?'写好想法后先生成讲稿；也可以直接填写独白。':'将完整朗读当前讲稿 · 生成后进入成片审核';
  }
  if(pending&&state.connected&&!production.uncertain&&!production.pollTimer&&!production.checking)scheduleScriptPoll();
  if(isPerson&&state.connected&&Date.now()-production.catalogAt>30000&&!production.catalogLoading)loadPresenterCatalog();
  if(typeof scheduleCreatorAssetResolution==='function')scheduleCreatorAssetResolution();
  if(typeof renderCreatorReplacement==='function')renderCreatorReplacement();
}
function scriptMessage(text,error=false){$('#productionScriptStatus').textContent=text;$('#productionScriptStatus').className='script-status'+(error?' error':'');}
function acceptScript(job,id){
  if(production.scriptId!==id)return;
  if(!job||job.id!==id||!['queued','running','succeeded','failed'].includes(job.status)||job.status==='succeeded'&&(typeof job.result?.spoken_script!=='string'||!job.result.spoken_script.trim()||job.result.spoken_script.length>1500))throw new Error('讲稿结果不完整，请核对后重试。');
  production.scriptJob=job;production.uncertain=false;
  $('#productionScriptPreview').hidden=job.status!=='succeeded';
  if(job.status==='succeeded'){$('#productionScriptSuggestion').textContent=job.result.spoken_script;$('#productionScriptAssumptions').textContent=(job.result.assumptions||[]).join('；');scriptMessage(production.scriptContext===productionContext()?'讲稿已准备好。阅读并采用后，可以继续修改。':'想法或素材已变化。这版讲稿已保留，请按当前想法重新生成。');}
  else scriptMessage(job.status==='failed'?job.error||'讲稿暂未完成，请重试。':job.status==='running'?'GPU 正在起草讲稿，通常需要几十秒…':'讲稿已加入队列，原有文字保持不变。',job.status==='failed');
  saveProductionDraft();updateProductionAccess();scheduleScriptPoll();
}
function scheduleScriptPoll(){clearTimeout(production.pollTimer);production.pollTimer=null;if(['queued','running'].includes(production.scriptJob?.status)&&state.connected)production.pollTimer=setTimeout(()=>{production.pollTimer=null;pollProductionScript();},2500);}
async function pollProductionScript(){
  if(production.checking||!production.scriptId||!state.connected)return;
  const id=production.scriptId;production.checking=true;
  try{acceptScript((await api('/api/scripts/'+encodeURIComponent(id))).script,id);}
  catch(error){production.uncertain=true;scriptMessage('读取暂时中断；点击核对会继续查找同一份讲稿。',true);clearTimeout(production.pollTimer);production.pollTimer=null;}
  finally{production.checking=false;saveProductionDraft();if(!production.uncertain)scheduleScriptPoll();updateProductionAccess();}
}
async function writeProductionScript(){
  if(creatorInputLocked()||production.checking||!state.connected||!state.config.production?.script_supported)return;
  if(['queued','running'].includes(production.scriptJob?.status)&&!production.uncertain)return pollProductionScript();
  const brief=$('#creatorBrief').value.trim();if(!brief){scriptMessage('先写一句你的想法。',true);return;}
  const retry=production.uncertain||production.scriptJob?.status==='failed'&&production.scriptContext===productionContext();
  if(!retry){production.scriptId=crypto.randomUUID();production.scriptBody={request_id:production.scriptId,mode:production.mode,subtype:$('#productionSubtype').value,brief,sources:production.mode==='talking_head'?[]:creatorSelection().filter(a=>a.ready).map(a=>a.source),brand:productionBrand(),target_duration:Number($('#productionDuration').value)};if(production.referenceIds?.length)production.scriptBody.reference_ids=production.referenceIds;if(production.parentId&&production.revisionKind==='rewrite')production.scriptBody.rewrite_source_id=production.parentId;production.scriptContext=productionContext();production.scriptJob=null;$('#productionScriptPreview').hidden=true;}
  const id=production.scriptId;production.checking=true;production.uncertain=true;saveProductionDraft();updateProductionAccess();scriptMessage('正在提交讲稿任务，已填写的独白保持不变…');
  try{
    let job;
    if(retry){try{job=(await api('/api/scripts/'+encodeURIComponent(id))).script;}catch(e){if(e.httpStatus!==404)throw e;}}
    if(job?.status==='failed')job=(await api('/api/scripts/'+encodeURIComponent(id)+'/retry',{method:'POST'})).script;
    else if(!job)job=(await api('/api/scripts',{method:'POST',body:JSON.stringify(production.scriptBody)})).script;
    acceptScript(job,id);
  }catch(error){production.uncertain=scriptRequestUncertain(error);scriptMessage(error.message,true);}
  finally{production.checking=false;saveProductionDraft();updateProductionAccess();}
}
function adoptProductionScript(){
  if(creatorInputLocked()||production.scriptJob?.status!=='succeeded'||production.scriptContext!==productionContext())return;
  $('#productionScript').value=production.scriptJob.result.spoken_script;$('#productionScriptPreview').hidden=true;scriptMessage('已采用，可以直接修改独白。生成视频时会完整朗读。');saveProductionDraft();updateProductionAccess();
}
async function generateProduction(){
  if(creator.busy||!state.connected)return;
  if(!creator.uncertain){
    const caps=state.config.production||{},selected=creatorSelection(),script=$('#productionScript').value.trim();
    if(!caps.modes?.[production.mode]?.ready)return;
    if(!script)return writeProductionScript();
    if(script.length>(production.mode==='talking_head'?450:1500))return;
    if(!$('#creatorBrief').value.trim()||!$('#productionVoice').value||(production.mode==='talking_head'?!selectedPresenterReady()||production.uploading:!selected.length||selected.some(a=>!a.ready)))return;
    creator.requestId=crypto.randomUUID();creator.requestBody={idempotency_key:creator.requestId,mode:production.mode,subtype:$('#productionSubtype').value,sources:production.mode==='talking_head'?[]:selected.map(a=>a.source),brief:$('#creatorBrief').value.trim(),spoken_script:script,voice_id:$('#productionVoice').value,visual_fill:$('#productionLoop').checked?'loop':'reject',brand:productionBrand(),options:productionOptions()};
    if($('#creatorTitle').value.trim())creator.requestBody.title=$('#creatorTitle').value.trim();else if(production.scriptJob?.status==='succeeded'&&production.scriptContext===productionContext()&&production.scriptJob.result.title)creator.requestBody.title=production.scriptJob.result.title;else if(production.parentId)creator.requestBody.title=(production.parentTitle||'视频作品').slice(0,145)+' · 新版本';
    if(production.parentId){creator.requestBody.parent_job_id=production.parentId;creator.requestBody.revision_kind=production.revisionKind;}if(production.referenceIds?.length)creator.requestBody.reference_ids=production.referenceIds;if(production.scriptJob?.status==='succeeded'&&production.scriptContext===productionContext())creator.requestBody.script_id=production.scriptId;
    if(production.mode==='talking_head'){creator.requestBody.presenter={kind:production.kind,portrait_id:production.portraitId,consent_confirmed:production.kind==='public'||$('#presenterConsent').checked};creator.requestBody.visual_fill='reject';creator.requestBody.options.source_audio='mute';}
  }
  creator.busy=true;creator.uncertain=true;saveCreatorDraft();saveProductionDraft();renderCreatorMaterials();creatorFeedback('已保存完整讲稿，正在提交到租用 GPU…');
  try{const result=await api('/api/creations',{method:'POST',body:JSON.stringify(creator.requestBody)});if(!result.job?.id)throw Object.assign(new Error('制作结果需要核对，请继续使用同一任务编号。'),{uncertain:true});creator.resultId=result.job.id;creator.uncertain=false;creator.requestBody=null;creator.requestId=null;state.jobs=[result.job,...state.jobs.filter(j=>j.id!==result.job.id)];renderJobs();renderCreatorResult();creatorFeedback(result.job.status==='withdrawn'?'已找到这次提交的任务，但它已被撤回。可在任务详情中继续编辑草稿。':result.created===false?'已找到原任务，没有重复生成。':'已加入制作队列，可在下方查看进度。');await refresh(true);}
  catch(error){creator.uncertain=error.uncertain===true;creatorFeedback(error.message,true);}
  finally{creator.busy=false;saveCreatorDraft();renderCreatorMaterials();}
}
async function loadPresenterCatalog(){
  production.catalogLoading=true;production.catalogAt=Date.now();
  try{const result=await api('/api/presenters');production.catalog=Array.isArray(result.presenters)?result.presenters:[];renderPresenterCatalog();}
  catch(error){$('#presenterStatus').textContent='人物目录暂不可用，连接恢复后可刷新查看。';}
  finally{production.catalogLoading=false;updateProductionAccess();}
}
function renderPresenterCatalog(){
  const items=production.catalog.filter(p=>p.kind===production.kind);$('#presenterEmpty').hidden=items.length>0;$('#presenterEmpty').textContent=production.kind==='public'?'公共数字人形象正在准备；可切换到“我的形象”上传已授权照片。':'还没有个人形象。上传一张正面照片即可保存。';
  $('#presenterUpload').hidden=production.kind!=='personal';
  $('#presenterGallery').innerHTML=items.map(p=>'<button type="button" class="presenter-look '+(production.portraitId===p.id?'active':'')+'" data-portrait-id="'+esc(p.id)+'" '+(creatorInputLocked()||p.ready===false?'disabled':'')+'>'+ (production.images.has(p.id)?'<img src="'+esc(production.images.get(p.id))+'" alt="'+esc(p.name)+'">':'')+'<span>'+esc(p.name)+'</span></button>').join('');
  for(const p of items)if(p.preview_url&&!production.images.has(p.id)&&/^\/api\/presenters\/[a-zA-Z0-9_-]+\/image$/.test(p.preview_url)){production.images.set(p.id,'');api(p.preview_url,{},'blob').then(blob=>{production.images.set(p.id,URL.createObjectURL(blob));renderPresenterCatalog();}).catch(()=>production.images.delete(p.id));}
}
async function importPresenter(file){
  if(!file||creatorInputLocked()||production.uploading||!$('#presenterConsent').checked)return;
  if(!['image/png','image/jpeg'].includes(file.type)||file.size>8*1024*1024||file.size<=0){$('#presenterStatus').textContent='请使用 8 MB 以内的 PNG 或 JPG 照片。';return;}
  const fingerprint=JSON.stringify([file.name,file.size,file.lastModified??null,file.type]);
  if(!production.portraitUpload||production.portraitUpload.fingerprint!==fingerprint)production.portraitUpload={id:crypto.randomUUID(),fingerprint,file};
  const upload=production.portraitUpload;
  production.uploading=true;updateProductionAccess();$('#presenterStatus').textContent='正在保存并检查人像…';
  try{const result=await api('/api/presenters/import?filename='+encodeURIComponent(file.name)+'&name='+encodeURIComponent(file.name.replace(/\.[^.]+$/,''))+'&id='+upload.id+'&consent_confirmed=true',{method:'POST',headers:{'Content-Type':file.type},body:file});if(typeof result.presenter?.id!=='string')throw Object.assign(new Error('照片保存结果需要核对，请重新选择同一张照片继续。'),{uncertain:true});production.portraitId=result.presenter.id;production.kind='personal';production.portraitUpload=null;await loadPresenterCatalog();saveProductionDraft();$('#presenterStatus').textContent='人物照片已保存。';}
  catch(error){if(!scriptRequestUncertain(error))production.portraitUpload=null;$('#presenterStatus').textContent=error.message+(production.portraitUpload?' 重新选择同一张照片，会沿用上次编号核对。':'');}
  finally{production.uploading=false;$('#presenterFile').value='';updateProductionAccess();}
}
document.addEventListener('click',event=>{
  const mode=event.target.closest('[data-production-mode]');if(mode)setProductionMode(mode.dataset.productionMode);
  const kind=event.target.closest('[data-presenter-kind]');if(kind&&!creatorInputLocked()){production.kind=kind.dataset.presenterKind;production.portraitId=null;for(const e of document.querySelectorAll('[data-presenter-kind]'))e.classList.toggle('active',e===kind);renderPresenterCatalog();saveProductionDraft();updateProductionAccess();}
  const portrait=event.target.closest('[data-portrait-id]');if(portrait&&!creatorInputLocked()){production.portraitId=portrait.dataset.portraitId;const recommendation=production.catalog.find(p=>p.id===production.portraitId)?.recommended_voice_id;if(!production.voiceExplicit&&state.config.production?.voices?.some(v=>v.id===recommendation)){$('#productionVoice').value=recommendation;production.preferredVoice=recommendation;}renderPresenterCatalog();saveProductionDraft();updateProductionAccess();}
});
for(const id of productionFields)for(const event of ['input','change'])$('#'+id).addEventListener(event,()=>{if(id==='productionVoice'){production.preferredVoice=$('#productionVoice').value;production.voiceExplicit=true;}saveProductionDraft();updateProductionAccess();});
$('#productionWriteScript').onclick=writeProductionScript;$('#productionAdoptScript').onclick=adoptProductionScript;
$('#presenterConsent').onchange=updateProductionAccess;$('#presenterUploadButton').onclick=()=>$('#presenterFile').click();$('#presenterFile').onchange=()=>importPresenter($('#presenterFile').files?.[0]);
loadProductionDraft();if(production.scriptJob)try{acceptScript(production.scriptJob,production.scriptId);}catch{production.scriptJob=null;}
updateProductionAccess();renderCreatorResult();
