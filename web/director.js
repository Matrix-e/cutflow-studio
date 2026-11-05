'use strict';
const DIRECTOR_KEY='cutflow.director.v1';
const DIRECTOR_DEFAULT_GOAL='根据这些素材，选择最有依据、最值得讲的一个主题。保留真实细节与原声，让观众看懂事情经过和一个有用的发现。';
const director={active:true,busy:false,reading:false,checking:false,pending:null,selected:null,lastSubmission:null,records:[],timer:null,scope:null,lastRead:0,listSignature:'',detailSignature:'',preflight:null,mediaKey:null,mediaBlob:null,coverBlob:null,mediaRequest:0};
const directorFields=['directorGoal','directorMode','directorDuration','directorAspect','directorTitle','directorSubtitles','directorMusic','directorBrandUsage'];
const directorChecks=new Set(['directorSubtitles','directorMusic']);
const directorEvidenceRefreshed=new Set();
const directorStages=[['preflight','检查制作条件'],['understanding','理解素材'],['planning','策划内容'],['scripting','脚本与分镜'],['editing','选材与剪辑'],['rendering','声音与渲染'],['quality','检查成片'],['delivery','准备交付']];
const directorStatusNames={queued:'等待制作',running:'制作中',blocked:'需要补充条件',failed:'制作未完成',completed:'成片已就绪'};
function directorAvailable(){return state.connected&&state.config?.director_supported===true;}
function directorId(value){return typeof value==='string'&&/^[a-zA-Z0-9-]{16,64}$/.test(value);}
function directorDeliverable(record){return record?.status==='completed'&&record.quality?.passed===true&&['video','subtitles','cover','copy','record','package'].every(kind=>record.artifacts?.some(a=>a.kind===kind&&directorArtifactPath(record,a)));}
function directorStatus(record){return record?.status==='completed'&&!directorDeliverable(record)?'交付信息待核对':directorStatusNames[record?.status]||'状态待核对';}
function directorCanRevise(record){return record?.can_revise===true||(record?.can_revise===undefined&&record?.status==='completed');}
function directorCanRetry(record){return record?.can_retry===true||(record?.can_retry===undefined&&['failed','blocked'].includes(record?.status)&&Number(record?.attempts||0)<3);}
function directorRecord(value){return value&&directorId(value.id)&&Object.hasOwn(directorStatusNames,value.status);}
function directorStageProgress(record){
  const index=stage=>directorStages.findIndex(([name])=>name===({repair:'quality',preparing_voice:'rendering'}[stage]||stage));
  const seen=(Array.isArray(record.events)?record.events:[]).map(event=>index(event.stage)).filter(value=>value>=0);
  const reported=index(record.stage),current=reported>=0?reported:seen.at(-1)??-1,furthest=Math.max(current,...seen);
  const renderEntered=record.stage==='rendering'||(record.events||[]).some(event=>event.stage==='rendering')||furthest>index('rendering');
  return directorStages.map(([stage,label],i)=>{
    if(directorDeliverable(record))return {stage,label,state:'done',text:'已完成'};
    if(i===current){
      if(['failed','blocked'].includes(record.status))return {stage,label,state:'issue',text:record.status==='blocked'?'需要补充条件':'此环节未完成'};
      if(record.status==='queued')return {stage,label,state:'paused',text:record.error?'等待重试':'等待继续'};
      if(record.status==='running')return {stage,label,state:'active',text:record.stage==='repair'?'正在修复后复查':record.stage==='preparing_voice'?'准备独白与声音':'正在进行'};
    }
    // Narration is prepared before final shot timing. Entering that sound
    // substage does not prove the earlier editing group has finished.
    if(stage==='editing'&&furthest===index('rendering')&&!renderEntered)return {stage,label,state:'',text:'等待声音确定后完成剪辑'};
    // Entering a later stage in this sequential pipeline proves earlier stages
    // returned successfully. Keep that history when failure changes stage=failed.
    return {stage,label,state:i<furthest?'done':'',text:i<furthest?'已完成':'等待前序步骤'};
  });
}
function directorMessage(message,error=false){$('#directorMessage').textContent=message;$('#directorMessage').className='media-status'+(error?' error':'');}
function saveDirector(){if(!localPage)return;try{localStorage.setItem(DIRECTOR_KEY,JSON.stringify({base:state.base,active:director.active,selected:director.selected,lastSubmission:director.lastSubmission,pending:director.pending,fields:Object.fromEntries(directorFields.map(id=>[id,directorChecks.has(id)?$('#'+id).checked:$('#'+id).value]))}));}catch{directorMessage('本机草稿存储暂不可用，已提交任务仍保存在制作服务中。',true);}}
function loadDirector(){if(!localPage)return;try{const d=JSON.parse(localStorage.getItem(DIRECTOR_KEY)||'null');if(!d||d.base!==state.base)return;director.active=d.active!==false;director.selected=directorId(d.selected)?d.selected:null;if(d.lastSubmission&&directorId(d.lastSubmission.id)&&typeof d.lastSubmission.context==='string')director.lastSubmission=d.lastSubmission;for(const id of directorFields){if(directorChecks.has(id)){if(typeof d.fields?.[id]==='boolean')$('#'+id).checked=d.fields[id];}else if(typeof d.fields?.[id]==='string')$('#'+id).value=d.fields[id];}if(d.pending&&directorId(d.pending.id)&&d.pending.body?.request_id===d.pending.id&&typeof d.pending.path==='string'&&/^\/api\/productions(?:\/[a-zA-Z0-9-]{16,64}\/(?:revise|retry))?$/.test(d.pending.path))director.pending=d.pending;}catch{}}
function directorFormContext(){return JSON.stringify({base:state.base,sources:creatorSelection().map(s=>s.source),goal:$('#directorGoal').value.trim()||DIRECTOR_DEFAULT_GOAL,mode:$('#directorMode').value||'auto',title:$('#directorTitle').value.trim(),options:directorOptions()});}
function directorOptions(){return {aspect:$('#directorAspect').value||'9:16',target_duration:Number($('#directorDuration').value)||30,subtitles:$('#directorSubtitles').checked,music:$('#directorMusic').checked,brand_usage:$('#directorBrandUsage').value||'auto'};}
function setDirectorActive(active){director.active=active;directorSync();saveDirector();if(!active&&typeof updateProductionAccess==='function')updateProductionAccess();if(active){$('#directorGoal').focus();refreshDirector(true);}}
function directorSync(){
  const slot=$('#directorMaterialSlot'),material=$('#productionMaterialPanel');if(!slot||!material)return;
  $('#directorWorkspace').hidden=!director.active;$('#directorManualWorkspace').hidden=director.active;
  if(director.active){if(material.parentElement!==slot)slot.appendChild(material);material.hidden=false;material.open=true;$('#productionMaterials').hidden=false;$('#creatorMaterialsHeading').innerHTML='<span class="step-number">1</span> 选择或导入素材';$('#creatorMaterialsHint').textContent='选中希望使用的视频，无需先排顺序；不选时从素材库自动挑选。';$('#creatorNext').textContent='下一步：写下目标 ↓';}
  else if(material.parentElement===slot){const layout=$('#productionScriptPanel').closest('.creator-layout');layout.insertBefore(material,$('#productionScriptPanel').closest('.creator-brief-panel'));}
  const selection=creatorSelection(),unready=selection.filter(s=>!s.ready),oldBusy=typeof creatorInputLocked==='function'&&creatorInputLocked();
  $('#directorSelectionStatus').textContent=creator.uploading?'素材正在上传与同步，完成后即可制作。':unready.length?creatorMaterialReadiness(selection):selection.length?'已选 '+selection.length+' 段 · 自动判断取舍与顺序':'未限定素材 · 自动从素材库选择候选画面';
  $('#directorStart').disabled=director.busy||!directorAvailable()||(!director.pending&&(creator.uploading||unready.length>0||oldBusy));
  $('#directorStart').textContent=!directorAvailable()?'自动制作服务尚未就绪':director.busy?'正在提交…':director.pending?'核对上次提交，不重复制作':director.lastSubmission?.context===directorFormContext()?'查看这次制作 →':'开始自动制作 →';
  $('#directorCheck').disabled=director.checking||!directorAvailable();$('#directorCheck').textContent=director.checking?'正在检查…':'检查制作条件';$('#directorRefresh').disabled=director.reading||!directorAvailable();
  if(state.connected&&!directorAvailable()&&!director.pending){director.unavailableNotice=true;directorMessage('自动制作服务正在升级或尚未启用。可继续编辑目标，原有制作方式仍可使用。');}else if(directorAvailable()&&director.unavailableNotice){director.unavailableNotice=false;directorMessage('自动制作服务已连接，可以开始制作。');}
  const selected=director.records.find(r=>r.id===director.selected);$('#directorRevise').disabled=director.busy||!directorAvailable()||!directorCanRevise(selected)||!$('#directorRevision').value.trim();
  const scope=JSON.stringify([state.base,state.epoch]);if(director.scope!==scope){director.scope=scope;director.lastRead=0;director.preflight=null;directorEvidenceRefreshed.clear();renderDirectorPreflight();}
  if(state.connected&&state.config?.director_supported===true&&!director.reading&&Date.now()-director.lastRead>10000)refreshDirector();
}
function directorArtifactPath(record,artifact){if(!directorRecord(record)||typeof artifact?.url!=='string')return null;const expected='/api/productions/'+record.id;return artifact.url===expected+'/video'||new RegExp('^'+expected+'/artifacts/[a-z_]+$').test(artifact.url)?artifact.url:null;}
function directorText(value){if(typeof value==='string')return value;if(Array.isArray(value))return value.map(directorText).filter(Boolean).join('；');if(value&&typeof value==='object')return String(value.message||value.description||value.text||value.label||value.summary||value.reason||'');return '';}
function directorClock(value){const t=Math.max(0,Number(value)||0);return Math.floor(t/60)+':'+String(Math.floor(t%60)).padStart(2,'0');}
function directorOnlyReferences(value,letters='TVS'){
  const parts=Array.isArray(value)?value:typeof value==='string'?value.trim().split(/[\s,，、;；]+/):value&&typeof value==='object'?Object.values(value):[];
  return parts.length>0&&parts.every(part=>typeof part==='string'?new RegExp('^['+letters+']\\d+(?:-\\d+)+$').test(part.trim()):Array.isArray(part)&&directorOnlyReferences(part,letters));
}
function directorReadableStructure(structure,scenes){
  if(!directorOnlyReferences(structure,'TV'))return structure;
  const opening=scenes[0]?.purpose,ending=scenes.at(-1)?.purpose;
  return {opening:directorOnlyReferences(opening)?'':opening,
    development:[...new Set(scenes.slice(1,-1).map(scene=>scene.relation).filter(value=>value&&!directorOnlyReferences(value)))],
    ending:directorOnlyReferences(ending)?'':ending};
}
function directorSceneSource(scene,sources){
  if(scene.kind==='brand_screenshot')return scene.brand_asset?.name||'小程序真实截图';
  const source=[scene.source,scene.source_name,sources[scene.source_index]].find(value=>typeof value==='string'&&value.trim()&&!directorOnlyReferences(value));
  return source?friendlySourceName(source):'原素材';
}
function directorBrandEvidence(plan){
  const used=new Set((plan.scenes||[]).flatMap(s=>s.evidence_ids||[]));
  const facts=(plan.brand_facts||[]).filter(f=>used.has(f.id));
  if(!facts.length)return '';
  return '<details class="director-details" data-director-detail="brand-evidence"><summary>本片采用的企业知识</summary><ul>'+facts.map(f=>'<li><p>'+esc(f.text)+'</p>'+(f.source?'<small>依据：'+esc(f.source)+'</small>':'')+'</li>').join('')+'</ul><p class="help">小程序截图说明对应界面；实际办理条件以当前页面与协议为准。</p></details>';
}
function directorPlanHtml(plan,sources=[]){
  if(!plan||typeof plan!=='object')return '';
  const scenes=Array.isArray(plan.scenes)?plan.scenes:[];
  const structure=directorReadableStructure(plan.structure,scenes),evidence=plan.evidence||plan.material_basis;
  const cards=[['主题',plan.theme],['核心观点',plan.viewpoint||plan.core_point],['观众获得什么',plan.audience_value],['表达结构',structure&&typeof structure==='object'&&!Array.isArray(structure)?[['开头',structure.opening],['展开',structure.development],['结尾',structure.ending]].filter(([,v])=>directorText(v)).map(([k,v])=>k+'：'+directorText(v)).join('；'):structure],['风格与时长',[plan.style,plan.target_duration?plan.target_duration+' 秒':'']],['素材依据',directorOnlyReferences(evidence)?'':evidence]];
  return '<details class="director-details" data-director-detail="plan"><summary>内容方案与脚本</summary><div class="director-plan-grid">'+cards.filter(([,v])=>directorText(v)).map(([k,v])=>'<div><strong>'+esc(k)+'</strong><p>'+esc(directorText(v))+'</p></div>').join('')+'</div>'+(plan.actual_script?'<p class="director-copy">'+esc(plan.actual_script)+'</p>':'')+(scenes.length?'<ol class="director-scenes">'+scenes.map(s=>'<li><strong>'+esc(s.purpose||({opening:'引入',development:'展开',ending:'收束'}[s.role])||s.role||s.title||'画面')+'</strong><p>'+esc(s.text||s.narration||s.spoken_text||s.description||'')+'</p><small>'+esc(directorSceneSource(s,sources))+(s.kind==='brand_screenshot'?' · 完整页面，随旁白展示':' · '+directorClock(s.source_in??s.start)+'–'+directorClock(s.source_out??s.end))+'</small>'+(s.visual_description?'<p class="director-evidence">画面：'+esc(s.visual_description)+'</p>':'')+(s.relation&&!directorOnlyReferences(s.relation)?'<small>段落关系：'+esc(s.relation)+'</small>':'')+'</li>').join('')+'</ol>':'')+(plan.posting_copy?'<h4>建议发布文案</h4><p class="director-copy">'+esc(plan.posting_copy)+'</p>':'')+'</details>'+directorBrandEvidence(plan);
}
function directorIssues(error,preflight){const issues=[];if(error?.message)issues.push(error.message);for(const item of error?.details||[])issues.push(directorText(item));for(const item of preflight?.blockers||[])issues.push([item.message||item.detail||item.label,item.solution].filter(Boolean).join('：'));return [...new Set(issues.filter(Boolean))];}
function renderDirectorPreflight(){const box=$('#directorPreflight'),p=director.preflight;box.hidden=!p;if(!p)return;const blockers=directorIssues(null,p),checks=Array.isArray(p.checks)?p.checks:[];box.innerHTML='<strong>'+(p.active&&p.ready?'制作条件已实际验证':p.ready?'初步检查通过，提交后先验证模型与声音':'以下条件需要处理')+'</strong>'+(blockers.length?'<ul>'+blockers.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'')+'<details><summary>查看检查明细</summary><ul>'+checks.map(c=>'<li>'+esc(c.label||c.name||c.id)+'：'+esc(c.detail||c.message||({passed:'已验证',blocked:'未就绪',unverified:'待验证'}[c.status]||c.status))+(c.solution?'<small>'+esc(c.solution)+'</small>':'')+'</li>').join('')+'</ul></details>';}
async function checkDirector(){if(director.checking||!directorAvailable())return;director.checking=true;directorSync();const ctx=connectionContext();try{const result=await api('/api/productions/preflight?mode='+encodeURIComponent($('#directorMode').value||'auto'),{},'json',ctx);if(!currentContext(ctx))return;if(typeof result.preflight?.ready!=='boolean')throw new Error('制作条件检查结果不完整。');director.preflight=result.preflight;renderDirectorPreflight();}catch(e){if(!e.stale)directorMessage(e.httpStatus===404?'自动制作服务尚未部署，原有制作方式仍保留。':e.message,true);}finally{director.checking=false;directorSync();}}
function acceptDirector(record){if(!directorRecord(record))throw new Error('制作任务返回不完整，保留本次提交以便核对。');director.records=[record,...director.records.filter(r=>r.id!==record.id)];director.selected=record.id;if(director.pending?.id===record.id){if(director.pending.kind==='new')director.lastSubmission={id:record.id,context:director.pending.context};director.pending=null;directorMessage('任务已保存，后续步骤会自动进行。');}saveDirector();renderDirector();scheduleDirectorPoll();}
function scheduleDirectorPoll(){clearTimeout(director.timer);director.timer=null;if(!directorAvailable())return;if(director.pending||director.records.some(r=>['queued','running'].includes(r.status)))director.timer=setTimeout(()=>{director.timer=null;refreshDirector(true);},3000);}
async function refreshDirector(force=false){
  if(director.reading||!directorAvailable()||(!force&&Date.now()-director.lastRead<10000))return;director.reading=true;director.lastRead=Date.now();const ctx=connectionContext();
  try{const response=await api('/api/productions',{},'json',ctx);if(!currentContext(ctx))return;if(!Array.isArray(response.productions)||response.productions.some(r=>!directorRecord(r)))throw new Error('制作记录暂未完整读取。');director.records=response.productions;if(!director.selected)director.selected=director.records[0]?.id||null;if(director.pending){const found=director.records.find(r=>r.id===director.pending.id);if(found){if(director.pending.kind==='new')director.lastSubmission={id:found.id,context:director.pending.context};director.pending=null;director.selected=found.id;}}
    if(director.selected){const result=await api('/api/productions/'+director.selected,{},'json',ctx);if(!currentContext(ctx))return;if(directorRecord(result.production))director.records=director.records.some(r=>r.id===result.production.id)?director.records.map(r=>r.id===result.production.id?result.production:r):[result.production,...director.records];}
    saveDirector();renderDirector();
  }catch(e){if(!e.stale)directorMessage(e.httpStatus===404?'自动制作服务正在升级；原有素材和作品保持不变。':e.message,true);}finally{director.reading=false;directorSync();scheduleDirectorPoll();}
}
async function submitDirector(kind='new'){
  if(director.busy||!directorAvailable())return;const selection=creatorSelection();
  if(!director.pending&&kind==='new'&&director.lastSubmission?.context===directorFormContext()){director.selected=director.lastSubmission.id;saveDirector();await refreshDirector(true);$('#directorResults').scrollIntoView({behavior:'smooth',block:'start'});directorMessage('这份目标与素材已经提交，正在显示原任务。修改目标或素材后可以制作新版本。');return;}
  if(!director.pending){
    if(kind==='new'&&(creator.uploading||selection.some(s=>!s.ready)||creatorInputLocked())){directorMessage('请等待素材就绪，上一次制作草稿保持不变。',true);return;}
    const parent=director.records.find(r=>r.id===director.selected),id=crypto.randomUUID();let body,path='/api/productions';
    if(kind==='revise'){const instruction=$('#directorRevision').value.trim();if(!parent||!directorCanRevise(parent)||!instruction)return;body={request_id:id,instruction};path+='/'+parent.id+'/revise';}
    else if(kind==='retry'){if(!parent||!directorCanRetry(parent))return;body={request_id:id};path+='/'+parent.id+'/retry';}
    else body={request_id:id,sources:selection.map(s=>s.source),goal:$('#directorGoal').value.trim()||DIRECTOR_DEFAULT_GOAL,mode:$('#directorMode').value||'auto',title:$('#directorTitle').value.trim(),options:directorOptions()};
    director.pending={id,body,path,kind,context:kind==='new'?directorFormContext():null,parent_id:parent?.id||null};saveDirector();
  }
  const receipt=director.pending,ctx=connectionContext();director.busy=true;directorSync();directorMessage('正在保存制作任务…');
  try{let record;try{record=(await api('/api/productions/'+receipt.id,{},'json',ctx)).production;}catch(e){if(e.httpStatus!==404)throw e;}
    if(!record)record=(await api(receipt.path,{method:'POST',body:JSON.stringify(receipt.body)},'json',ctx)).production;
    if(!currentContext(ctx))return;if(!directorRecord(record))throw new Error('提交结果需要核对，已保留原请求。');if(receipt.kind!=='retry'&&record.id!==receipt.id)throw new Error('任务编号需要核对，已保留原请求。');if(receipt.kind==='new')director.lastSubmission={id:record.id,context:receipt.context};director.pending=null;acceptDirector(record);$('#directorResults').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(e){if(!e.stale){if(e.httpStatus&&e.httpStatus<500&&e.httpStatus!==408)director.pending=null;directorMessage(director.pending?'提交结果暂未确认。点击“核对上次提交”会查找同一任务，不会重复制作。':e.message,true);}}finally{director.busy=false;saveDirector();directorSync();scheduleDirectorPoll();}
}
function renderDirector(){
  const records=director.records,listSig=JSON.stringify(records.map(r=>[r.id,r.title,r.status,r.stage,r.updated_at,director.selected]));
  if(typeof refreshAssetInsightEvidence==='function')for(const record of records){if(directorEvidenceRefreshed.has(record.id)||!Array.isArray(record.sources)||!(record.events||[]).some(event=>['planning','scripting','editing','rendering','quality','repair','delivery','completed'].includes(event.stage)))continue;directorEvidenceRefreshed.add(record.id);refreshAssetInsightEvidence(record.sources);}
  if(listSig!==director.listSignature){director.listSignature=listSig;$('#directorTaskList').innerHTML=records.map(r=>'<button type="button" data-director-task="'+esc(r.id)+'" class="'+(r.id===director.selected?'active':'')+'"><strong>'+esc(r.title||r.content_plan?.title||r.goal||'自动制作')+'</strong><small>'+esc(directorStatus(r))+(r.parent_id?' · 修改版':'')+'</small></button>').join('');}
  const r=records.find(x=>x.id===director.selected);if(!r)return;
  const signature=JSON.stringify(r);if(signature!==director.detailSignature){director.detailSignature=signature;const box=$('#directorTaskDetail'),opened=new Set(Array.from(box.querySelectorAll?.('details[open]')||[]).map(e=>e.dataset.directorDetail));const events=Array.isArray(r.events)?r.events:[];const issues=directorIssues(r.error,r.preflight);const checks=Array.isArray(r.quality?.checks)?r.quality.checks:[];
    box.innerHTML='<div class="director-status"><div><span class="eyebrow">'+esc(directorStatus(r))+'</span><h3>'+esc(r.title||r.content_plan?.title||'正在准备内容方案')+'</h3><p class="help">'+esc(r.status==='completed'&&!directorDeliverable(r)?'请刷新核对交付文件与检查记录':r.stage_label||directorStatusNames[r.status])+(r.parent_id?' · 原版本已保留':'')+'</p></div><span class="tag light">'+esc(date(r.created_at))+'</span></div><ol class="director-steps">'+directorStageProgress(r).map(step=>'<li class="'+step.state+'">'+step.label+'<b>'+step.text+'</b></li>').join('')+'</ol>'+(issues.length?'<div class="director-issue"><strong>本次制作尚未完成，已有成果保留</strong><ul>'+issues.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>'+(directorCanRetry(r)?'<button type="button" class="button subtle" data-director-retry="'+r.id+'">条件解决后继续制作</button>':'')+'</div>':'')+directorPlanHtml(r.content_plan,r.sources||[])+(r.quality?'<details class="director-details" data-director-detail="quality"><summary>成片检查记录</summary><p>'+esc(r.quality.passed===true?'已通过本次自动检查，建议播放确认表达与审美。':'关键检查尚未全部通过。')+'</p><ul>'+checks.map(c=>'<li>'+esc(c.label||c.name||c.id)+'：'+esc(c.detail||c.message||(c.passed===true?'通过':c.passed===false?'未通过':'待核查'))+'</li>').join('')+'</ul>'+((r.quality.issues||[]).length?'<p>'+esc(directorText(r.quality.issues))+'</p>':'')+'</details>':'')+'<details class="director-details" data-director-detail="events"><summary>制作记录</summary><ol class="director-logs">'+events.map(e=>'<li><time>'+esc(date(e.created_at))+'</time>'+esc(e.message||e.stage_label||e.stage)+'</li>').join('')+'</ol></details>';
    for(const detail of box.querySelectorAll?.('details')||[])if(opened.has(detail.dataset.directorDetail))detail.open=true;
  }
  $('#directorRevisionPanel').hidden=!directorCanRevise(r);
  const deliverable=directorDeliverable(r);$('#directorMedia').hidden=!deliverable;
  if(deliverable){const signature=r.id+':'+JSON.stringify(r.artifacts);if(director.mediaKey!==signature){director.mediaKey=signature;loadDirectorVideo(r);$('#directorDownloads').innerHTML=r.artifacts.filter(a=>directorArtifactPath(r,a)).map(a=>'<button type="button" class="button '+(a.kind==='video'?'primary':'subtle')+'" data-director-download="'+esc(a.kind)+'">'+esc(({video:'下载成片',subtitles:'下载字幕',cover:'下载封面',copy:'标题与发布文案',record:'制作记录',package:'完整交付包'}[a.kind]||a.name))+' ↓</button>').join('');}}else if(director.mediaKey){clearDirectorMedia();}
}
function clearDirectorMedia(){director.mediaRequest++;director.mediaKey=null;const video=$('#directorVideo');video.pause();video.removeAttribute('src');video.load?.();if(director.mediaBlob)URL.revokeObjectURL(director.mediaBlob);director.mediaBlob=null;}
async function loadDirectorVideo(record){
  const artifact=record.artifacts.find(a=>a.kind==='video'),path=directorArtifactPath(record,artifact);if(!path)return;const seq=++director.mediaRequest,ctx=connectionContext(),video=$('#directorVideo'),status=$('#directorVideoStatus');video.pause();video.removeAttribute('src');video.load?.();if(director.mediaBlob)URL.revokeObjectURL(director.mediaBlob);director.mediaBlob=null;status.textContent='正在读取成片…';
  try{let url;if(canStreamMedia())url=new URL(path,location.origin).href;else {const blob=await api(path,{},'blob',ctx);if(seq!==director.mediaRequest||!currentContext(ctx))return;url=URL.createObjectURL(blob);director.mediaBlob=url;}if(seq!==director.mediaRequest)return;setMediaSource(video,status,url,()=>seq===director.mediaRequest&&currentContext(ctx),'成片已读取，请播放检查画面与声音。');}catch(e){if(seq===director.mediaRequest){status.textContent=e.message;status.className='media-status error';}}
}
async function downloadDirector(kind){const record=director.records.find(r=>r.id===director.selected),artifact=record?.artifacts?.find(a=>a.kind===kind);const path=directorArtifactPath(record,artifact);if(!path||!directorDeliverable(record))return;const context=connectionContext();directorMessage('正在准备下载…');try{const blob=await api(path,{},'blob',context);if(!currentContext(context))return;const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=String(artifact.name||kind).replace(/[<>:"/\\|?*\u0000-\u001f]/g,'_');a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);directorMessage('下载已开始，原作品仍保存在工作台。');}catch(e){if(!e.stale)directorMessage(e.message,true);}}
$('#directorManual').onclick=()=>setDirectorActive(false);$('#directorBack').onclick=()=>setDirectorActive(true);$('#directorStart').onclick=()=>submitDirector();$('#directorRevise').onclick=()=>submitDirector('revise');$('#directorCheck').onclick=checkDirector;$('#directorRefresh').onclick=()=>refreshDirector(true);$('#directorBrand').onclick=()=>$('#openBrandContext').click();
for(const id of directorFields)$('#'+id).addEventListener(directorChecks.has(id)||['directorMode','directorAspect','directorDuration','directorBrandUsage'].includes(id)?'change':'input',()=>{saveDirector();if(id==='directorMode'){director.preflight=null;renderDirectorPreflight();}directorSync();});
$('#directorRevision').addEventListener('input',directorSync);
document.addEventListener('click',event=>{let b;if((b=event.target.closest('[data-director-goal]'))){$('#directorGoal').value=b.dataset.directorGoal;saveDirector();directorSync();}else if((b=event.target.closest('[data-director-task]'))){director.selected=b.dataset.directorTask;director.detailSignature='';saveDirector();renderDirector();refreshDirector(true);}else if((b=event.target.closest('[data-director-download]')))downloadDirector(b.dataset.directorDownload);else if((b=event.target.closest('[data-director-retry]')))submitDirector('retry');});
loadDirector();directorSync();
