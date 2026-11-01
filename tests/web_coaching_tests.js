"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class Element {
  constructor(tag="div") {
    this.tag=tag; this.tagName=tag.toUpperCase(); this.children=[]; this.listeners={}; this.dataset={}; this.attributes={};
    this.style={}; this.value=""; this.disabled=false; this.hidden=false; this.checked=false; this.className="";
    this.classList={add:(name)=>{this.className+=" "+name;},remove:(name)=>{this.className=this.className.split(/\s+/).filter((x)=>x!==name).join(" ");},
      contains:(name)=>this.className.split(/\s+/).includes(name),toggle:(name,on)=>{this.classList.remove(name);if(on)this.classList.add(name);}};
  }
  append(...children) { for(const item of children) for(const child of item.tag==="fragment" ? item.children : [item]) {child.parentElement=this;this.children.push(child);} }
  replaceChildren(...children) {this.children=[];this.append(...children);}
  before(element) {this.beforeElement=element;}
  addEventListener(name,callback) {this.listeners[name]=callback;}
  setAttribute(name,value) {this.attributes[name]=String(value);}
  querySelector(selector) {return this.children.find((x)=>selector.startsWith(".") ? x.className.split(/\s+/).includes(selector.slice(1)) : x.dataset.square===selector.match(/"(.*?)"/)?.[1]) || null;}
  focus() {}
  closest(selector) {
    const match=selector.match(/^(button)?\[data-([a-z-]+)\]$/),key=match?.[2].replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());
    return match && (!match[1] || this.tag==="button") && this.dataset[key]!==undefined ? this : this.parentElement?.closest(selector);
  }
  dispatch(name,event={}) {return this.listeners[name]?.(event);}
}
const flush=async()=>{for(let i=0;i<50;i++)await Promise.resolve();};
const initialFrame={fen:"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 42",board:{e1:"K",h1:"R",a1:"R",e8:"k",h8:"r",a8:"r"},turn:"white",ply:0};
const castleFrame={fen:"r3k2r/8/8/8/8/8/8/R4RK1 b kq - 1 42",board:{g1:"K",f1:"R",a1:"R",e8:"k",h8:"r",a8:"r"},turn:"black",ply:1,last_move:"e1g1"};
const bothCastled={fen:"r4rk1/8/8/8/8/8/8/R4RK1 w - - 2 43",board:{g1:"K",f1:"R",a1:"R",g8:"k",f8:"r",a8:"r"},turn:"white",ply:2,last_move:"e8g8"};
const finalFrame={fen:"r4rk1/8/8/8/8/8/8/1R3RK1 b - - 3 43",board:{g1:"K",f1:"R",b1:"R",g8:"k",f8:"r",a8:"r"},turn:"black",ply:3,last_move:"a1b1"};
const replay=()=>({summary:{human_color:"white",result:"1-0",white:"Human",black:"Regicide",ply_count:3},pgn:"test-pgn",frames:[initialFrame,castleFrame,bothCastled,finalFrame]});
const idle=()=>({revision:0,job_id:null,status:"idle",progress:{completed:0,total:0}});
function snapshot(extra={}) {
  const candidate={uci:"e1g1",san:"O-O",pv_uci:["e1g1","e8g8"],pv_san:["O-O","O-O"],pv_frames:[initialFrame,castleFrame,bothCastled],score_cp_white:100,mate_white:null,winner:null,bound:"exact",depth:9};
  const result={revision:1,job_id:"job-a",status:"running",progress:{completed:3,total:3},game:{human_color:"white",result:"1-0",ply_count:3},frames:replay().frames,
    moves:[
      {ply:1,color:"white",uci:"e1g1",san:"O-O",fen_before:initialFrame.fen,classification:"best",label:"추천 수",loss_cp:0,best:candidate,played:candidate,explanation:"추천 수와 같습니다."},
      {ply:2,color:"black",uci:"e8g8",san:"O-O",fen_before:castleFrame.fen,classification:"mistake",label:"실수",loss_cp:120,best:{...candidate,uci:"e8c8",san:"O-O-O",score_cp_white:-120,pv_frames:[castleFrame,bothCastled]},played:{...candidate,uci:"e8g8",san:"O-O",score_cp_white:0,pv_frames:[castleFrame,bothCastled]},explanation:"흑의 실수"},
      {ply:3,color:"white",uci:"a1b1",san:"Rb1",fen_before:bothCastled.fen,classification:"blunder",label:"큰 실수",loss_cp:350,best:{...candidate,uci:"a1a8",san:"Rxa8",pv_frames:[bothCastled,{...finalFrame,board:{g1:"K",f1:"R",a8:"R",g8:"k",f8:"r"}}]},played:{...candidate,uci:"a1b1",san:"Rb1",score_cp_white:-250,pv_frames:[bothCastled,finalFrame]},explanation:"상대 응수를 확인하세요."}],
    summary:{human_analyzed:2,critical_count:1,message:"내 수 2개 중 큰 실수 1개"},notice:"Regicide 탐색 기준",error:null,...extra};
  for(const row of result.moves)if(row.fen_after===undefined)row.fen_after=result.frames[row.ply]?.fen;
  return result;
}
function taughtSnapshot(extra={}) {
  const report=snapshot({status:"completed",...extra});
  report.coach={name:"루크",summary:"왕을 지킨 장면과 놓친 기회를 함께 살펴봐요.",lesson_plies:[1,3],version:1};
  for(const row of report.moves) {
    row.retry_legal_moves=row.ply===1 ? ["e1g1","e1c1","a1a2"] : row.ply===2 ? ["e8g8","e8c8"] : ["a1a8","a1b1"];
    row.coach={mood:row.ply===1 ? "celebrate" : "think",dialogue:row.ply===1 ? "O-O로 왕을 안전하게 지켰어요." : "상대의 응수를 확인해 볼까요?",
      hint:"왕의 안전과 상대의 다음 수를 먼저 확인해 보세요.",focus_squares:[row.ply===1 ? "e1" : "a1"],
      evidence:[{kind:"line",text:"추천 수순에서 왕의 위치 보기",squares:["g1"],line:"best",ply_index:1}]};
  }
  return report;
}
function retryResult(extra={}) {
  const line=taughtSnapshot().moves[0].best;
  return {job_id:"job-a",revision:1,ply:1,uci:"e1g1",san:"O-O",verdict:"recommended",message:"좋아요. 충분히 좋은 선택이에요.",line,best:line,loss_cp:0,...extra};
}
function annotation(row,kind=row.classification) {
  const symbols={great:"!!",best:"★",good:"✓",inaccuracy:"?!",mistake:"?",blunder:"??",allows_mate:"??",missed_mate:"?",uncertain:"…"};
  return {symbol:symbols[kind],kind,label:kind==="great" ? "탁월 후보" : row.label,square:row.uci.slice(2,4),
    scope:"actual_move",provisional:true,ply:row.ply,uci:row.uci,fen_after:row.fen_after,reason:"제한 시간 분석 결과입니다."};
}

