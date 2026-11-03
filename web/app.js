'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const localPage = ['127.0.0.1','localhost','[::1]'].includes(location.hostname);
const state = {base:localPage ? location.origin : '', token:'', connected:false, jobs:[], technicalTests:[],technicalTestCount:0,technicalLoading:null,technicalLoaded:false,assets:[], config:{}, filter:'all', selected:null, videoBlob:null,videoSource:null,mediaStreaming:false,epoch:0,phase:'idle',lastCheckAt:null,lastSuccessAt:null,lastConnectionError:'',refreshing:null,requests:new Set(),localGateway:false,reauthNeeded:false,reauthAttempted:false,reviewRequest:0,actionBusy:false,toastKind:null};
const statusNames = {queued:'排队中',analyzing:'分析素材',rendering:'渲染检查',review:'等待审核',approved:'已审核',publishing:'正在推送',published:'已推送',failed:'需要处理'};
const presets = {gentle:{silence_min:0.9,padding:0.25,min_clip:0.5,remove_silence:true,normalize_audio:true},course:{silence_min:1.5,padding:0.4,min_clip:0.8,remove_silence:true,normalize_audio:true},compact:{silence_min:0.7,padding:0.2,min_clip:0.5,remove_silence:true,normalize_audio:true},preserve:{remove_silence:false,normalize_audio:true,target_aspect:'original'}};
const sourcePreview={source:null,blob:null,url:null,request:0};
const mediaListeners=new WeakMap();
function canStreamMedia(){return localPage&&state.localGateway&&state.mediaStreaming&&state.base===location.origin;}
function mediaAddress(path){const url=new URL(path,state.base);if(url.origin!==location.origin||!/^\/api\/(?:assets\/video|jobs\/[^/]+\/video)$/.test(url.pathname))throw new Error('视频地址不可用。');return url.href;}
function clearMediaElement(video){mediaListeners.get(video)?.();mediaListeners.delete(video);video.pause();video.removeAttribute('src');video.load?.();}
function setMediaSource(video,status,url,valid,loadedMessage,assignSource=true){
  mediaListeners.get(video)?.();
  let checkedFailure=false;
  const active=()=>valid()&&video.src===url&&(!video.currentSrc||video.currentSrc===url);
  const loaded=()=>{if(active()){status.textContent=loadedMessage;status.className='media-status';}};
  const failed=()=>{if(active()){status.textContent='视频暂时无法播放，请检查连接后重新打开，或确认原片格式是否受浏览器支持。';status.className='media-status error';if(!checkedFailure&&canStreamMedia()&&state.connected){checkedFailure=true;const address=new URL(url);api(address.pathname+address.search,{headers:{Range:'bytes=0-0'}},'headers').catch(error=>{if(active())reportError(error);});}}};
  video.addEventListener('loadedmetadata',loaded);video.addEventListener('canplay',loaded);video.addEventListener('error',failed);
  mediaListeners.set(video,()=>{video.removeEventListener?.('loadedmetadata',loaded);video.removeEventListener?.('canplay',loaded);video.removeEventListener?.('error',failed);});if(assignSource)video.src=url;
}
function assetName(asset){return asset?.display_name||asset?.name||asset?.source||'视频素材';}
function suggestedPreset(source){return state.assets.find(a=>a.source===source)?.suggested_preset==='preserve'?'preserve':'gentle';}
function updatePresetHelp(){const preserve=$('#editPreset').value==='preserve';$('#aspect').disabled=preserve;if(preserve)$('#aspect').value='original';$('#presetHelp').textContent=preserve?'完整保留所有画面与原有节奏，不裁剪停顿。保持原比例，仍会规范编码和整理音量；适合品牌包装、音乐片和已有成片。':'按音量检测长停顿，不会理解语义或自动删除口误。保留人物画面与原始语速；成片进入审核区。';}
function safeVideoFilename(title){let name=String(title||'成片').replace(/[<>:"/\\|?*\u0000-\u001f\u007f]/g,'_').replace(/\.mp4$/i,'').trim().replace(/[. ]+$/g,'').slice(0,120);if(!name)name='成片';if(/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name))name='成片-'+name;return name+'.mp4';}
function downloadReviewedVideo(){if(!state.selected||isTechnicalTest(state.selected)||!state.videoSource)return;const a=document.createElement('a');a.href=canStreamMedia()?mediaAddress(state.selected.video_url):state.videoSource;a.download=safeVideoFilename(state.selected.title);a.click();toast('已开始下载成片；此操作不会审核或发布视频。');}
let toastTimer;
function toast(message,error=false,kind=null){state.toastKind=kind||(error&&message===state.lastConnectionError?'connection':'business');$('#toast').textContent=message;$('#toast').className='toast visible'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('#toast').className='toast';state.toastKind=null;},6500);}
function reportError(error){if(error.stale||(!error.uncertain&&error.connectionEpoch!==undefined&&error.connectionEpoch!==state.epoch))return;toast(error.message,true,error.uncertain?'operation-uncertain':error.connectivity?'connection':'business');}
function showPage(name){document.body?.classList.toggle('creating',name==='create');if(name==='create'&&typeof renderCreatorMaterials==='function')renderCreatorMaterials();document.querySelectorAll('.page').forEach(el=>el.classList.toggle('active',el.id==='page-'+name));document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.page===name));$('#pageTitle').textContent=({create:'开始制作',overview:'运行状态',assets:'素材库',jobs:'剪辑与审核',settings:'连接与设置',collections:'关键词采集',knowledge:'作品知识库'})[name] || '制作总览';if(name==='knowledge'&&typeof refreshKnowledge==='function')refreshKnowledge();if(name==='collections'&&typeof refreshCollections==='function')refreshCollections(true);if(name==='assets'&&typeof refreshLocalLibrary==='function')refreshLocalLibrary(true);}
function bytes(n){return n>=1073741824?(n/1073741824).toFixed(2)+' GB':(n/1048576).toFixed(1)+' MB';}
function time(s){return Number.isFinite(Number(s))?Number(s).toFixed(1)+' 秒':'—';}
function date(s){return s?new Date(typeof s==='number'?s*1000:s).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'';}
function connectionContext(){return {base:state.base,token:state.token,epoch:state.epoch};}
function currentContext(context){return context.epoch===state.epoch&&context.base===state.base&&context.token===state.token;}
function staleRequest(){return Object.assign(new Error('连接状态已变化，请以最新检查结果为准。'),{stale:true});}
function uncertainRequest(){return Object.assign(new Error('操作结果需核实，请查看任务记录；不会自动重复提交。'),{uncertain:true});}
function nextEpoch(){state.epoch++;for(const request of state.requests)if(request.readOnly)request.controller.abort();}
function isTechnicalTest(job){return job?.job_kind==='technical_test';}
function updateReviewActions(){if(typeof renderWorkActions==='function')renderWorkActions(state.selected);if(typeof renderProductionProgress==='function')renderProductionProgress();if(typeof updateCompatibilityControls==='function')updateCompatibilityControls();const j=state.selected,technical=isTechnicalTest(j);$('#approveButton').disabled=technical||!state.connected||state.actionBusy||j?.status!=='review';$('#publishButton').disabled=technical||!state.connected||state.actionBusy||j?.status!=='approved'||!state.config.delivery?.enabled||!state.config.delivery?.configured;$('#reviewActions').hidden=technical;$('#reviewActions').style.display=technical?'none':'';$('#downloadVideoButton').hidden=technical;$('#downloadVideoButton').disabled=technical||!j?.has_video||!state.videoSource;$('#retryJobButton').hidden=technical||j?.can_retry!==true;$('#retryJobButton').disabled=!state.connected||state.actionBusy||j?.can_retry!==true;$('#technicalTestNotice').hidden=!technical;$('#reviewEyebrow').textContent=technical?'TECHNICAL TEST / 非正式成片':'FINAL REVIEW';$('#diagnosticReason').textContent=technical?(j.diagnostic_reason||'用于检查同步、GPU 编码和文件交付是否可用，不代表真实素材的剪辑效果。'):'';$('#reviewHelp').textContent=technical?'该测试记录仅供排查技术链路，不能作为成片下载、审核或推送到企业微信群。':'下载只保存视频，不会审核或发布。推送会发送到已配置的企业微信群；结果不确定时先核实，避免重复发送。';$('#sourceStartButton').disabled=!state.connected||!sourcePreview.source;}
function runtimeSettings(){
  if(localPage){let note=$('#desktopConnectionStatus');if(!note){note=document.createElement('div');note.id='desktopConnectionStatus';note.className='note';$('#connectionForm').before(note);}const runtime=state.connected?'GPU 已连接':state.phase==='checking'?'GPU 连接中':state.phase==='error'?'GPU 连接异常':'GPU 待连接';note.innerHTML='<strong>本地页面已运行</strong><p>连接方式：'+(state.localGateway?'本地自动连接':'手动配置')+'<br>'+runtime+'<br>最近检查：'+esc(state.lastCheckAt?new Date(state.lastCheckAt).toLocaleTimeString('zh-CN',{hour12:false}):'尚未检查')+'<br>上次验证成功：'+esc(state.lastSuccessAt?new Date(state.lastSuccessAt).toLocaleTimeString('zh-CN',{hour12:false}):'尚未验证')+'</p>';}
  let stale=$('#dataFreshness');if(!stale){stale=document.createElement('p');stale.id='dataFreshness';stale.className='help';$('#connectionBanner').after(stale);}stale.hidden=state.connected||!(state.jobs.length||state.technicalTests.length||state.assets.length);stale.textContent='当前显示上次读取的数据 · 待更新；连接恢复前暂停审核和推送。';
  updateReviewActions();
  if(typeof updateLocalImportAccess==='function')updateLocalImportAccess();if(typeof updateCreatorAccess==='function')updateCreatorAccess();
}
function setConnection(ok,message,phase='error'){
  state.connected=ok;state.phase=ok?'connected':phase;const gpu=state.localGateway||state.executionTarget==='remote_gpu';const name=gpu?'GPU':'工作台';
  $('#connectionPill').className='pill '+(ok?'online':'neutral');$('#connectionPill').innerHTML='<i></i>'+name+(ok?'已连接':phase==='checking'?'连接中':phase==='error'?'连接异常':'待连接');$('#connectionBanner').className='connection-banner'+(ok?' connected':'');$('#bannerTitle').textContent=ok?name+'已连接':phase==='checking'?'正在检查'+name+'连接':phase==='error'?name+'连接异常':'连接你的剪辑工作台';$('#bannerText').textContent=message||(ok?'素材目录、制作任务和审核状态已同步。':'请检查服务连接；原有素材和任务信息仍会保留。');$('#connectButton').textContent=ok?'连接设置':'连接设置 ↗';runtimeSettings();
  if(!ok)$('#serviceState').textContent=phase==='checking'?'正在检查服务状态':phase==='error'?'连接异常 · 服务状态待更新':'尚未连接';
}
function connectionFailure(context,error,status=0){
  if(!currentContext(context))return;nextEpoch();state.lastCheckAt=Date.now();state.lastConnectionError=error.message;
  error.connectionEpoch=state.epoch;
  if(status===401&&state.localGateway&&!state.reauthAttempted)state.reauthNeeded=true;
  setConnection(false,error.message,'error');
}
async function api(path,options={},format='json',context=connectionContext()){
  if(!currentContext(context))throw staleRequest();
  if(!context.base||!context.token)throw new Error('请先连接剪辑服务并填写工作台访问令牌。');
  const method=(options.method||'GET').toUpperCase();
  const readOnly=['GET','HEAD'].includes(method),outdated=()=>readOnly?staleRequest():uncertainRequest();
  if(!readOnly&&!state.connected)throw Object.assign(new Error('连接尚未恢复，请先检查任务状态后再操作。'),{connectivity:true});
    const controller=new AbortController(),request={controller,readOnly};state.requests.add(request);const timeout=setTimeout(()=>controller.abort(),path.endsWith('/publish')?300000:format==='blob'?120000:path.startsWith('/api/presenters/import?')?60000:20000);
  try{
    const response=await fetch(context.base+path,{...options,headers:{Authorization:'Bearer '+context.token,...(options.body?{'Content-Type':'application/json'}:{}),...options.headers},cache:'no-store',signal:controller.signal});
    if(!currentContext(context))throw outdated();
    if(!response.ok){let body;try{body=await response.json();}catch{}if(!currentContext(context))throw outdated();const error=Object.assign(new Error(typeof body?.detail==='string'?body.detail:'服务请求失败（'+response.status+'）'),{httpStatus:response.status,connectivity:response.status===401||response.status>=500,uncertain:!readOnly&&response.status>=500});if(error.connectivity)connectionFailure(context,error,response.status);throw error;}
    const value=format==='headers'?(await response.body?.cancel(),true):format==='blob'?await response.blob():await response.json();if(!currentContext(context))throw outdated();return value;
  }catch(error){
    if(error.stale||error.httpStatus||error.uncertain)throw error;
    if(!currentContext(context))throw outdated();
    const failure=Object.assign(new Error(!readOnly?'操作结果需核实，请查看任务记录；不会自动重复提交。':error.name==='AbortError'?'服务检查超时，正在等待连接恢复。':'无法完成服务请求，请等待连接检查。'),{connectivity:true,uncertain:!readOnly});connectionFailure(context,failure);throw failure;
  }finally{clearTimeout(timeout);state.requests.delete(request);}
}
async function readLocalSession(context){
  const response=await fetch('/local-session',{headers:{'X-CutFlow-Local':'1'},cache:'no-store',signal:AbortSignal.timeout(4000)});
  if(!currentContext(context))throw staleRequest();
  if(!response.ok)throw Object.assign(new Error('本机连接配置暂不可用，请等待下一次检查。'),{connectivity:true});
  const session=await response.json();if(!currentContext(context))throw staleRequest();
  if(session.local_gateway!==true||typeof session.token!=='string'||session.token.length<24)throw Object.assign(new Error('本机连接配置不完整，请检查启动程序。'),{connectivity:true});
  nextEpoch();state.base=location.origin;state.token=session.token;state.localGateway=true;state.mediaStreaming=session.media_streaming===true;$('#connectionForm').hidden=true;runtimeSettings();
}
function refresh(silent=false){
  if(!state.base||!state.token){if(!silent)showPage('settings');return Promise.resolve();}
  if(state.refreshing&&currentContext(state.refreshing.context))return state.refreshing.promise;
  const flight={context:connectionContext(),promise:null};state.refreshing=flight;
  flight.promise=(async()=>{
    let context=flight.context;
    const reviewRevision=state.reviewRequest;
    try{
      if(!state.connected)setConnection(false,'正在检查服务连接，已有数据暂不更新。','checking');
      if(state.reauthNeeded){state.reauthNeeded=false;state.reauthAttempted=true;await readLocalSession(context);context=connectionContext();flight.context=context;}
      const [status,assets,jobs,config]=await Promise.all([api('/api/status',{},'json',context),api('/api/assets',{},'json',context),api('/api/jobs',{},'json',context),api('/api/config',{},'json',context)]);
      if(!currentContext(context))return;
      if(typeof status?.worker_running!=='boolean'||typeof status?.execution_target!=='string'||!Array.isArray(assets?.assets)||!Array.isArray(jobs?.jobs)||typeof config?.inbox!=='string'||!assets.assets.every(a=>a&&typeof a.source==='string')||!jobs.jobs.every(j=>j&&typeof j.id==='string'&&typeof j.status==='string'))throw Object.assign(new Error('服务返回的数据不完整，等待重新检查。'),{connectivity:true});
      state.assets=assets.assets;state.jobs=jobs.jobs.filter(j=>!isTechnicalTest(j));state.technicalTestCount=Number.isInteger(status.technical_test_count)&&status.technical_test_count>=0?status.technical_test_count:0;state.config=config;state.executionTarget=status.execution_target;state.lastCheckAt=Date.now();state.lastSuccessAt=state.lastCheckAt;state.reauthAttempted=false;
      const latest=state.selected&&state.jobs.find(j=>j.id===state.selected.id);
      const refreshOpenReview=latest&&$('#reviewDialog').open&&reviewRevision===state.reviewRequest&&(
        latest.status!==state.selected.status||latest.has_video!==state.selected.has_video||
        latest.production?.stage!==state.selected.production?.stage||latest.updated_at!==state.selected.updated_at||
        latest.has_video&&!state.videoSource);
      if(latest)state.selected={...state.selected,...latest};
      else if(state.selected&&!isTechnicalTest(state.selected)){state.reviewRequest++;state.selected=null;$('#reviewDialog').close();updateReviewActions();toast('任务列表已更新，请重新打开记录查看。');}
      setConnection(true,config.auto_publish?'自动发布已启用；质量检查通过的任务将按配置推送。':'素材已读取。新建剪辑后，成片会进入审核区，原始素材保持完整。');
      if(state.toastKind==='connection')toast('GPU 连接已恢复。请先查看任务最新状态，再确认是否继续刚才的操作。',false,'recovery');
      const counts=status.counts||{};
      $('#statAssets').textContent=state.assets.length;$('#statReview').textContent=counts.review||0;$('#statPublished').textContent=counts.published||0;$('#statActive').textContent=['queued','analyzing','rendering','publishing'].reduce((sum,k)=>sum+(counts[k]||0),0);
      $('#assetBadge').textContent=state.assets.length;$('#jobBadge').textContent=state.jobs.length;
      $('#serviceState').textContent=status.worker_running?(status.execution_target==='remote_gpu'?'远端 GPU 服务运行中':'本地服务运行中'):'工作线程未运行';$('#gpuState').textContent=status.gpu?.available?(status.gpu.name+' · '+Math.round(status.gpu.memory_mb/1024)+' GB'):'当前服务未检测到 GPU';
      $('#wecomState').textContent=status.delivery?.configured?(status.delivery.enabled?'已配置并启用':'已配置 · 尚未启用'):'待配置机器人';
      $('#remoteInboxPath').textContent=config.inbox;if(!state.localGateway)$('#inboxPath').textContent='当前连接远程素材库。请在这台电脑打开本地工作台，即可直接选择视频导入。';renderAssets();renderJobs();if(state.filter==='technical_test')refreshTechnicalTests(true);if(typeof refreshCollections==='function'&&$('#page-collections').classList.contains('active'))refreshCollections(true);
      if(refreshOpenReview&&$('#reviewDialog').open&&state.selected?.id===latest.id)await review(latest.id);
    }catch(error){if(currentContext(context))connectionFailure(context,error);if(!silent)reportError(error);}
  })().finally(()=>{if(state.refreshing===flight)state.refreshing=null;});
  return flight.promise;
}
function jobRows(jobs){return jobs.map(j=>{const technical=isTechnicalTest(j),title=technical?'GPU 链路自测（彩条测试片）':j.title,label=technical?(j.result?.qc?.passed===true?'自测通过':'技术自测记录'):(statusNames[j.status]||j.status);return `<div class="job-row"><span class="job-icon">${technical?'⚙':'▷'}</span><div class="job-info"><b>${esc(title)}</b><small>${technical?'技术自测 · 非正式成片 · ':''}${esc(date(j.created_at))}${technical?'':' · '+esc(j.source?.startsWith('portrait:')?'人物口播 · 自选形象':j.source)}</small></div><span class="status ${technical?'technical_test':esc(j.status)}">${esc(label)}</span><button class="button subtle" data-review="${esc(j.id)}">${technical?'查看自测记录':j.has_video?'查看':'详情'}</button></div>`;}).join('');}
function renderJobs(){if(typeof renderProductionProgress==='function')renderProductionProgress();if(typeof renderCreatorResult==='function')renderCreatorResult();
  const technical=state.filter==='technical_test',production=state.jobs.filter(j=>!isTechnicalTest(j));
  const filtered=technical?state.technicalTests:production.filter(j=>state.filter==='all'||j.status===state.filter);
  const intro=technical?'<div class="note"><strong>技术自测（非成片）</strong><p>以下是保留的链路检查记录，不计入正式任务或成片数量，不提供审核与推送操作。</p></div>':'';
  const empty=technical?(state.technicalLoading?'正在读取技术自测记录。':state.technicalLoaded?'暂无技术自测记录。':'连接工作台后读取技术自测记录。'):(production.length?'此分类暂无正式任务。':'还没有正式成片。');
  $('#jobsList').innerHTML=intro+(filtered.length?jobRows(filtered):'<div class="empty"><span class="empty-icon">'+(technical?'⚙':'▷')+'</span><strong>'+empty+'</strong>'+(technical?'':'<p>放入口播/课程/访谈原视频后开始制作。</p>')+'</div>');
  $('#technicalTestCount').textContent=state.technicalTestCount;
  $('#recentJobs').className=production.length?'':'empty compact';
  $('#recentJobs').innerHTML=production.length?jobRows(production.slice(0,3)):'<span class="empty-icon">▷</span><strong>还没有正式成片。</strong><p>放入口播/课程/访谈原视频后开始制作。</p>';
}
function refreshTechnicalTests(silent=false){
  if(!state.connected){renderJobs();if(!silent)toast('连接工作台后可查看技术自测记录。');return Promise.resolve();}
  if(state.technicalLoading&&currentContext(state.technicalLoading.context))return state.technicalLoading.promise;
  const flight={context:connectionContext(),promise:null};state.technicalLoading=flight;renderJobs();
  flight.promise=(async()=>{try{const data=await api('/api/jobs?kind=technical_test',{},'json',flight.context);if(!currentContext(flight.context))return;if(!Array.isArray(data.jobs))throw new Error('技术自测记录格式不完整。');state.technicalTests=data.jobs.filter(isTechnicalTest);state.technicalLoaded=true;const latest=state.selected&&state.technicalTests.find(j=>j.id===state.selected.id);if(latest)state.selected={...state.selected,...latest};updateReviewActions();}catch(error){if(!silent)reportError(error);}})().finally(()=>{if(state.technicalLoading===flight){state.technicalLoading=null;renderJobs();}});
  return flight.promise;
}
function selectJobFilter(filter){state.filter=filter;document.querySelectorAll('[data-filter]').forEach(button=>button.classList.toggle('active',button.dataset.filter===filter));renderJobs();return filter==='technical_test'?refreshTechnicalTests():Promise.resolve();}
function renderAssets(){if(typeof renderCreatorMaterials==='function')renderCreatorMaterials();
  const selectedSource=$('#sourceSelect').value;
  $('#sourceSelect').innerHTML='<option value="">选择一段素材</option>'+state.assets.map(a=>`<option value="${esc(a.source)}">${esc(assetName(a))}</option>`).join('');
  if(state.assets.some(a=>a.source===selectedSource))$('#sourceSelect').value=selectedSource;
  $('#assetsList').innerHTML=state.assets.length?state.assets.map(a=>`<article class="panel asset-card"><button class="asset-poster" data-preview="${esc(a.source)}" aria-label="预览原片：${esc(assetName(a))}"><span>▷</span><small>预览原片</small></button><div class="asset-body"><h3>${esc(assetName(a))}</h3><p>${a.suggested_preset==='preserve'?'完整保留 · 适合包装与已有成片':'可用于制作 · 在开始制作中组合使用'}</p><div class="asset-footer"><span>${bytes(a.size)} · ${esc(a.extension?.slice(1).toUpperCase())}</span><button class="button subtle" data-add-to-creator="${esc(a.source)}">选择这段素材 ＋</button></div></div></article>`).join(''):'<div class="empty panel"><span class="empty-icon">▱</span><strong>还没有已同步的视频</strong><p>先在上方选择本机视频。导入后会显示同步进度；同步完成后可在这里预览。</p></div>';
}
function newJob(source=''){
  if(!state.connected){showPage('settings');toast('先连接工作台，再创建剪辑任务。');return;}
  if(!state.assets.length){showPage('assets');toast('素材目录还是空的，先放入一段视频。');return;}
  $('#sourceSelect').value=source;$('#jobTitle').value=source?assetName(state.assets.find(a=>a.source===source)).replace(/\.[^.]+$/,'')+' · 初剪':'';$('#editPreset').value=suggestedPreset(source);updatePresetHelp();$('#jobDialog').showModal();
}
async function previewSource(source){
  if(!state.connected){toast('GPU 连接恢复后可以预览已同步的原片。',true);return;}
  const context=connectionContext(),request=++sourcePreview.request;sourcePreview.source=source;
  clearMediaElement($('#sourceVideo'));if(sourcePreview.blob)URL.revokeObjectURL(sourcePreview.blob);sourcePreview.blob=null;sourcePreview.url=null;
  $('#sourceTitle').textContent=assetName(state.assets.find(a=>a.source===source));$('#sourceVideoStatus').textContent='正在读取原片，较大的视频需要一些时间…';$('#sourceVideoStatus').className='media-status';$('#sourceDetails').textContent='原片预览，不会修改素材。导入的素材会自动处理；如需调整参数，可另建剪辑版本。';updateReviewActions();if(!$('#sourceDialog').open)$('#sourceDialog').showModal();
  try{const path='/api/assets/video?source='+encodeURIComponent(source);let url;if(canStreamMedia())url=mediaAddress(path);else{const blob=await api(path,{},'blob',context);if(request!==sourcePreview.request||!currentContext(context)||!$('#sourceDialog').open)return;sourcePreview.blob=URL.createObjectURL(blob);url=sourcePreview.blob;}sourcePreview.url=url;setMediaSource($('#sourceVideo'),$('#sourceVideoStatus'),url,()=>request===sourcePreview.request&&sourcePreview.source===source&&$('#sourceDialog').open,'原片已加载，点击播放查看。');if(!canStreamMedia())$('#sourceVideoStatus').textContent='原片已加载，点击播放查看。';}
  catch(error){if(request===sourcePreview.request&&!error.stale){$('#sourceVideoStatus').textContent='原片加载失败。'+error.message;$('#sourceVideoStatus').className='media-status error';reportError(error);}}
}
async function review(id){
  const context=connectionContext(),request=++state.reviewRequest;
  try{const data=await api('/api/jobs/'+id,{},'json',context);if(request!==state.reviewRequest||!currentContext(context))return;const j=data.job,technical=isTechnicalTest(j),reuseVideo=state.selected?.id===j.id&&state.selected?.video_url===j.video_url&&j.has_video&&state.videoSource;state.selected=j;if(typeof renderCreationReview==='function')renderCreationReview(j);$('#reviewTitle').textContent=technical?'GPU 链路自测（彩条测试片）':j.title;
    const warnings=[...(j.plan?.warnings||[]),...(j.result?.qc?.warnings||[])];
    $('#reviewDetails').innerHTML=`<p><span class="status ${technical?'technical_test':esc(j.status)}">${technical?(j.result?.qc?.passed===true?'自测通过':'技术自测记录'):esc(statusNames[j.status]||j.status)}</span>　${technical?'测试输出':'预期成片'} ${time(j.plan?.duration)} · 保留 ${j.plan?.clips?.length||'—'} 段</p>${j.error?'<div class="note">'+esc(j.error)+'</div>':''}${j.status==='failed'&&j.retry_block_reason?'<p class="help">'+esc(j.retry_block_reason)+'</p>':''}<details><summary>${technical?'查看技术检查记录':'剪辑记录与检查报告'}</summary><pre>${esc(JSON.stringify({...(technical?{原记录名称:j.title,源文件:j.source,自测原因:j.diagnostic_reason}:{}),检查:j.result?.qc||'尚未完成',提示:warnings,事件:j.events||[]},null,2))}</pre></details>`;
    if(!reuseVideo){if(typeof resetCompatibilityPreview==='function')resetCompatibilityPreview();clearMediaElement($('#reviewVideo'));if(state.videoBlob){URL.revokeObjectURL(state.videoBlob);state.videoBlob=null;}state.videoSource=null;}
    $('#reviewVideo').hidden=!j.has_video;$('#reviewVideoStatus').className='media-status';$('#reviewVideoStatus').textContent=!j.has_video?'任务还没有可预览的视频。':reuseVideo?'视频已加载，播放位置保持不变。':'正在读取视频，较大的文件需要一些时间…';
    updateReviewActions();if(!$('#reviewDialog').open)$('#reviewDialog').showModal();
    if(reuseVideo)setMediaSource($('#reviewVideo'),$('#reviewVideoStatus'),state.videoSource,()=>request===state.reviewRequest&&state.selected?.id===id&&$('#reviewDialog').open,technical?'测试视频已加载，仅供技术检查。':'成片已加载，播放位置保持不变。',false);
    if(j.has_video&&!reuseVideo){
      let url;
      if(canStreamMedia())url=mediaAddress(j.video_url);
      else{const blob=await api(j.video_url,{},'blob',context);if(request!==state.reviewRequest||!currentContext(context)||state.selected?.id!==id||!$('#reviewDialog').open)return;state.videoBlob=URL.createObjectURL(blob);url=state.videoBlob;}
      state.videoSource=url;setMediaSource($('#reviewVideo'),$('#reviewVideoStatus'),url,()=>request===state.reviewRequest&&state.selected?.id===id&&$('#reviewDialog').open,technical?'测试视频已加载，仅供技术检查。':'视频文件已读取，请播放检查；如画面空白或无声，可切换兼容播放。');if(!canStreamMedia())$('#reviewVideoStatus').textContent=technical?'测试视频已加载，仅供技术检查。':'视频文件已读取，请播放检查；如画面空白或无声，可切换兼容播放。';updateReviewActions();
    }
  }catch(e){if(request===state.reviewRequest){if(!e.stale&&$('#reviewDialog').open){$('#reviewVideoStatus').textContent='视频读取失败。'+e.message;$('#reviewVideoStatus').className='media-status error';}reportError(e);}}
}
document.querySelectorAll('[data-page]').forEach(el=>el.addEventListener('click',()=>showPage(el.dataset.page)));
document.querySelectorAll('[data-goto]').forEach(el=>el.addEventListener('click',()=>showPage(el.dataset.goto)));
document.querySelectorAll('[data-close]').forEach(el=>el.addEventListener('click',()=>$('#'+el.dataset.close).close()));
$('#reviewDialog').addEventListener('close',()=>{state.reviewRequest++;clearMediaElement($('#reviewVideo'));state.selected=null;if(state.videoBlob)URL.revokeObjectURL(state.videoBlob);state.videoBlob=null;state.videoSource=null;updateReviewActions();});
$('#sourceDialog').addEventListener('close',()=>{sourcePreview.request++;clearMediaElement($('#sourceVideo'));if(sourcePreview.blob)URL.revokeObjectURL(sourcePreview.blob);sourcePreview.blob=null;sourcePreview.url=null;sourcePreview.source=null;updateReviewActions();});
$('#sourceStartButton').onclick=()=>{const source=sourcePreview.source;if(!source||!state.connected)return;$('#sourceDialog').close();if(typeof addSourceToCreator==='function')addSourceToCreator(source);else newJob(source);};
$('#downloadVideoButton').onclick=downloadReviewedVideo;
$('#connectButton').onclick=()=>showPage('settings');$('#refreshButton').onclick=()=>refresh();$('#scanButton').onclick=()=>refresh();$('#newJobButton').onclick=()=>typeof openCreator==='function'?openCreator():newJob();$('#newJobButton2').onclick=()=>typeof openCreator==='function'?openCreator():newJob();
document.addEventListener('click',event=>{const source=event.target.closest('[data-source]');if(source)newJob(source.dataset.source);const preview=event.target.closest('[data-preview]');if(preview)previewSource(preview.dataset.preview);const item=event.target.closest('[data-review]');if(item)review(item.dataset.review);});
document.querySelectorAll('[data-filter]').forEach(el=>el.addEventListener('click',()=>selectJobFilter(el.dataset.filter)));
$('#sourceSelect').onchange=()=>{const source=$('#sourceSelect').value;$('#jobTitle').value=source?assetName(state.assets.find(a=>a.source===source)).replace(/\.[^.]+$/,'')+' · 初剪':'';$('#editPreset').value=suggestedPreset(source);updatePresetHelp();};
$('#editPreset').onchange=updatePresetHelp;
$('#connectionForm').onsubmit=async event=>{event.preventDefault();try{let value=$('#apiBase').value.trim() || (localPage?location.origin:'');const u=new URL(value);if(u.username||u.password||u.search||u.hash||!['https:','http:'].includes(u.protocol))throw new Error('请使用不含账号、密钥或查询参数的服务地址。');if(u.protocol==='http:'&&!['localhost','127.0.0.1','[::1]'].includes(u.hostname))throw new Error('远程服务必须使用 HTTPS。');nextEpoch();state.base=u.origin;state.token=$('#apiToken').value.trim();state.reauthNeeded=false;state.reauthAttempted=false;setConnection(false,'正在检查服务连接。','checking');await refresh();if(state.connected){$('#apiToken').value='';showPage('overview');toast('工作台已连接。');}}catch(e){reportError(e);}};
$('#disconnectButton').onclick=()=>{nextEpoch();$('#sourceDialog').close();state.token='';state.mediaStreaming=false;state.reauthNeeded=false;state.reauthAttempted=false;$('#apiToken').value='';state.selected=null;$('#reviewDialog').close();$('#reviewVideo').pause();$('#reviewVideo').removeAttribute('src');if(state.videoBlob)URL.revokeObjectURL(state.videoBlob);state.videoBlob=null;state.videoSource=null;state.jobs=[];state.technicalTests=[];state.technicalTestCount=0;state.technicalLoaded=false;state.assets=[];state.config={};if(typeof resetCollections==='function')resetCollections();setConnection(false,'已手动断开连接。','idle');renderAssets();renderJobs();['statAssets','statReview','statPublished','statActive','assetBadge','jobBadge'].forEach(id=>$('#'+id).textContent='—');$('#serviceState').textContent='尚未连接';$('#wecomState').textContent='待配置机器人';$('#inboxPath').textContent='连接服务后读取实际路径';toast('已断开连接，访问令牌已从当前页面清除。');};
$('#jobForm').onsubmit=async event=>{event.preventDefault();const button=$('#submitJob');button.disabled=true;try{const preserve=$('#editPreset').value==='preserve';await api('/api/jobs',{method:'POST',body:JSON.stringify({source:$('#sourceSelect').value,title:$('#jobTitle').value,settings:{...presets[$('#editPreset').value],target_aspect:preserve?'original':$('#aspect').value},idempotency_key:crypto.randomUUID()})});$('#jobDialog').close();await refresh();showPage('jobs');toast('任务已加入剪辑队列。');}catch(e){reportError(e);}finally{button.disabled=false;}};
$('#retryJobButton').onclick=async()=>{if(!state.selected||isTechnicalTest(state.selected)||state.selected.can_retry!==true||state.selected.status!=='failed'||!state.connected||state.actionBusy)return;const id=state.selected.id;state.actionBusy=true;updateReviewActions();try{await api('/api/jobs/'+id+'/retry',{method:'POST'});$('#reviewDialog').close();await refresh();toast('任务已重新加入处理队列，不会自动发布。');}catch(e){reportError(e);}finally{state.actionBusy=false;updateReviewActions();}};
$('#approveButton').onclick=async()=>{if(!state.selected||isTechnicalTest(state.selected)||!state.connected||state.actionBusy)return;const id=state.selected.id;state.actionBusy=true;updateReviewActions();try{await api('/api/jobs/'+id+'/approve',{method:'POST'});toast('审核已确认。');await refresh();if(state.connected)await review(id);}catch(e){reportError(e);}finally{state.actionBusy=false;updateReviewActions();}};
$('#publishButton').onclick=async()=>{if(!state.selected||isTechnicalTest(state.selected)||!state.connected||state.actionBusy)return;const id=state.selected.id;state.actionBusy=true;updateReviewActions();try{await api('/api/jobs/'+id+'/publish',{method:'POST'});toast('企业微信已确认接收成片。');await refresh();if(state.connected)await review(id);}catch(e){reportError(e);}finally{state.actionBusy=false;updateReviewActions();}};
$('#downloadPreset').onclick=()=>{const config={...presets.gentle,target_aspect:'original',encoder:'cpu',normalize_audio:true,remove_silence:true,crf:18,fps:30};const url=URL.createObjectURL(new Blob([JSON.stringify(config,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='cutflow-preset.json';a.click();URL.revokeObjectURL(url);toast('剪辑参数已下载，可用作观察器的参数文件。');};
const fragment=new URLSearchParams(location.hash.slice(1));if(localPage&&fragment.get('token')){state.token=fragment.get('token');history.replaceState(null,'',location.pathname);}
async function connectDesktop(){
  const context=connectionContext();
  if(localPage){
    try{
      await readLocalSession(context);
          const access=$('.local-access');access.innerHTML='<strong>下次如何打开？</strong><p>双击「打开本地工作台.cmd」，即可启动网站并连接 GPU。</p><a class="text-button" href="http://127.0.0.1:8767/">本地工作台：127.0.0.1:8767 ↗</a>';
          document.title='商剪工坊 · 本地工作台';
    }catch(error){if(error.stale)return;}
  }
  runtimeSettings();
  $('#apiBase').value=state.base;
  if(state.token)await refresh(true);
}
$('#apiBase').value=state.base;renderJobs();connectDesktop();
function probeIfVisible(){if(state.base&&state.token&&!document.hidden)return refresh(true);}
setInterval(probeIfVisible,8000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)probeIfVisible();});
