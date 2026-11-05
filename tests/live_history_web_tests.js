"use strict";
// Actual shipped controller and rendered DOM, with deterministic HTTP/time.
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
class Element {
  constructor() { this.children=[];this.style={};this.dataset={};this.listeners={};this.attributes={};this.classes=new Set();this.value="";this.textContent="";this.disabled=false;
    this.classList={add:v=>this.classes.add(v),toggle:(v,b)=>b?this.classes.add(v):this.classes.delete(v)}; }
  append(...x){this.children.push(...x);} replaceChildren(...x){this.children=x.flatMap(v=>v.fragment?v.children:[v]);}
  setAttribute(k,v){this.attributes[k]=v;} addEventListener(k,v){this.listeners[k]=v;} querySelector(){return null;} showModal(){this.open=true;} close(){this.open=false;}
}
const elements=new Map(), byId=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const events={}, pending=[], calls=[]; let now=1000;
const sandbox={document:{getElementById:byId,querySelector:()=>({content:"fixture"}),createElement:()=>new Element(),createDocumentFragment:()=>Object.assign(new Element(),{fragment:true}),activeElement:null,body:new Element()},console,
  performance:{now:()=>now},setTimeout:()=>{},clearTimeout:()=>{},setInterval:()=>1,clearInterval:()=>{},URL:{},AbortController,
  window:{addEventListener:(key,value)=>{events[key]=value;}},fetch:(url,options)=>new Promise(resolve=>{const c={url,body:options.body?JSON.parse(options.body):null,resolve};pending.push(c);calls.push(c);})};