function browser(options={}) {
  const elements=new Map(),requests=[],events=[],lifecycle={},timeouts=new Map(),motion=[],motionCancels=[];let nextTimer=0;
  const get=(id)=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get("coaching-time").value="1500";get("coaching-side").value="white";get("coaching-filter").value="both";
  const context={
    document:{getElementById:get,querySelector:()=>({content:"local-token"}),createElement:(tag)=>new Element(tag),
      createElementNS:(_ns,tag)=>new Element(tag),createDocumentFragment:()=>new Element("fragment"),hidden:false,
      addEventListener:(name,fn)=>{lifecycle["document-"+name]=fn;}},
    window:{addEventListener:(name,fn)=>{lifecycle[name]=fn;},dispatchEvent:(event)=>events.push(event),
      RegicideMotion:{replace:(board,fragment,state)=>{motion.push(state);board.replaceChildren(fragment);},cancel:(...args)=>{motionCancels.push(args);}}},
    CustomEvent:class {constructor(type,init){this.type=type;this.detail=init.detail;}},
    fetch:(url,options)=>new Promise((resolve,reject)=>{
      options.signal?.addEventListener("abort",()=>reject(Object.assign(new Error("abort"),{name:"AbortError"})),{once:true});
      requests.push({url,options,finish:(data,ok=true)=>resolve({ok,json:async()=>data,text:async()=>data})});
    }),
    AbortController,TextDecoder,TypeError,setTimeout:(callback,ms)=>{const id=++nextTimer;timeouts.set(id,{callback,ms});return id;},
    clearTimeout:(id)=>timeouts.delete(id),setInterval:()=>1,clearInterval(){}
  };
  const speech={spoken:[],cancelled:0,voices:options.voices || [],listeners:{}};
  if(options.voices) {
    context.window.speechSynthesis={getVoices:()=>speech.voices,speak:(utterance)=>speech.spoken.push(utterance),cancel:()=>{speech.cancelled++;},
      addEventListener:(name,callback)=>{speech.listeners[name]=callback;}};
    context.window.SpeechSynthesisUtterance=class {constructor(text){this.text=text;}};
  }
  const source=fs.readFileSync(path.join(__dirname,"../web/human-play/coaching.js"),"utf8").replace("  const timer = setInterval", "  window.testApi={enter,leave,action,poll,loadSource,loadFile,accept,render,choosePly,seek,showLine,startRetry,submitRetry,retrySquare,showEvidence};\n  const timer = setInterval");
  vm.runInNewContext(source,context);
  const ui={get,requests,events,lifecycle,timeouts,motion,motionCancels,speech,document:context.document,api:context.window.testApi,
    async click(id){get(id).dispatch("click");await flush();},
    async finish(data,ok=true){requests.at(-1).finish(data,ok);await flush();},
    square(name){return get("coaching-board").children.find((x)=>x.dataset.coachingSquare===name);},
    badges(){return get("coaching-board").children.flatMap((cell)=>cell.children).filter((child)=>child.classList.contains("coaching-move-badge"));},
    async clickSquare(name){get("coaching-board").dispatch("click",{target:ui.square(name)});await flush();},
    async replayStep(){const item=[...timeouts.entries()].find(([,value])=>value.ms===650);if(!item)return false;timeouts.delete(item[0]);item[1].callback();await flush();return true;},
    async voiceStep(){const item=[...timeouts.entries()].find(([,value])=>value.ms===300);if(!item)return false;timeouts.delete(item[0]);item[1].callback();await flush();return true;},
    async replayAll(){for(let i=0;i<20;i++)if(!await ui.replayStep())return;throw new Error("replay did not finish");}};
  ui.open=async(extra={})=>{
    void ui.api.enter(extra.library===true);await flush();
    requests.find((x)=>x.url==="/api/coaching").finish(extra.session || idle());
    requests.find((x)=>x.url==="/api/state").finish({result:"*",moves:[],human_color:"white",...extra.live});
    requests.find((x)=>x.url==="/api/games").finish({games:[{game_id:"saved-game",started_at:"2026-11-01T12:30:23Z",started_at_local:"2026-11-01 21:30:23",status:"사용자 승리",ply_count:3,human_color:"white"}]});
    await flush();
  };
  ui.prepare=async()=>{await ui.open();void ui.api.loadSource("saved-game");await ui.finish(replay());};
  ui.start=async(next=snapshot())=>{await ui.prepare();await ui.click("coaching-start");await ui.finish(next);};
  return ui;
}
const cases=[]; const test=(name,run)=>cases.push({name,run});

