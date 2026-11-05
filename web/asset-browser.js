'use strict';

// Browsing snapshots never prove that a selected source is still renderable.
const assetBrowsers=Object.fromEntries(['main','replacement'].map(name=>[name,{name,query:'',limit:50,assets:[],total:null,next:null,snapshot:null,snapshotAt:null,expiresAt:null,scope:'',autoIdentity:null,draftContext:'',status:'idle',message:'',initialized:false,busy:false,sequence:0,timer:null,controller:null}]));
const creatorBrowsedAssets=new Map();
function assetSearchSupported(){return state.config.asset_search_supported===true;}
function assetBrowserScope(){return JSON.stringify([state.base,state.token,state.epoch]);}
function assetBrowserDraftContext(){return typeof creatorResolutionDraftContext==='function'?creatorResolutionDraftContext(creatorResolutionInputs()):'';}
function assetBrowserActive(session){return session.name==='main'?!!$('#page-create')?.classList.contains('active')&&(typeof productionMode!=='function'||productionMode()!=='talking_head'):!!(typeof creatorReplacement!=='undefined'&&creatorReplacement.draft&&!creatorReplacement.draft.invalid&&$('#creatorReplacementDialog')?.open);}
function cancelAssetBrowser(session,message=''){
  const pending=session.busy||session.timer;clearTimeout(session.timer);session.timer=null;session.controller?.abort();session.controller=null;session.sequence++;session.busy=false;
  if(pending){session.status=session.assets.length?'ready':'paused';session.message=message||'本次读取已停止，可刷新当前搜索。';}
}
function resetAssetBrowser(session,query){
  cancelAssetBrowser(session);session.query=String(query||'').trim().slice(0,100);session.assets=[];session.total=null;session.next=null;session.snapshot=null;session.snapshotAt=null;session.expiresAt=null;session.status='idle';session.message='';session.initialized=false;session.scope=assetBrowserScope();session.draftContext=assetBrowserDraftContext();
}
function syncAssetBrowsers(){
  const scope=assetBrowserScope(),draftContext=assetBrowserDraftContext();
  for(const session of Object.values(assetBrowsers)){
    if(session.scope!==scope){resetAssetBrowser(session,session.name==='main'?$('#creatorAssetSearch')?.value:session.query);if(session.autoIdentity===JSON.stringify([state.base,state.token])){session.initialized=true;session.status='paused';session.message='连接已更新，请刷新当前搜索。已选素材保留。';}}
    if(session.draftContext!==draftContext){cancelAssetBrowser(session,'草稿已变化，本次读取已停止；已加载页面保留。');session.draftContext=draftContext;}
    if(!assetSearchSupported()||!state.connected||!assetBrowserActive(session)){if(session.busy||session.timer)cancelAssetBrowser(session);continue;}
    if(session.snapshot&&Date.parse(session.expiresAt)<=Date.now()){if(session.busy||session.timer)cancelAssetBrowser(session);session.status='stale';session.message='这轮搜索已过期，请刷新搜索后继续。已选素材保持不变。';}
    if(!session.initialized&&session.status==='idle'){session.initialized=true;session.autoIdentity=JSON.stringify([state.base,state.token]);session.status='scheduled';session.timer=setTimeout(()=>{session.timer=null;requestAssetBrowser(session,false);},300);}
  }
}
function assetBrowserPageChanged(page){if(page!=='create')cancelAssetBrowser(assetBrowsers.main);}
function changeAssetBrowserQuery(session,query){resetAssetBrowser(session,query);syncAssetBrowsers();renderAssetBrowserOwner(session);}
function assetBrowserCurrent(session){return assetSearchSupported()&&state.connected&&session.scope===assetBrowserScope()&&assetBrowserActive(session);}
function assetBrowserCanChoose(session){return assetBrowserCurrent(session)&&session.status==='ready'&&!session.busy&&!!session.snapshot&&Date.parse(session.expiresAt)>Date.now();}
function validBrowserAsset(asset){return !!asset&&typeof asset.source==='string'&&asset.source.length>0&&asset.source.length<=4096&&typeof asset.name==='string'&&(asset.display_name==null||typeof asset.display_name==='string')&&Number.isFinite(asset.size)&&asset.size>=0;}
function validateAssetSearchResponse(data,session,more){
  if(!data||!Array.isArray(data.assets)||data.assets.length>session.limit||!data.assets.every(validBrowserAsset)||data.query!==session.query||data.limit!==session.limit||!Number.isInteger(data.total)||data.total<0||data.complete!==true||typeof data.has_more!=='boolean'||!(/^[a-f0-9]{32}$/).test(data.snapshot_id)||!Number.isFinite(Date.parse(data.snapshot_at))||!Number.isFinite(Date.parse(data.expires_at))||Date.parse(data.expires_at)<=Date.parse(data.snapshot_at))throw new Error('素材搜索结果不完整，请刷新当前搜索。');
  if(data.has_more?(typeof data.next_cursor!=='string'||!data.next_cursor||data.next_cursor.length>2048):data.next_cursor!==null)throw new Error('素材分页信息不完整，请刷新当前搜索。');
  if(more&&(data.snapshot_id!==session.snapshot||data.total!==session.total||data.snapshot_at!==session.snapshotAt||data.expires_at!==session.expiresAt||data.next_cursor===session.next))throw new Error('搜索快照已变化，请刷新后重新浏览。');
  const sources=new Set(more?session.assets.map(asset=>asset.source):[]);for(const asset of data.assets){if(sources.has(asset.source))throw new Error('素材分页出现重复记录，请刷新当前搜索。');sources.add(asset.source);}
  if(sources.size>data.total||(!data.has_more&&sources.size!==data.total)||data.has_more&&!data.assets.length)throw new Error('素材分页数量不完整，请刷新当前搜索。');
}
async function requestAssetBrowser(session,more=false){
  if(!assetBrowserCurrent(session)||session.busy)return;
  if(more&&(!assetBrowserCanChoose(session)||!session.next))return;
  clearTimeout(session.timer);session.timer=null;session.initialized=true;session.autoIdentity=JSON.stringify([state.base,state.token]);
  const sequence=++session.sequence,context=connectionContext(),draftContext=assetBrowserDraftContext(),controller=new AbortController();
  const query=session.query,cursor=more?session.next:null;
  session.controller=controller;session.busy=true;session.status='loading';session.message='';renderAssetBrowserOwner(session);
  const active=()=>session.sequence===sequence&&session.query===query&&assetBrowserCurrent(session)&&currentContext(context)&&assetBrowserDraftContext()===draftContext;
  try{
    const params=new URLSearchParams({q:query,limit:String(session.limit)});if(cursor)params.set('cursor',cursor);
    const data=await api('/api/assets/search?'+params.toString(),{signal:controller.signal},'json',context);
    if(!active())return;validateAssetSearchResponse(data,session,more);
    session.assets=more?[...session.assets,...data.assets]:data.assets;session.total=data.total;session.next=data.next_cursor;session.snapshot=data.snapshot_id;session.snapshotAt=data.snapshot_at;session.expiresAt=data.expires_at;session.status='ready';
  }catch(error){if(active()){session.status=[400,409].includes(error.httpStatus)?'stale':'error';session.message=session.status==='stale'?'目录已变化或这轮搜索已过期，请刷新当前搜索。已选素材保持不变。':'素材搜索暂未完成，请刷新重试。已选素材保持不变。';}}
  finally{if(session.sequence===sequence){session.busy=false;session.controller=null;if(session.status==='loading'){session.status=session.assets.length?'ready':'paused';session.message='本次读取已停止，请刷新当前搜索。';}renderAssetBrowserOwner(session);}}
}
function refreshAssetBrowser(session){resetAssetBrowser(session,session.name==='main'?$('#creatorAssetSearch').value:$('#creatorReplacementSearch')?.value||session.query);session.initialized=true;requestAssetBrowser(session,false);renderAssetBrowserOwner(session);}
function renderAssetBrowserOwner(session){if(session.name==='main'){if(typeof renderCreatorMaterials==='function')renderCreatorMaterials();}else if(typeof renderCreatorReplacement==='function')renderCreatorReplacement();}
function assetBrowserItems(session,pool){
  return session.assets.map(asset=>{const known=pool.find(item=>item.key==='s:'+asset.source);return {...(known||{key:'s:'+asset.source,source:asset.source,name:asset.display_name||asset.name,size:asset.size,ready:false,status:'搜索结果 · 选入后核对',availability:'pending_catalog'}),searchAsset:asset};});
}
function renderAssetBrowserControls(session,status,more){
  if(!status||!more)return;
  if(!assetSearchSupported()){status.textContent='仅查找当前已加载的素材；这台工作台尚未支持全库名称搜索。';more.hidden=true;return;}
  status.textContent=!state.connected?'连接待恢复，已选素材保留。':session.busy?'正在读取全库名称搜索结果…':session.message||(!session.snapshot?'正在准备全库名称搜索…':'已显示 '+session.assets.length+' / '+session.total+' 段'+(session.query?' · “'+session.query+'”':' · 全部素材')+'。搜索结果用于浏览，选入后会核对素材。');
  more.hidden=!session.next;more.disabled=!assetBrowserCanChoose(session)||!session.next;more.textContent=session.busy?'正在加载…':'加载更多素材';
}
function ensureCreatorSearchControls(){
  const input=$('#creatorAssetSearch');input.maxLength=100;
  if($('#creatorSearchStatus'))return;
  const controls=document.createElement('div');controls.className='creator-search-controls';controls.innerHTML='<p id="creatorSearchStatus" class="help" role="status" aria-live="polite"></p><button type="button" class="button subtle" id="creatorSearchMore" hidden>加载更多素材</button>';
  $('#creatorAssetGrid').after(controls);$('#creatorSearchMore').onclick=()=>requestAssetBrowser(assetBrowsers.main,true);
}
function rememberCreatorBrowsedAsset(asset){if(validBrowserAsset(asset))creatorBrowsedAssets.set(asset.source,{asset,scope:assetBrowserScope()});}
function creatorSelectedBrowseAssets(){return creator.selected.filter(key=>key.startsWith('s:')).map(key=>creatorBrowsedAssets.get(key.slice(2))).filter(entry=>entry?.scope===assetBrowserScope()).map(entry=>entry.asset);}
function selectCreatorSearchResult(source,on){
  const session=assetBrowsers.main,asset=session.assets.find(item=>item.source===source);
  if(creatorInputLocked())return;
  if(on&&(!assetBrowserCanChoose(session)||!asset)){creatorFeedback('搜索结果待确认，请刷新当前搜索后再选入。',true);renderCreatorMaterials();return;}
  if(on)rememberCreatorBrowsedAsset(asset);toggleCreatorMaterial('s:'+source,on);
}
