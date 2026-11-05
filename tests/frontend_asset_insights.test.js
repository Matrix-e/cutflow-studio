'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
function setup(){
  const state={base:'local',token:'test',epoch:1,connected:true},calls=[],nodes=[];
  const h={state,calls,nodes,impl:async()=>({status:'ready',summary:'新原声摘录',summary_kind:'director_speech_excerpt',frames:[]})};
  const context=vm.createContext({state,Map,Set,JSON,Array,Number,Promise,setTimeout,
    esc:s=>String(s??'').replace(/[<>]/g,c=>c==='<'?'&lt;':'&gt;'),
    document:{querySelectorAll:()=>nodes,addEventListener(){}},
    connectionContext:()=>({...state}),currentContext:c=>c.epoch===state.epoch,
    api:async(route,options)=>{calls.push({route,options});return h.impl(route,options);}
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/asset-insights.js'),'utf8')+'\nglobalThis.app={insightCards,refreshAssetInsightEvidence};',context);h.app=context.app;return h;
}
test('completed material evidence refresh is read-only and deduplicated',async()=>{
  const h=setup();h.app.insightCards.set('a.mp4',{status:'ready',summary:'旧摘要',started:true});
  await h.app.refreshAssetInsightEvidence(['a.mp4','a.mp4']);
  assert.equal(h.calls.length,1);assert.deepEqual(Object.keys(h.calls[0].options),[]);
  assert.equal(h.app.insightCards.get('a.mp4').summary,'新原声摘录');
});
test('inflight card already polls and does not launch duplicate requests',async()=>{
  const h=setup();h.app.insightCards.set('a.mp4',{status:'analyzing',started:true});
  await h.app.refreshAssetInsightEvidence(['a.mp4','unknown.mp4']);assert.equal(h.calls.length,0);
});
test('old connection response cannot replace cards from another service',async()=>{
  const h=setup();h.app.insightCards.set('a.mp4',{status:'ready',summary:'原记录',started:true});
  let resolve;h.impl=()=>new Promise(r=>resolve=r);const running=h.app.refreshAssetInsightEvidence(['a.mp4']);
  h.state.epoch++;resolve({status:'ready',summary:'过期记录'});await running;
  assert.equal(h.app.insightCards.get('a.mp4').summary,'原记录');
});
test('temporary evidence read failure retains previous frame and summary',async()=>{
  const h=setup();const prior={status:'ready',summary:'保留记录',frames:[{time:1}],started:true};h.app.insightCards.set('a.mp4',prior);
  h.impl=async()=>{throw new Error('offline');};await h.app.refreshAssetInsightEvidence(['a.mp4']);
  assert.equal(h.app.insightCards.get('a.mp4'),prior);assert.equal(prior.evidenceRefreshing,false);
});