test("opening the chooser does not start an engine or alter live game",async()=>{
  const ui=browser();await ui.open();
  assert.deepEqual(ui.requests.map((x)=>x.url).sort(),["/api/coaching","/api/games","/api/state"]);
  assert.equal(ui.get("live-view").hidden,true);
  assert.equal(ui.get("coaching-view").hidden,false);
  assert.equal(ui.get("coaching-source").children[1].disabled,true);
  assert.equal(ui.get("coaching-start").disabled,true);
  assert.equal(ui.get("analysis-mode-button").disabled,true);
  assert.equal(ui.events.at(-1).type,"regicide-coaching-mode");
});
test("saved game is reviewed before explicit analysis with selected side and budget",async()=>{
  const ui=browser();await ui.prepare();
  assert.match(ui.get("coaching-game").textContent,/Human — Regicide.*3반수/);
  assert.equal(ui.get("coaching-start").disabled,false);
  assert.equal(ui.requests.some((x)=>x.url==="/api/coaching"&&x.options.body),false);
  ui.get("coaching-side").value="black";ui.get("coaching-time").value="5000";
  await ui.click("coaching-start");
  assert.deepEqual(JSON.parse(ui.requests.at(-1).options.body),{action:"start",revision:0,pgn:"test-pgn",human_color:"black",movetime_ms:5000});
  await ui.finish(snapshot({game:{human_color:"black"}}));
  assert.equal(ui.get("coaching-source").disabled,true);
  assert.equal(ui.get("coaching-cancel").disabled,false);
  assert.equal(ui.get("coaching-board").children[0].dataset.coachingSquare,"h1");
});
test("live current source must still be finished at load time",async()=>{
  const ui=browser();await ui.open();void ui.api.loadSource("current");
  await ui.finish({result:"*",moves:[{uci:"e2e4"}]});
  assert.match(ui.get("coaching-error").textContent,/끝난 뒤/);
  assert.equal(ui.requests.some((x)=>x.url==="/api/pgn"),false);
  assert.equal(ui.get("coaching-start").disabled,true);
});
test("finished current game auto previews but never auto starts coaching",async()=>{
  const ui=browser();await ui.open({live:{result:"0-1",moves:[{uci:"e2e4"}],human_color:"black"}});
  await ui.finish({result:"0-1",moves:[{uci:"e2e4"}],human_color:"black"});
  assert.equal(ui.requests.at(-1).url,"/api/pgn");await ui.finish("finished-pgn");
  assert.equal(ui.requests.at(-1).url,"/api/pgn-preview");await ui.finish(replay());
  assert.equal(ui.get("coaching-side").value,"black");
  assert.equal(ui.get("coaching-start").disabled,false);
  assert.equal(ui.requests.filter((x)=>x.url==="/api/coaching").length,1);
});
test("both sides display by default with actor labels and optional my-side filter",async()=>{
  const ui=browser();await ui.start();
  assert.equal(ui.get("coaching-moves").children.length,3);
  assert.match(ui.get("coaching-moves").children[1].children[0].textContent,/상대 수 · 흑/);
  assert.match(ui.get("coaching-summary").textContent,/나 백 · 2수.*\n상대 흑 · 1수/);
  assert.match(ui.get("coaching-move-title").textContent,/42\. O-O/);
  assert.equal(ui.square("g1").children[0].textContent,"♔");
  assert.equal(ui.badges()[0].textContent,"★");
  await ui.click("coaching-before");
  assert.ok(ui.square("e1").className.includes("coaching-best-from"));
  assert.equal(ui.get("coaching-board").children.filter((x)=>x.tag==="svg").length,1);
  await ui.click("coaching-after");
  ui.get("coaching-filter").value="mine";ui.get("coaching-filter").dispatch("change");
  assert.equal(ui.get("coaching-moves").children.length,2);
  await ui.click("coaching-next-critical");await ui.replayAll();
  assert.match(ui.get("coaching-move-title").textContent,/43\. Rb1/);
  assert.match(ui.get("coaching-classification").textContent,/3.50/);
  assert.equal(ui.square("b1").children[0].textContent,"♖");
  assert.equal(ui.square("a1").children.some((x)=>x.className.includes("piece")),false);
  ui.get("coaching-filter").value="both";ui.get("coaching-filter").dispatch("change");
  assert.equal(ui.get("coaching-moves").children.length,3);
  await ui.click("coaching-prev-critical");assert.match(ui.get("coaching-move-title").textContent,/42\.\.\. O-O/);
});
test("both alternatives replay server frames including both king and rook",async()=>{
  const ui=browser();await ui.start();
  await ui.click("coaching-best-line");await ui.click("coaching-line-next");
  assert.equal(ui.square("g1").children[0].textContent,"♔");
  assert.equal(ui.square("f1").children[0].textContent,"♖");
  assert.equal(ui.square("e1").children.some((x)=>x.className.includes("piece")),false);
  assert.equal(ui.get("coaching-board").children.filter((x)=>x.tag==="svg").length,0);
  assert.match(ui.get("coaching-score").textContent,/수순 시작점/);
  await ui.click("coaching-line-next");assert.equal(ui.square("g8").children[0].textContent,"♚");
  await ui.click("coaching-mainline");assert.equal(ui.square("g1").children[0].textContent,"♔");
  await ui.click("coaching-played-line");assert.equal(ui.get("coaching-variation").hidden,false);
  assert.equal(ui.requests.length,5);
});
test("next mistake replays every actual intervening move including the target and ends after it",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));
  ui.get("coaching-filter").value="mine";ui.get("coaching-filter").dispatch("change");
  await ui.click("coaching-next-critical");
  assert.equal(ui.get("coaching-context").hidden,false);
  assert.match(ui.get("coaching-context-status").textContent,/0\/2반수/);
  assert.equal(ui.square("g1").children[0].textContent,"♔");
  assert.equal(ui.get("coaching-context-moves").children.length,2);
  assert.match(ui.get("coaching-context-moves").children[0].textContent,/상대 수 · 흑/);
  assert.equal(ui.get("coaching-board").children.some((x)=>x.tag==="svg"),false);
  await ui.replayStep();
  assert.equal(ui.square("g1").children[0].textContent,"♔");assert.equal(ui.square("g8").children[0].textContent,"♚");
  assert.match(ui.get("coaching-context-status").textContent,/1\/2반수/);
  assert.equal(ui.motion.at(-1).animate,true);
  assert.equal(ui.badges()[0].textContent,"?");
  assert.equal(ui.badges()[0].parentElement.dataset.coachingSquare,"g8");
  await ui.replayStep();
  assert.equal(ui.square("g8").children[0].textContent,"♚");assert.equal(ui.square("b1").children[0].textContent,"♖");
  assert.equal(ui.square("a1").children.some((x)=>x.className.includes("piece")),false);
  assert.match(ui.get("coaching-move-title").textContent,/Rb1/);
  assert.match(ui.get("coaching-context-status").textContent,/목표 수를 둔 뒤/);
  assert.equal(ui.badges()[0].textContent,"??");
  assert.equal(await ui.replayStep(),false);
});
test("context replay can pause, resume or skip without applying missing moves",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));ui.api.choosePly(3);
  await ui.replayStep();await ui.click("coaching-context-pause");
  assert.equal(await ui.replayStep(),false);
  assert.equal(ui.square("g8").children[0].textContent,"♚");
  assert.equal(ui.get("coaching-context-pause").textContent,"이어서 보기");
  await ui.click("coaching-context-pause");await ui.replayStep();
  assert.equal(ui.square("g8").children[0].textContent,"♚");
  assert.equal(ui.square("b1").children[0].textContent,"♖");
  ui.api.seek(0);ui.api.choosePly(3);await ui.click("coaching-context-skip");
  assert.equal(ui.get("coaching-context").hidden,true);
  assert.equal(ui.square("g8").children[0].textContent,"♚");
  assert.equal(ui.motion.at(-1).animate,false);
  assert.equal(await ui.replayStep(),false);
});
test("context replay from a variation returns to its actual source before following the game",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));await ui.click("coaching-best-line");
  await ui.click("coaching-line-next");await ui.click("coaching-line-next");
  assert.equal(ui.square("g8").children[0].textContent,"♚");
  ui.api.choosePly(3);
  assert.equal(ui.get("coaching-variation").hidden,true);assert.equal(ui.square("g1").children[0].textContent,"♔");
  assert.equal(ui.square("e8").children[0].textContent,"♚");
  await ui.replayStep();assert.equal(ui.square("g8").children[0].textContent,"♚");
  await ui.replayStep();assert.equal(ui.square("b1").children[0].textContent,"♖");
});
test("manual stepping, changing sources and leaving stop stale context timers",async()=>{
  for(const action of ["step","source","leave","pagehide"]){
    const ui=browser();await ui.start(snapshot({status:"completed"}));ui.api.choosePly(3);
    const stale=[...ui.timeouts.values()].find((item)=>item.ms===650).callback;
    if(action==="step")await ui.click("coaching-prev");
    if(action==="source"){void ui.api.loadSource("another");await ui.finish(replay());}
    if(action==="leave")await ui.click("coaching-return");
    if(action==="pagehide")ui.lifecycle.pagehide();
    const previous=ui.motion.length;stale();await flush();
    assert.equal(ui.motion.length,previous,action);assert.equal(await ui.replayStep(),false,action);
  }
});
test("foreign job replacement stops the previous context traversal",async()=>{
  const ui=browser();await ui.start();ui.api.choosePly(3);
  const stale=[...ui.timeouts.values()].find((item)=>item.ms===650).callback;
  void ui.api.poll();await ui.finish(snapshot({revision:2,job_id:"foreign"}));
  const previous=ui.motion.length;stale();await flush();
  assert.equal(ui.motion.length,previous);assert.equal(ui.get("coaching-context").hidden,true);
});
test("hiding the browser stops replay instead of advancing in the background",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));ui.api.choosePly(3);
  ui.document.hidden=true;ui.lifecycle["document-visibilitychange"]();
  assert.equal(await ui.replayStep(),false);assert.equal(ui.get("coaching-context").hidden,true);
  assert.equal(ui.square("g1").children[0].textContent,"♔");
});
test("both-side critical navigation includes opponent mistakes and explicit opponent filtering",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));
  await ui.click("coaching-next-critical");await ui.replayAll();
  assert.match(ui.get("coaching-move-title").textContent,/상대 수 · 흑/);
  assert.equal(ui.get("coaching-played-label").textContent,"상대가 둔 수 · 흑");
  ui.get("coaching-filter").value="opponent";ui.get("coaching-filter").dispatch("change");
  assert.equal(ui.get("coaching-moves").children.length,1);
  assert.equal(ui.get("coaching-next-critical").disabled,true);
});
test("archive entry shows local timestamps and replays saved game without starting analysis",async()=>{
  const ui=browser();await ui.open({library:true,live:{result:"0-1",moves:[{uci:"e2e4"}]}});
  assert.equal(ui.requests.length,3);
  assert.match(ui.get("coaching-title").textContent,/보관함.*복기/);
  assert.match(ui.get("coaching-source").children[2].textContent,/2026-11-01 21:30:23/);
  void ui.api.loadSource("saved-game");
  const data=replay();data.summary.started_at_local="2026-10-30 22:15:03";data.summary.ended_at_local="2026-10-30 22:39:57";
  await ui.finish(data);
  assert.match(ui.get("coaching-game-time").textContent,/시작 2026-10-30 22:15:03.*종료 2026-10-30 22:39:57/);
  await ui.click("coaching-last");assert.equal(ui.square("b1").children[0].textContent,"♖");
  await ui.click("coaching-first");assert.equal(ui.square("e1").children[0].textContent,"♔");
  assert.equal(ui.requests.filter((request)=>request.url==="/api/coaching" && request.options.body).length,0);
  assert.equal(ui.requests.some((request)=>["/api/move","/api/new","/api/engine"].includes(request.url)),false);
});
test("mainline first/end and flip do not alter logical positions or report",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));
  await ui.click("coaching-last");assert.equal(ui.square("b1").children[0].textContent,"♖");
  assert.equal(ui.get("coaching-next").disabled,true);
  await ui.click("coaching-flip");assert.equal(ui.get("coaching-board").children[0].dataset.coachingSquare,"h1");
  assert.ok(ui.get("coaching-evaluation-bar").className.includes("black-view"));
  await ui.click("coaching-first");assert.equal(ui.square("e1").children[0].textContent,"♔");
  assert.equal(ui.get("coaching-prev").disabled,true);
});
test("pending results have no guessed score, label, recommendation or line",async()=>{
  const ui=browser();const data=snapshot({progress:{completed:0,total:3}});
  data.moves=data.moves.map((x)=>({...x,classification:"pending",label:"분석 대기",loss_cp:null,best:null,played:null,explanation:""}));
  await ui.start(data);
  assert.equal(ui.get("coaching-best-line").disabled,true);
  assert.equal(ui.get("coaching-board").children.filter((x)=>x.tag==="svg").length,0);
  assert.ok(ui.get("coaching-evaluation-bar").className.includes("unknown"));
  assert.match(ui.get("coaching-progress").textContent,/0\/3/);
});
test("scores retain white perspective, mate winner zero and bounds",async()=>{
  const ui=browser();const data=snapshot();
  data.moves[0].best={...data.moves[0].best,score_cp_white:-100,bound:"upper"};
  await ui.start(data);assert.match(ui.get("coaching-best").textContent,/≤ -1\.00/);
  await ui.click("coaching-before");
  const next=snapshot();next.moves[0].best={...next.moves[0].best,score_cp_white:null,mate_white:0,winner:"black"};
  void ui.api.poll();await ui.finish(next);
  assert.match(ui.get("coaching-best").textContent,/흑 메이트 0/);
  assert.equal(ui.get("coaching-evaluation-white").style.height,"0%");
});
test("old poll cannot undo cancellation or replace the newer report",async()=>{
  const ui=browser();await ui.start();
  void ui.api.poll();const old=ui.requests.at(-1);
  await ui.click("coaching-cancel");await ui.finish(snapshot({revision:2,status:"cancelled"}));
  old.finish(snapshot());await flush();
  assert.match(ui.get("coaching-progress").textContent,/분석 중지/);
  assert.equal(ui.get("coaching-cancel").disabled,true);
  assert.equal(ui.get("coaching-start").disabled,false);
});
test("foreign job detaches old report and is not cancelled on return",async()=>{
  const ui=browser();await ui.start();void ui.api.poll();
  await ui.finish(snapshot({revision:2,job_id:"foreign"}));
  assert.match(ui.get("coaching-error").textContent,/다른 화면/);
  assert.equal(ui.get("coaching-moves").children.length,0);
  assert.equal(ui.get("coaching-start").disabled,false);
  assert.equal(ui.get("coaching-cancel").disabled,true);
  const before=ui.requests.length;await ui.click("coaching-return");
  assert.equal(ui.requests.length,before);
});
test("changing my side after completion updates filters, both-side summary and actor labels",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));
  ui.get("coaching-filter").value="mine";ui.get("coaching-filter").dispatch("change");
  assert.equal(ui.get("coaching-moves").children.length,2);
  ui.get("coaching-side").value="black";ui.get("coaching-side").dispatch("change");
  assert.equal(ui.get("coaching-moves").children.length,1);
  assert.match(ui.get("coaching-moves").children[0].children[0].textContent,/흑/);
  assert.match(ui.get("coaching-summary").textContent,/나 흑 · 1수.*\n상대 백 · 2수/);
  await ui.click("coaching-next-critical");await ui.replayAll();assert.match(ui.get("coaching-move-title").textContent,/내 수 · 흑/);
});
test("failed new-source start never attaches the previous job report",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));
  void ui.api.loadSource("second-game");
  const next=replay();next.summary.white="Second";next.pgn="second-pgn";await ui.finish(next);
  assert.equal(ui.get("coaching-moves").children.length,0);
  await ui.click("coaching-start");
  await ui.finish({error:"새 분석 실패",coaching:snapshot({status:"completed"})},false);
  assert.equal(ui.get("coaching-moves").children.length,0);
  assert.match(ui.get("coaching-game").textContent,/Second/);
  assert.equal(ui.get("coaching-best-line").disabled,true);
  assert.equal(ui.get("coaching-start").disabled,false);
});
test("foreign running job at entry is never silently adopted or rendered",async()=>{
  const ui=browser();await ui.open({session:snapshot({job_id:"foreign"})});
  assert.equal(ui.get("coaching-moves").children.length,0);
  assert.equal(ui.get("coaching-cancel").disabled,true);
  assert.match(ui.get("coaching-notice").textContent,/다른 화면/);
  await ui.api.leave();assert.equal(ui.requests.length,3);
});
test("revision conflict carries forward server revision without displaying foreign data",async()=>{
  const ui=browser();await ui.prepare();await ui.click("coaching-start");
  await ui.finish({error:"다른 코칭이 먼저 시작됐습니다.",coaching:snapshot({revision:3,job_id:"foreign"})},false);
  assert.equal(ui.get("coaching-moves").children.length,0);
  assert.equal(ui.get("coaching-start").disabled,false);
  await ui.click("coaching-start");assert.equal(JSON.parse(ui.requests.at(-1).options.body).revision,3);
});
test("return cancels only owned running work and rejects late responses",async()=>{
  const ui=browser();await ui.start();void ui.api.poll();const old=ui.requests.at(-1);
  await ui.click("coaching-return");
  assert.deepEqual(JSON.parse(ui.requests.at(-1).options.body),{action:"cancel",revision:1});
  await ui.finish(snapshot({revision:2,status:"cancelled"}));old.finish(snapshot());await flush();
  assert.equal(ui.get("live-view").hidden,false);assert.equal(ui.get("coaching-view").hidden,true);
  assert.equal(ui.get("analysis-mode-button").disabled,false);assert.equal(ui.events.at(-1).detail,false);
});
test("failed cancel still returns with visible and truthful warning",async()=>{
  const ui=browser();await ui.start();await ui.click("coaching-return");await ui.finish({error:"연결 끊김"},false);
  assert.equal(ui.get("coaching-view").hidden,true);
  assert.equal(ui.get("live-view").beforeElement.hidden,false);
  assert.match(ui.get("live-view").beforeElement.textContent,/중지를 확인하지 못/);
});
test("file is size bounded, UTF-8 validated, previewed and requires explicit side choice",async()=>{
  const ui=browser();await ui.open();const count=ui.requests.length;
  await ui.api.loadFile({size:1048577});assert.equal(ui.requests.length,count);
  assert.match(ui.get("coaching-error").textContent,/1MiB/);
  void ui.api.loadFile({size:2,arrayBuffer:async()=>new Uint8Array([0xc3,0x28]).buffer});await flush();
  assert.match(ui.get("coaching-error").textContent,/UTF-8/);
  const raw=new TextEncoder().encode("valid pgn");
  void ui.api.loadFile({size:raw.length,arrayBuffer:async()=>raw.buffer});await flush();
  assert.equal(ui.requests.at(-1).url,"/api/pgn-preview");
  const data=replay();data.summary.white="<img src=x onerror=alert(1)>";
  await ui.finish(data);assert.match(ui.get("coaching-game").textContent,/<img/);
  assert.equal(ui.get("coaching-game").children.length,0);
  assert.equal(ui.get("coaching-start").disabled,false);
  assert.equal(ui.get("coaching-side").disabled,false);
});
test("timeout releases controls and pagehide invalidates delayed snapshots",async()=>{
  const ui=browser();await ui.prepare();await ui.click("coaching-start");
  [...ui.timeouts.values()].at(-1).callback();await flush();
  assert.match(ui.get("coaching-error").textContent,/초과/);assert.equal(ui.get("coaching-start").disabled,false);
  await ui.click("coaching-start");const pending=ui.requests.at(-1);ui.lifecycle.pagehide();
  pending.finish(snapshot());await flush();assert.equal(ui.get("coaching-moves").children.length,0);
});
test("arrow keys navigate only coaching, never intercept input controls",async()=>{
  const ui=browser();await ui.start();let prevented=false;
  ui.lifecycle.keydown({key:"ArrowRight",target:{tagName:"SELECT"},preventDefault(){prevented=true;}});
  assert.equal(prevented,false);assert.equal(ui.square("e8").children[0].textContent,"♚");
  ui.lifecycle.keydown({key:"ArrowRight",target:{tagName:"BODY"},preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(ui.square("g8").children[0].textContent,"♚");
});
test("analysis mode prevents overlapping coaching ownership",async()=>{
  const ui=browser();ui.lifecycle["regicide-analysis-mode"]({detail:true});await ui.api.enter();
  assert.equal(ui.requests.length,0);assert.equal(ui.get("coaching-mode-button").disabled,true);
});
test("coaching mode pauses and resumes live hint and evaluation polling",async()=>{
  const elements=new Map(),events={},requests=[];
  const get=(id)=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const state={game_id:"live",revision:0,fen:"start",human_color:"white",human_to_move:true,engine_to_move:false,result:"*",
    board:{e2:"P",e7:"p"},moves:[],legal_moves:["e2e4"],engine:{name:"Regicide",movetime_ms:1000},hint_enabled:true};
  const context={document:{getElementById:get,querySelector:()=>({content:"token"}),createElement:(tag)=>new Element(tag),
    createDocumentFragment:()=>new Element("fragment"),activeElement:null},
    window:{addEventListener:(name,fn)=>{events[name]=fn;}},AbortController,
    fetch:(url,options)=>{requests.push({url,options});return Promise.resolve({ok:true,json:async()=>url==="/api/state" ? state : {}});},
    setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){}};
  const source=fs.readFileSync(path.join(__dirname,"../web/human-play/app.js"),"utf8")
    .replace("  void initialize();","  window.testApi={pollHint,pollEvaluation};\n  void initialize();");
  vm.runInNewContext(source,context);await flush();requests.length=0;
  events["regicide-coaching-mode"]({detail:true});
  await context.window.testApi.pollHint();await context.window.testApi.pollEvaluation();assert.equal(requests.length,0);
  events["regicide-coaching-mode"]({detail:false});
  await context.window.testApi.pollHint();await context.window.testApi.pollEvaluation();
  assert.deepEqual(requests.map((x)=>x.url).sort(),["/api/evaluation","/api/hint"]);
});
test("Rook uses server dialogue and highlights evidence only at its matching position",async()=>{
  const ui=browser(),report=taughtSnapshot();
  report.moves[0].coach.evidence.push({text:"invalid",line:"best",ply_index:100,squares:["e1"]});
  await ui.start(report);
  assert.equal(ui.get("coach-mascot").dataset.mood,"celebrate");
  assert.match(ui.get("coach-dialogue").textContent,/O-O로 왕/);
  assert.match(ui.get("coach-overview").textContent,/놓친 기회/);
  assert.equal(ui.get("coach-lessons").children.length,2);
  assert.equal(ui.get("coach-evidence").children.length,1);
  assert.equal(ui.square("e1").classList.contains("coaching-evidence-square"),false);
  await ui.click("coach-focus");
  assert.equal(ui.square("e1").classList.contains("coaching-evidence-square"),true);
  ui.get("coach-evidence").dispatch("click",{target:ui.get("coach-evidence").children[0]});await flush();
  assert.equal(ui.square("g1").children[0].textContent,"♔");
  assert.equal(ui.square("g1").classList.contains("coaching-evidence-square"),true);
  await ui.click("coaching-line-next");
  assert.equal(ui.square("g1").classList.contains("coaching-evidence-square"),false);
});

test("positive lesson navigation follows the actual game with neutral transit guidance",async()=>{
  const ui=browser(),report=taughtSnapshot();report.moves[2].classification="good";report.moves[2].coach.dialogue="이 수가 좋았어요.";
  await ui.start(report);await ui.click("coach-lesson-next");
  assert.match(ui.get("coach-dialogue").textContent,/moves actually played/);
  assert.equal(ui.get("coach-evidence").hidden,true);
  assert.equal(ui.get("coach-retry").disabled,true);
  await ui.click("coach-retry");assert.equal(ui.get("coach-retry-panel").hidden,true);
  await ui.replayAll();
  assert.match(ui.get("coaching-context-status").textContent,/목표 수를 둔 뒤/);
  assert.doesNotMatch(ui.get("coaching-context-status").textContent,/실수/);
  assert.equal(ui.get("coach-dialogue").textContent,"이 수가 좋았어요.");
  assert.equal(ui.get("coach-retry").disabled,false);
});

test("positive retry hides played-equals-best solution, lists, evaluation and arrows",async()=>{
  const ui=browser();await ui.start(taughtSnapshot());
  await ui.click("coaching-best-line");await ui.click("coaching-line-next");
  const cancelled=ui.motionCancels.length;
  await ui.click("coach-retry");
  assert.equal(ui.motionCancels.length,cancelled+1);
  assert.equal(ui.motion.at(-1).animate,false);
  assert.equal(ui.square("e1").children[0].textContent,"♔");
  for(const id of ["coaching-summary-card","coaching-moves-card","coaching-comparison","coaching-critical-navigation","coach-overview-box","coach-lesson-controls","coach-evidence"])assert.equal(ui.get(id).hidden,true,id);
  for(const id of ["coach-dialogue","coach-scene","coaching-move-title","coaching-position","coaching-classification","coaching-explanation","coaching-played","coaching-best","coaching-pv"])
    assert.doesNotMatch(ui.get(id).textContent || "",/O-O/,id);
  assert.equal(ui.get("coaching-moves").children.length,0);
  assert.equal(ui.get("coaching-board").children.some((item)=>item.tag==="svg"),false);
  assert.equal(ui.square("e1").classList.contains("coaching-best-from"),false);
  assert.match(ui.get("coaching-score").textContent,/가렸습니다/);
  assert.equal(ui.get("coaching-evaluation-bar").classList.contains("unknown"),true);
  assert.equal(ui.get("coach-retry-lines").hidden,true);
  const count=ui.requests.length;await ui.click("coaching-best-line");
  assert.equal(ui.requests.length,count);assert.equal(ui.get("coaching-variation").hidden,true);
});

test("retry click submits once, preserves report and reveals fresh candidate and best replay explicitly",async()=>{
  const ui=browser(),report=taughtSnapshot(),original=JSON.stringify(report);await ui.start(report);await ui.click("coach-retry");
  await ui.clickSquare("e1");assert.equal(ui.square("g1").classList.contains("coaching-retry-target"),true);
  await ui.clickSquare("g1");
  const request=ui.requests.at(-1),count=ui.requests.length;
  assert.deepEqual(JSON.parse(request.options.body),{action:"retry",ply:1,uci:"e1g1",revision:1,job_id:"job-a"});
  assert.equal(ui.square("g1").children[0].textContent,"♔");assert.equal(ui.square("f1").children[0].textContent,"♖");
  assert.equal(ui.get("coach-retry-again").disabled,true);assert.equal(ui.get("coach-retry-reveal").disabled,true);
  await ui.clickSquare("a1");await ui.click("coach-retry");await ui.click("coach-retry-reveal");assert.equal(ui.requests.length,count);
  const freshFrame={...castleFrame,board:{c1:"K",d1:"R",h1:"R",e8:"k",a8:"r",h8:"r"},last_move:"e1c1"};
  await ui.finish(retryResult({verdict:"try_again",message:"다른 방법도 생각해 볼까요?",best:{...report.moves[0].best,san:"O-O-O",uci:"e1c1",pv_frames:[initialFrame,freshFrame],score_cp_white:275},loss_cp:175}));
  assert.equal(ui.get("coach-mascot").dataset.mood,"think");
  assert.equal(ui.get("coaching-comparison").hidden,true);
  assert.equal(ui.get("coach-retry-revealed").hidden,true);
  assert.doesNotMatch(ui.get("coach-dialogue").textContent,/O-O-O|1.75/);
  await ui.click("coach-retry-reveal");
  assert.match(ui.get("coach-retry-revealed").textContent,/O-O-O.*1.75/);
  assert.equal(ui.get("coach-retry-lines").hidden,false);
  await ui.click("coach-retry-best-line");await ui.click("coaching-line-next");
  assert.match(ui.get("coaching-position").textContent,/재도전의 새 추천/);
  assert.equal(ui.square("c1").children[0].textContent,"♔");
  assert.match(ui.get("coaching-score").textContent,/\+2.75/);
  await ui.click("coach-retry-candidate-line");await ui.click("coaching-line-next");
  assert.equal(ui.square("g1").children[0].textContent,"♔");
  await ui.click("coach-retry-again");
  assert.equal(ui.square("e1").children[0].textContent,"♔");
  assert.equal(ui.get("coaching-variation").hidden,true);assert.equal(ui.get("coach-retry-feedback").hidden,true);
  assert.equal(JSON.stringify(report),original);
});

test("pointer-owned retry drag supports rook-to-king castling without native HTML takeover",async()=>{
  for(const flip of [false,true]) {
    const ui=browser();await ui.start(taughtSnapshot());await ui.click("coach-retry");if(flip)await ui.click("coaching-flip");
    const event=(square,x)=>({target:ui.square(square),pointerId:2,button:0,clientX:x,clientY:10,cancelable:true,preventDefault(){}});
    assert.equal(ui.square("h1").draggable,false);
    assert.equal(ui.square("h1").classList.contains("coaching-retry-origin"),true);
    ui.get("coaching-board").dispatch("pointerdown",event("h1",10));
    let prevented=false;ui.get("coaching-board").dispatch("dragstart",{preventDefault(){prevented=true;}});
    assert.equal(prevented,true);
    ui.lifecycle.pointermove(event("e1",50));ui.lifecycle.pointerup(event("e1",50));
    await flush();
    assert.equal(JSON.parse(ui.requests.at(-1).options.body).uci,"e1g1");
    assert.equal(ui.square("g1").children[0].textContent,"♔");
  }
});

test("mouse-only drag and compatibility mouse events each submit exactly one legal move",async()=>{
  for(const pointerEvents of [false,true]) {
    const ui=browser();await ui.start(taughtSnapshot());await ui.click("coach-retry");const count=ui.requests.length;
    const event=(square,x)=>({target:ui.square(square),pointerId:3,button:0,clientX:x,clientY:10,cancelable:true,preventDefault(){}});
    if(pointerEvents)ui.get("coaching-board").dispatch("pointerdown",event("e1",10));
    ui.get("coaching-board").dispatch("mousedown",event("e1",10));
    if(pointerEvents)ui.lifecycle.pointermove(event("g1",50));
    ui.lifecycle.mousemove(event("g1",50));
    if(pointerEvents)ui.lifecycle.pointerup(event("g1",50));
    ui.lifecycle.mouseup(event("g1",50));await ui.clickSquare("g1");
    assert.equal(ui.requests.length,count+1);assert.equal(JSON.parse(ui.requests.at(-1).options.body).uci,"e1g1");
  }
});

test("pointer release uses final hit test even without intermediate events or with captured source target",async()=>{
  const ui=browser();await ui.start(taughtSnapshot());await ui.click("coach-retry");
  const event=(square,x)=>({target:ui.square(square),pointerId:5,button:0,clientX:x,clientY:10,cancelable:true,preventDefault(){}});
  ui.document.elementFromPoint=(x)=>ui.square(x===50 ? "g1" : "e1");
  ui.get("coaching-board").dispatch("pointerdown",event("e1",10));
  ui.lifecycle.pointerup(event("e1",50));await flush();
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).uci,"e1g1");
});

