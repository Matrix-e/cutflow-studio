'use strict';
// Local file operations are deliberately separate from remote GPU health.
const localImports={items:[],inbox:'',maxBytes:2147483648,file:null,requestId:null,busy:false,loading:false,error:'',token:null};
function localImportAvailable(){return localPage&&state.localGateway&&state.base===location.origin&&!!state.token;}
function localContext(){return {token:state.token,origin:location.origin};}
function currentLocalContext(context){return localImportAvailable()&&context.token===state.token&&context.origin===location.origin;}
function importFeedback(message,error=false){$('#importFeedback').textContent=message;$('#importFeedback').className='media-status'+(error?' error':'');}
function updateImportModeHelp(){$('#importModeHelp').textContent=$('#importMode').value==='library'?'只保存和同步素材。选择素材、填写想法后，点击生成才开始制作。':$('#importMode').value==='preserve'?'保留所有画面与原有节奏、保持原比例，仍会规范编码并整理音量。适合品牌包装、音乐片和已有成片。':'适合口播、课程、访谈：清理较长停顿，保留原始语速。';}
function updateLocalImportAccess(){
  const available=localImportAvailable();$('#localImportPanel').hidden=!available;
  $('#chooseImportFile').disabled=!available||localImports.busy;$('#importMode').disabled=localImports.busy;$('#startImportButton').disabled=!available||localImports.busy||!localImports.file;$('#openInboxButton').disabled=!available;
  if(available){$('#inboxPath').textContent=localImports.inbox||'视频先保存在这台电脑，随后自动同步到 GPU。';if(localImports.items.length)renderLocalImports();if(localImports.token!==state.token){localImports.token=state.token;refreshLocalLibrary(true);}}
  else{localImports.token=null;localImports.items=[];$('#localImportsList').innerHTML='';}
}
async function localRequest(path,options={}){
  if(!localImportAvailable())throw new Error('请从这台电脑打开本地工作台，再导入视频。');
  const context=localContext();let response;
  try{response=await fetch(path,{...options,headers:{Authorization:'Bearer '+context.token,...options.headers},cache:'no-store',signal:AbortSignal.timeout(15000)});}
  catch{throw new Error((options.method&&options.method!=='GET')?'本机操作结果需核实，请刷新导入记录；不会自动重复执行。':'暂时无法读取本机素材记录，请稍后刷新。');}
  if(!currentLocalContext(context))throw new Error('本机连接已变化，请刷新导入记录查看操作结果。');
  let body;try{body=await response.json();}catch{throw new Error('本机工作台返回的信息不完整，请刷新后重试。');}
  if(!response.ok){if(response.status===401&&!state.reauthAttempted)state.reauthNeeded=true;throw new Error(typeof body.detail==='string'?body.detail:'本机操作未完成（'+response.status+'）。');}
  return body;
}
function renderLocalImports(){
  $('#importLimit').textContent='单个视频不超过 '+bytes(localImports.maxBytes);
  $('#localImportsList').innerHTML=localImports.items.length?localImports.items.slice(0,30).map(item=>{
    const synced=item.status==='synced',mode=item.mode==='library'?'仅入库':item.mode==='preserve'?'完整保留':'口播保守处理',job=item.mode!=='library'&&item.remote_source&&state.jobs.find(j=>j.source===item.remote_source&&!isTechnicalTest(j));
    const label=job?statusNames[job.status]||'已同步到 GPU':synced?'已同步到 GPU':'已导入本机 · 等待同步';
    return '<article class="local-import-row"><div><strong>'+esc(item.name)+'</strong><p>'+bytes(item.size)+' · '+mode+' · '+esc(date(item.created_at))+'</p></div><span class="status '+(synced?'published':'review')+'">'+esc(label)+'</span>'+(job?'<button class="button subtle" data-import-job="'+esc(job.id)+'" '+(!state.connected?'disabled':'')+'>查看任务</button>':synced&&item.remote_source?'<button class="button subtle" data-import-preview="'+esc(item.remote_source)+'" '+(!state.connected?'disabled':'')+'>预览原片</button>':'<span class="help">'+(state.connected?'等待同步程序处理':'GPU 恢复连接后继续同步')+'</span>')+'</article>';
  }).join(''):'<p class="help">还没有本机导入记录。选择一段视频即可开始。</p>';
}
async function refreshLocalLibrary(silent=false){
  if(!localImportAvailable()||localImports.loading)return;
  const context=localContext();localImports.loading=true;
  try{const data=await localRequest('/local-api/library');if(!currentLocalContext(context))return;
    if(typeof data.inbox!=='string'||!Array.isArray(data.imports))throw new Error('本机素材记录格式不完整。');
    localImports.inbox=data.inbox;localImports.items=data.imports;localImports.maxBytes=Math.min(2147483648,Number(data.max_bytes)||2147483648);localImports.error='';$('#inboxPath').textContent=data.inbox;renderLocalImports();if(typeof renderCreatorMaterials==='function')renderCreatorMaterials();
  }catch(error){localImports.error=error.message;if(!silent)importFeedback(error.message,true);else if(!localImports.items.length)$('#localImportsList').innerHTML='<p class="media-status error">'+esc(error.message)+'</p>';}
  finally{localImports.loading=false;}
}
function selectImportFile(file){
  if(localImports.busy)return;
  if(!file)return;
  localImports.file=null;localImports.requestId=null;$('#importSelectedName').textContent='尚未选择视频';$('#importSelectedSize').textContent='请选择一个有效的视频文件';updateLocalImportAccess();
  if(!/\.(mp4|mov|mkv|webm|avi|m4v|mts)$/i.test(file.name)){importFeedback('请选择 MP4、MOV、MKV、WebM、AVI、M4V 或 MTS 视频。',true);return;}
  if(!Number.isFinite(file.size)||file.size<=0||file.size>localImports.maxBytes){importFeedback(file.size>localImports.maxBytes?'视频超过单文件上限 '+bytes(localImports.maxBytes)+'，本次未导入。':'这个文件为空，无法导入。',true);return;}
  localImports.file=file;localImports.requestId=crypto.randomUUID();$('#importSelectedName').textContent=file.name;$('#importSelectedSize').textContent=bytes(file.size);$('#importProgressBox').hidden=true;importFeedback('已选好视频。确认处理方式后，点击“导入到本机”。');updateLocalImportAccess();
}
function uploadLocalFile(file,mode,id,onProgress){
  const context=localContext();
  if(!localImportAvailable())return Promise.reject(new Error('本机工作台尚未就绪。'));
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();const query=new URLSearchParams({filename:file.name,mode,id});
    xhr.open('POST','/local-api/import?'+query.toString());xhr.setRequestHeader('Authorization','Bearer '+context.token);xhr.setRequestHeader('Content-Type','application/octet-stream');xhr.responseType='json';xhr.timeout=1800000;
    xhr.upload.onprogress=event=>{if(currentLocalContext(context)&&event.lengthComputable)onProgress(event.loaded,event.total);};
    xhr.onload=()=>{if(!currentLocalContext(context)){reject(new Error('本机连接已变化，请刷新导入记录查看结果；不会自动重新上传。'));return;}
      const body=xhr.response;if(xhr.status>=200&&xhr.status<300&&body?.import){resolve(body);return;}
      if(xhr.status===401&&!state.reauthAttempted)state.reauthNeeded=true;
      reject(new Error(typeof body?.detail==='string'?body.detail:'导入未完成，请刷新本机记录核实。'));
    };
    const uncertain=()=>reject(new Error('上传连接中断，结果需核实。请先刷新导入记录；不会自动重复上传。'));
    xhr.onerror=uncertain;xhr.ontimeout=uncertain;xhr.onabort=uncertain;xhr.send(file);
  });
}
async function startLocalImport(){
  if(localImports.busy||!localImports.file||!localImportAvailable())return;
  const file=localImports.file,mode=['library','preserve'].includes($('#importMode').value)?$('#importMode').value:'gentle',id=localImports.requestId;
  localImports.busy=true;updateLocalImportAccess();$('#importProgressBox').hidden=false;$('#importProgress').value=0;$('#importProgressText').textContent='准备导入…';importFeedback('正在保存到这台电脑，请保持页面打开。');
  try{const data=await uploadLocalFile(file,mode,id,(done,total)=>{const percent=Math.min(100,Math.round(done/total*100));$('#importProgress').value=percent;$('#importProgressText').textContent=percent===100?'上传完成，正在检查并保存文件…':percent+'% · '+bytes(done)+' / '+bytes(total);});
    const item=data.import;localImports.items=[item,...localImports.items.filter(old=>old.id!==item.id)];renderLocalImports();$('#importProgress').value=100;$('#importProgressText').textContent='已保存到本机';importFeedback(data.duplicate?'这段视频已在本机，无需重复导入。':'导入完成，视频已保存到本机。同步进度可在下方查看。');localImports.file=null;localImports.requestId=null;$('#importFile').value='';$('#importSelectedName').textContent='可以继续选择下一段视频';$('#importSelectedSize').textContent='原文件保持完整';await refreshLocalLibrary(true);
  }catch(error){importFeedback(error.message,true);$('#importProgressText').textContent='请核实导入结果';}
  finally{localImports.busy=false;updateLocalImportAccess();}
}
$('#chooseImportFile').onclick=()=>$('#importFile').click();
$('#importFile').onchange=()=>selectImportFile($('#importFile').files?.[0]);
$('#importMode').onchange=()=>{localImports.requestId=localImports.file?crypto.randomUUID():null;updateImportModeHelp();};
$('#startImportButton').onclick=startLocalImport;
$('#refreshImportsButton').onclick=()=>refreshLocalLibrary();
$('#openInboxButton').onclick=async()=>{try{await localRequest('/local-api/open-folder',{method:'POST'});importFeedback('素材文件夹已打开。');}catch(error){importFeedback(error.message,true);}};
const dropZone=$('#importDropZone');
['dragenter','dragover'].forEach(name=>dropZone.addEventListener(name,event=>{event.preventDefault();if(!localImports.busy)dropZone.classList.add('drag-over');}));
['dragleave','drop'].forEach(name=>dropZone.addEventListener(name,event=>{event.preventDefault();dropZone.classList.remove('drag-over');}));
dropZone.addEventListener('drop',event=>{event.preventDefault();dropZone.classList.remove('drag-over');if(localImports.busy)return;const files=event.dataTransfer?.files;if(files?.length>1){importFeedback('每次导入一段视频，请先选择其中一个文件。',true);return;}selectImportFile(files?.[0]);});
document.addEventListener('click',event=>{const button=event.target.closest('[data-import-preview]');if(button&&!button.disabled)previewSource(button.dataset.importPreview);const job=event.target.closest('[data-import-job]');if(job&&!job.disabled)review(job.dataset.importJob);});
setInterval(()=>{if(!document.hidden&&localImportAvailable())refreshLocalLibrary(true);},8000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshLocalLibrary(true);});
updateImportModeHelp();updateLocalImportAccess();
