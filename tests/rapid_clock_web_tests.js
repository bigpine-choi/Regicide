"use strict";
// Shipped client, fake transport/monotonic time: presentation and concurrency only.
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
class Element {
  constructor() { this.children=[];this.style={};this.dataset={};this.listeners={};this.attributes={};this.classes=new Set();this.value="";this.textContent="";this.disabled=false;
    this.classList={add:v=>this.classes.add(v),toggle:(v,b)=>b?this.classes.add(v):this.classes.delete(v)}; }
  append(...x){this.children.push(...x);} replaceChildren(...x){this.children=x.flatMap(v=>v.fragment?v.children:[v]);}
  setAttribute(k,v){this.attributes[k]=v;} addEventListener(k,v){this.listeners[k]=v;} querySelector(){return null;} showModal(){this.open=true;} close(){this.open=false;}
}
const elements=new Map(), byId=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const events={}, intervals=[], pending=[], calls=[];let now=1000;
const sandbox={document:{getElementById:byId,querySelector:()=>({content:"fixture"}),createElement:()=>new Element(),createDocumentFragment:()=>Object.assign(new Element(),{fragment:true}),activeElement:null,body:new Element()},console,
  performance:{now:()=>now}, setTimeout:()=>{},clearTimeout:()=>{},setInterval:(callback,ms)=>{intervals.push({callback,ms});return intervals.length;},clearInterval:()=>{},URL:{},AbortController,
  window:{addEventListener:(k,v)=>{events[k]=v;}},fetch:(url,options)=>new Promise(resolve=>{const c={url,body:options.body?JSON.parse(options.body):null,resolve};pending.push(c);calls.push(c);})};
for(const [id,value] of [["human-side","white"],["boss-mode","normal"],["rapid-time-control","10+0"],["think-time","1000"]])byId(id).value=value;
const source=fs.readFileSync(path.join(__dirname,"../web/human-play/app.js"),"utf8");
vm.runInNewContext(source.replace(/\}\)\(\);\s*$/,"globalThis.__probe = {applyState, renderClockAndAdvice, pollClock, clockText, state:()=>state};\n})();"),sandbox);
const probe=sandbox.__probe;
function fixture(revision=1,sample=1000){return {game_id:"clock-game",revision,fen:"4k3/8/8/8/8/8/4P3/4K3 w - - 0 1",board:{e1:"K",e2:"P",e8:"k"},human_color:"white",human_to_move:true,engine_to_move:false,legal_moves:["e2e4"],moves:[],last_move:null,result:"*",status:"내 차례",can_undo:false,in_check:false,error:null,
 engine:{name:"Regicide fixture",movetime_ms:1000,settings_revision:0,extra_budget_ms:0},clock:{enabled:true,control_id:"10+5",label:"10+5 래피드",white_ms:600000,black_ms:599000,running_color:"white",status:"running",turn_elapsed_ms:0,sampled_monotonic_ms:sample},
 decision_support:{status:"ready",complexity:{score:67,label:"높음",reasons:["기물 접촉이 많습니다."]},recommendation:{status:"ready",total_ms:30000,remaining_ms:30000,spent_ms:0,reason:"남은 시간을 보존하면서 위협을 확인하세요.",urgency:"normal"}}};}