test("pointer cancellation, lost window focus and Escape discard an unfinished drag",async()=>{
  for(const cancelledBy of ["pointercancel","blur","Escape"]) {
    const ui=browser();await ui.start(taughtSnapshot());await ui.click("coach-retry");const count=ui.requests.length;
    const event=(square,x)=>({target:ui.square(square),pointerId:7,button:0,clientX:x,clientY:10,cancelable:true,preventDefault(){}});
    ui.get("coaching-board").dispatch("pointerdown",event("e1",10));ui.lifecycle.pointermove(event("g1",50));
    if(cancelledBy==="Escape")ui.lifecycle.keydown({...event("e1",10),key:"Escape"});
    else ui.lifecycle[cancelledBy]();
    ui.lifecycle.pointerup(event("g1",50));await flush();
    assert.equal(ui.requests.length,count);
    assert.equal(ui.square("e1").classList.contains("coaching-retry-drag-source"),false);
    assert.equal(ui.square("g1").classList.contains("coaching-retry-drag-target"),false);
  }
});

test("pointer drag submits legal move once and rejects offboard, stale and invalid drops",async()=>{
  const ui=browser();await ui.start(taughtSnapshot());await ui.click("coach-retry");const initial=ui.requests.length;
  const event=(name,x,y)=>({target:name ? ui.square(name) : new Element(),pointerId:8,button:0,clientX:x,clientY:y,cancelable:true,preventDefault(){}});
  ui.get("coaching-board").dispatch("pointerdown",event("e1",10,10));
  ui.lifecycle.pointermove(event("e2",40,10));ui.lifecycle.pointerup(event("e2",40,10));
  assert.equal(ui.requests.length,initial);
  ui.get("coaching-board").dispatch("pointerdown",event("e1",10,10));
  ui.lifecycle.pointermove(event(null,50,50));ui.lifecycle.pointerup(event(null,50,50));
  assert.equal(ui.requests.length,initial);
  ui.get("coaching-board").dispatch("pointerdown",event("e1",10,10));
  ui.lifecycle.pointermove(event("g1",40,10));
  assert.equal(ui.square("g1").classList.contains("coaching-retry-drag-target"),true);
  ui.lifecycle.pointerup(event("g1",40,10));await flush();
  await ui.clickSquare("g1");ui.lifecycle.pointerup(event("g1",40,10));
  assert.equal(ui.requests.length,initial+1);assert.equal(JSON.parse(ui.requests.at(-1).options.body).uci,"e1g1");
  await ui.finish(retryResult());await ui.click("coach-retry-again");
  ui.get("coaching-board").dispatch("pointerdown",event("e1",10,10));
  ui.lifecycle.pointermove(event("g1",40,10));await ui.click("coach-retry-cancel");
  ui.lifecycle.pointerup(event("g1",40,10));assert.equal(ui.requests.length,initial+1);
});

