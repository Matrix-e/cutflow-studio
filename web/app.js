'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const localPage = ['127.0.0.1','localhost','[::1]'].includes(location.hostname);
const state = {base:localPage ? location.origin : '', token:'', connected:false, jobs:[], assets:[], config:{}, filter:'all', selected:null, videoBlob:null};
const statusNames = {queued:'排队中',analyzing:'分析素材',rendering:'渲染检查',review:'等待审核',approved:'已审核',publishing:'正在推送',published:'已推送',failed:'需要处理'};
const presets = {gentle:{silence_min:0.9,padding:0.25,min_clip:0.5},course:{silence_min:1.5,padding:0.4,min_clip:0.8},compact:{silence_min:0.7,padding:0.2,min_clip:0.5}};
let toastTimer;
function toast(message,error=false){$('#toast').textContent=message;$('#toast').className='toast visible'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').className='toast',6500);}
function showPage(name){document.querySelectorAll('.page').forEach(el=>el.classList.toggle('active',el.id==='page-'+name));document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.page===name));$('#pageTitle').textContent=({overview:'制作总览',assets:'素材库',jobs:'剪辑与审核',settings:'连接与设置',collections:'关键词采集'})[name] || '制作总览';if(name==='collections'&&typeof refreshCollections==='function')refreshCollections(true);}
function bytes(n){return n>=1073741824?(n/1073741824).toFixed(2)+' GB':(n/1048576).toFixed(1)+' MB';}
function time(s){return Number.isFinite(Number(s))?Number(s).toFixed(1)+' 秒':'—';}
function date(s){return s?new Date(typeof s==='number'?s*1000:s).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'';}
async function api(path,options={}){
  if(!state.base || !state.token) throw new Error('请先连接剪辑服务并填写工作台访问令牌。');
  const response=await fetch(state.base+path,{...options,headers:{Authorization:'Bearer '+state.token,...(options.body?{'Content-Type':'application/json'}:{}),...options.headers},cache:'no-store',signal:AbortSignal.timeout(path.endsWith('/publish')?300000:20000)});
  if(!response.ok){let body;try{body=await response.json();}catch{}throw new Error(typeof body?.detail==='string'?body.detail:'服务请求失败（'+response.status+'）');}
  return response.json();
}
function setConnection(ok,message){state.connected=ok;$('#connectionPill').className='pill '+(ok?'online':'neutral');$('#connectionPill').innerHTML='<i></i>'+(ok?'工作台已连接':'待连接');$('#connectionBanner').className='connection-banner'+(ok?' connected':'');$('#bannerTitle').textContent=ok?'工作台已就绪':'连接你的剪辑工作台';$('#bannerText').textContent=message || (ok?'素材目录、制作任务和审核状态已同步。':'云端页面用于配置与查看操作流程。实际素材、任务和成片由你自己的工作台处理。');$('#connectButton').textContent=ok?'连接设置':'连接设置 ↗';}
async function refresh(silent=false){
  if(!state.base || !state.token){if(!silent)showPage('settings');return;}
  try{
    const [status,assets,jobs,config]=await Promise.all([api('/api/status'),api('/api/assets'),api('/api/jobs'),api('/api/config')]);
    state.assets=assets.assets;state.jobs=jobs.jobs;state.config=config;
    setConnection(true,config.auto_publish?'自动发布已启用；质量检查通过的任务将按配置推送。':'素材已读取。新建剪辑后，成片会进入审核区，原始素材保持完整。');
    const counts=status.counts||{};
    $('#statAssets').textContent=state.assets.length;$('#statReview').textContent=counts.review||0;$('#statPublished').textContent=counts.published||0;$('#statActive').textContent=['queued','analyzing','rendering','publishing'].reduce((sum,k)=>sum+(counts[k]||0),0);
    $('#assetBadge').textContent=state.assets.length;$('#jobBadge').textContent=state.jobs.length;
    $('#serviceState').textContent=status.worker_running?(status.execution_target==='remote_gpu'?'远端 GPU 服务运行中':'本地服务运行中'):'工作线程未运行';$('#gpuState').textContent=status.gpu?.available?(status.gpu.name+' · '+Math.round(status.gpu.memory_mb/1024)+' GB'):'当前服务未检测到 GPU';
    $('#wecomState').textContent=status.delivery?.configured?(status.delivery.enabled?'已配置并启用':'已配置 · 尚未启用'):'待配置机器人';
    $('#inboxPath').textContent=config.inbox;renderAssets();renderJobs();if(typeof refreshCollections==='function'&&$('#page-collections').classList.contains('active'))refreshCollections(true);
  }catch(e){setConnection(false,e.message);if(!silent)toast(e.message,true);}
}
function jobRows(jobs){return jobs.map(j=>`<div class="job-row"><span class="job-icon">▷</span><div class="job-info"><b>${esc(j.title)}</b><small>${esc(date(j.created_at))} · ${esc(j.source)}</small></div><span class="status ${esc(j.status)}">${esc(statusNames[j.status]||j.status)}</span><button class="button subtle" data-review="${esc(j.id)}">${j.has_video?'查看':'详情'}</button></div>`).join('');}
function renderJobs(){
  const filtered=state.jobs.filter(j=>state.filter==='all'||j.status===state.filter);
  $('#jobsList').innerHTML=filtered.length?jobRows(filtered):'<div class="empty"><span class="empty-icon">▷</span><strong>这里还没有任务</strong><p>从素材库选一个视频，开始第一次剪辑。</p></div>';
  $('#recentJobs').className=state.jobs.length?'':'empty compact';
  $('#recentJobs').innerHTML=state.jobs.length?jobRows(state.jobs.slice(0,3)):'<span class="empty-icon">▷</span><strong>第一个好作品，从一段素材开始</strong><p>新建任务，或使用启动器开启文件夹自动处理。</p>';
}
function renderAssets(){
  $('#sourceSelect').innerHTML='<option value="">选择一段素材</option>'+state.assets.map(a=>`<option value="${esc(a.source)}">${esc(a.source)}</option>`).join('');
  $('#assetsList').innerHTML=state.assets.length?state.assets.map(a=>`<article class="panel asset-card"><div class="asset-poster" aria-label="视频文件">▷</div><div class="asset-body"><h3>${esc(a.name)}</h3><p>${esc(a.source)}</p><div class="asset-footer"><span>${bytes(a.size)} · ${esc(a.extension?.slice(1).toUpperCase())}</span><button class="button subtle" data-source="${esc(a.source)}">开始剪辑 ↗</button></div></div></article>`).join(''):'<div class="empty panel"><span class="empty-icon">▱</span><strong>素材目录暂时为空</strong><p>把 MP4、MOV 等视频放入上方目录，再点击扫描。</p></div>';
}
function newJob(source=''){
  if(!state.connected){showPage('settings');toast('先连接工作台，再创建剪辑任务。');return;}
  if(!state.assets.length){showPage('assets');toast('素材目录还是空的，先放入一段视频。');return;}
  $('#sourceSelect').value=source;$('#jobTitle').value=source?source.split('/').pop().replace(/\.[^.]+$/,'')+' · 初剪':'';$('#jobDialog').showModal();
}
async function review(id){
  try{const data=await api('/api/jobs/'+id);const j=data.job;state.selected=j;$('#reviewTitle').textContent=j.title;
    const warnings=[...(j.plan?.warnings||[]),...(j.result?.qc?.warnings||[])];
    $('#reviewDetails').innerHTML=`<p><span class="status ${esc(j.status)}">${esc(statusNames[j.status]||j.status)}</span>　预期成片 ${time(j.plan?.duration)} · 保留 ${j.plan?.clips?.length||'—'} 段</p>${j.error?'<div class="note">'+esc(j.error)+'</div>':''}<details><summary>剪辑记录与检查报告</summary><pre>${esc(JSON.stringify({检查:j.result?.qc||'尚未完成',提示:warnings,事件:j.events||[]},null,2))}</pre></details>`;
    $('#approveButton').disabled=j.status!=='review';$('#publishButton').disabled=j.status!=='approved'||!state.config.delivery?.enabled||!state.config.delivery?.configured;
    $('#reviewVideo').pause();$('#reviewVideo').removeAttribute('src');$('#reviewVideo').hidden=!j.has_video;
    if(state.videoBlob){URL.revokeObjectURL(state.videoBlob);state.videoBlob=null;}
    $('#reviewDialog').showModal();
    if(j.has_video){
      const response=await fetch(state.base+j.video_url,{headers:{Authorization:'Bearer '+state.token},cache:'no-store'});
      if(!response.ok)throw new Error('无法读取成片。');
      const blob=await response.blob();if(state.selected?.id!==id || !$('#reviewDialog').open)return;
      state.videoBlob=URL.createObjectURL(blob);$('#reviewVideo').src=state.videoBlob;
    }
  }catch(e){toast(e.message,true);}
}
document.querySelectorAll('[data-page]').forEach(el=>el.addEventListener('click',()=>showPage(el.dataset.page)));
document.querySelectorAll('[data-goto]').forEach(el=>el.addEventListener('click',()=>showPage(el.dataset.goto)));
document.querySelectorAll('[data-close]').forEach(el=>el.addEventListener('click',()=>$('#'+el.dataset.close).close()));
$('#reviewDialog').addEventListener('close',()=>{$('#reviewVideo').pause();$('#reviewVideo').removeAttribute('src');state.selected=null;if(state.videoBlob)URL.revokeObjectURL(state.videoBlob);state.videoBlob=null;});
$('#connectButton').onclick=()=>showPage('settings');$('#refreshButton').onclick=()=>refresh();$('#scanButton').onclick=()=>refresh();$('#newJobButton').onclick=()=>newJob();$('#newJobButton2').onclick=()=>newJob();
document.addEventListener('click',event=>{const source=event.target.closest('[data-source]');if(source)newJob(source.dataset.source);const item=event.target.closest('[data-review]');if(item)review(item.dataset.review);});
document.querySelectorAll('[data-filter]').forEach(el=>el.addEventListener('click',()=>{state.filter=el.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===el));renderJobs();}));
$('#sourceSelect').onchange=()=>{$('#jobTitle').value=$('#sourceSelect').value.split('/').pop().replace(/\.[^.]+$/,'')+' · 初剪';};
$('#connectionForm').onsubmit=async event=>{event.preventDefault();try{let value=$('#apiBase').value.trim() || (localPage?location.origin:'');const u=new URL(value);if(u.username||u.password||u.search||u.hash||!['https:','http:'].includes(u.protocol))throw new Error('请使用不含账号、密钥或查询参数的服务地址。');if(u.protocol==='http:'&&!['localhost','127.0.0.1','[::1]'].includes(u.hostname))throw new Error('远程服务必须使用 HTTPS。');state.base=u.origin;state.token=$('#apiToken').value.trim();await refresh();if(state.connected){$('#apiToken').value='';showPage('overview');toast('工作台已连接。');}}catch(e){toast(e.message,true);}};
$('#disconnectButton').onclick=()=>{state.token='';$('#apiToken').value='';state.selected=null;$('#reviewDialog').close();$('#reviewVideo').pause();$('#reviewVideo').removeAttribute('src');if(state.videoBlob)URL.revokeObjectURL(state.videoBlob);state.videoBlob=null;state.jobs=[];state.assets=[];state.config={};if(typeof resetCollections==='function')resetCollections();setConnection(false);renderAssets();renderJobs();['statAssets','statReview','statPublished','statActive','assetBadge','jobBadge'].forEach(id=>$('#'+id).textContent='—');$('#serviceState').textContent='尚未连接';$('#wecomState').textContent='待配置机器人';$('#inboxPath').textContent='连接服务后读取实际路径';toast('已断开连接，访问令牌已从当前页面清除。');};
$('#jobForm').onsubmit=async event=>{event.preventDefault();const button=$('#submitJob');button.disabled=true;try{await api('/api/jobs',{method:'POST',body:JSON.stringify({source:$('#sourceSelect').value,title:$('#jobTitle').value,settings:{...presets[$('#editPreset').value],target_aspect:$('#aspect').value},idempotency_key:crypto.randomUUID()})});$('#jobDialog').close();await refresh();showPage('jobs');toast('任务已加入剪辑队列。');}catch(e){toast(e.message,true);}finally{button.disabled=false;}};
$('#approveButton').onclick=async()=>{if(!state.selected)return;const id=state.selected.id;$('#approveButton').disabled=true;try{await api('/api/jobs/'+id+'/approve',{method:'POST'});toast('审核已确认。');await refresh();await review(id);}catch(e){toast(e.message,true);}};
$('#publishButton').onclick=async()=>{if(!state.selected)return;const id=state.selected.id;$('#publishButton').disabled=true;try{await api('/api/jobs/'+id+'/publish',{method:'POST'});toast('企业微信已确认接收成片。');await refresh();await review(id);}catch(e){toast(e.message,true);await refresh(true);}};
$('#downloadPreset').onclick=()=>{const config={...presets.gentle,target_aspect:'original',encoder:'cpu',normalize_audio:true,remove_silence:true,crf:18,fps:30};const url=URL.createObjectURL(new Blob([JSON.stringify(config,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='cutflow-preset.json';a.click();URL.revokeObjectURL(url);toast('剪辑参数已下载，可用作观察器的参数文件。');};
const fragment=new URLSearchParams(location.hash.slice(1));if(localPage&&fragment.get('token')){state.token=fragment.get('token');history.replaceState(null,'',location.pathname);}
async function connectDesktop(){
  if(localPage){
    try{
      const result=await fetch('/local-session',{headers:{'X-CutFlow-Local':'1'},cache:'no-store',signal:AbortSignal.timeout(4000)});
      if(result.ok){
        const session=await result.json();
        if(session.local_gateway===true&&typeof session.token==='string'&&session.token.length>=24){
          state.base=location.origin;state.token=session.token;state.localGateway=true;
          $('#connectionForm').hidden=true;
          const info=document.createElement('div');info.className='note';info.innerHTML='<strong>已启用本地自动连接</strong><p>网站在这台电脑上运行，剪辑由远端 GPU 完成。访问凭据由本机程序管理，无需填写。</p>';
          $('#connectionForm').before(info);
          const access=$('.local-access');access.innerHTML='<strong>下次如何打开？</strong><p>双击「打开本地工作台.cmd」，即可启动网站并连接 GPU。</p><a class="text-button" href="http://127.0.0.1:8767/">本地工作台：127.0.0.1:8767 ↗</a>';
          document.title='商剪工坊 · 本地工作台';
        }
      }
    }catch{}
  }
  $('#apiBase').value=state.base;
  if(state.token)await refresh(true);
}
$('#apiBase').value=state.base;renderJobs();connectDesktop();
setInterval(()=>{if(state.connected&&!document.hidden&&!$('#jobDialog').open)refresh(true);},8000);