async function flush(){for(let i=0;i<15;i++)await Promise.resolve();}
async function reply(url,value){const i=pending.findIndex(c=>c.url===url);assert.ok(i>=0,url);const [c]=pending.splice(i,1);c.resolve({ok:true,json:async()=>structuredClone(value)});await flush();return c;}
(async()=>{
 const html=fs.readFileSync(path.join(__dirname,"../web/human-play/index.html"),"utf8"),select=html.match(/<select id="rapid-time-control"[\s\S]*?<\/select>/)[0];
 assert.deepEqual([...select.matchAll(/option value="([^"]+)"/g)].map(m=>m[1]),["10+0","10+5","15+10"]);assert.match(select,/value="10\+0" selected/);
 await reply("/api/state",fixture());
 assert.equal(byId("human-clock").textContent,"▶ 10:00");assert.equal(byId("opponent-clock").textContent,"9:59");assert.match(byId("human-complexity").textContent,/높음 67\/100 · 실험/);
 const nodes=byId("board").children;
 byId("board").listeners.click({target:{closest:()=>({dataset:{square:"e2"}})}});
 const selectedNodes=byId("board").children;
 void probe.pollClock();const fresh=fixture(1,1100);fresh.clock.white_ms=599900;fresh.clock.turn_elapsed_ms=100;fresh.decision_support.recommendation.remaining_ms=29900;await reply("/api/state",fresh);
 assert.equal(byId("board").children,selectedNodes,"same-position poll preserves rendered squares/drag targets");
 assert.ok(byId("board").children.some(c=>c.dataset.square==="e2"&&c.classes.has("selected")),"timer poll preserves selection");
 now+=2200;probe.renderClockAndAdvice();assert.equal(byId("human-clock").textContent,"▶ 9:58");assert.equal(byId("opponent-clock").textContent,"9:59");assert.match(byId("recommended-time").textContent,/총 30초 권장 · 앞으로 28초/);
 const stale=fixture(1,999);stale.clock.white_ms=700000;probe.applyState(stale,true);assert.equal(byId("human-clock").textContent,"▶ 9:58","old same-revision packet never restores time");
 const duplicate=fixture();probe.applyState(duplicate,true);assert.equal(byId("human-clock").textContent,"▶ 9:58","identical timestamp does not reset countdown");
 now=4000;probe.renderClockAndAdvice();
 assert.equal(byId("human-clock").textContent,"▶ 9:57");
 void probe.pollClock();const delayed=fixture(1,2000);delayed.clock.white_ms=599000;delayed.clock.turn_elapsed_ms=1000;delayed.decision_support.recommendation.remaining_ms=29000;await reply("/api/state",delayed);
 assert.equal(byId("human-clock").textContent,"▶ 9:57","delayed fresher snapshot never puts elapsed time back");
 assert.match(byId("recommended-time").textContent,/앞으로 27초/,"delayed advice preserves elapsed recommendation time");
 const moved=fixture(2,3300);moved.clock={...moved.clock,white_ms:602000,black_ms:599000,running_color:"black"};moved.human_to_move=false;moved.decision_support.status="inactive";probe.applyState(moved);assert.equal(byId("human-clock").textContent,"10:02","increment comes only from server");
 now+=2100;probe.renderClockAndAdvice();assert.equal(byId("human-clock").textContent,"10:02");assert.equal(byId("opponent-clock").textContent,"▶ 9:57");assert.match(byId("human-complexity").textContent,/상대 차례/);
 void probe.pollClock();const opponentRefresh=structuredClone(moved);opponentRefresh.clock.sampled_monotonic_ms=5600;opponentRefresh.decision_support=null;await reply("/api/state",opponentRefresh);assert.match(byId("human-complexity").textContent,/상대 차례/);
 const paused=fixture(3,6000);paused.clock.status="paused";paused.clock.running_color=null;paused.clock.white_ms=5000;paused.decision_support.recommendation={status:"inactive",total_ms:null,remaining_ms:null,reason:"시계 일시 정지"};probe.applyState(paused);now+=5000;probe.renderClockAndAdvice();assert.equal(byId("human-clock").textContent,"0:05.0");assert.match(byId("recommended-time").textContent,/일시 정지/);
 const low=fixture(4,12000);low.clock.white_ms=700;low.decision_support.recommendation.remaining_ms=400;probe.applyState(low);now+=1200;probe.renderClockAndAdvice();assert.equal(byId("human-clock").textContent,"▶ 0:00");assert.equal(probe.state().result,"*","client never invents flagfall result");assert.match(byId("recommended-time").textContent,/이제 결정/);
 events["regicide-analysis-mode"]({detail:true});assert.equal(byId("decision-support").hidden,true);void probe.pollClock();assert.equal(pending.at(-1).url,"/api/state","analysis still polls timeout");const ended=fixture(5,15000);ended.result="0-1";ended.clock.status="finished";ended.clock.running_color=null;ended.clock.white_ms=0;await reply("/api/state",ended);assert.equal(probe.state().result,"0-1");
 events["regicide-analysis-mode"]({detail:false});assert.match(byId("human-complexity").textContent,/대국 종료/);
 const legacy=fixture(6,16000);delete legacy.clock;delete legacy.decision_support;probe.applyState(legacy);assert.match(byId("human-clock").textContent,/새 대국에서 시간 선택/);assert.equal(byId("opponent-clock").hidden,true);const before=calls.length;await probe.pollClock();assert.equal(calls.length,before,"untimed legacy has no clock polling");
 byId("rapid-time-control").value="15+10";byId("rapid-time-control").listeners.change();assert.match(byId("rapid-time-help").textContent,/15분.*10초/);
 byId("new-game-form").listeners.submit({preventDefault(){}});assert.equal(pending.at(-1).body.time_control,"15+10");const next=fixture(7,17000);next.game_id="new-game";await reply("/api/new",next);assert.equal(byId("rapid-time-control").value,"15+10","draft survives state renders");
 assert.equal(probe.clockText(-300),"0:00");assert.equal(probe.clockText(NaN),"—");
 console.log("Rapid clock browser checks: PASS (controls, live clock, stale/duplicate packets, increments, pause, flags, background poll, advice, legacy)");
})().catch(e=>{console.error(e);process.exitCode=1;});