test("each promotion choice is explicit and submitted with its exact suffix",async()=>{
  for(const promoted of ["q","r","b","n"]) {
    const ui=browser(),report=taughtSnapshot(),before={...initialFrame,board:{e1:"K",e8:"k",a7:"P"}};
    report.frames=[before,...report.frames.slice(1)];report.moves[0].retry_legal_moves=["a7a8q","a7a8r","a7a8b","a7a8n"];
    await ui.start(report);await ui.click("coach-retry");const count=ui.requests.length;
    await ui.clickSquare("a7");await ui.clickSquare("a8");
    assert.equal(ui.requests.length,count);assert.equal(ui.get("coach-retry-promotion").children.length,4);
    const button=ui.get("coach-retry-promotion").children.find((item)=>item.dataset.retryPromotion==="a7a8"+promoted);
    button.dispatch("click");await flush();
    assert.equal(JSON.parse(ui.requests.at(-1).options.body).uci,"a7a8"+promoted);
    assert.equal(ui.motion.at(-1).pieces.a8,promoted.toUpperCase());
    assert.equal(report.frames[0].board.a7,"P");
  }
});

test("retry en passant display removes captured pawn while leaving archived board untouched",async()=>{
  const ui=browser(),report=taughtSnapshot(),before={...initialFrame,board:{e1:"K",e8:"k",e5:"P",d5:"p"},turn:"white"};
  report.frames[0]=before;report.moves[0].retry_legal_moves=["e5d6"];
  await ui.start(report);await ui.click("coach-retry");await ui.clickSquare("e5");await ui.clickSquare("d6");
  assert.equal(ui.motion.at(-1).pieces.d6,"P");assert.equal(ui.motion.at(-1).pieces.d5,undefined);
  assert.equal(before.board.d5,"p");assert.equal(before.board.e5,"P");
});

