'use strict';

// The capability is checked again at every action. Older servers expose no controls.
const queueDraftUI={busy:new Set(),uncertain:new Set(),request:0,editRevision:0,legacy:null,legacyStorageError:''};
const LEGACY_WITHDRAWN_DRAFTS='cutflow.withdrawn.legacy.v1';
function queueDraftSupported(){return state.config.queue_withdrawal_supported===true;}
function queueDraftJob(id){return state.jobs.find(job=>job.id===id)||(state.selected?.id===id?state.selected:null);}
function queueDraftIdle(){return !creatorInputLocked()&&!production.checking&&!production.uncertain&&!['queued','running'].includes(production.scriptJob?.status)&&!creator.uploading&&!production.uploading;}
function queueDraftActionsHtml(job){
  if(!queueDraftSupported()||!job||isTechnicalTest(job))return '';
  const busy=queueDraftUI.busy.has(job.id),disabled=!state.connected||busy;
  if(job.status==='queued'&&job.can_withdraw===true)return '<div class="queue-draft-row"><button type="button" class="text-button" data-withdraw-job="'+esc(job.id)+'" '+(disabled?'disabled':'')+'>'+(busy?'正在核对…':queueDraftUI.uncertain.has(job.id)?'核对撤回状态':'撤回并保留草稿')+'</button></div>';
  if(job.status==='withdrawn'&&job.can_resume_draft===true)return '<div class="queue-draft-row"><button type="button" class="text-button" data-resume-withdrawn="'+esc(job.id)+'" '+(disabled?'disabled':'')+'>'+(busy?'正在读取草稿…':'继续编辑草稿')+'</button></div>';
  return '';
}
function renderQueueDraftActions(job){
  const node=$('#queueDraftActions');if(!node)return;
  const actions=queueDraftActionsHtml(job);node.hidden=!actions;
  node.innerHTML=actions?'<strong>'+(job.status==='withdrawn'?'草稿已保留':'尚未开始制作')+'</strong><p class="help">'+(job.status==='withdrawn'?'继续编辑不会立即生成。原任务和历史保留，修改后由你决定何时生成新任务。':'只可撤回仍在排队的任务；已经开始制作的任务不能在这里撤回。')+'</p>'+actions:'';
  updateLegacyDraftAccess();
}
function redrawQueueDrafts(){renderJobs();updateReviewActions();}
function rememberQueueJob(job){
  if(!job||typeof job.id!=='string')throw new Error('任务状态返回不完整，请重新核对。');
  state.jobs=[job,...state.jobs.filter(item=>item.id!==job.id)];
  if(state.selected?.id===job.id){
    state.selected={...state.selected,...job};
    const badge=$('#reviewDetails .status');if(badge){badge.textContent=statusNames[job.status]||job.status;badge.className='status '+job.status;}
    const record=$('#reviewDetails pre');if(record&&Array.isArray(job.events))record.textContent=JSON.stringify({检查:job.result?.qc||'尚未完成',提示:[...(job.plan?.warnings||[]),...(job.result?.qc?.warnings||[])],事件:job.events},null,2);
  }
  redrawQueueDrafts();
}
async function checkWithdrawal(id){
  const result=await api('/api/jobs/'+encodeURIComponent(id));
  if(result.job?.id!==id)throw new Error('任务状态未能确认，请再次核对。');
  rememberQueueJob(result.job);queueDraftUI.uncertain.delete(id);
  if(result.job.status==='withdrawn')toast('已确认撤回，草稿和原任务记录均已保留。');
  else if(result.job.status==='queued')toast('已核对，任务仍在排队。如需撤回，请再次点击“撤回并保留草稿”。');
  else toast('任务已经开始或结束，不能撤回。已更新为最新状态。');
  return result.job;
}
async function withdrawQueuedJob(id){
  if(!queueDraftSupported()||!state.connected||queueDraftUI.busy.has(id))return;
  const job=queueDraftJob(id);if(!job||job.status!=='queued'||job.can_withdraw!==true||isTechnicalTest(job))return;
  const origin=state.base,token=state.token;queueDraftUI.busy.add(id);redrawQueueDrafts();
  try{
    // An uncertain write is reconciled by a read; never resend it automatically.
    if(queueDraftUI.uncertain.has(id)){await checkWithdrawal(id);return;}
    const result=await api('/api/jobs/'+encodeURIComponent(id)+'/withdraw',{method:'POST'});
    if(result.job?.id!==id||result.job.status!=='withdrawn')throw Object.assign(new Error('撤回结果待核实，请查询最新任务状态。'),{uncertain:true});
    rememberQueueJob(result.job);toast('任务已撤回。点击“继续编辑草稿”可修改后再制作。');
  }catch(error){
    const unknown=error.uncertain===true||error.connectivity===true;
    if(unknown)queueDraftUI.uncertain.add(id);
    if((unknown||error.httpStatus===409)&&state.base===origin&&state.token===token){
      try{await checkWithdrawal(id);}catch{toast('撤回结果尚未核实。连接恢复后点击“核对撤回状态”，不会自动重复提交。',true);}
    }else reportError(error);
  }finally{queueDraftUI.busy.delete(id);redrawQueueDrafts();}
}
async function resumeWithdrawnDraft(id){
  if(!queueDraftSupported()||!state.connected||queueDraftUI.busy.has(id))return;
  const job=queueDraftJob(id);if(!job||job.status!=='withdrawn'||job.can_resume_draft!==true||isTechnicalTest(job))return;
  try{if(!queueDraftIdle()||legacyDraftPending(id)){toast('请先完成或核对当前提交，当前草稿不会被替换。');return;}}
  catch(error){toast(error.message,true);updateLegacyDraftAccess();return;}
  if(queueDraftUI.legacy?.scope===state.base&&queueDraftUI.legacy.id===id&&queueDraftUI.legacy.uncertain){ensureLegacyDraftDialog();updateLegacyDraftAccess();if(!$('#legacyWithdrawnDialog').open)$('#legacyWithdrawnDialog').showModal();return;}
  const request=++queueDraftUI.request,variantRequest=++knowledgeUI.variantRequest,context=variantDraftContext(),revision=queueDraftUI.editRevision;
  queueDraftUI.busy.add(id);redrawQueueDrafts();
  try{
    const data=await api('/api/jobs/'+encodeURIComponent(id)+'/draft');
    if(request!==queueDraftUI.request||variantRequest!==knowledgeUI.variantRequest)return;
    if(!queueDraftSupported()||!queueDraftIdle()||legacyDraftPending(id)||context!==variantDraftContext()||revision!==queueDraftUI.editRevision){toast('你已编辑或提交当前草稿，本次载入已停止。需要时可再次点击继续编辑。');return;}
    if(data.withdrawn_job_id!==id)throw new Error('草稿与所选任务不一致，未替换当前内容。');
    if(data.draft_kind==='withdrawn_legacy'){openLegacyWithdrawnDraft(data);return;}
    if(data.draft_kind!=='withdrawn')throw new Error('当前任务不是可恢复的撤回草稿。');
    if(legacyDraftPending())throw new Error('请先核对旧版草稿的提交结果。');
    await applyWorkDraft(data,'withdrawn');creatorFeedback('已恢复撤回草稿，尚未提交制作。修改后点击“生成视频”创建新任务，原任务记录保留。');toast(localPage?'草稿已带入；当前原有内容已先保存，尚未生成新视频。':'草稿已带入，尚未生成新视频。');
  }catch(error){if(error.httpStatus===409){try{const latest=await api('/api/jobs/'+encodeURIComponent(id));rememberQueueJob(latest.job);}catch{}}reportError(error);}
  finally{queueDraftUI.busy.delete(id);redrawQueueDrafts();}
}

