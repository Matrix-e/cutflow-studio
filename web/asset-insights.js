/* Source cards are independent of selection/draft state; no automatic selections. */
const insightCards = new Map();
let insightScope = '', insightActive = 0;
function insightContext(){return JSON.stringify([state.base,state.token,state.epoch]);}
function assetInsightHtml(item){
  if(!item.source||!item.ready)return '<div class="insight-placeholder">同步完成后显示画面与内容总结</div>';
  return '<div class="asset-insight" data-insight-source="'+esc(item.source)+'"><div class="insight-placeholder">正在准备关键画面与内容分析…</div></div>';
}
function paintAssetInsight(source, data){
  for(const node of document.querySelectorAll('[data-insight-source]')){
    if(node.dataset.insightSource!==source)continue;
    const signature=JSON.stringify(data);if(node._insightSignature===signature)continue;node._insightSignature=signature;
    const expanded=node.querySelector('details')?.open;
    const frames=(data.frames||[]).filter(f=>/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(f.image));
    const r=data.recommendation;
    node.innerHTML=(frames.length?'<button type="button" class="insight-frames" data-preview="'+esc(source)+'" aria-label="播放原视频">'+frames.map(f=>'<span><img src="'+f.image+'" alt="原片 '+esc(f.time)+' 秒画面"><small>'+esc(f.time)+'s</small></span>').join('')+'<b>▷ 播放</b></button>':'<div class="insight-placeholder">'+(data.status==='failed'?'画面提取未完成':'正在排队提取画面…')+'</div>')+
      '<div class="insight-body"><p class="insight-summary"><strong>'+(data.summary_kind==='director_speech_excerpt'?'带时间的原声摘录':'内容总结')+'</strong>'+esc(data.summary||'正在识别原声并总结…')+'</p>'+
      (r?'<div class="insight-rating"><strong>画面推荐度 · 技术初评 '+esc(r.score)+'/100</strong><span>'+esc(r.label)+'</span></div><meter min="0" max="100" value="'+Number(r.score)+'" aria-label="画面技术推荐度"></meter><details><summary>推荐依据与分析范围</summary><ul>'+r.reasons.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul><p>'+esc(r.scope)+'</p><p>'+esc(data.summary_basis||'原声分析中')+'</p></details>':'<p class="help">画面推荐度：待分析</p>')+
      (data.status==='analyzing'||data.status==='pending'?'<p class="insight-status" role="status">◷ 正在分析，完成后自动更新</p>':'')+
      (['partial','failed'].includes(data.status)?'<button type="button" class="text-button" data-insight-retry="'+esc(source)+'">重试分析</button>':'')+'</div>';
    if(expanded&&node.querySelector('details'))node.querySelector('details').open=true;
  }
}
function hydrateAssetInsights(){
  const scope=insightContext();
  if(scope!==insightScope){insightCards.clear();insightScope=scope;}
  if(!state.connected)return;
  for(const node of document.querySelectorAll('[data-insight-source]')){
    const source=node.dataset.insightSource;
    if(!insightCards.has(source))insightCards.set(source,{status:'pending',started:false});
    paintAssetInsight(source,insightCards.get(source));
  }
  pumpAssetInsights();
}
function pumpAssetInsights(){
  if(!state.connected)return;
  for(const [source,item] of insightCards){
    if(insightActive>=2)break;
    if(item.started)continue;
    item.started=true;insightActive++;
    runAssetInsight(source,insightScope).finally(()=>{insightActive--;pumpAssetInsights();});
  }
}
async function runAssetInsight(source,scope){
  const context=connectionContext(),path='/api/assets/insights?source='+encodeURIComponent(source);
  try{
    let data=await api(path,{method:'POST'},'json',context);
    while(scope===insightScope&&currentContext(context)){
      insightCards.set(source,{...data,started:true});paintAssetInsight(source,data);
      if(!['pending','analyzing'].includes(data.status))return;
      await new Promise(resolve=>setTimeout(resolve,3000));
      if(scope!==insightScope||!currentContext(context))return;
      data=await api(path,{},'json',context);
    }
  }catch(error){if(scope===insightScope&&currentContext(context)){const data={status:'failed',started:true,summary:'分析暂时不可用，请检查连接后重试。'};insightCards.set(source,data);paintAssetInsight(source,data);}}
}
async function refreshAssetInsightEvidence(sources){
  if(!state.connected||!Array.isArray(sources))return;
  const context=connectionContext(),scope=insightScope;
  for(const source of new Set(sources)){
    const existing=insightCards.get(source);
    // In-flight cards already poll; finished cards need a read-only refresh when
    // the production publishes new evidence. Never start another GPU analysis.
    if(!existing||['pending','analyzing'].includes(existing.status)||existing.evidenceRefreshing)continue;
    existing.evidenceRefreshing=true;
    try{
      const data=await api('/api/assets/insights?source='+encodeURIComponent(source),{},'json',context);
      if(scope!==insightScope||!currentContext(context))return;
      insightCards.set(source,{...data,started:true});paintAssetInsight(source,data);
    }catch{}finally{existing.evidenceRefreshing=false;}
  }
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-insight-retry]');if(!button)return;
  button.disabled=true;const source=button.dataset.insightRetry,context=connectionContext();
  try{await api('/api/assets/insights?retry=true&source='+encodeURIComponent(source),{method:'POST'},'json',context);if(!currentContext(context))return;insightCards.set(source,{status:'pending',started:false});hydrateAssetInsights();}
  catch{button.disabled=false;toast('未能重新开始分析，请稍后重试。');}
});