test("late retry responses cannot revive exercises after navigation, source, filter, side or exit changes",async()=>{
  const operations=[
    ui=>ui.api.seek(1),
    ui=>{ui.get("coaching-filter").value="opponent";ui.get("coaching-filter").dispatch("change");},
    ui=>{ui.get("coaching-side").value="black";ui.get("coaching-side").dispatch("change");},
    ui=>{void ui.api.loadSource("another");},
    ui=>{void ui.api.loadFile({size:2,arrayBuffer:async()=>new TextEncoder().encode("ok").buffer});},
    ui=>ui.api.leave(),
    ui=>{ui.document.hidden=true;ui.lifecycle["document-visibilitychange"]();},
    ui=>{ui.lifecycle.pagehide();},
    ui=>ui.click("coach-retry-cancel")
  ];
  for(const operation of operations) {
    const ui=browser();await ui.start(taughtSnapshot());await ui.click("coach-retry");await ui.clickSquare("e1");await ui.clickSquare("g1");
    const pending=ui.requests.at(-1);await operation(ui);await flush();
    pending.finish(retryResult({message:"LATE SHOULD NOT APPEAR"}));await flush();
    assert.doesNotMatch(ui.get("coach-dialogue").textContent,/LATE/);
    assert.doesNotMatch(ui.get("coach-retry-feedback").textContent || "",/LATE/);
    if(!ui.document.hidden && operation!==operations[7])assert.equal(ui.get("coach-retry-panel").hidden,true);
  }
});

test("retry revision conflict adopts only new revision and releases stale job ownership",async()=>{
  const ui=browser();await ui.start(taughtSnapshot());await ui.click("coach-retry");await ui.clickSquare("e1");await ui.clickSquare("g1");
  await ui.finish({error:"코칭이 바뀌었습니다.",coaching:taughtSnapshot({revision:3,job_id:"new-job"})},false);
  assert.equal(ui.get("coach-retry-panel").hidden,true);
  assert.equal(ui.get("coaching-moves").children.length,0);
  assert.match(ui.get("coaching-error").textContent,/다른 화면/);
  await ui.click("coaching-start");assert.equal(JSON.parse(ui.requests.at(-1).options.body).revision,3);
});

test("retry mismatched response remains hidden and network failure permits reset",async()=>{
  const ui=browser();await ui.start(taughtSnapshot());await ui.click("coach-retry");await ui.clickSquare("e1");await ui.clickSquare("g1");
  await ui.finish(retryResult({ply:2,message:"WRONG POSITION"}));
  assert.match(ui.get("coach-retry-feedback").textContent,/다른 위치/);
  assert.equal(ui.get("coach-retry-lines").hidden,true);
  assert.equal(ui.get("coach-retry-again").disabled,false);
  await ui.click("coach-retry-again");await ui.clickSquare("e1");await ui.clickSquare("g1");
  await ui.finish({error:"연결을 확인해 주세요."},false);
  assert.match(ui.get("coach-retry-feedback").textContent,/연결/);
  await ui.click("coach-retry-again");assert.equal(ui.square("e1").children[0].textContent,"♔");
});

