"use strict";

// Presentation only. Unlock on a user gesture, then play only sounds whose
// buffers are already ready. An old move must never be queued for later.
(() => {
  let context=null, loading=false, enabled=true;
  const buffers={}, active=new Set();
  const bytes=Object.fromEntries(["move","capture"].map((kind)=>[kind,
    fetch("/audio/"+kind+".wav").then((response)=>response.ok ? response.arrayBuffer() : null).catch(()=>null)
  ]));

  function unlock() {
    if(document.hidden || !enabled)return;
    try {
      const AudioContext=window.AudioContext || window.webkitAudioContext;
      if(!AudioContext)return;
      if(!context)context=new AudioContext();
      if(context.state==="suspended")context.resume().catch(()=>{});
      if(!loading) {
        loading=true;
        for(const kind of ["move","capture"])bytes[kind].then((data)=>data && context.decodeAudioData(data))
          .then((buffer)=>{if(buffer)buffers[kind]=buffer;}).catch(()=>{});
      }
    } catch (_) { /* Audio support never gates chess input. */ }
  }

  function stop() {
    for(const source of active) { try { source.stop(); } catch (_) {} }
    active.clear();
  }

  function play(kind) {
    if(!enabled || document.hidden || context?.state!=="running" || !buffers[kind])return false;
    try {
      const source=context.createBufferSource(), gain=context.createGain();
      source.buffer=buffers[kind];gain.gain.value=0.4;
      source.connect(gain);gain.connect(context.destination);
      source.onended=()=>{active.delete(source);source.disconnect();gain.disconnect();};
      active.add(source);source.start();
      return true;
    } catch (_) { return false; }
  }

  document.addEventListener("pointerdown",unlock,{capture:true,passive:true});
  document.addEventListener("keydown",unlock,{capture:true});
  document.addEventListener("visibilitychange",()=>{if(document.hidden)stop();});
  window.addEventListener("pagehide",stop);
  window.RegicideMoveSound={play,stop,get enabled(){return enabled;},set enabled(value){enabled=Boolean(value);if(!enabled)stop();}};
})();
