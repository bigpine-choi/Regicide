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
vm.runInNewContext(source.replace(/\}\)\(\);\s*$/,"globalThis.__probe = {applyState, renderClockAndAdvice, pollClock, engineMove, clockText, state:()=>state};\n})();"),sandbox);
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
 const paced=fixture(8,20000);paced.game_id="new-game";paced.human_to_move=false;paced.engine_to_move=true;paced.clock.running_color="black";paced.decision_support=null;probe.applyState(paced);
 const postsBefore=calls.filter(c=>c.url==="/api/engine").length;void probe.engineMove();await flush();
 assert.equal(calls.filter(c=>c.url==="/api/engine").length,postsBefore+1);
 now+=46000;void probe.pollClock();const waiting=structuredClone(paced);waiting.clock.sampled_monotonic_ms=66000;waiting.clock.black_ms=553000;waiting.clock.turn_elapsed_ms=46000;await reply("/api/state",waiting);
 void probe.engineMove();await flush();assert.equal(calls.filter(c=>c.url==="/api/engine").length,postsBefore+1,"a long paced turn and state polls keep one engine POST");
 assert.ok(pending.some(c=>c.url==="/api/engine"),"engine POST remains pending beyond45s of simulated time");
 assert.equal(probe.state().revision,8,"state polls cannot invent an engine move");
 const pacedReply=fixture(9,67000);pacedReply.game_id="new-game";await reply("/api/engine",pacedReply);assert.equal(probe.state().revision,9,"original long request applies one completed turn");
 for(const [index,color] of ["white","black"].entries()) {
  const overtime=fixture(10+index*2,70000+index*10);overtime.game_id="new-game";overtime.human_color=color;overtime.turn=color;
  Object.assign(overtime.clock,{[color+"_ms"]:0,running_color:color,timeout_policy:"engine_only",soft_timeout_color:color,overtime_color:color});
  overtime.decision_support.recommendation={status:"inactive",total_ms:null,remaining_ms:null,reason:"연습 시간이 소진됐지만 계속 둘 수 있습니다."};
  const from=color==="white"?"e2":"e7",to=color==="white"?"e4":"e5";
  if(color==="black") {overtime.board={e1:"K",e7:"p",e8:"k"};overtime.fen="4k3/4p3/8/8/8/8/8/4K3 b - - 0 1";overtime.legal_moves=["e7e5"];}
  probe.applyState(overtime);assert.equal(byId("recommended-time").textContent,"내 시간 소진 · 계속 진행");assert.match(byId("human-complexity").textContent,/높음/);
  for(const square of [from,to])byId("board").listeners.click({target:{closest:()=>({dataset:{square}})}});
  assert.equal(pending.find(c=>c.url==="/api/move").body.move,from+to,"human can submit a move at zero as "+color);
  const incremented=structuredClone(overtime);incremented.revision++;incremented.human_to_move=false;incremented.decision_support=null;incremented.clock.running_color=color==="white"?"black":"white";incremented.clock[color+"_ms"]=5000;incremented.clock.overtime_color=null;incremented.clock.sampled_monotonic_ms++;
  await reply("/api/move",incremented);assert.equal(probe.state().result,"*");assert.doesNotMatch(byId("recommended-time").textContent,/내 시간 소진/);
 }
 // The desktop editor shares this host state with the web view. These are
 // presentation fixtures, not claims that the browser implements the editor.
 for (const [index, [control, color]] of ["10+0", "10+5", "15+10"].flatMap(control =>
   ["white", "black"].map(color => [control, color])).entries()) {
  now = 100000 + index * 100000;
  const opponent = color === "white" ? "black" : "white";
  const original = fixture(20 + index * 6, now);
  original.game_id = "handicap-clock-" + index;
  original.human_color = color; original.turn = color;
  original.engine.sparring_mode = true; original.handicap_mode = false;
  Object.assign(original.clock, {control_id: control, label: control + " 래피드", increment_ms: control === "10+0" ? 0 : control === "10+5" ? 5000 : 10000,
    [color + "_ms"]: 123000, [opponent + "_ms"]: 234000, running_color: color, editable: true});
  probe.applyState(original);
  const initialHuman = byId("human-clock").textContent, initialEngine = byId("opponent-clock").textContent;
  assert.equal(initialHuman, "▶ 2:03"); assert.equal(initialEngine, "3:54");
  const preview = structuredClone(original); preview.revision++; preview.handicap_mode = true;
  preview.human_to_move = false; preview.status = "상대 수 편집 중"; preview.decision_support = null;
  Object.assign(preview.clock, {status: "paused", running_color: null, editable: false, sampled_monotonic_ms: now + 1});
  probe.applyState(preview);
  now += 30000; probe.renderClockAndAdvice();
  assert.equal(byId("human-clock").textContent, "2:03", "preview keeps human time paused: " + control + "/" + color);
  assert.equal(byId("opponent-clock").textContent, initialEngine, "preview keeps engine time paused");
  assert.equal(byId("clock-adjust").hidden, true, "preview blocks independent time editing");
  const engineCalls = calls.filter(call => call.url === "/api/engine").length;
  await probe.engineMove();
  assert.equal(calls.filter(call => call.url === "/api/engine").length, engineCalls, "preview never restarts the engine");
  void probe.pollClock(); const previewPoll = structuredClone(preview); previewPoll.clock.sampled_monotonic_ms = now;
  await reply("/api/state", previewPoll);
  const cancelled = structuredClone(original); cancelled.revision += 2; cancelled.clock.sampled_monotonic_ms = ++now;
  probe.applyState(cancelled);
  assert.equal(byId("human-clock").textContent, initialHuman, "cancel restores the live side and balances");
  now += 2500; probe.renderClockAndAdvice();
  assert.equal(byId("human-clock").textContent, "▶ 2:01", "cancel resumes only the human clock");
  assert.equal(byId("opponent-clock").textContent, initialEngine);
  const secondPreview = structuredClone(preview); secondPreview.revision += 2;
  secondPreview.clock[color + "_ms"] = 120500; secondPreview.clock.sampled_monotonic_ms = ++now;
  probe.applyState(secondPreview); now += 30000;
  const replaced = structuredClone(cancelled); replaced.revision += 2;
  replaced.last_move = color === "white" ? "e8d8" : "e1d1";
  replaced.clock[color + "_ms"] = 120500; replaced.clock.sampled_monotonic_ms = ++now;
  probe.applyState(replaced);
  assert.equal(probe.state().clock.enabled, true, "replacement remains timed: " + control + "/" + color);
  assert.equal(probe.state().clock.control_id, control);
  assert.equal(probe.state().clock.increment_ms, original.clock.increment_ms, "replacement keeps the chosen increment");
  assert.equal(probe.state().clock[opponent + "_ms"], 234000, "replacement does not reset or add a second increment");
  assert.equal(probe.state().engine.sparring_mode, true);
  assert.equal(byId("think-time-field").hidden, true, "replacement keeps automatic sparring timing");
  assert.match(byId("opponent-detail").textContent, /생각시간 자동 조절/);
  assert.equal(byId("clock-adjust").hidden, false, "live time buttons return after replacement");
  now += 2000; probe.renderClockAndAdvice();
  assert.equal(byId("human-clock").textContent, "▶ 1:59", "human clock continues after replacement");
  assert.equal(byId("opponent-clock").textContent, initialEngine);
  probe.applyState(secondPreview, true);
  assert.equal(probe.state().handicap_mode, false, "late preview cannot pause the resumed game");
  assert.equal(byId("human-clock").textContent, "▶ 1:59");
  void probe.pollClock();
  const resumedPoll = structuredClone(replaced); resumedPoll.clock.sampled_monotonic_ms = now;
  resumedPoll.clock[color + "_ms"] = 118500; resumedPoll.clock.turn_elapsed_ms = 2000;
  await reply("/api/state", resumedPoll);
  assert.equal(byId("human-clock").textContent, "▶ 1:59", "replacement keeps normal clock polling");
 }
 assert.equal(probe.clockText(-300),"0:00");assert.equal(probe.clockText(NaN),"—");
 console.log("Rapid clock browser checks: PASS (controls, live clock, stale/duplicate packets, increments, pause, flags, background poll, advice, legacy, handicap preview/cancel/replacement for 3 controls and both colors)");
})().catch(e=>{console.error(e);process.exitCode=1;});