test("retry rejects mismatched candidate or best root even when response outer identity matches",async()=>{
  for(const kind of ["line","best","candidate_uci"]) {
    const ui=browser();await ui.start(taughtSnapshot());await ui.click("coach-retry");await ui.clickSquare("e1");await ui.clickSquare("g1");
    const result=retryResult();
    if(kind==="candidate_uci")result.line={...result.line,uci:"e1c1"};
    else result[kind]={...result[kind],pv_frames:[castleFrame,bothCastled]};
    await ui.finish(result);
    assert.match(ui.get("coach-retry-feedback").textContent,/다른 위치의 수순/);
    assert.equal(ui.get("coach-retry-lines").hidden,true);
    assert.equal(ui.square("g1").children[0].textContent,"♔");
  }
});

test("retry is unavailable for running or unanalyzed rows and keyboard input uses legal targets",async()=>{
  const ui=browser();await ui.start(taughtSnapshot({status:"running"}));await ui.click("coach-retry");
  assert.equal(ui.get("coach-retry-panel").hidden,true);
  await ui.click("coaching-cancel");await ui.finish(taughtSnapshot({revision:2,status:"cancelled"}));
  await ui.click("coach-retry");
  const event=(name,key)=>({key,target:ui.square(name),preventDefault(){}});
  ui.lifecycle.keydown(event("e1","Enter"));assert.equal(ui.square("g1").attributes.tabindex,"0");
  ui.lifecycle.keydown(event("g1"," "));await flush();
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).uci,"e1g1");
  const pending=browser(),report=taughtSnapshot();report.moves[0].classification="pending";
  await pending.start(report);await pending.click("coach-retry");assert.equal(pending.get("coach-retry-panel").hidden,true);
});

test("automatic speech is absent-safe and selects only local English voices",async()=>{
  const absent=browser();await absent.start(taughtSnapshot());await absent.click("coach-speak");assert.equal(absent.get("coach-speak").disabled,true);
  const ui=browser({voices:[{name:"Remote English",lang:"en-US",localService:false},{name:"Korean",lang:"ko-KR",localService:true}]});
  await ui.start(taughtSnapshot());await ui.click("coach-speak");
  assert.equal(ui.speech.spoken.length,0);assert.equal(ui.get("coach-speak").disabled,true);
  ui.speech.voices.push({name:"Local English",lang:"en",localService:true});ui.speech.listeners.voiceschanged();
  assert.equal(ui.get("coach-speak").disabled,false);assert.equal(ui.speech.spoken.length,0);
  await ui.voiceStep();assert.equal(ui.speech.spoken.length,1);assert.equal(ui.speech.spoken[0].voice.localService,true);
  assert.equal(ui.speech.spoken[0].lang,"en");assert.equal(ui.speech.spoken[0].text,ui.get("coach-dialogue").textContent);
  await ui.click("coach-speech-stop");assert.equal(ui.speech.cancelled,1);assert.equal(ui.get("coach-speech-stop").hidden,true);
  ui.api.render();assert.equal(await ui.voiceStep(),false,"explicit stop does not restart on polling");
});

test("automatic speech stops on retry, navigation and hidden document; stale callbacks cannot stop newer speech",async()=>{
  const ui=browser({voices:[{name:"Local English",lang:"en-US",localService:true}]});
  await ui.start(taughtSnapshot());await ui.voiceStep();const old=ui.speech.spoken[0];
  await ui.click("coach-retry");assert.equal(ui.speech.cancelled,1);
  await ui.voiceStep();assert.equal(ui.speech.spoken.length,2);assert.doesNotMatch(ui.speech.spoken[1].text,/O-O/);
  old.onend();assert.equal(ui.get("coach-speech-stop").hidden,false);
  ui.api.seek(1);assert.equal(ui.speech.cancelled,2);
  await ui.voiceStep();ui.document.hidden=true;ui.lifecycle["document-visibilitychange"]();
  assert.equal(ui.speech.cancelled,3);assert.equal(ui.get("coach-speech-stop").hidden,true);
  ui.api.render();assert.equal(await ui.voiceStep(),false,"hidden document never reschedules speech");
});

test("300ms speech debounce keeps only the latest scene and ignores repeated renders and late timers",async()=>{
  const ui=browser({voices:[{name:"English",lang:"en-GB",localService:true}]});await ui.start(taughtSnapshot());
  const oldTimer=[...ui.timeouts.values()].find((item)=>item.ms===300).callback;
  ui.api.seek(2);ui.api.seek(3);ui.api.render();ui.api.render();
  assert.equal([...ui.timeouts.values()].filter((item)=>item.ms===300).length,1);
  assert.equal(ui.speech.spoken.length,0);await ui.voiceStep();
  assert.equal(ui.speech.spoken.length,1);assert.equal(ui.speech.spoken[0].text,ui.get("coach-dialogue").textContent);
  oldTimer();assert.equal(ui.speech.cancelled,0,"obsolete timer cannot cancel latest voice");
  ui.api.render();assert.equal(await ui.voiceStep(),false);assert.equal(ui.speech.spoken.length,1);
  ui.api.seek(2);await ui.voiceStep();assert.equal(ui.speech.spoken.length,2,"a different move is read even when its wording is identical");
});

test("voice toggle defaults on, mutes navigation and resumes only the current scene",async()=>{
  const ui=browser({voices:[{name:"English",lang:"en-US",localService:true}]});await ui.start(taughtSnapshot());
  assert.equal(ui.get("coach-speak").attributes["aria-pressed"],"true");
  await ui.click("coach-speak");assert.equal(ui.get("coach-speak").textContent,"음성 꺼짐");
  ui.api.seek(3);ui.api.render();assert.equal(await ui.voiceStep(),false);
  await ui.click("coach-speak");assert.equal(ui.get("coach-speak").textContent,"음성 켜짐");
  await ui.voiceStep();assert.equal(ui.speech.spoken.length,1);
  await ui.click("coach-speak");assert.equal(ui.speech.cancelled,1);
});

test("actual-game walkthrough remains quiet until its target, including paused intermediate frames",async()=>{
  const ui=browser({voices:[{name:"English",lang:"en-US",localService:true}]});await ui.start(taughtSnapshot());await ui.voiceStep();
  ui.api.choosePly(3,true,true);assert.equal(ui.speech.cancelled,1);
  assert.equal(await ui.voiceStep(),false);await ui.replayStep();assert.equal(await ui.voiceStep(),false);
  await ui.click("coaching-context-pause");ui.api.render();assert.equal(await ui.voiceStep(),false);
  await ui.click("coaching-context-pause");await ui.replayAll();await ui.voiceStep();
  assert.equal(ui.speech.spoken.length,2);assert.doesNotMatch(ui.speech.spoken[1].text,/Following the moves/);
});

test("overview, variation and retry feedback are read from revealed content only",async()=>{
  const ui=browser({voices:[{name:"English",lang:"en-US",localService:true}]});await ui.start(taughtSnapshot());await ui.voiceStep();
  ui.get("coach-overview-box").open=true;ui.get("coach-overview-box").dispatch("toggle");await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,ui.get("coach-overview").textContent);
  ui.api.seek(2);await ui.voiceStep();assert.equal(ui.speech.spoken.at(-1).text,ui.get("coach-dialogue").textContent);
  ui.api.seek(1);ui.api.showLine("best");await ui.voiceStep();const count=ui.speech.spoken.length;
  ui.api.showLine("played");await ui.voiceStep();assert.equal(ui.speech.spoken.length,count+1);
  await ui.click("coach-retry");await ui.voiceStep();assert.doesNotMatch(ui.speech.spoken.at(-1).text,/O-O/);
  await ui.clickSquare("e1");await ui.clickSquare("g1");assert.equal(await ui.voiceStep(),false,"submitted move waits for feedback");
  const result=retryResult({message:"Your move keeps the king safe. Check the next reply."});await ui.finish(result);await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,result.message);
});

test("pending speech is invalidated on return, pagehide, source replacement and hidden documents",async()=>{
  for(const operation of [ui=>ui.api.leave(),ui=>ui.lifecycle.pagehide(),ui=>ui.api.loadSource("saved-game"),ui=>{ui.document.hidden=true;ui.lifecycle["document-visibilitychange"]();}]) {
    const ui=browser({voices:[{name:"English",lang:"en-US",localService:true}]});await ui.start(taughtSnapshot());
    const pending=[...ui.timeouts.values()].find((item)=>item.ms===300).callback;
    void operation(ui);await flush();pending();await ui.voiceStep();assert.equal(ui.speech.spoken.length,0);
  }
});

