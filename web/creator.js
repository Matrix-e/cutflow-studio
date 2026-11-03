'use strict';
const CREATOR_DRAFT='cutflow.creator.draft.v1';
const creator={selected:[],uploads:new Map(),uploading:false,busy:false,resultId:null,requestId:null,requestBody:null,uncertain:false};
const creatorFields=['creatorBrief','creatorTitle','creatorDuration','creatorAspect','creatorPace','creatorOriginalAudio','creatorMusic','creatorOpeningTitle'];
function creatorInputLocked(){return creator.busy||creator.uncertain;}
function saveCreatorDraft(){
  if(!localPage||typeof localStorage==='undefined')return;
  try{localStorage.setItem(CREATOR_DRAFT,JSON.stringify({fields:Object.fromEntries(creatorFields.map(id=>[id,$('#'+id).value])),selected:creator.selected.slice(0,12),resultId:creator.resultId,requestId:creator.requestId,requestBody:creator.requestBody,uncertain:creator.uncertain}));}catch{}
}
function loadCreatorDraft(){
  if(!localPage||typeof localStorage==='undefined')return;
  try{const draft=JSON.parse(localStorage.getItem(CREATOR_DRAFT)||'null');if(!draft)return;
    for(const id of creatorFields)if(typeof draft.fields?.[id]==='string')$('#'+id).value=draft.fields[id].slice(0,id==='creatorBrief'?3000:160);
    if(Array.isArray(draft.selected))creator.selected=[...new Set(draft.selected.filter(v=>typeof v==='string'&&v.length<=4200))].slice(0,12);
    if(typeof draft.resultId==='string'&&/^[a-zA-Z0-9_-]+$/.test(draft.resultId))creator.resultId=draft.resultId;
    if(typeof draft.requestId==='string'&&draft.requestBody&&typeof draft.requestBody==='object'&&draft.uncertain){creator.requestId=draft.requestId;creator.requestBody=draft.requestBody;creator.uncertain=true;}
  }catch{}
}
function creatorFeedback(text,error=false){$('#creatorFeedback').textContent=text;$('#creatorFeedback').className='media-status'+(error?' error':'');}
function creatorPool(){
  const pool=state.assets.map(asset=>({key:'s:'+asset.source,source:asset.source,name:assetName(asset),size:asset.size,ready:true,status:'已同步 · 可制作'}));
  const imports=typeof localImports==='undefined'?[]:localImports.items;
  for(const item of imports){
    const asset=typeof item.remote_source==='string'&&item.remote_source?pool.find(a=>a.ready&&a.source===item.remote_source):null,localKey='l:'+item.id;
    if(asset){creator.selected=creator.selected.map(k=>k===localKey?asset.key:k);continue;}
    pool.unshift({key:localKey,name:item.name,size:item.size,source:null,ready:false,status:item.status==='synced'?'同步已完成 · 正在读取素材':'已上传 · 等待同步'});
  }
  for(const [id,item] of creator.uploads){if(!imports.some(i=>i.id===id))pool.unshift({key:'l:'+id,name:item.file.name,size:item.file.size,ready:false,status:item.error?'导入需处理':item.status,retryId:item.error?id:null});}
  creator.selected=[...new Set(creator.selected)];return pool;
}
function creatorSelection(pool=creatorPool()){return creator.selected.map(key=>pool.find(a=>a.key===key)||{key,name:'素材暂时不可用，请刷新或移除后重选',ready:false,status:'待确认'});}
function renderCreatorMaterials(){
  const pool=creatorPool(),query=$('#creatorAssetSearch').value.trim().toLowerCase(),shown=pool.filter(a=>a.name.toLowerCase().includes(query)),locked=creatorInputLocked();
  $('#creatorAssetGrid').innerHTML=shown.length?shown.map(item=>`<article class="creator-asset-card ${creator.selected.includes(item.key)?'selected':''}"><label><input type="checkbox" data-creator-select="${esc(item.key)}" ${creator.selected.includes(item.key)?'checked':''} ${locked?'disabled':''} aria-label="选择素材：${esc(item.name)}"><span><strong>${esc(item.name)}</strong><small>${bytes(item.size||0)} · ${esc(item.status)}</small></span></label><div class="creator-card-actions">${item.source?'<button type="button" class="text-button" data-preview="'+esc(item.source)+'">预览</button>':''}${item.retryId?'<button type="button" class="text-button" data-creator-retry="'+esc(item.retryId)+'" '+(locked||creator.uploading?'disabled':'')+'>核对并重试导入</button>':''}</div></article>`).join(''):'<div class="creator-asset-empty">'+(query?'没有找到同名素材。':state.connected?'还没有视频，点击上方「上传视频」。':'正在连接素材库，你可以先写下想法。')+'</div>';
  const selected=creatorSelection(pool);$('#creatorSelectedCount').textContent='已选 '+selected.length+' 段';
  $('#creatorSelection').innerHTML=selected.length?selected.map((item,index)=>`<li class="creator-selected-item"><span class="creator-number">${index+1}</span><span class="creator-selected-name">${esc(item.name)}<small>${esc(item.ready?'已就绪':item.status)}</small></span><div class="creator-order-buttons"><button type="button" data-creator-move="${index}" data-direction="-1" aria-label="上移：${esc(item.name)}" ${index===0||locked?'disabled':''}>↑</button><button type="button" data-creator-move="${index}" data-direction="1" aria-label="下移：${esc(item.name)}" ${index===selected.length-1||locked?'disabled':''}>↓</button><button type="button" data-creator-remove="${esc(item.key)}" aria-label="移除：${esc(item.name)}" ${locked?'disabled':''}>×</button></div></li>`).join(''):'<li class="help">勾选上面的素材，或上传新视频；最多选择 12 段。</li>';
  updateCreatorAccess();renderCreatorResult();
}
function updateCreatorAccess(){
  const selected=creatorSelection(),brief=$('#creatorBrief').value.trim(),waiting=selected.filter(a=>!a.ready).length;
  const supported=state.config.creation_supported===true,locked=creatorInputLocked();
  for(const id of creatorFields)$('#'+id).disabled=locked;
  $('#creatorUploadButton').disabled=!localImportAvailable()||!supported||creator.uploading||locked;
  $('#creatorFileInput').disabled=$('#creatorUploadButton').disabled;
  $('#creatorClearSelection').disabled=locked||!selected.length;
  $('#creatorGenerate').disabled=creator.busy||!state.connected||!supported||(!creator.uncertain&&(!brief||!selected.length||waiting>0||creator.uploading));
  $('#creatorGenerate').textContent=creator.busy?'正在提交制作任务…':creator.uncertain?'核对并继续提交':creator.resultId?'按当前想法再做一版 →':'生成视频 →';
  $('#creatorReadiness').textContent=creator.busy?'任务只提交一次，请稍候。':!supported?'制作服务正在连接或升级，你可以先写下想法。':!state.connected?'等待制作服务连接恢复。':creator.uncertain?'上次提交结果待确认；再次点击会使用相同编号核对，不会重复创建。':!selected.length?'先选素材，再写下你的想法。':waiting?waiting+' 段素材正在等待同步，完成后即可生成。':!brief?'素材已就绪，写下你的想法就可以生成。':selected.length+' 段素材已就绪 · 生成后进入成片审核';
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
function openCreator(source){showPage('create');if(source)addSourceToCreator(source);else renderCreatorMaterials();$('#creatorBrief').focus?.();}
function creatorOptions(){
  const options={};const duration=$('#creatorDuration').value,aspect=$('#creatorAspect').value,pace=$('#creatorPace').value,voice=$('#creatorOriginalAudio').value,music=$('#creatorMusic').value,title=$('#creatorOpeningTitle').value.trim();
  if(duration)options.target_duration=Number(duration);if(aspect)options.aspect=aspect;if(pace)options.pace=pace;if(voice)options.keep_original_audio=voice==='true';if(music)options.music=music==='true';if(title)options.opening_title=title;
  return options;
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
  if(creator.busy||!state.connected||state.config.creation_supported!==true)return;
  const selected=creatorSelection();
  if(!creator.uncertain&&(!selected.length||selected.some(a=>!a.ready)||!$('#creatorBrief').value.trim())){updateCreatorAccess();return;}
  if(!creator.uncertain){creator.requestId=crypto.randomUUID();creator.requestBody={sources:selected.map(a=>a.source),brief:$('#creatorBrief').value.trim(),title:$('#creatorTitle').value.trim(),options:creatorOptions(),idempotency_key:creator.requestId};}
  creator.busy=true;creator.uncertain=true;creatorFeedback('正在把所选素材和想法交给制作任务…');saveCreatorDraft();renderCreatorMaterials();
  try{
    const result=await api('/api/creations',{method:'POST',body:JSON.stringify(creator.requestBody)});
    if(!result.job?.id)throw Object.assign(new Error('任务结果需核实，请使用同一编号重新查看。'),{uncertain:true});
    creator.resultId=result.job.id;creator.uncertain=false;creator.requestBody=null;creator.requestId=null;
    state.jobs=[result.job,...state.jobs.filter(j=>j.id!==result.job.id)];renderJobs();renderCreatorResult();
    creatorFeedback(result.created===false?'已找到这次提交的任务，没有重复创建。':'任务已开始制作，完成后可在下方直接查看成片。');
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
  const job=state.jobs.find(j=>j.id===creator.resultId);$('#creatorResult').hidden=!job;if(!job)return;
  $('#creatorResult').innerHTML='<strong>'+esc(statusNames[job.status]||job.status)+' · '+esc(job.title)+'</strong><p>'+esc(job.brief||'')+'</p>'+creationPlanHtml(job)+'<button class="button dark" type="button" data-review="'+esc(job.id)+'">'+(job.has_video?'查看成片':'查看制作进度')+'</button>';
}
function renderCreationReview(job){
  $('#creationReviewBrief').hidden=!job.brief;
  $('#creationReviewBrief').innerHTML=job.brief?'<strong>你对这条视频的想法</strong><p>'+esc(job.brief)+'</p><details><summary>实际制作方案与所用素材</summary>'+creationPlanHtml(job)+'<ol>'+(job.sources||[]).map(s=>'<li>'+esc(assetName(state.assets.find(a=>a.source===s)||{source:s}))+'</li>').join('')+'</ol></details>':'';
}
$('#creatorNext').onclick=()=>$('#creatorBrief').focus?.();
$('#creatorUploadButton').onclick=()=>{if(!creatorInputLocked()&&!creator.uploading)$('#creatorFileInput').click();};
$('#creatorFileInput').onchange=()=>uploadCreatorFiles($('#creatorFileInput').files||[]);
$('#creatorAssetSearch').oninput=renderCreatorMaterials;
$('#creatorGenerate').onclick=generateCreation;
$('#creatorRefreshAssets').onclick=async()=>{await Promise.allSettled([refresh(true),refreshLocalLibrary(true)]);renderCreatorMaterials();};
$('#creatorClearSelection').onclick=()=>{if(!creatorInputLocked()){creator.selected=[];saveCreatorDraft();renderCreatorMaterials();}};
for(const id of creatorFields)$('#'+id).addEventListener('input',()=>{saveCreatorDraft();updateCreatorAccess();});
document.addEventListener('change',event=>{const checkbox=event.target.closest('[data-creator-select]');if(checkbox)toggleCreatorMaterial(checkbox.dataset.creatorSelect,checkbox.checked);});
document.addEventListener('click',event=>{
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
