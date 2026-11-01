"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const script=fs.readFileSync(path.join(__dirname,"../web/human-play/move_sound.js"),"utf8");
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
function environment({supported=true,blocked=false,missing=false}={}) {
  const listeners={},requests=[],contexts=[],sources=[],gains=[];
  class AudioContext {
    constructor(){this.state="suspended";this.destination={};this.resumes=0;contexts.push(this);}
    resume(){this.resumes++;if(blocked)return Promise.reject(new Error("autoplay blocked"));this.state="running";return Promise.resolve();}
    decodeAudioData(data){return Promise.resolve({kind:data});}
    createGain(){const gain={gain:{value:0},connect(){},disconnect(){this.disconnected=true;}};gains.push(gain);return gain;}
    createBufferSource(){const source={connect(){},disconnect(){this.disconnected=true;},start(){this.started=true;},stop(){this.stopped=true;this.onended?.();}};sources.push(source);return source;}
  }
  const context={fetch:(url)=>{requests.push(url);return missing ? Promise.reject(new Error("offline")) : Promise.resolve({ok:true,arrayBuffer:()=>Promise.resolve(url)});},
    document:{hidden:false,addEventListener:(name,fn)=>{listeners[name]=fn;}},
    window:{AudioContext:supported ? AudioContext : undefined,addEventListener:(name,fn)=>{listeners[name]=fn;}}};
  vm.runInNewContext(script,context);
  return {api:context.window.RegicideMoveSound,listeners,requests,contexts,sources,gains,document:context.document};
}
const cases=[];const test=(name,run)=>cases.push({name,run});
test("preload does not create an audio context or queue pre-gesture moves",async()=>{
  const ui=environment();await flush();assert.equal(ui.contexts.length,0);
  assert.deepEqual(ui.requests,["/audio/move.wav","/audio/capture.wav"]);assert.equal(ui.api.enabled,true);
  assert.equal(ui.api.play("move"),false);ui.listeners.pointerdown();await flush();
  assert.equal(ui.contexts.length,1);assert.equal(ui.sources.length,0);
  assert.equal(ui.api.play("move"),true);assert.equal(ui.sources.length,1);assert.equal(ui.sources[0].buffer.kind,"/audio/move.wav");
  assert.equal(ui.gains[0].gain.value,0.4);assert.equal(ui.sources[0].started,true);
});
test("keyboard unlock supports the independent capture buffer without speech APIs",async()=>{
  const ui=environment();ui.listeners.keydown();await flush();
  assert.equal(ui.api.play("capture"),true);assert.equal(ui.sources[0].buffer.kind,"/audio/capture.wav");
  assert.equal(ui.api.play("unknown"),false);ui.sources[0].onended();assert.equal(ui.sources[0].disconnected,true);assert.equal(ui.gains[0].disconnected,true);
});
test("loading, unsupported devices and autoplay rejection never queue or throw",async()=>{
  for(const options of [{supported:false},{blocked:true},{missing:true}]){
    const ui=environment(options);ui.listeners.pointerdown();assert.equal(ui.api.play("move"),false);await flush();
    assert.equal(ui.api.play("move"),false);assert.equal(ui.sources.length,0);
  }
  const ui=environment();ui.listeners.pointerdown();assert.equal(ui.api.play("move"),false);await flush();assert.equal(ui.sources.length,0);
});
test("suspension never resumes from a move callback or replays earlier moves",async()=>{
  const ui=environment();ui.listeners.pointerdown();await flush();ui.contexts[0].state="suspended";
  const resumes=ui.contexts[0].resumes;assert.equal(ui.api.play("move"),false);assert.equal(ui.contexts[0].resumes,resumes);
  ui.listeners.pointerdown();await flush();assert.equal(ui.sources.length,0);assert.equal(ui.api.play("move"),true);
});
test("hidden page, leaving and disabling stop effects without changing speech",async()=>{
  for(const action of ["hidden","pagehide","disabled"]){
    const ui=environment();ui.listeners.pointerdown();await flush();ui.api.play("move");
    if(action==="hidden"){ui.document.hidden=true;ui.listeners.visibilitychange();assert.equal(ui.api.play("capture"),false);}
    if(action==="pagehide")ui.listeners.pagehide();
    if(action==="disabled"){ui.api.enabled=false;assert.equal(ui.api.play("capture"),false);}
    assert.equal(ui.sources[0].stopped,true);
  }
});
test("effects script loads before shared motion in all board modes",()=>{
  const html=fs.readFileSync(path.join(__dirname,"../web/human-play/index.html"),"utf8");
  assert.ok(html.indexOf('src="/move_sound.js"')<html.indexOf('src="/board_motion.js"'));
});
(async()=>{for(const {name,run}of cases){await run();console.log("PASS "+name);}console.log("Browser move audio: "+cases.length+" checks passed.");})().catch((error)=>{console.error(error);process.exitCode=1;});
