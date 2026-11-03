'use strict';
const IDEA_DRAFT='cutflow.idea.draft.v1';
const ideaAssistant={id:null,requestBody:null,job:null,pending:false,checking:false,uncertain:false,pollTimer:null,undoText:null,appliedText:null,revision:0,snapshot:null,editedText:'',sequence:0};
function ideaContext(){return JSON.stringify({brief:$('#creatorBrief').value,selected:creator.selected,options:creatorOptions(),mode:typeof productionMode==='function'?productionMode():'edit'});}
function ideaMatches(){return ideaAssistant.snapshot?.revision===ideaAssistant.revision&&ideaAssistant.snapshot?.context===ideaContext();}
function ideaResultValid(result){return result&&typeof result.expanded_brief==='string'&&result.expanded_brief.trim().length>20&&result.expanded_brief.length<=2400&&typeof result.production_brief==='string'&&result.production_brief.trim().length>0&&result.production_brief.length<=500&&Array.isArray(result.assumptions)&&result.assumptions.length<=15&&result.assumptions.every(x=>typeof x==='string'&&x.length<=600);}
function saveIdeaDraft(){
  if(!localPage||typeof localStorage==='undefined')return;
  try{localStorage.setItem(IDEA_DRAFT,JSON.stringify({id:ideaAssistant.id,requestBody:ideaAssistant.requestBody,job:ideaAssistant.job,uncertain:ideaAssistant.uncertain,undoText:ideaAssistant.undoText,appliedText:ideaAssistant.appliedText,revision:ideaAssistant.revision,snapshot:ideaAssistant.snapshot,editedText:ideaAssistant.editedText}));}catch{}
}
function loadIdeaDraft(){
  if(!localPage||typeof localStorage==='undefined')return;
  try{
    const d=JSON.parse(localStorage.getItem(IDEA_DRAFT)||'null');if(!d)return;
    if(typeof d.id==='string'&&/^[a-f0-9-]{36}$/i.test(d.id)&&d.requestBody?.request_id===d.id&&typeof d.requestBody.brief==='string'&&d.requestBody.brief.length<=3000){ideaAssistant.id=d.id;ideaAssistant.requestBody=d.requestBody;ideaAssistant.uncertain=d.uncertain===true;}
    if(d.job?.id===ideaAssistant.id&&['queued','running','succeeded','failed'].includes(d.job.status)&&(!d.job.result||ideaResultValid(d.job.result)))ideaAssistant.job=d.job;
    ideaAssistant.revision=Number.isSafeInteger(d.revision)&&d.revision>=0?d.revision:0;
    if(d.snapshot&&typeof d.snapshot.context==='string'&&d.snapshot.context.length<70000&&Number.isSafeInteger(d.snapshot.revision))ideaAssistant.snapshot=d.snapshot;
    for(const key of ['undoText','appliedText'])if(typeof d[key]==='string'&&d[key].length<=3000)ideaAssistant[key]=d[key];
    if(typeof d.editedText==='string'&&d.editedText.length<=2400)ideaAssistant.editedText=d.editedText;
    ideaAssistant.pending=['queued','running'].includes(ideaAssistant.job?.status);
  }catch{}
}
function ideaMessage(message,error=false){$('#ideaStatus').textContent=message;$('#ideaStatus').className='idea-status'+(error?' error':'');}
function updateIdeaAccess(){
  const supported=state.config.idea_refinement_supported===true,locked=creatorInputLocked(),job=ideaAssistant.job;
  $('#ideaRefineButton').disabled=locked||ideaAssistant.checking||!state.connected||!supported;
  $('#ideaRefineButton').textContent=ideaAssistant.pending?'查看完善进度…':ideaAssistant.uncertain?'核对完善结果':job?.status==='succeeded'&&ideaMatches()?'查看完善方案':'✦ 帮我完善想法';
  $('#ideaApplyButton').disabled=locked||!state.connected||job?.status!=='succeeded'||!ideaMatches()||!ideaResultValid(job.result)||!$('#ideaEditor').value.trim();
  $('#ideaAgainButton').disabled=locked||ideaAssistant.pending||ideaAssistant.checking||!state.connected||!supported;
  $('#ideaRetryButton').disabled=locked||ideaAssistant.pending||ideaAssistant.checking||!state.connected||!supported;
  $('#ideaRetryButton').hidden=!(ideaAssistant.uncertain||job?.status==='failed');
  $('#ideaEditor').disabled=locked||job?.status!=='succeeded';
  $('#ideaUndoButton').hidden=!ideaAssistant.appliedText;
  $('#ideaUndoButton').disabled=locked||$('#creatorBrief').value!==ideaAssistant.appliedText;
  $('#ideaChanged').hidden=!ideaAssistant.snapshot||ideaMatches()||job?.status!=='succeeded';
  if(!supported&&!ideaAssistant.id)ideaMessage(state.connected?'创意助手正在准备，原有制作功能可以继续使用。':'连接后可将一句想法展开成完整方案。');
  if(ideaAssistant.pending&&state.connected&&!ideaAssistant.checking&&!ideaAssistant.pollTimer)scheduleIdeaPoll();
}
function renderIdeaPanel(){
  const job=ideaAssistant.job,result=job?.result;
  $('#ideaOriginal').textContent=ideaAssistant.requestBody?.brief||'';
  $('#ideaLoading').hidden=!(ideaAssistant.pending||ideaAssistant.checking&&!job);
  $('#ideaLoading').textContent=ideaAssistant.checking&&!job?'正在提交你的想法，原文保持不变…':job?.status==='running'?'正在梳理受众、开场和内容结构，通常需要几十秒…':'正在排队完善想法，原文不会被覆盖。';
  $('#ideaResultSection').hidden=job?.status!=='succeeded';
  $('#ideaFailure').hidden=!(job?.status==='failed'||ideaAssistant.uncertain&&!ideaAssistant.checking);
  $('#ideaFailure').textContent=ideaAssistant.uncertain?'提交结果暂未确认。点击核对会查找原任务，不会重复生成。':job?.error||'暂未完成，请稍后重试。';
  if(result&&ideaResultValid(result)){
    $('#ideaEditor').value=ideaAssistant.editedText||result.expanded_brief;
    $('#ideaExecution').textContent=result.production_brief;
    $('#ideaAssumptions').textContent=result.assumptions.length?result.assumptions.map(v=>'• '+v).join('\n'):'没有额外补充的业务事实；请按实际素材核对方案。';
  }
  updateIdeaAccess();
}
function showIdeaPanel(){renderIdeaPanel();if(!$('#ideaDialog').open)$('#ideaDialog').showModal();}
function acceptIdeaRecord(record,requestedId){
  if(ideaAssistant.id!==requestedId)return false;
  if(!record||record.id!==requestedId||!['queued','running','succeeded','failed'].includes(record.status)||record.status==='succeeded'&&!ideaResultValid(record.result))throw new Error('完善结果不完整，原想法已保留，请重新完善。');
  const previous=ideaAssistant.job;
  ideaAssistant.job={id:record.id,status:record.status,result:record.result||null,error:typeof record.error==='string'?record.error.slice(0,600):null};
  if(record.status==='succeeded'&&(previous?.id!==record.id||previous?.status!=='succeeded'))ideaAssistant.editedText=record.result.expanded_brief;
  ideaAssistant.pending=['queued','running'].includes(record.status);ideaAssistant.uncertain=false;
  saveIdeaDraft();renderIdeaPanel();
  if(record.status==='succeeded')ideaMessage(ideaMatches()?'完善方案已准备好，查看后可修改并采用。':'方案已准备好；你已修改想法，请查看后基于当前文字重新完善。');
  else if(record.status==='failed')ideaMessage(ideaAssistant.job.error||'这次未能完成，原想法已保留。',true);
  else ideaMessage('正在完善想法，你可以继续查看素材。');
  scheduleIdeaPoll();return true;
}
function scheduleIdeaPoll(){
  clearTimeout(ideaAssistant.pollTimer);ideaAssistant.pollTimer=null;
  if(ideaAssistant.pending&&state.connected)ideaAssistant.pollTimer=setTimeout(()=>{ideaAssistant.pollTimer=null;pollIdeaRefinement().catch(()=>{});},2000);
}
async function startIdeaRefinement(force=false){
  if(creatorInputLocked()||ideaAssistant.pending||ideaAssistant.checking||!state.connected||state.config.idea_refinement_supported!==true)return;
  if(ideaAssistant.uncertain){showIdeaPanel();return retryIdeaRefinement();}
  if(!force&&ideaAssistant.job?.status==='succeeded'&&ideaMatches()){showIdeaPanel();return;}
  const original=$('#creatorBrief').value,brief=original.trim();
  if(!brief){ideaMessage('先写一句简单想法，例如“汽车托管，让车主放心”。',true);$('#creatorBrief').focus?.();return;}
  if(brief.length>3000){ideaMessage('想法太长，请缩短到 3000 字以内。',true);return;}
  clearTimeout(ideaAssistant.pollTimer);
  const id=crypto.randomUUID(),body={request_id:id,brief:original,sources:creatorSelection().filter(a=>a.ready).map(a=>a.source)};
  ideaAssistant.id=id;ideaAssistant.requestBody=body;ideaAssistant.job=null;ideaAssistant.editedText='';
  ideaAssistant.snapshot={revision:ideaAssistant.revision,context:ideaContext()};ideaAssistant.uncertain=true;ideaAssistant.checking=true;
  saveIdeaDraft();showIdeaPanel();ideaMessage('正在提交想法，原文保持不变…');
  try{const result=await api('/api/ideas',{method:'POST',body:JSON.stringify(body)});acceptIdeaRecord(result.idea,id);}
  catch(error){if(ideaAssistant.id===id){ideaAssistant.uncertain=error.uncertain===true;ideaAssistant.pending=false;ideaMessage(error.message,true);$('#ideaFailure').hidden=false;$('#ideaFailure').textContent=error.message;}}
  finally{if(ideaAssistant.id===id){ideaAssistant.checking=false;$('#ideaLoading').hidden=!ideaAssistant.pending;saveIdeaDraft();updateIdeaAccess();}}
}
async function pollIdeaRefinement(){
  if(!ideaAssistant.id||ideaAssistant.checking||!state.connected)return;
  const id=ideaAssistant.id;ideaAssistant.checking=true;updateIdeaAccess();
  try{const result=await api('/api/ideas/'+encodeURIComponent(id));acceptIdeaRecord(result.idea,id);}
  catch(error){if(ideaAssistant.id===id){ideaAssistant.pending=false;ideaAssistant.uncertain=true;ideaMessage('读取完善结果暂时中断，原文已保留；连接后可核对原任务。',true);renderIdeaPanel();}}
  finally{if(ideaAssistant.id===id){ideaAssistant.checking=false;saveIdeaDraft();updateIdeaAccess();scheduleIdeaPoll();}}
}
async function retryIdeaRefinement(){
  if(creatorInputLocked()||ideaAssistant.checking||ideaAssistant.pending||!state.connected||!ideaAssistant.id)return;
  const id=ideaAssistant.id;ideaAssistant.checking=true;updateIdeaAccess();
  try{
    let found;
    try{found=(await api('/api/ideas/'+encodeURIComponent(id))).idea;}
    catch(error){if(error.httpStatus!==404)throw error;}
    if(!found){const result=await api('/api/ideas',{method:'POST',body:JSON.stringify(ideaAssistant.requestBody)});acceptIdeaRecord(result.idea,id);}
    else if(found.status==='failed'&&!ideaAssistant.uncertain){const result=await api('/api/ideas/'+encodeURIComponent(id)+'/retry',{method:'POST'});acceptIdeaRecord(result.idea,id);}
    else acceptIdeaRecord(found,id);
  }catch(error){if(ideaAssistant.id===id){ideaAssistant.uncertain=true;ideaAssistant.pending=false;ideaMessage(error.message,true);renderIdeaPanel();}}
  finally{if(ideaAssistant.id===id){ideaAssistant.checking=false;saveIdeaDraft();updateIdeaAccess();scheduleIdeaPoll();}}
}
function applyIdeaRefinement(){
  const result=ideaAssistant.job?.result;
  if(creatorInputLocked()||!state.connected||ideaAssistant.job?.status!=='succeeded'||!ideaMatches()||!ideaResultValid(result))return;
  const edited=$('#ideaEditor').value.trim();
  if(!edited||edited.length>2400||edited.includes('【剪辑执行要求】')){ideaMessage('请将创意方案控制在 2400 字内；剪辑执行要求会单独附在末尾。',true);return;}
  const next=typeof productionMode==='function'&&productionMode()!=='edit'?edited:edited+'\n\n【剪辑执行要求】\n'+result.production_brief;
  if(next.length>3000){ideaMessage('方案过长，原想法已保留，请缩短后再采用。',true);return;}
  ideaAssistant.undoText=$('#creatorBrief').value;ideaAssistant.appliedText=next;ideaAssistant.editedText=edited;
  $('#creatorBrief').value=next;ideaAssistant.revision++;
  saveCreatorDraft();saveIdeaDraft();updateCreatorAccess();$('#ideaDialog').close();
  ideaMessage('已采用完整方案，可继续修改；需要时可撤销回原想法。');
}
function undoIdeaRefinement(){
  if(creatorInputLocked()||!ideaAssistant.appliedText||$('#creatorBrief').value!==ideaAssistant.appliedText||typeof ideaAssistant.undoText!=='string')return;
  $('#creatorBrief').value=ideaAssistant.undoText;ideaAssistant.appliedText=null;ideaAssistant.undoText=null;ideaAssistant.revision++;
  saveCreatorDraft();saveIdeaDraft();updateCreatorAccess();ideaMessage('已恢复采用前的原想法。');
}
function noteIdeaInputChange(){ideaAssistant.revision++;saveIdeaDraft();updateIdeaAccess();}
$('#ideaRefineButton').onclick=()=>ideaAssistant.pending?showIdeaPanel():startIdeaRefinement(false);
$('#ideaAgainButton').onclick=()=>startIdeaRefinement(true);
$('#ideaRetryButton').onclick=retryIdeaRefinement;
$('#ideaApplyButton').onclick=applyIdeaRefinement;
$('#ideaUndoButton').onclick=undoIdeaRefinement;
$('#ideaCloseButton').onclick=()=>$('#ideaDialog').close();
$('#ideaEditor').addEventListener('input',()=>{ideaAssistant.editedText=$('#ideaEditor').value;saveIdeaDraft();updateIdeaAccess();});
for(const id of creatorFields)for(const event of ['input','change'])$('#'+id).addEventListener(event,noteIdeaInputChange);
document.addEventListener('change',event=>{if(event.target.closest('[data-creator-select]'))noteIdeaInputChange();});
document.addEventListener('click',event=>{if(event.target.closest('[data-creator-move],[data-creator-remove],[data-add-to-creator],[data-brief-example],#creatorClearSelection'))noteIdeaInputChange();});
loadIdeaDraft();renderIdeaPanel();scheduleIdeaPoll();