for(const [id,value] of [["human-side","white"],["boss-mode","normal"],["rapid-time-control","10+0"],["think-time","1000"]])byId(id).value=value;
const source=fs.readFileSync(path.join(__dirname,"../web/human-play/app.js"),"utf8");
vm.runInNewContext(source.replace(/\}\)\(\);\s*$/,"globalThis.__probe={applyState,renderClockAndAdvice,engineMove,state:()=>state,displayedState,browsingHistory};\n})();"),sandbox);
const probe=sandbox.__probe;
const flush=async()=>{for(let i=0;i<18;i++)await Promise.resolve();};
async function reply(url,value){const index=pending.findIndex(c=>c.url===url);assert.ok(index>=0,url);const [c]=pending.splice(index,1);c.resolve({ok:true,json:async()=>structuredClone(value)});await flush();}
function history(game="history-game",revision=2){
  const first={game_id:game,revision,fen:"4k3/4p3/8/8/8/8/4P3/4K3 w - - 0 1",board:{e1:"K",e8:"k",e2:"P",e7:"p"},turn:"white",human_color:"white",human_to_move:false,engine_to_move:false,legal_moves:[],in_check:false,last_move:null};
  const second=structuredClone(first);delete second.board.e2;second.board.e4="P";second.turn="black";second.last_move="e2e4";second.fen="4k3/4p3/8/8/4P3/8/8/4K3 b - - 0 1";
  const third=structuredClone(second);delete third.board.e7;third.board.e5="p";third.turn="white";third.last_move="e7e5";third.fen="4k3/8/8/4p3/4P3/8/8/4K3 w - - 0 2";
  return{game_id:game,revision,frames:[first,second,third],moves:[{ply:1,uci:"e2e4",san:"e4",actor:"human"},{ply:2,uci:"e7e5",san:"e5",actor:"engine"}]};
}
function state(detail=history()){
  return{...structuredClone(detail.frames.at(-1)),moves:structuredClone(detail.moves),human_to_move:true,legal_moves:["e1d1"],result:"*",error:null,status:"당신 차례입니다",can_undo:true,
    engine:{name:"History fixture",movetime_ms:1000,settings_revision:0,extra_budget_ms:0},
    clock:{enabled:true,control_id:"10+0",label:"10분",white_ms:600000,black_ms:600000,running_color:"white",status:"running",turn_elapsed_ms:0,sampled_monotonic_ms:1000}};
}
const clickSquare=square=>byId("board").listeners.click({target:{closest:()=>({dataset:{square}})}});
const square=id=>byId("board").children.find(child=>child.dataset.square===id);
(async()=>{
  const html=fs.readFileSync(path.join(__dirname,"../web/human-play/index.html"),"utf8");
  for(const id of ["live-history-prev","live-history-next","live-history-current","live-history-status"])assert.ok(html.includes(`id="${id}"`));
  const detail=history(), initial=state(detail);await reply("/api/state",initial);
  byId("live-history-prev").listeners.click();await reply("/api/history",detail);
  assert.equal(probe.browsingHistory(),true);assert.equal(byId("move-count").textContent,"1수");
  assert.equal(square("e7").children[0].textContent,"♟");assert.equal(square("e5").children.length,0);
  assert.ok(square("e4").disabled);assert.equal(JSON.stringify(probe.state()),JSON.stringify(initial));
  const before=calls.length;clickSquare("e4");clickSquare("e5");
  byId("new-game-form").listeners.submit({preventDefault(){}});
  assert.equal(calls.length,before,"historical board/form cannot submit actual game mutations");
  now+=2200;probe.renderClockAndAdvice();assert.equal(byId("human-clock").textContent,"▶ 9:58","actual clock continues in historical view");
  assert.match(byId("status-detail").textContent,/대국과 시계는 계속/);assert.match(byId("evaluation-label").textContent,/과거 위치/);
  const next=structuredClone(initial);next.revision=3;next.moves.push({ply:3,uci:"e1d1",san:"Kd1",actor:"human"});
  delete next.board.e1;next.board.d1="K";next.fen="4k3/8/8/4p3/4P3/8/8/3K4 b - - 1 2";
  probe.applyState(next);assert.equal(probe.displayedState().moves.length,1,"incoming move preserves selected historical ply");
  byId("live-history-next").listeners.click();await flush();assert.equal(byId("move-count").textContent,"2수");
  byId("live-history-next").listeners.click();await flush();assert.equal(probe.browsingHistory(),false);assert.equal(byId("move-count").textContent,"3수");
  const thinking=structuredClone(next);thinking.human_to_move=false;thinking.engine_to_move=true;probe.applyState(thinking);
  void probe.engineMove();await flush();assert.ok(pending.some(call=>call.url==="/api/engine"));
  byId("live-history-prev").listeners.click();await flush();assert.equal(probe.displayedState().moves.length,2,"navigation remains available during an engine POST");
  const done=structuredClone(thinking);done.revision=4;done.engine_to_move=false;done.human_to_move=true;
  done.moves.push({ply:4,uci:"e8d8",san:"Kd8",actor:"engine"});delete done.board.e8;done.board.d8="k";
  done.fen="3k4/8/8/4p3/4P3/8/8/3K4 w - - 2 3";await reply("/api/engine",done);
  assert.equal(probe.displayedState().moves.length,2,"engine finishes normally without replacing an old board");
  byId("live-history-current").listeners.click();assert.equal(probe.browsingHistory(),false);assert.equal(probe.displayedState().fen,done.fen);
  const changed=state(history("replacement",5));probe.applyState(changed);
  byId("live-history-prev").listeners.click();await flush();assert.ok(pending.some(call=>call.url==="/api/history"));
  const reset=state(history("new-game",6));reset.moves=[];probe.applyState(reset);
  await reply("/api/history",history("replacement",5));assert.equal(probe.browsingHistory(),false);assert.equal(probe.state().game_id,"new-game");
  const undoBase=state(history("undo-game",7));probe.applyState(undoBase);
  byId("live-history-prev").listeners.click();await reply("/api/history",history("undo-game",7));
  const undo=structuredClone(undoBase);undo.revision=8;undo.moves=undo.moves.slice(0,1);probe.applyState(undo);
  assert.equal(probe.browsingHistory(),false,"undo truncation returns safely to the actual position");
  let prevented=false;events.keydown({key:"ArrowLeft",target:{tagName:"INPUT"},preventDefault(){prevented=true;}});assert.equal(prevented,false,"typing fields keep their arrow keys");
  console.log("Live-history browser checks: PASS (read-only past boards, live clocks/search, engine completion, return, undo/new-game/stale-response guards, keyboard focus)");
})().catch(error=>{console.error(error);process.exitCode=1;});