function legacyStoredDrafts(){
  if(!localPage)return [];
  const invalid=()=>new Error('本机旧版草稿记录损坏或格式不完整，已停止载入和提交；已有记录不会被覆盖。');
  try{
    let raw;try{raw=localStorage.getItem(LEGACY_WITHDRAWN_DRAFTS);}catch{throw new Error('无法读取本机旧版草稿记录，已停止载入和提交，请检查浏览器存储权限。');}
    if(raw===null){queueDraftUI.legacyStorageError='';return [];}
    let entries;try{entries=JSON.parse(raw);}catch{throw invalid();}
    if(!Array.isArray(entries)||entries.length>30)throw invalid();
    const seen=new Set();
    for(const item of entries){
      if(!item||typeof item!=='object'||Array.isArray(item)||typeof item.id!=='string'||!item.id||typeof item.scope!=='string'||!item.scope||typeof item.title!=='string'||typeof item.source!=='string'||typeof item.settingsText!=='string'||typeof item.uncertain!=='boolean')throw invalid();
      const identity=JSON.stringify([item.scope,item.id]);if(seen.has(identity))throw invalid();seen.add(identity);
      if(item.requestId!==null&&typeof item.requestId!=='string')throw invalid();
      if(item.requestBody!==null&&(!item.requestBody||typeof item.requestBody!=='object'||Array.isArray(item.requestBody)))throw invalid();
      if(item.uncertain&&(!item.requestId||item.requestBody?.idempotency_key!==item.requestId))throw invalid();
    }
    queueDraftUI.legacyStorageError='';return entries;
  }catch(error){queueDraftUI.legacyStorageError=error.message;throw error;}
}
function legacyDraftPending(exceptId=null){const draft=queueDraftUI.legacy;return (draft?.scope===state.base&&(draft.busy||(draft.uncertain&&draft.id!==exceptId)))||legacyStoredDrafts().some(item=>item.scope===state.base&&item.uncertain&&item.id!==exceptId);}
function persistLegacyDraft(draft=queueDraftUI.legacy){
  if(!localPage||!draft)return;
  const {id,scope,title,source,settingsText,requestId,requestBody,uncertain}=draft;
  const entries=legacyStoredDrafts().filter(item=>item.id!==id||item.scope!==scope);
  if(entries.length>=30)throw new Error('旧版草稿已保存 30 份，无法再新增。所有已有草稿和待核实的提交编号均已保留；本次未提交。');
  entries.push({id,scope,title,source,settingsText,requestId,requestBody,uncertain});
  // Fail before submitting if the request identity cannot be preserved locally.
  try{localStorage.setItem(LEGACY_WITHDRAWN_DRAFTS,JSON.stringify(entries));}catch{throw new Error('浏览器未能保存旧版草稿，请保留当前内容并检查存储权限。');}
}
function ensureLegacyDraftDialog(){
  if($('#legacyWithdrawnDialog'))return;
  const dialog=document.createElement('dialog');dialog.id='legacyWithdrawnDialog';dialog.className='work-drafts-dialog legacy-withdrawn-dialog';
  dialog.innerHTML='<div class="work-drafts-heading"><div><div class="eyebrow">SAVED DRAFT</div><h2>继续编辑旧版剪辑草稿</h2></div><button type="button" class="close-button" id="legacyWithdrawnClose" aria-label="保存并关闭">×</button></div><p class="help">原任务已撤回，全部原设置保留。这里的修改不会影响原片；点击下方按钮后才会创建新任务。</p><form id="legacyWithdrawnForm"><label>作品名称<input id="legacyWithdrawnTitle" maxlength="160"></label><label>素材<input id="legacyWithdrawnSource" list="legacyWithdrawnSources" maxlength="4000" required></label><datalist id="legacyWithdrawnSources"></datalist><p class="help">可保留原素材，也可从已同步素材中选择。</p><details><summary>高级：完整原剪辑设置</summary><p class="help">保留全部设置，只有需要精确调整时才修改。格式不正确时不会提交。</p><label>剪辑设置<textarea id="legacyWithdrawnSettings" rows="14" spellcheck="false" required></textarea></label></details><p id="legacyWithdrawnStatus" class="work-drafts-status" role="status" aria-live="polite"></p><div class="work-draft-actions"><button type="submit" class="button primary" id="legacyWithdrawnSubmit">生成新任务</button><button type="button" class="button subtle" id="legacyWithdrawnSave">保存并关闭</button></div></form>';
  document.body.appendChild(dialog);
  $('#legacyWithdrawnForm').onsubmit=submitLegacyWithdrawnDraft;
  for(const id of ['legacyWithdrawnTitle','legacyWithdrawnSource','legacyWithdrawnSettings'])$('#'+id).addEventListener('input',()=>{const draft=queueDraftUI.legacy;if(!draft||draft.busy||draft.uncertain)return;draft.title=$('#legacyWithdrawnTitle').value;draft.source=$('#legacyWithdrawnSource').value;draft.settingsText=$('#legacyWithdrawnSettings').value;try{persistLegacyDraft();}catch(error){$('#legacyWithdrawnStatus').textContent=error.message;}});
  const close=()=>{if(queueDraftUI.legacy?.busy)return;try{persistLegacyDraft();dialog.close();if(queueDraftUI.legacy?.uncertain)toast('提交结果尚待核实，内容与编号已保留。连接恢复后，从原撤回任务继续核对。');}catch(error){$('#legacyWithdrawnStatus').textContent=error.message;}};
  $('#legacyWithdrawnClose').onclick=close;$('#legacyWithdrawnSave').onclick=close;
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
}
function openLegacyWithdrawnDraft(data){
  if(!queueDraftSupported())return;
  if(queueDraftUI.legacy?.scope===state.base&&(queueDraftUI.legacy.busy||queueDraftUI.legacy.uncertain))throw new Error('先核对当前旧版草稿的提交结果。');
  if(!data.settings||typeof data.settings!=='object'||Array.isArray(data.settings)||typeof data.source!=='string')throw new Error('旧版草稿数据不完整，原内容未被替换。');
  const entries=legacyStoredDrafts(),saved=entries.find(item=>item.id===data.withdrawn_job_id&&item.scope===state.base);
  if(!saved&&localPage&&entries.length>=30)throw new Error('旧版草稿已保存 30 份，无法再新增；已有草稿与提交编号均已保留，当前内容未替换。');
  ensureLegacyDraftDialog();
  queueDraftUI.legacy={id:data.withdrawn_job_id,scope:state.base,title:data.title||'',source:data.source,settingsText:JSON.stringify(data.settings,null,2),requestId:null,requestBody:null,uncertain:false,...saved,busy:false};
  const draft=queueDraftUI.legacy;$('#legacyWithdrawnTitle').value=draft.title;$('#legacyWithdrawnSource').value=draft.source;$('#legacyWithdrawnSettings').value=draft.settingsText;
  $('#legacyWithdrawnSources').innerHTML=state.assets.map(asset=>'<option value="'+esc(asset.source)+'">'+esc(assetName(asset))+'</option>').join('');
  $('#legacyWithdrawnStatus').textContent=draft.uncertain?'上次提交结果待核实。请保持同一编号，明确点击下方核对提交。':'已载入全部原设置，尚未创建新任务。';
  $('#reviewDialog').close();updateLegacyDraftAccess();if(!$('#legacyWithdrawnDialog').open)$('#legacyWithdrawnDialog').showModal();
}
function updateLegacyDraftAccess(){
  const draft=queueDraftUI.legacy;if(!draft||!$('#legacyWithdrawnDialog'))return;
  // Rendering uses only the last known error; storage parsing happens in guarded actions.
  if(queueDraftUI.legacyStorageError)$('#legacyWithdrawnStatus').textContent=queueDraftUI.legacyStorageError;
  const locked=draft.busy||draft.uncertain||draft.scope!==state.base;
  for(const id of ['legacyWithdrawnTitle','legacyWithdrawnSource','legacyWithdrawnSettings'])$('#'+id).disabled=locked;
  $('#legacyWithdrawnSubmit').disabled=!queueDraftSupported()||!state.connected||draft.busy||draft.scope!==state.base;
  $('#legacyWithdrawnSubmit').textContent=draft.busy?'正在提交…':draft.uncertain?'使用同一编号核对提交':'生成新任务';
  $('#legacyWithdrawnClose').disabled=draft.busy;$('#legacyWithdrawnSave').disabled=draft.busy;
}
async function submitLegacyWithdrawnDraft(event){
  event.preventDefault();const draft=queueDraftUI.legacy;
  if(!draft||draft.busy||!queueDraftSupported()||!state.connected||draft.scope!==state.base)return;
  const wasUncertain=draft.uncertain,current=()=>queueDraftUI.legacy===draft&&draft.scope===state.base;let submitted=false;
  try{
    if(!draft.uncertain){
      const settings=JSON.parse($('#legacyWithdrawnSettings').value);
      if(!settings||typeof settings!=='object'||Array.isArray(settings))throw new Error('剪辑设置必须是完整的设置对象。');
      draft.title=$('#legacyWithdrawnTitle').value;draft.source=$('#legacyWithdrawnSource').value;draft.settingsText=$('#legacyWithdrawnSettings').value;
      if(!draft.source.trim())throw new Error('请选择素材。');
      draft.requestId=crypto.randomUUID();draft.requestBody={title:draft.title.trim(),source:draft.source.trim(),settings,idempotency_key:draft.requestId};
    }
    if(!draft.requestId||draft.requestBody?.idempotency_key!==draft.requestId)throw new Error('原提交编号缺失，请先在任务列表核实，不能以新编号重复提交。');
    draft.uncertain=true;persistLegacyDraft(draft);draft.busy=true;updateLegacyDraftAccess();$('#legacyWithdrawnStatus').textContent='正在提交。不会自动重复提交，原设置保持不变。';
    submitted=true;const result=await api('/api/jobs',{method:'POST',body:JSON.stringify(draft.requestBody)});
    if(!result.job?.id)throw Object.assign(new Error('提交结果待核实，请保持原编号。'),{uncertain:true});
    draft.uncertain=false;draft.requestId=null;draft.requestBody=null;let saveWarning='';try{persistLegacyDraft(draft);}catch(error){saveWarning=' 本机草稿记录未能更新：'+error.message;}
    if(current()){rememberQueueJob(result.job);$('#legacyWithdrawnDialog').close();toast((result.job.status==='withdrawn'?'已找到这次提交的任务，但它已被撤回。可在任务记录中继续编辑草稿。':result.created===false?'已找到同一编号的任务，没有重复创建。':'新任务已加入队列，原撤回记录保留。')+saveWarning,!!saveWarning);}
  }catch(error){
    if((!submitted&&!wasUncertain)||(submitted&&error.uncertain!==true&&error.connectivity!==true)){draft.uncertain=false;draft.requestId=null;draft.requestBody=null;}
    if(current())$('#legacyWithdrawnStatus').textContent=(error instanceof SyntaxError?'剪辑设置格式不正确，请检查高级设置。':error.message)+(draft.uncertain?' 当前内容和编号已锁定，连接恢复后由你明确核对提交。':'');
    try{persistLegacyDraft(draft);}catch{}
  }finally{draft.busy=false;if(current())updateLegacyDraftAccess();}
}

document.addEventListener('input',event=>{if(creatorFields.includes(event.target.id)||productionFields.includes(event.target.id))queueDraftUI.editRevision++;});
document.addEventListener('change',event=>{if(creatorFields.includes(event.target.id)||productionFields.includes(event.target.id)||event.target.id==='presenterConsent')queueDraftUI.editRevision++;});
document.addEventListener('click',event=>{
  const withdraw=event.target.closest('[data-withdraw-job]');if(withdraw&&!withdraw.disabled)withdrawQueuedJob(withdraw.dataset.withdrawJob);
  const resume=event.target.closest('[data-resume-withdrawn]');if(resume&&!resume.disabled)resumeWithdrawnDraft(resume.dataset.resumeWithdrawn);
});
redrawQueueDrafts();
