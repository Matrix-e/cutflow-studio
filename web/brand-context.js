'use strict';
const brandContextUI={revision:null,scope:null,loading:false,saving:false,dirty:false,summary:'',images:[],imageUrls:new Map(),upload:null,confirmedFacts:[],evidenceDraft:null};
const brandContextFields={culture:'brandCulture',facts:'brandFacts',advantages:'brandAdvantages',miniapp:'brandMiniapp',creative_rules:'brandCreativeRules'};
function brandContextScope(){return JSON.stringify([state.base,state.token]);}
function brandMessage(text,error=false){$('#brandContextStatus').textContent=text;$('#brandContextStatus').className='help'+(error?' error':'');}
function brandButtons(){
  const busy=brandContextUI.loading||brandContextUI.saving;
  const ready=state.connected&&brandContextUI.revision!==null&&brandContextUI.scope===brandContextScope();
  for(const id of Object.values(brandContextFields))$('#'+id).disabled=brandContextUI.loading;
  $('#brandContextSave').disabled=busy||!ready;
  $('#brandContextReload').disabled=busy;
  $('#brandScreenshotChoose').disabled=busy||!ready;
  const save=$('#brandEvidenceSave');if(save)save.disabled=busy||!ready||brandContextUI.dirty;
  const fields=$('#brandEvidenceFields');if(fields)fields.disabled=busy;
}
function resetBrandImages(){for(const url of brandContextUI.imageUrls.values())if(url)URL.revokeObjectURL(url);brandContextUI.imageUrls.clear();}
function brandImageUrl(item){return typeof item?.url==='string'&&/^\/api\/brand-context\/screenshots\/[a-zA-Z0-9_-]+(?:\/image)?$/.test(item.url)?item.url:null;}
function renderBrandImages(){
  $('#brandScreenshots').innerHTML=brandContextUI.images.length?brandContextUI.images.map(item=>{
    const image=brandContextUI.imageUrls.get(item.id);
    const linked=brandEvidenceReady(item);
    return '<figure class="brand-screenshot"><div id="brandImage-'+esc(item.id)+'">'+brandImageHtml(item,image)+'</div><figcaption>'+esc(item.name)+'<p class="help">'+(linked?'已关联知识，可作界面说明':'尚未关联知识')+'</p><button type="button" class="text-button" data-brand-evidence="'+esc(item.id)+'">'+(linked?'查看或修改用途':'用于视频 →')+'</button></figcaption></figure>';
  }).join(''):'<p class="help">添加真实截图，并关联对应知识，之后可用于视频中的界面说明。</p>';
  $('#brandScreenshots').innerHTML+=brandEvidenceEditor();
  const scope=brandContextUI.scope,context=connectionContext();
  for(const item of brandContextUI.images){
    const url=brandImageUrl(item);if(!url||brandContextUI.imageUrls.has(item.id))continue;
    brandContextUI.imageUrls.set(item.id,'');
    api(url,{},'blob',context).then(async blob=>{
      if(scope!==brandContextUI.scope||!currentContext(context))return;
      // Old uploads predate normalized-file hashes. Hash the exact authenticated
      // preview bytes so their metadata can be reviewed without another upload.
      if(!item.sha256&&crypto.subtle&&typeof blob.arrayBuffer==='function'){
        const digest=await crypto.subtle.digest('SHA-256',await blob.arrayBuffer());
        if(scope!==brandContextUI.scope||!currentContext(context))return;
        item.sha256=[...new Uint8Array(digest)].map(b=>b.toString(16).padStart(2,'0')).join('');
      }
      const image=URL.createObjectURL(blob);brandContextUI.imageUrls.set(item.id,image);
      const holder=$('#brandImage-'+item.id);if(holder)holder.innerHTML=brandImageHtml(item,image);
    }).catch(()=>{if(scope===brandContextUI.scope)brandMessage('部分截图暂未加载，可以重新读取。',true);});
  }
}
function brandImageHtml(item,image){return image?'<img src="'+esc(image)+'" alt="'+esc(item.name)+'">':'<div class="insight-placeholder">正在读取截图…</div>';}
function brandConfirmedFacts(data){return Array.isArray(data.confirmed_facts)?data.confirmed_facts.filter(f=>f&&typeof f.id==='string'&&typeof f.text==='string'&&(!f.status||f.status==='confirmed')):[];}
function brandEvidenceReady(item){const ids=new Set(brandContextUI.confirmedFacts.map(f=>f.id));return item.review_status==='confirmed'&&item.kind==='miniapp_screenshot'&&/^[a-f0-9]{64}$/.test(item.sha256||'')&&item.fact_ids?.length&&item.fact_ids.every(id=>ids.has(id));}
function openBrandEvidence(id){
  if(brandContextUI.saving||brandContextUI.loading||brandContextUI.scope!==brandContextScope())return;
  if(brandContextUI.evidenceDraft){
    if(brandContextUI.evidenceDraft.id===id)$('#brandEvidenceEditor')?.scrollIntoView({block:'nearest',behavior:'smooth'});
    else brandMessage('请先保存或取消当前截图的用途修改。',true);
    return;
  }
  const item=brandContextUI.images.find(x=>x.id===id);if(!item)return;
  const ids=new Set(brandContextUI.confirmedFacts.map(f=>f.id));
  brandContextUI.evidenceDraft={id,page_route:item.page_route||'',source_reference:item.source_reference||'',purpose:item.purpose||'',capture_mode:['local_miniapp','published_miniapp'].includes(item.capture_mode)?item.capture_mode:'local_miniapp',fact_ids:(item.fact_ids||[]).filter(value=>ids.has(value)),confirmed:false};
  renderBrandImages();brandButtons();$('#brandEvidenceEditor')?.scrollIntoView({block:'nearest',behavior:'smooth'});
}
function brandEvidenceEditor(){
  const d=brandContextUI.evidenceDraft;if(!d)return '';
  const item=brandContextUI.images.find(x=>x.id===d.id),facts=brandContextUI.confirmedFacts;
  return '<section class="panel" id="brandEvidenceEditor" style="grid-column:1/-1"><h3>这张截图怎样用于视频</h3><p>'+esc(item?.name||'小程序截图')+'</p><p class="help">只用于展示真实界面与入口，不代表已经办理、签约、获得收益或线上服务有效。</p><form id="brandEvidenceForm"><fieldset id="brandEvidenceFields" style="border:0;padding:0;margin:0"><div class="form-row"><label>截图来自<select data-brand-field="capture_mode"><option value="local_miniapp"'+(d.capture_mode==='local_miniapp'?' selected':'')+'>本机打开的小程序页面</option><option value="published_miniapp"'+(d.capture_mode==='published_miniapp'?' selected':'')+'>已发布的小程序页面</option></select></label><label>页面名称或位置<input data-brand-field="page_route" maxlength="300" value="'+esc(d.page_route)+'" placeholder="例如：托管 → 智能托管首页" required></label></div><label>来源说明<input data-brand-field="source_reference" maxlength="600" value="'+esc(d.source_reference)+'" placeholder="例如：我在本机打开 Super Ecar 智能托管页后截取" required></label><label>用它讲什么<input data-brand-field="purpose" maxlength="500" value="'+esc(d.purpose)+'" placeholder="例如：介绍托管前可以在哪里咨询规则" required></label><p><strong>这张图对应哪些已确认知识？</strong></p>'+(facts.length?'<div style="max-height:260px;overflow:auto">'+facts.map(f=>'<label class="production-check" style="display:flex;gap:10px;align-items:flex-start;margin:10px 0"><input type="checkbox" data-brand-fact="'+esc(f.id)+'"'+(d.fact_ids.includes(f.id)?' checked':'')+'><span>'+esc(f.text)+'</span></label>').join('')+'</div>':'<p class="help">暂无可关联的已确认知识，请先保存上方品牌知识，再重新读取。</p>')+'<label class="production-check" style="margin:16px 0;display:flex;gap:10px"><input type="checkbox" data-brand-field="confirmed"'+(d.confirmed?' checked':'')+'>我已查看这张图，确认它与所选知识对应，且不是官网或设计示意图。</label><div class="brand-context-actions"><button id="brandEvidenceSave" class="button primary" type="submit">保存并用于视频</button><button class="button subtle" type="button" data-brand-cancel>取消</button></div><p id="brandEvidenceStatus" class="help" role="status"></p></fieldset></form></section>';
}
function brandEvidenceMessage(message,error=false){const box=$('#brandEvidenceStatus');if(box){box.textContent=message;box.className='help'+(error?' error':'');}brandMessage(message,error);}
async function saveBrandEvidence(event){
  event?.preventDefault();const d=brandContextUI.evidenceDraft;
  if(!d||brandContextUI.saving||brandContextUI.loading||!state.connected||brandContextUI.scope!==brandContextScope())return;
  if(brandContextUI.dirty){brandEvidenceMessage('请先保存上方品牌知识，再关联截图。',true);return;}
  const item=brandContextUI.images.find(x=>x.id===d.id),ids=new Set(brandContextUI.confirmedFacts.map(f=>f.id));
  if(!brandContextUI.imageUrls.get(d.id)){brandEvidenceMessage('请等待截图显示后再核对；读取失败时可取消并重新读取。',true);return;}
  if(!item||!/^[a-f0-9]{64}$/.test(item.sha256||'')){brandEvidenceMessage('当前浏览器未能核对这张旧截图，请用本地工作台重新读取后关联；原图会保留。',true);return;}
  if(!d.confirmed||![d.page_route,d.source_reference,d.purpose].every(v=>typeof v==='string'&&v.trim())||!d.fact_ids.length||d.fact_ids.length>16||d.fact_ids.some(id=>!ids.has(id))){brandEvidenceMessage('请填写截图来源和用途，选择 1–16 条对应知识，并勾选已核对。',true);return;}
  const sameSource=d.page_route===item.page_route&&d.source_reference===item.source_reference&&d.capture_mode===item.capture_mode;
  const evidence={kind:'miniapp_screenshot',capture_mode:d.capture_mode,page_route:d.page_route.trim(),source_reference:d.source_reference.trim(),source_sha256:sameSource?item.source_sha256||'':'',purpose:d.purpose.trim(),fact_ids:[...new Set(d.fact_ids)],verification_scope:item.verification_scope||'仅核验截图可见的小程序界面、栏目与入口；不证明线上可用性、实际办理、审批、签约、收益或服务效果。',review_status:'confirmed'};
  const context=connectionContext();brandContextUI.saving=true;brandButtons();brandEvidenceMessage('正在保存截图用途…');
  try{
    const result=await api(item.url+'/evidence',{method:'PUT',body:JSON.stringify({expected_sha256:item.sha256,evidence})},'json',context);
    if(!currentContext(context))return;
    if(!result.screenshot||result.screenshot.id!==item.id||result.screenshot.sha256!==item.sha256||!brandEvidenceReady(result.screenshot))throw new Error('保存结果待核对，请重新读取截图后确认。');
    brandContextUI.images=brandContextUI.images.map(x=>x.id===item.id?result.screenshot:x);brandContextUI.evidenceDraft=null;
    renderBrandImages();brandMessage('截图用途已保存，后续相关视频可选用；原图片保持不变。');
  }catch(error){if(currentContext(context))brandEvidenceMessage(error.message+' 当前填写内容已保留。',true);}
  finally{brandContextUI.saving=false;brandButtons();}
}
async function loadBrandContext(){
  if(brandContextUI.loading||brandContextUI.saving)return;
  if(!state.connected){brandMessage('连接恢复后可读取品牌知识。');brandButtons();return;}
  if(brandContextUI.dirty){brandMessage('还有未保存的修改，请先保存。');return;}
  if(brandContextUI.evidenceDraft){brandEvidenceMessage('请先保存或取消截图用途修改。',true);return;}
  const scope=brandContextScope(),context=connectionContext();
  if(brandContextUI.scope!==scope){resetBrandImages();brandContextUI.revision=null;brandContextUI.images=[];brandContextUI.confirmedFacts=[];}
  brandContextUI.loading=true;brandContextUI.scope=scope;brandButtons();brandMessage('正在读取品牌知识…');
  try{
    const data=await api('/api/brand-context',{},'json',context);
    if(!currentContext(context)||scope!==brandContextUI.scope)return;
    if(!Number.isInteger(data.revision)||Object.keys(brandContextFields).some(key=>typeof data[key]!=='string'))throw new Error('品牌知识返回不完整，请稍后重试。');
    for(const [key,id]of Object.entries(brandContextFields))$('#'+id).value=data[key];
    brandContextUI.revision=data.revision;renderBrandCultureSummary(data);brandContextUI.images=Array.isArray(data.screenshots)?data.screenshots:[];
    brandContextUI.confirmedFacts=brandConfirmedFacts(data);
    resetBrandImages();renderBrandImages();brandMessage('已读取，修改后点击保存。');
  }catch(error){if(currentContext(context))brandMessage(error.message,true);}
  finally{brandContextUI.loading=false;brandButtons();}
}
async function saveBrandContext(event){
  event.preventDefault();if(brandContextUI.saving||brandContextUI.loading||!state.connected||brandContextUI.revision===null||brandContextUI.scope!==brandContextScope())return;
  const context=connectionContext(),payload={revision:brandContextUI.revision,...Object.fromEntries(Object.entries(brandContextFields).map(([key,id])=>[key,$('#'+id).value.trim()]))};
  const submitted=JSON.stringify(payload);brandContextUI.saving=true;brandButtons();brandMessage('正在保存…');
  try{
    const result=await api('/api/brand-context',{method:'PUT',body:JSON.stringify(payload)},'json',context);
    if(!currentContext(context))return;
    if(!Number.isInteger(result.revision))throw new Error('保存结果待确认，请重新读取后核对。');
    brandContextUI.revision=result.revision;renderBrandCultureSummary({...payload,...result});
    if(Array.isArray(result.confirmed_facts))brandContextUI.confirmedFacts=brandConfirmedFacts(result);
    if(brandContextUI.evidenceDraft){const ids=new Set(brandContextUI.confirmedFacts.map(f=>f.id));brandContextUI.evidenceDraft.fact_ids=brandContextUI.evidenceDraft.fact_ids.filter(id=>ids.has(id));}
    renderBrandImages();
    const current={revision:payload.revision,...Object.fromEntries(Object.entries(brandContextFields).map(([key,id])=>[key,$('#'+id).value.trim()]))};
    brandContextUI.dirty=JSON.stringify(current)!==submitted;
    brandMessage(brandContextUI.dirty?'已保存提交时的内容；你刚输入的修改尚未保存。':'已保存，后续创作会自动参考。');
  }catch(error){if(currentContext(context))brandMessage(error.httpStatus===409?'其他窗口已更新品牌知识。当前修改保留，请先复制需要保留的文字，再重新打开页面核对最新版本。':error.message,true);}
  finally{brandContextUI.saving=false;brandButtons();}
}
async function uploadBrandScreenshot(file){
  if(!file||brandContextUI.saving||brandContextUI.loading||!state.connected||brandContextUI.revision===null||brandContextUI.scope!==brandContextScope())return;
  if(!['image/png','image/jpeg','image/webp'].includes(file.type)||file.size<=0||file.size>8*1024*1024){brandMessage('请选择不超过 8 MB 的 PNG、JPG 或 WebP 图片。',true);return;}
  const identity=[file.name,file.size,file.lastModified].join(':');
  if(brandContextUI.upload?.identity!==identity)brandContextUI.upload={identity,id:crypto.randomUUID()};
  const context=connectionContext();brandContextUI.saving=true;brandButtons();brandMessage('正在保存小程序截图…');
  try{
    const result=await api('/api/brand-context/screenshots?name='+encodeURIComponent(file.name)+'&request_id='+brandContextUI.upload.id,{method:'POST',headers:{'Content-Type':file.type},body:file},'json',context);
    if(!currentContext(context))return;
    const item=result.screenshot;if(!item?.id||!brandImageUrl(item))throw new Error('截图结果待确认，请重新读取。');
    brandContextUI.images=[...brandContextUI.images.filter(image=>image.id!==item.id),item];brandContextUI.upload=null;
    renderBrandImages();brandMessage('截图已加入。点击“用于视频”，核对对应知识和用途后即可选用。');
  }catch(error){if(currentContext(context))brandMessage(error.message+' 可重新选择同一图片核对上传结果。',true);}
  finally{brandContextUI.saving=false;$('#brandScreenshotFile').value='';brandButtons();}
}
for(const id of Object.values(brandContextFields))$('#'+id).addEventListener('input',()=>{brandContextUI.dirty=true;brandMessage('有未保存的修改。');});
$('#brandContextForm').addEventListener('submit',saveBrandContext);
$('#brandContextReload').onclick=loadBrandContext;
$('#brandScreenshotChoose').onclick=()=>$('#brandScreenshotFile').click();
$('#brandScreenshotFile').addEventListener('change',()=>uploadBrandScreenshot($('#brandScreenshotFile').files?.[0]));
$('#brandScreenshots').addEventListener('click',event=>{
  const button=event.target.closest('[data-brand-evidence]');if(button)openBrandEvidence(button.dataset.brandEvidence);
  if(event.target.closest('[data-brand-cancel]')&&!brandContextUI.saving){brandContextUI.evidenceDraft=null;renderBrandImages();brandButtons();}
});
$('#brandScreenshots').addEventListener('submit',event=>{if(event.target.id==='brandEvidenceForm')saveBrandEvidence(event);});
function editBrandEvidence(event){
  const d=brandContextUI.evidenceDraft;if(!d||brandContextUI.saving)return;const el=event.target,field=el.dataset?.brandField,fact=el.dataset?.brandFact;
  if(field&&['page_route','source_reference','purpose','capture_mode','confirmed'].includes(field))d[field]=field==='confirmed'?el.checked:el.value;
  if(fact)d.fact_ids=el.checked?[...new Set([...d.fact_ids,fact])]:d.fact_ids.filter(id=>id!==fact);
}
$('#brandScreenshots').addEventListener('input',editBrandEvidence);
$('#brandScreenshots').addEventListener('change',editBrandEvidence);
$('#openBrandContext').onclick=()=>{showPage('knowledge');loadBrandContext();$('#brandContextPanel').scrollIntoView({block:'start',behavior:'smooth'});};
document.addEventListener('click',event=>{const nav=event.target.closest('[data-page="knowledge"], [data-goto="knowledge"]');if(nav)loadBrandContext();});
brandButtons();

function renderBrandCultureSummary(data){
  const culture=typeof data.culture==='string'?data.culture.trim().replace(/\s+/g,' '):'';
  const hasKnowledge=['facts','advantages','miniapp'].some(key=>typeof data[key]==='string'&&data[key].trim());
  let summary=hasKnowledge?'已关联品牌知识，写稿时自动使用。':'保存企业文化与业务资料，后续自动沿用。';
  if(culture){const sentence=culture.match(/^.+?[。！？!?][”’」』]?/u)?.[0];summary=culture.length<=160?culture:sentence&&sentence.length<=160?sentence:'已保存企业文化，写稿时自动使用。';}
  brandContextUI.summary=summary;$('#brandCultureSummary').textContent=summary;
  if(typeof updateStoryWorkflow==='function')updateStoryWorkflow();
}
if(state.connected&&typeof production!=='undefined'&&production.mode==='story')loadBrandContext();
