'use strict';
const STORY_DRAFT='cutflow.story.assembly.v1';
const storyAssembly={id:null,body:null,context:null,record:null,busy:false,uncertain:false,timer:null,previewSignature:'',jobFetched:null,jobFetching:false};
function storyBrief(){return $('#creatorBrief').value.trim()||'结合企业文化与品牌知识，讲清一个真实、有用的故事';}
function storyOptions(){return {aspect:$('#productionAspect').value,target_duration:Number($('#productionDuration').value),music:$('#productionMusic').checked,subtitles:$('#productionSubtitles').checked,opening_title:$('#productionOpeningTitle').value.trim()};}
function storyDraftContext(){return JSON.stringify({base:state.base||'',script:$('#productionScript').value.trim(),brief:storyBrief(),selected:creator.selected,options:storyOptions(),parent:production.parentId,revision:production.revisionKind,references:production.referenceIds,brandRevision:typeof brandContextUI==='undefined'?null:brandContextUI.revision});}
function storyLineage(){return {...(production.parentId?{parent_job_id:production.parentId,revision_kind:production.revisionKind==='presenter'?'remix':production.revisionKind}:{}),...(production.referenceIds?.length?{reference_ids:[...production.referenceIds]}:{})};}
function saveStoryAssembly(){if(!localPage||typeof localStorage==='undefined')return;try{localStorage.setItem(STORY_DRAFT,JSON.stringify({id:storyAssembly.id,body:storyAssembly.body,context:storyAssembly.context,record:storyAssembly.record,uncertain:storyAssembly.uncertain}));}catch{}}
function loadStoryAssembly(){if(!localPage||typeof localStorage==='undefined')return;try{const d=JSON.parse(localStorage.getItem(STORY_DRAFT)||'null');if(d&&/^[a-f0-9-]{36}$/i.test(d.id)&&d.body?.request_id===d.id&&typeof d.context==='string'){Object.assign(storyAssembly,{id:d.id,body:d.body,context:d.context,record:d.record?.id===d.id?d.record:null,uncertain:d.uncertain===true});}}catch{}}
function storyPlanCurrent(){return storyAssembly.context===storyDraftContext();}
function storyPlanReady(){return storyAssembly.record?.status==='ready'&&storyPlanCurrent()&&validStoryResult(storyAssembly.record.result);}
function storyAssemblyPending(){return ['queued','running'].includes(storyAssembly.record?.status)||(storyAssembly.body?.auto_render&&storyAssembly.record?.status==='ready'&&!storyAssembly.record.job_id&&!storyAssembly.record.error);}
function recordStoryJobReceipt(body,job){if(body?.assembly_plan_id===storyAssembly.id&&job?.id&&storyAssembly.record){storyAssembly.record.job_id=job.id;storyAssembly.jobFetched=job.id;saveStoryAssembly();}}
function validStoryResult(result){return !!result&&typeof result.script==='string'&&/^[a-f0-9]{64}$/i.test(result.script_sha256||'')&&Array.isArray(result.sources)&&result.sources.length>0&&result.sources.length<=12&&result.sources.every(s=>typeof s==='string'&&s.length>0)&&Array.isArray(result.shots)&&result.shots.length>0&&result.shots.length<=100&&result.shots.every(s=>typeof s.source==='string'&&typeof s.narration==='string'&&[s.source_in,s.source_out,s.timeline_start,s.timeline_end].every(Number.isFinite)&&s.source_in>=0&&s.source_out>s.source_in&&s.timeline_start>=0&&s.timeline_end>s.timeline_start);}
function storyStatus(text,error=false){$('#storyAssemblyStatus').textContent=text;$('#storyAssemblyStatus').className='media-status'+(error?' error':'');}
function storyClock(seconds){const n=Math.max(0,Math.round(Number(seconds)||0));return Math.floor(n/60)+':'+String(n%60).padStart(2,'0');}
function renderStoryAssembly(){
  const record=storyAssembly.record,current=storyPlanCurrent(),result=record?.result;
  const signature=JSON.stringify([record,current]);if(storyAssembly.previewSignature===signature)return;storyAssembly.previewSignature=signature;
  if(record?.status!=='ready'||!validStoryResult(result)){$('#storyAssemblyPreview').innerHTML='';return;}
  const summary=Array.isArray(result.summary)?result.summary.join('；'):typeof result.summary==='string'?result.summary:'';
  $('#storyAssemblyPreview').innerHTML=(current?'':'<p class="story-stale">脚本、素材或设置已改变，重新编排后将使用当前版本。</p>')+(summary?'<p class="story-plan-summary">'+esc(summary)+'</p>':'')+'<ol class="story-shots">'+result.shots.map((shot,index)=>{
    const thumbnail=typeof shot.thumbnail==='string'&&/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(shot.thumbnail)?shot.thumbnail:null;
    const file=shot.source.split(/[\\/]/).pop();
    return '<li class="story-shot"><button type="button" class="story-shot-preview" data-story-shot="'+index+'" aria-label="预览第 '+(index+1)+' 段">'+(thumbnail?'<img src="'+thumbnail+'" alt="'+esc(file)+'">':'<span>▷</span>')+'<small>'+storyClock(shot.source_in)+'–'+storyClock(shot.source_out)+'</small></button><div><strong>第 '+(index+1)+' 段 · '+storyClock(shot.timeline_start)+'–'+storyClock(shot.timeline_end)+'</strong><p>'+esc(shot.narration)+'</p><small>'+esc(file)+'</small>'+(shot.selection_note?'<p class="story-selection-basis">'+esc(shot.selection_note)+'</p>':'')+'</div></li>';
  }).join('')+'</ol>';
}
function scheduleStoryPoll(){clearTimeout(storyAssembly.timer);storyAssembly.timer=null;if(!storyAssembly.uncertain&&state.connected&&storyAssemblyPending())storyAssembly.timer=setTimeout(()=>{storyAssembly.timer=null;pollStoryAssembly();},2500);}
async function adoptStoryJob(record){
  if(!record.job_id||storyAssembly.jobFetched===record.job_id||storyAssembly.jobFetching)return;
  const context=connectionContext();storyAssembly.jobFetching=true;
  try{const data=await api('/api/jobs/'+encodeURIComponent(record.job_id),{},'json',context);if(!currentContext(context)||storyAssembly.record?.job_id!==record.job_id)return;if(!data.job?.id)throw new Error('成片任务正在准备');storyAssembly.jobFetched=record.job_id;if(production.mode==='story'&&storyPlanCurrent()){creator.resultId=data.job.id;saveCreatorDraft();}state.jobs=[data.job,...state.jobs.filter(j=>j.id!==data.job.id)];renderJobs();renderCreatorResult();}
  catch{storyStatus('已开始精剪，成片任务正在读取。');}
  finally{storyAssembly.jobFetching=false;}
}
function acceptStoryAssembly(record,id){
  if(storyAssembly.id!==id)return;
  if(!record||record.id!==id||!['queued','running','ready','failed'].includes(record.status)||(record.status==='ready'&&(!validStoryResult(record.result)||record.result.script!==storyAssembly.body.spoken_script)))throw new Error('编排结果需要核对，请点击重试。');
  storyAssembly.record=record;storyAssembly.uncertain=false;saveStoryAssembly();renderStoryAssembly();updateStoryWorkflow();scheduleStoryPoll();
  if(record.job_id)adoptStoryJob(record);
}
async function pollStoryAssembly(){
  if(storyAssembly.busy||!storyAssembly.id||!state.connected)return;
  const id=storyAssembly.id,context=connectionContext();storyAssembly.busy=true;
  try{const data=await api('/api/assembly-plans/'+encodeURIComponent(id),{},'json',context);if(currentContext(context))acceptStoryAssembly(data.assembly,id);}
  catch(error){if(currentContext(context)){storyAssembly.uncertain=true;storyStatus('编排进度暂未读取，点击核对可继续。',true);}}
  finally{storyAssembly.busy=false;saveStoryAssembly();updateStoryWorkflow();if(!storyAssembly.uncertain)scheduleStoryPoll();}
}
async function requestStoryAssembly(autoRender=false){
  if(storyAssembly.busy||creatorInputLocked()||!state.connected)return;
  const pending=storyAssemblyPending();if(pending&&!storyAssembly.uncertain)return pollStoryAssembly();
  const script=$('#productionScript').value.trim(),selected=creatorSelection();
  if(!storyAssembly.uncertain&&(!script||script.length>1500||selected.some(s=>!s.ready)||creator.uploading))return;
  const retry=storyAssembly.uncertain;
  if(!retry){storyAssembly.id=crypto.randomUUID();storyAssembly.body={request_id:storyAssembly.id,spoken_script:script,brief:storyBrief(),title:$('#creatorTitle').value.trim(),sources:selected.map(s=>s.source),options:storyOptions(),auto_render:autoRender,...storyLineage()};if(production.scriptJob?.status==='succeeded'&&production.scriptContext===productionContext())storyAssembly.body.script_id=production.scriptId;storyAssembly.context=storyDraftContext();storyAssembly.record=null;storyAssembly.jobFetched=null;}
  const id=storyAssembly.id,context=connectionContext();storyAssembly.busy=true;storyAssembly.uncertain=true;saveStoryAssembly();updateStoryWorkflow();storyStatus('正在提交自动选材与编排…');
  try{
    let record;
    if(retry){try{record=(await api('/api/assembly-plans/'+encodeURIComponent(id),{},'json',context)).assembly;}catch(error){if(error.httpStatus!==404)throw error;}}
    if(!record)record=(await api('/api/assembly-plans',{method:'POST',body:JSON.stringify(storyAssembly.body)},'json',context)).assembly;
    if(currentContext(context))acceptStoryAssembly(record,id);
  }catch(error){if(currentContext(context)){storyAssembly.uncertain=scriptRequestUncertain(error);storyStatus(error.message,true);}}
  finally{storyAssembly.busy=false;saveStoryAssembly();updateStoryWorkflow();if(!storyAssembly.uncertain)scheduleStoryPoll();}
}
async function generateStoryProduction(){
  if(storyAssembly.busy||!state.connected||!state.config.production?.modes?.story?.ready)return;
  if(storyAssembly.uncertain||!storyPlanReady())return requestStoryAssembly(true);
  if(storyAssembly.record.job_id){adoptStoryJob(storyAssembly.record);storyStatus('这版编排已开始制作，可在下方查看进度。');return;}
  const result=storyAssembly.record.result;
  creator.requestId=crypto.randomUUID();creator.requestBody={idempotency_key:creator.requestId,mode:'story',assembly_plan_id:storyAssembly.id,spoken_script:$('#productionScript').value.trim(),expected_script_sha256:result.script_sha256,sources:[...result.sources],brief:storyBrief(),options:storyOptions(),title:$('#creatorTitle').value.trim(),...storyLineage()};
  if(storyAssembly.body.script_id)creator.requestBody.script_id=storyAssembly.body.script_id;
  creator.uncertain=true;saveCreatorDraft();await generateProduction();
}
function updateStoryWorkflow(){
  if(typeof directorSync==='function'&&director.active){directorSync();return;}
  if(storyAssembly.context){try{if(JSON.parse(storyAssembly.context).base!==(state.base||'')){clearTimeout(storyAssembly.timer);Object.assign(storyAssembly,{id:null,body:null,context:null,record:null,uncertain:false,timer:null});saveStoryAssembly();}}catch{}}
  const active=production.mode==='story';
  const materialPanel=$('#productionMaterialPanel'),layout=$('#productionScriptPanel').closest('.creator-layout');
  materialPanel.hidden=$('#productionMaterials').hidden;
  $('#storyMaterialSummary').hidden=!active;
  if(storyAssembly.lastMode!==production.mode){materialPanel.open=!active;storyAssembly.lastMode=production.mode;}
  if(layout?.appendChild){if(active&&materialPanel.nextElementSibling!==null)layout.appendChild(materialPanel);else if(!active){const briefPanel=$('#productionScriptPanel').closest('.creator-brief-panel');if(briefPanel&&materialPanel.nextElementSibling!==briefPanel)layout.insertBefore(materialPanel,briefPanel);}}
  for(const id of ['storyEnterprise','storyAssemblyPanel','storyRenderHeading'])$('#'+id).hidden=!active;
  $('#creatorMaterialsHeading').innerHTML=active?'素材范围（可选）':'<span class="step-number">1</span> 选择素材';$('#creatorMaterialsHint').textContent=active?'不选择时，自动从素材库挑选。也可以上传或限定使用范围。':'已有素材直接勾选；新视频可以一次上传多个。';
  $('#productionMaterials').classList.toggle('story-materials',active);$('#productionScriptHeading').textContent=active?'2  视频脚本':'视频里实际说的话';
  $('#productionScriptPanel').closest('.creator-layout')?.classList.toggle('story-layout',active);
  if(!active)return;
  if(state.connected&&typeof loadBrandContext==='function'&&typeof brandContextUI!=='undefined'&&!brandContextUI.loading&&!brandContextUI.dirty&&brandContextUI.revision===null&&storyAssembly.brandRequestedScope!==brandContextScope()){storyAssembly.brandRequestedScope=brandContextScope();loadBrandContext();}
  $('#productionScriptPanel').hidden=false;$('#productionSubtitles').closest('label').hidden=true;$('#storySubtitles').checked=$('#productionSubtitles').checked;
  const script=$('#productionScript').value.trim(),selection=creatorSelection(),pending=storyAssemblyPending(),scriptPending=production.checking||['queued','running'].includes(production.scriptJob?.status);
  const blocked=creatorInputLocked()||storyAssembly.busy||!state.connected||(!storyAssembly.uncertain&&(pending||scriptPending||creator.uploading||selection.some(s=>!s.ready)));
  $('#storyPlanButton').disabled=blocked||(!storyAssembly.uncertain&&(!script||script.length>1500));
  $('#storyPlanButton').textContent=storyAssembly.uncertain?'核对上次编排':pending?'正在自动编排…':storyAssembly.record?'重新选材与编排':'自动选材与编排';
  $('#productionWriteScript').disabled=creatorInputLocked()||production.checking||(scriptPending&&!production.uncertain)||!state.connected||state.config.production?.script_supported!==true;
  $('#productionWriteScript').textContent=production.uncertain?'核对上次脚本':scriptPending?'正在生成脚本…':'一键生成视频脚本';
  $('#storySourceScope').textContent=selection.length?'限定在已选的 '+selection.length+' 段素材中挑选':'从素材库自动选择合适画面';
  $('#creatorGenerate').disabled=creator.busy||!state.connected||(!creator.uncertain&&(blocked||(!storyAssembly.uncertain&&(!script||script.length>1500||!state.config.production?.modes?.story?.ready))));
  $('#creatorGenerate').textContent=creator.uncertain?'核对并继续制作':storyAssembly.uncertain?'核对并继续精剪':pending&&storyAssembly.body?.auto_render?'选材后自动开始精剪…':'一键自动精剪 →';
  $('#creatorReadiness').textContent=!script?'先生成脚本，也可以直接填写。':storyPlanReady()?'按这版脚本和画面编排制作。':'自动选择合适素材、编排画面并精剪。';
  $('#storySubtitles').disabled=creatorInputLocked();
  const r=storyAssembly.record;
  if(!storyAssembly.busy&&!storyAssembly.uncertain){if(r?.status==='failed')storyStatus(r.error||'这次编排未完成，可以重新尝试。',true);else if(pending){const labels={queued:'等待编排',analyzing:'分析素材',transcribing:'识别素材原声',selecting:'选择合适画面',planning:'编排画面',planning_visuals:'编排画面',rendering:'准备精剪'};storyStatus((labels[r.stage]||'正在自动选材与编排')+(storyAssembly.body?.auto_render?' · 完成后自动精剪':''));}else if(r?.job_id)storyStatus('已进入精剪队列。');else if(r?.status==='ready'&&r.error)storyStatus('编排已完成，精剪暂未开始。点击“一键自动精剪”继续。',true);else if(r?.status==='ready')storyStatus(storyPlanCurrent()?'画面已编排好，可以直接精剪。':'内容已改变，点击重新编排。');}
  renderStoryAssembly();if(pending&&!storyAssembly.timer&&!storyAssembly.busy&&!storyAssembly.uncertain)scheduleStoryPoll();
}
$('#storyPlanButton').onclick=()=>requestStoryAssembly(false);
$('#storyChooseMaterials').onclick=()=>{$('#productionMaterialPanel').open=true;$('#productionMaterials').scrollIntoView({behavior:'smooth',block:'start'});$('#creatorAssetSearch').focus();};
$('#storyEditCulture').onclick=()=>{$('#openBrandContext').click();$('#brandCulture')?.focus();};
$('#storySubtitles').addEventListener('change',()=>{$('#productionSubtitles').checked=$('#storySubtitles').checked;saveProductionDraft();updateStoryWorkflow();});
let storyPreviewRange=null;
document.addEventListener('click',async event=>{const button=event.target.closest('[data-story-shot]');if(!button){if(event.target.closest('[data-preview]'))storyPreviewRange=null;return;}const shot=storyAssembly.record?.result?.shots?.[Number(button.dataset.storyShot)];if(!shot||typeof previewSource!=='function')return;const range={source:shot.source,start:shot.source_in,end:shot.source_out};storyPreviewRange=range;await previewSource(shot.source);if(storyPreviewRange!==range)return;const video=$('#sourceVideo');const seek=()=>{if(storyPreviewRange===range)video.currentTime=range.start;};if(video.readyState>=1)seek();else video.addEventListener('loadedmetadata',seek,{once:true});$('#sourceDetails').textContent='本段使用 '+storyClock(shot.source_in)+'–'+storyClock(shot.source_out)+'，原片保持完整。';});
$('#sourceVideo').addEventListener('timeupdate',()=>{const video=$('#sourceVideo');if(storyPreviewRange&&video.currentTime>=storyPreviewRange.end){video.pause();storyPreviewRange=null;}});
$('#sourceDialog').addEventListener('close',()=>{storyPreviewRange=null;});
loadStoryAssembly();updateStoryWorkflow();