test("actual move badges use safe class fallbacks and reserve brilliant symbol for explicit validated metadata",async()=>{
  for(const [kind,symbol] of Object.entries({best:"★",good:"✓",inaccuracy:"?!",mistake:"?",blunder:"??",allows_mate:"??",missed_mate:"?",uncertain:"…"})) {
    const ui=browser(),report=taughtSnapshot();report.moves[0].classification=kind;
    await ui.start(report);assert.equal(ui.badges().length,1);
    assert.equal(ui.badges()[0].textContent,symbol);assert.equal(ui.badges()[0].parentElement.dataset.coachingSquare,"g1");
    assert.match(ui.badges()[0].attributes["aria-label"],/Regicide 제한 탐색/);
  }
  const ui=browser(),report=taughtSnapshot();report.moves[0].annotation=annotation(report.moves[0],"great");
  await ui.start(report);assert.equal(ui.badges()[0].textContent,"!!");
  assert.match(ui.badges()[0].title,/탁월 후보/);assert.match(ui.get("coaching-badge-status").textContent,/탁월 후보/);
  const invented=browser(),invalid=taughtSnapshot();invalid.moves[0].classification="great";
  await invented.start(invalid);assert.equal(invented.badges().length,0);
});

test("foreign or inconsistent annotation metadata never falls back to a plausible-looking badge",async()=>{
  for(const change of [
    {scope:"best_line"},{provisional:false},{ply:2},{uci:"e1c1"},{square:"f1"},{fen_after:initialFrame.fen},
    {symbol:"!!"},{kind:"great",symbol:"★"},{kind:"blunder",symbol:"??"}
  ]) {
    const ui=browser(),report=taughtSnapshot();report.moves[0].annotation={...annotation(report.moves[0]),...change};
    await ui.start(report);assert.equal(ui.badges().length,0,JSON.stringify(change));
  }
});

test("badge attachment requires exact actual frame ply, FEN, UCI and occupied destination",async()=>{
  const changes=[
    report=>{report.frames[1]={...report.frames[1],ply:2};},
    report=>{report.frames[1]={...report.frames[1],fen:initialFrame.fen};},
    report=>{report.frames[1]={...report.frames[1],last_move:"e1c1"};},
    report=>{report.frames[1]={...report.frames[1],board:{e1:"K",f1:"R"}};},
    report=>{report.frames[1]={...report.frames[1],board:{...report.frames[1].board,g1:"k"}};},
    report=>{report.moves[0].fen_after=null;},
    report=>{report.moves[0].classification="pending";},
    report=>{report.moves[0].ply=2;}
  ];
  for(const change of changes) {
    const ui=browser(),report=taughtSnapshot();change(report);await ui.start(report);
    assert.equal(ui.badges().length,0);
  }
});

test("selection and before-after controls keep the same evaluated move while first and step show their exact frames",async()=>{
  const ui=browser();await ui.start(taughtSnapshot());ui.api.choosePly(2,false);
  assert.equal(ui.square("g8").children[0].textContent,"♚");assert.equal(ui.badges()[0].textContent,"?");
  await ui.click("coaching-before");
  assert.match(ui.get("coaching-move-title").textContent,/상대 수 · 흑/);
  assert.equal(ui.square("e8").children[0].textContent,"♚");assert.equal(ui.badges().length,0);
  await ui.click("coaching-after");assert.equal(ui.badges()[0].parentElement.dataset.coachingSquare,"g8");
  await ui.click("coaching-first");assert.equal(ui.badges().length,0);assert.match(ui.get("coaching-position").textContent,/^0\/3/);
  await ui.click("coaching-next");assert.equal(ui.badges()[0].parentElement.dataset.coachingSquare,"g1");
  await ui.click("coaching-last");assert.equal(ui.badges()[0].parentElement.dataset.coachingSquare,"b1");
  await ui.click("coaching-prev");assert.equal(ui.badges()[0].parentElement.dataset.coachingSquare,"g8");
  assert.match(ui.get("coaching-move-title").textContent,/상대 수 · 흑/);
});

test("badges remain absent on every hypothetical line and throughout retry including revealed answer",async()=>{
  const ui=browser();await ui.start(taughtSnapshot());assert.equal(ui.badges().length,1);
  for(const kind of ["best","played"]) {
    await ui.click("coaching-"+kind+"-line");assert.equal(ui.badges().length,0);
    await ui.click("coaching-line-next");assert.equal(ui.badges().length,0);
    await ui.click("coaching-mainline");assert.equal(ui.badges().length,1);
  }
  await ui.click("coach-retry");assert.equal(ui.badges().length,0);
  await ui.clickSquare("e1");await ui.clickSquare("g1");await ui.finish(retryResult());
  assert.equal(ui.badges().length,0);await ui.click("coach-retry-reveal");assert.equal(ui.badges().length,0);
  await ui.click("coach-retry-best-line");await ui.click("coaching-line-next");assert.equal(ui.badges().length,0);
  await ui.click("coach-retry-candidate-line");await ui.click("coaching-line-next");assert.equal(ui.badges().length,0);
  await ui.click("coach-retry-cancel");assert.equal(ui.badges()[0].parentElement.dataset.coachingSquare,"g1");
});

test("castle badge stays on the moved king when flipped and on the correct promotion piece at every edge",async()=>{
  const castle=browser();await castle.start(taughtSnapshot());
  assert.equal(castle.badges()[0].parentElement.dataset.coachingSquare,"g1");assert.equal(castle.square("f1").querySelector(".coaching-move-badge"),null);
  await castle.click("coaching-flip");assert.equal(castle.badges()[0].parentElement.dataset.coachingSquare,"g1");
  for(const color of ["white","black"])for(const file of ["a","h"])for(const piece of ["q","r","b","n"]) {
    const ui=browser(),report=taughtSnapshot(),from=file+(color==="white" ? "7" : "2"),to=file+(color==="white" ? "8" : "1"),uci=from+to+piece;
    const row=report.moves[0];Object.assign(row,{uci,color,fen_after:"promotion-"+uci,classification:"good"});
    report.frames[1]={fen:row.fen_after,ply:1,last_move:uci,turn:color==="white" ? "black" : "white",board:{[to]:color==="white" ? piece.toUpperCase() : piece}};
    row.annotation=annotation(row);
    await ui.start(report);assert.equal(ui.badges()[0].parentElement.dataset.coachingSquare,to);
    assert.equal(ui.motion.at(-1).pieces[to],report.frames[1].board[to]);
    await ui.click("coaching-flip");assert.equal(ui.badges()[0].parentElement.dataset.coachingSquare,to);
  }
});

test("pending analysis and archive-only replay have no grades and a matching update adds the settled badge",async()=>{
  const ui=browser(),pending=taughtSnapshot({status:"running"});pending.moves[0].classification="pending";
  await ui.start(pending);assert.equal(ui.badges().length,0);
  void ui.api.poll();await ui.finish(taughtSnapshot({status:"completed"}));assert.equal(ui.badges()[0].textContent,"★");
  await ui.api.loadSource("");assert.equal(ui.badges().length,0);
  const archive=browser();await archive.prepare();await archive.click("coaching-last");
  assert.equal(archive.badges().length,0);
});

test("badge CSS is an inert overlay sized from the board, and the legend distinguishes limited candidate evidence",()=>{
  const css=fs.readFileSync(path.join(__dirname,"../web/human-play/style.css"),"utf8");
  const rule=css.match(/#coaching-board \.coaching-move-badge \{([^}]+)\}/)?.[1] || "";
  assert.match(rule,/position:absolute/);assert.match(rule,/pointer-events:none/);assert.match(rule,/3\.2cqw/);
  assert.match(rule,/top:3%/);assert.match(rule,/right:3%/);assert.doesNotMatch(rule,/transform:translate/);
  const html=fs.readFileSync(path.join(__dirname,"../web/human-play/index.html"),"utf8");
  assert.match(html,/제한 시간 안에 다른 대안까지 비교/);assert.match(html,/객관적인 유일수나 묘수의 증명은 아닙니다/);
});

test("all UI ids exist and output has no HTML interpolation sink",()=>{
  const js=fs.readFileSync(path.join(__dirname,"../web/human-play/coaching.js"),"utf8");
  const html=fs.readFileSync(path.join(__dirname,"../web/human-play/index.html"),"utf8");
  for(const match of js.matchAll(/get\("([a-z][a-z-]+)"\)/g))assert.ok(html.includes('id="'+match[1]+'"'),match[1]);
  assert.doesNotMatch(js,/innerHTML|insertAdjacentHTML/);
  assert.match(html,/src="\/coaching.js"/);
});

(async()=>{for(const {name,run} of cases){await run();console.log("PASS "+name);}console.log("Browser coaching: "+cases.length+" checks passed.");})().catch((error)=>{console.error(error);process.exitCode=1;});
