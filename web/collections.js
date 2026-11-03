'use strict';
let collectionState={tasks:[],items:[],busy:false};
const collectionStatus={queued:'等待本机采集',running:'采集中',succeeded:'本轮已结束',failed:'需要处理',interrupted:'已中断',not_configured:'尚未配置'};
const downloadStatus={downloaded:'已下载到本机',not_requested:'参考链接',reference_only:'参考链接',not_available:'暂不可下载',download_unavailable:'暂不可下载',failed:'下载失败',unavailable:'暂不可下载'};
const rightsNames={unknown:'待确认',owned:'自有素材',licensed:'已有授权',reference:'仅作参考'};
function resetCollections(){collectionState={tasks:[],items:[],busy:false};$('#collectionTasks').innerHTML='<div class="empty compact"><p>连接工作台后读取任务。</p></div>';$('#collectionItems').innerHTML='<div class="empty compact"><p>连接工作台后读取采集结果。</p></div>';$('#browserCollectorStatus').textContent='尚未连接';$('#collectionTaskCount').textContent='—';$('#collectionItemCount').textContent='—';$('#collectionTaskFilter').innerHTML='<option value="">全部关键词</option>';}
async function refreshCollections(silent=false){
 if(!state.connected){if(!silent)toast('先连接工作台，再管理采集任务。',true,'connection');return;}
 if(collectionState.busy)return;
 collectionState.busy=true;
 const base=state.base,token=state.token;
 try{
 const query=new URLSearchParams({limit:'100'});if($('#collectionTaskFilter').value)query.set('task_id',$('#collectionTaskFilter').value);if($('#collectionRightsFilter').value)query.set('rights_status',$('#collectionRightsFilter').value);
 const [config,tasks,items]=await Promise.all([api('/api/collections/config'),api('/api/collections/tasks'),api('/api/collections/items?'+query)]);
 if(!state.connected||state.base!==base||state.token!==token)return;
 collectionState.tasks=tasks.tasks;collectionState.items=items.items;
 $('#browserCollectorStatus').textContent=({online:'本机采集器已连接',offline:'本机采集器已离线',local_agent_required:'等待启动本机采集器'})[config.browser?.status]||'等待本机采集器';
 const selected=$('#collectionTaskFilter').value;$('#collectionTaskFilter').innerHTML='<option value="">全部关键词</option>'+tasks.tasks.map(t=>'<option value="'+esc(t.id)+'">'+esc(t.keyword)+'</option>').join('');$('#collectionTaskFilter').value=selected;
 renderCollections();
 }catch(e){if(!silent)reportError(e);}finally{collectionState.busy=false;}
}
function renderCollections(){
 $('#collectionTaskCount').textContent=collectionState.tasks.length+' 个';$('#collectionItemCount').textContent=collectionState.items.length+' 条';
 $('#collectionTasks').innerHTML=collectionState.tasks.map(t=>{const r=t.last_run;return '<article class="collection-task"><div class="section-heading"><strong>'+esc(t.keyword)+'</strong><span class="status">'+(t.enabled?'每 '+(t.interval_seconds/3600)+' 小时':'已暂停定时')+'</span></div><p>'+esc(r?collectionStatus[r.status]||r.status:'尚未执行')+(r?' · '+esc(date(r.created_at)):'')+'</p>'+(r?.message?'<p class="help">'+esc(r.message)+'</p>':'')+(r?.status==='succeeded'?'<p class="help">发现 '+r.items_seen+' 条 · 新增 '+r.items_added+' 条；下载状态见右侧。</p>':'')+'<div class="form-buttons"><button class="button subtle" data-collect-run="'+esc(t.id)+'">立即采集</button><button class="text-button" data-collect-toggle="'+esc(t.id)+'">'+(t.enabled?'暂停定时':'恢复定时')+'</button><button class="text-button" data-collect-filter="'+esc(t.id)+'">查看结果</button></div></article>';}).join('')||'<div class="empty compact"><p>添加第一个关键词，开始收集素材。</p></div>';
 $('#collectionItems').innerHTML=collectionState.items.map(i=>'<article class="collection-item"><div class="section-heading"><strong>'+esc(i.title||'抖音视频')+'</strong><span class="status">'+esc(downloadStatus[i.download_status]||i.download_status)+'</span></div><p>'+esc(i.author||'作者未读取')+' · '+esc(i.item_id||'链接待解析')+'</p><p class="help">'+esc(i.download_reason||'')+'</p><div class="collection-item-actions"><a href="'+esc(i.source_url)+'" target="_blank" rel="noopener noreferrer" class="text-button">查看原始视频 ↗</a><label>使用标记<select data-collect-rights="'+esc(i.id)+'">'+Object.entries(rightsNames).map(([k,v])=>'<option value="'+k+'" '+(k===i.rights_status?'selected':'')+'>'+v+'</option>').join('')+'</select></label></div>'+(i.local_filename?'<p class="help">本机文件：'+esc(i.local_filename)+' · 在“采用采集素材”中选择后才进入剪辑。</p>':'')+'<p class="help">标记用于整理；使用素材时还需记录授权依据。</p></article>').join('')||'<div class="empty compact"><strong>尚无匹配的采集结果</strong><p>启动本机采集器，再运行关键词任务。</p></div>';
}
$('#refreshCollections').onclick=()=>refreshCollections();
$('#collectionTaskFilter').onchange=()=>refreshCollections();$('#collectionRightsFilter').onchange=()=>refreshCollections();
$('#collectionTaskForm').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;try{const frequency=Number($('#collectionFrequency').value);await api('/api/collections/tasks',{method:'POST',body:JSON.stringify({keyword:$('#collectionKeyword').value.trim(),provider:'browser',enabled:frequency>0,interval_seconds:frequency||21600,max_items:Number($('#collectionLimit').value)})});$('#collectionKeyword').value='';await refreshCollections();toast('关键词已保存；定时采集需要本机采集器保持运行。');}catch(err){reportError(err);}finally{b.disabled=false;}};
$('#collectionImportForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/collections/items/import',{method:'POST',body:JSON.stringify({source_url:$('#collectionSourceUrl').value.trim(),title:$('#collectionSourceTitle').value.trim()})});e.target.reset();await refreshCollections();toast('参考链接已保存。');}catch(err){reportError(err);}};
document.addEventListener('click',async e=>{const button=e.target.closest('[data-collect-run],[data-collect-toggle],[data-collect-filter]');if(!button)return;button.disabled=true;try{if(button.dataset.collectFilter){$('#collectionTaskFilter').value=button.dataset.collectFilter;}else if(button.dataset.collectRun){await api('/api/collections/tasks/'+button.dataset.collectRun+'/run',{method:'POST'});toast('任务已排队，等待本机浏览器采集器执行。');}else{const task=collectionState.tasks.find(t=>t.id===button.dataset.collectToggle);await api('/api/collections/tasks/'+task.id,{method:'PATCH',body:JSON.stringify({enabled:!task.enabled})});}await refreshCollections();}catch(err){reportError(err);}finally{button.disabled=false;}});
document.addEventListener('change',async e=>{if(!e.target.dataset.collectRights)return;try{await api('/api/collections/items/'+e.target.dataset.collectRights,{method:'PATCH',body:JSON.stringify({rights_status:e.target.value})});toast('使用标记已保存。');}catch(err){reportError(err);await refreshCollections();}});

