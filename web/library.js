'use strict';
const knowledgeUI={items:[],busy:false,loaded:false,editing:null,editEpoch:0,revision:0,variant:null,variantRequest:0,conflict:null};
const workLabels={edit:'原声剪辑',talking_head:'人物口播',narrated_promo:'独白宣传',brand_promo:'企业／沙龙宣传'};
function completedWork(job){return !!job&&!isTechnicalTest(job)&&job.has_video&&['review','approved','published'].includes(job.status)&&job.result?.qc?.passed===true;}
function renderWorkActions(job){
  const node=$('#workActions');if(!node)return;node.hidden=!completedWork(job);if(node.hidden){node.innerHTML='';return;}
  node.innerHTML='<strong>继续完善这条作品</strong><div class="work-action-row"><button class="button subtle" data-collect-work="'+esc(job.id)+'">☆ 收藏与标注</button>'+(job.spoken_script?'<button class="button subtle" data-rewrite-work="'+esc(job.id)+'">✦ 一键改文案</button>':'')+(job.mode==='talking_head'?'<button class="button subtle" data-presenter-work="'+esc(job.id)+'">换人物，做新版本</button>':'')+'<button class="text-button" data-face-swap>原视频换脸 · 待开通</button></div>'+(job.parent_job_id?'<p class="help">基于原作品生成的新版本 <button class="text-button" data-review="'+esc(job.parent_job_id)+'">查看原作品 ↗</button></p>':'')+(job.reference_works?.length?'<p class="help">参考作品：'+job.reference_works.map(r=>esc(r.title)+'（标注第 '+esc(r.revision)+' 版）').join('、')+'</p>':'');
  node.querySelectorAll('button').forEach(b=>b.disabled=!state.connected||creatorInputLocked());
}
async function refreshKnowledge(){
  if(knowledgeUI.busy||!state.connected)return;knowledgeUI.busy=true;$('#knowledgeStatus').textContent='正在读取作品知识库…';
  const context=connectionContext();try{const response=await api('/api/knowledge?limit=500',{},'json',context);if(!currentContext(context))return;knowledgeUI.items=response.items||[];knowledgeUI.loaded=true;renderKnowledge();$('#knowledgeStatus').textContent='已保存 '+knowledgeUI.items.length+' 份作品。点击“参考制作”，下一次讲稿就会带上参考。';}
  catch(e){$('#knowledgeStatus').textContent=e.message;}finally{knowledgeUI.busy=false;}
}
function renderKnowledge(){
  const q=$('#knowledgeSearch').value.trim().toLowerCase(),favorites=$('#knowledgeFavorites').checked;
  const items=knowledgeUI.items.filter(i=>(!favorites||i.favorite)&&(!q||[i.title,i.notes,...i.tags].join(' ').toLowerCase().includes(q)));
  $('#knowledgeCards').innerHTML=items.length?items.map(i=>'<article class="panel knowledge-card"><div class="knowledge-cover"><span>'+esc(workLabels[i.snapshot.mode]||'视频作品')+'</span><button class="button dark" data-review="'+esc(i.id)+'">▷ 试看作品</button></div><div class="knowledge-card-body"><div class="knowledge-card-title"><h3>'+esc(i.title)+'</h3><span>'+(i.favorite?'★':'☆')+'</span></div><div class="knowledge-tags">'+i.tags.map(t=>'<span class="tag light">'+esc(t)+'</span>').join('')+'</div><p>'+esc(i.notes||'还没有标注。写下喜欢的地方，让下次参考更明确。')+'</p><details><summary>已提取的参考</summary><p>开场：'+esc(i.learning_profile.opening?.text||'未选择开场参考')+'</p><p>文案 '+esc(i.learning_profile.structure?.sentence_count??'—')+' 句 · '+esc(i.snapshot.options?.aspect||'原画幅')+' · '+(i.snapshot.options?.music?'有配乐':'无额外配乐')+'</p><p class="help">来自已有讲稿与设置，没有自动观看画面。</p></details><div class="work-action-row"><button class="button primary" data-reference-work="'+esc(i.id)+'" '+(!i.favorite?'disabled':'')+'>参考制作 →</button><button class="button subtle" data-collect-work="'+esc(i.id)+'">修改标注</button>'+(i.snapshot.spoken_script?'<button class="text-button" data-rewrite-work="'+esc(i.id)+'">改文案</button>':'')+(i.snapshot.mode==='talking_head'?'<button class="text-button" data-presenter-work="'+esc(i.id)+'">换人物</button>':'')+'</div></div></article>').join(''):'<div class="panel empty"><strong>'+(!knowledgeUI.loaded?'连接后读取作品库':'还没有符合条件的作品')+'</strong><p>在成片旁点击“收藏与标注”，把满意的作品留下来。</p><button class="button dark" data-open-jobs>去看已完成作品 →</button></div>';
  renderWorkContext();
}
async function collectWork(id){
  if(!state.connected||creatorInputLocked())return;const epoch=++knowledgeUI.editEpoch;knowledgeUI.editing=id;knowledgeUI.revision=0;knowledgeUI.conflict=null;$('#knowledgeConflict').hidden=true;$('#knowledgeSave').disabled=true;$('#knowledgeSaveStatus').textContent='正在读取…';
  try{let item;try{item=(await api('/api/knowledge/'+encodeURIComponent(id))).item;}catch(e){if(e.httpStatus!==404)throw e;}
    if(knowledgeUI.editing!==id||knowledgeUI.editEpoch!==epoch)return;
    const job=state.jobs.find(j=>j.id===id)||state.selected;knowledgeUI.revision=item?.revision||0;$('#knowledgeTitle').value=item?.title||job?.title||'优秀作品';$('#knowledgeFavorite').checked=item?.favorite!==false;$('#knowledgeTags').value=(item?.tags||[]).join('，');$('#knowledgeNotes').value=item?.notes||'';
    document.querySelectorAll('.learn-fields input').forEach(e=>e.checked=item?item.learn_fields.includes(e.value):true);
    $('#knowledgeSaveStatus').textContent='保存后可在下次制作中引用。';$('#knowledgeSave').disabled=false;$('#knowledgeDialog').showModal();
  }catch(e){toast(e.message,true);}
}
async function saveKnowledge(event){
  event.preventDefault();if($('#knowledgeSave').disabled)return;$('#knowledgeSave').disabled=true;
  const id=knowledgeUI.editing,epoch=knowledgeUI.editEpoch,current=()=>knowledgeUI.editing===id&&knowledgeUI.editEpoch===epoch,body={expected_revision:knowledgeUI.revision,title:$('#knowledgeTitle').value.trim(),favorite:$('#knowledgeFavorite').checked,tags:$('#knowledgeTags').value.split(/[,，、]/).map(t=>t.trim()).filter(Boolean),notes:$('#knowledgeNotes').value.trim(),learn_fields:[...document.querySelectorAll('.learn-fields input:checked')].map(e=>e.value)};
  try{const result=await api('/api/knowledge/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify(body)});if(current()){knowledgeUI.revision=result.item.revision;$('#knowledgeSaveStatus').textContent='已保存。';$('#knowledgeDialog').close();toast('作品与标注已保存，可在作品知识库中参考制作。');}await refreshKnowledge();}
  catch(e){if(!current())return;$('#knowledgeSaveStatus').textContent=e.message+' 你的标注仍在这里。';if(e.httpStatus===409){try{const latest=(await api('/api/knowledge/'+encodeURIComponent(id))).item;if(current()){knowledgeUI.conflict=latest;$('#knowledgeConflictText').textContent='最新标注（第 '+latest.revision+' 版）：'+latest.notes;$('#knowledgeConflict').hidden=false;}}catch{}}}finally{if(current())$('#knowledgeSave').disabled=false;}
}
function renderWorkContext(){
  const node=$('#workContext');if(!node)return;const refs=production.referenceIds||[];node.hidden=!production.parentId&&!refs.length;
  node.innerHTML=(production.parentId?'<div><b>'+esc(production.parentTitle||'已有作品')+' · 新版本</b><span>原片保留</span></div>':'')+(refs.length?'<div><b>本次参考</b>'+refs.map(id=>'<button class="reference-chip" data-remove-reference="'+esc(id)+'">'+esc(knowledgeUI.items.find(i=>i.id===id)?.title||'已选参考作品')+' ×</button>').join('')+'<p class="help">参考表达方式；活动时间、地点与业务信息以你的新想法为准。</p></div>':'')+'<button class="text-button" id="clearWorkContext">清除参考与版本关联</button>';
  const clear=$('#clearWorkContext');if(clear)clear.onclick=()=>{if(creatorInputLocked())return;production.parentId=null;production.parentTitle=null;production.revisionKind=null;production.referenceIds=[];saveProductionDraft();renderWorkContext();updateProductionAccess();};
}
function referenceWork(id){
  if(creatorInputLocked()){toast('先核对当前提交结果，再更换参考。');return;}const item=knowledgeUI.items.find(i=>i.id===id);if(!item?.favorite)return;
  const refs=production.referenceIds||[];if(!refs.includes(id)&&refs.length>=3){toast('一次最多参考 3 份作品，请先移除一份。');return;}production.referenceIds=[...new Set([...refs,id])];
  const profile=item.learning_profile||{},options=item.snapshot.options||{};
  if(profile.visual_format&&['16:9','9:16','1:1','original'].includes(options.aspect))$('#productionAspect').value=options.aspect;
  if('music' in profile)$('#productionMusic').checked=options.music===true;if('subtitles' in profile)$('#productionSubtitles').checked=options.subtitles===true;
  saveProductionDraft();showPage('create');renderWorkContext();updateProductionAccess();$('#creatorBrief').focus();toast('已加入参考。写下这次的新想法，再生成讲稿。');
}
async function prepareWorkVariant(id,kind){
  if(creatorInputLocked()||production.checking||production.uncertain||['queued','running'].includes(production.scriptJob?.status)){toast('当前任务仍需核对，请稍后再开始新版本。');return;}
  const request=++knowledgeUI.variantRequest,context=variantDraftContext();
  try{const data=await api('/api/jobs/'+encodeURIComponent(id)+'/draft');if(request!==knowledgeUI.variantRequest)return;if(creatorInputLocked()||context!==variantDraftContext()){toast('你已编辑当前草稿，本次带入已停止。可再次点击开始新版本。');return;}knowledgeUI.variant={...data,kind};
    if(kind==='rewrite'){$('#variantTitle').textContent='一键改文案';$('#variantOriginal').textContent='原作品：'+data.title;$('#variantInstruction').value='';$('#variantStatus').textContent='';$('#variantDialog').showModal();$('#variantInstruction').focus();}
    else{await applyWorkDraft(data,kind);toast('原讲稿和设置已带入，选择新人物后生成新版本。');$('#productionPresenter').scrollIntoView({behavior:'smooth',block:'start'});}
  }catch(e){toast(e.message,true);}
}
function variantDraftContext(){return JSON.stringify({context:productionContext(),fields:productionFields.map(id=>productionChecks.has(id)?$('#'+id).checked:$('#'+id).value),portrait:production.portraitId,kind:production.kind});}
async function applyWorkDraft(data,kind){
  if(creatorInputLocked())throw new Error('请先核对当前制作任务');const d=data.draft,options=d.options||{};
  production.mode=d.mode||'edit';production.parentId=data.parent_job_id;production.parentTitle=data.title;production.revisionKind=kind;production.referenceIds=[];production.scriptId=null;production.scriptJob=null;production.scriptBody=null;production.scriptContext=null;production.uncertain=false;clearTimeout(production.pollTimer);production.pollTimer=null;$('#productionScriptPreview').hidden=true;scriptMessage('');
  creator.selected=(data.sources||[]).map(s=>'s:'+s);creator.resultId=null;$('#creatorBrief').value=d.brief||'';$('#productionScript').value=d.spoken_script||'';$('#productionSubtype').value=d.subtype||'salon_promo';
  for(const [key,id] of Object.entries({name:'productionBrand',cta:'productionCTA',event_date:'productionDate',event_place:'productionPlace'}))$('#'+id).value=d.brand?.[key]||'';
  for(const [key,id] of Object.entries({opening_title:'productionOpeningTitle',aspect:'productionAspect',voice_style:'productionStyle',source_audio:'productionSourceAudio'}))if(options[key]!=null)$('#'+id).value=options[key];
  if(options.target_duration)$('#productionDuration').value=String(options.target_duration);$('#productionMusic').checked=options.music===true;$('#productionSubtitles').checked=options.subtitles!==false;$('#productionLoop').checked=d.visual_fill==='loop';production.preferredVoice=d.voice_id||null;production.voiceExplicit=true;$('#productionVoice').value=d.voice_id||'';
  if(d.presenter){production.kind=d.presenter.kind;production.portraitId=kind==='presenter'?null:d.presenter.portrait_id;$('#presenterConsent').checked=!!d.presenter.consent_confirmed;document.querySelectorAll('[data-presenter-kind]').forEach(e=>e.classList.toggle('active',e.dataset.presenterKind===production.kind));}
  $('#reviewDialog').close();showPage('create');renderCreatorMaterials();updateProductionAccess();renderCreatorResult();renderWorkContext();saveCreatorDraft();saveProductionDraft();if(production.mode==='talking_head')await loadPresenterCatalog();
}
async function rewriteWork(){
  const instruction=$('#variantInstruction').value.trim();if(!instruction){$('#variantStatus').textContent='写一句希望怎么改，也可点上面的快捷选项。';return;}$('#variantRewrite').disabled=true;
  try{await applyWorkDraft(knowledgeUI.variant,'rewrite');$('#creatorBrief').value='修改这条作品的讲稿：'+instruction;saveCreatorDraft();saveProductionDraft();$('#variantDialog').close();await writeProductionScript();$('#productionScriptPanel').scrollIntoView({behavior:'smooth',block:'start'});}
  catch(e){$('#variantStatus').textContent=e.message;}finally{$('#variantRewrite').disabled=false;}
}
document.addEventListener('click',event=>{
  const collect=event.target.closest('[data-collect-work]');if(collect)collectWork(collect.dataset.collectWork);
  const rewrite=event.target.closest('[data-rewrite-work]');if(rewrite)prepareWorkVariant(rewrite.dataset.rewriteWork,'rewrite');
  const person=event.target.closest('[data-presenter-work]');if(person)prepareWorkVariant(person.dataset.presenterWork,'presenter');
  const reference=event.target.closest('[data-reference-work]');if(reference)referenceWork(reference.dataset.referenceWork);
  const remove=event.target.closest('[data-remove-reference]');if(remove&&!creatorInputLocked()){production.referenceIds=(production.referenceIds||[]).filter(id=>id!==remove.dataset.removeReference);saveProductionDraft();renderWorkContext();updateProductionAccess();}
  const tip=event.target.closest('[data-rewrite-tip]');if(tip)$('#variantInstruction').value=tip.dataset.rewriteTip;
  if(event.target.closest('[data-face-swap]')){$('#faceSwapReason').textContent=state.config.face_swap?.reason||'原视频换脸尚未开通。';$('#faceSwapDialog').showModal();}
  if(event.target.closest('[data-open-jobs]'))showPage('jobs');
});
$('#knowledgeForm').onsubmit=saveKnowledge;$('#knowledgeSearch').oninput=renderKnowledge;$('#knowledgeFavorites').onchange=renderKnowledge;$('#knowledgeRefresh').onclick=refreshKnowledge;$('#variantRewrite').onclick=rewriteWork;
$('#knowledgeRebase').onclick=()=>{if(knowledgeUI.conflict&&knowledgeUI.conflict.id===knowledgeUI.editing){knowledgeUI.revision=knowledgeUI.conflict.revision;knowledgeUI.conflict=null;$('#knowledgeConflict').hidden=true;saveKnowledge({preventDefault(){}});}};
renderWorkContext();
