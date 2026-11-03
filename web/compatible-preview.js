'use strict';
// Compatibility is a separate viewing copy. The original MP4 remains the delivery file.
const compatibilityPreview={request:0,timer:null,busy:false,mode:'original',original:null,failed:false};
function resetCompatibilityPreview(){
  compatibilityPreview.request++;clearTimeout(compatibilityPreview.timer);
  Object.assign(compatibilityPreview,{timer:null,busy:false,mode:'original',original:null,failed:false});
  $('#compatibilityStatus').textContent='';updateCompatibilityControls();
}
function updateCompatibilityControls(){
  const available=state.config.preview_supported===true&&canStreamMedia()&&state.selected?.has_video;
  $('#compatibilityControls').hidden=!available;
  $('#reloadVideoButton').disabled=!state.videoSource||!state.connected;
  const direct=$('#standaloneVideoLink');direct.hidden=!state.videoSource;
  if(state.videoSource)direct.href=state.videoSource;
  $('#compatibilityButton').disabled=!state.connected||compatibilityPreview.busy||compatibilityPreview.failed;
  $('#compatibilityButton').textContent=compatibilityPreview.busy?'正在准备兼容播放…':compatibilityPreview.mode==='webm'?'切回原画质':'画面空白或无声？切换兼容播放';
}
function switchPreviewSource(url,mode,id,restorePosition){
  const video=$('#reviewVideo'),position=Number.isFinite(restorePosition)?restorePosition:Number.isFinite(video.currentTime)?video.currentTime:0;
  video.pause();state.videoSource=url;compatibilityPreview.mode=mode;
  setMediaSource(video,$('#reviewVideoStatus'),url,()=>state.selected?.id===id&&$('#reviewDialog').open,
    mode==='webm'?'已切换兼容预览，点击播放检查画面和声音。下载仍为原画质 MP4。':'已切回原画质，点击播放检查画面和声音。');
  const restore=()=>{if(state.selected?.id===id&&video.src===url&&Number.isFinite(video.duration))video.currentTime=position>=video.duration-.1?0:Math.min(position,video.duration);video.removeEventListener('loadedmetadata',restore);};
  video.addEventListener('loadedmetadata',restore);
  updateCompatibilityControls();
}
async function requestCompatibilityPreview(){
  const job=state.selected;
  if(!job?.has_video||!state.connected||!canStreamMedia()||compatibilityPreview.busy||compatibilityPreview.failed)return;
  if(compatibilityPreview.mode==='webm'){
    switchPreviewSource(compatibilityPreview.original||mediaAddress(job.video_url),'original',job.id);
    $('#compatibilityStatus').textContent='原画质播放；兼容预览已保留，下次切换无需重新生成。';return;
  }
  const request=++compatibilityPreview.request,context=connectionContext(),id=job.id;
  const valid=()=>request===compatibilityPreview.request&&currentContext(context)&&state.selected?.id===id&&$('#reviewDialog').open;
  compatibilityPreview.original=state.videoSource||mediaAddress(job.video_url);
  compatibilityPreview.busy=true;updateCompatibilityControls();
  $('#compatibilityStatus').textContent='正在准备适合浏览器的预览，原成片保持完整。';
  const read=async(method='GET')=>{
    try{
      const result=await api('/api/jobs/'+encodeURIComponent(id)+'/preview',{method},'json',context);
      if(!valid())return;
      if(result.status==='ready'){
        if(result.video_url!=='/api/jobs/'+id+'/video?preview=webm')throw new Error('兼容预览地址不正确。');
        compatibilityPreview.busy=false;switchPreviewSource(mediaAddress(result.video_url),'webm',id);
        $('#compatibilityStatus').textContent='兼容预览已就绪。请点击播放；下载和企业微信交付仍使用原画质 MP4。';
      }else if(['queued','rendering'].includes(result.status)){
        $('#compatibilityStatus').textContent=result.status==='queued'?'兼容预览已排队，准备好后会自动切换。':'正在生成兼容预览，完成后会自动切换。';
        compatibilityPreview.timer=setTimeout(()=>read(),2000);
      }else{
        compatibilityPreview.busy=false;compatibilityPreview.failed=true;
        $('#compatibilityStatus').textContent=result.error||'兼容预览未完成。可以下载原成片播放；原剪辑任务不受影响。';updateCompatibilityControls();
      }
    }catch(error){
      if(request===compatibilityPreview.request&&state.selected?.id===id&&$('#reviewDialog').open){
        compatibilityPreview.busy=false;$('#compatibilityStatus').textContent='兼容预览状态暂时无法读取。恢复连接后可重新查看，不会重复剪辑或发布。';updateCompatibilityControls();reportError(error);
      }
    }
  };
  return read('POST');
}
$('#compatibilityButton').onclick=requestCompatibilityPreview;
$('#reloadVideoButton').onclick=()=>{
  const j=state.selected,url=state.videoSource;
  if(!j?.has_video||!url||!state.connected)return;
  const video=$('#reviewVideo'),position=video.currentTime;
  clearMediaElement(video);
  switchPreviewSource(url,compatibilityPreview.mode,j.id,position);
  $('#compatibilityStatus').textContent='已重新读取视频。请点击播放；仍然空白时可使用独立播放或兼容播放。';
};
$('#reviewDialog').addEventListener('close',resetCompatibilityPreview);
