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
function teachingSnapshot() {
  const report=taughtSnapshot(),row=report.moves[2];
  const teaching={concept:"undefended_piece",alternative:{uci:"a1a8",san:"Rxa8",reason:"wins Black's rook"},
    principle:"Look for free material before making a quiet move.",line:"best",ply_index:1,grounding:"verified_move_effect"};
  const white={dialogue:`Instead, Rxa8 ${teaching.alternative.reason}. ${teaching.principle}`,
    hint:"Check which pieces can be captured before choosing a move.",teaching:structuredClone(teaching)};
  const black={dialogue:`White could play Rxa8: it ${teaching.alternative.reason}. ${teaching.principle}`,
    hint:"Check what your opponent can capture before choosing a move.",teaching:structuredClone(teaching)};
  row.best={...row.best,san:"Rxa8",pv_san:["Rxa8"]};
  row.coach={...row.coach,...white,teaching,perspectives:{white,black}};
  return report;
}
function continuationSnapshot(count=6,terminal=false) {
  const result=taughtSnapshot(),row=result.moves[0];
  const fourth={fen:"1r3rk1/8/8/8/8/8/8/1R3RK1 w - - 4 44",board:{g1:"K",f1:"R",b1:"R",g8:"k",f8:"r",b8:"r"},turn:"white",last_move:"a8b8",ply:4};
  const fifth={fen:"1r3rk1/8/8/8/8/8/1R6/5RK1 b - - 5 44",board:{g1:"K",f1:"R",b2:"R",g8:"k",f8:"r",b8:"r"},turn:"black",last_move:"b1b2",ply:5};
  const sixth={fen:"5rk1/1r6/8/8/8/8/1R6/5RK1 w - - 6 45",board:{g1:"K",f1:"R",b2:"R",g8:"k",f8:"r",b7:"r"},turn:"white",last_move:"b8b7",ply:6};
  let pv=[initialFrame,castleFrame,bothCastled,finalFrame,fourth,fifth,sixth],uci=["e1g1","e8g8","a1b1","a8b8","b1b2","b8b7"],san=["O-O","O-O","Rb1","Rb8","Rb2","Rb7"];
  const actual={fen:"r3k2r/8/8/8/8/8/R7/4K2R b Kkq - 1 42",board:{e1:"K",h1:"R",a2:"R",e8:"k",h8:"r",a8:"r"},turn:"black",last_move:"a1a2",ply:1};
  row.uci="a1a2";row.san="Ra2";row.classification="mistake";row.fen_after=actual.fen;
  result.frames=[initialFrame,actual];result.moves=[row];result.progress={completed:1,total:1};
  if(terminal) {
    const before={fen:"8/8/8/8/8/1qk5/8/K7 b - - 0 1",board:{a1:"K",c3:"k",b3:"q"},turn:"black",ply:0};
    const after={fen:"8/8/8/8/8/2k5/1q6/K7 w - - 1 2",board:{a1:"K",c3:"k",b2:"q"},turn:"white",ply:1,last_move:"b3b2"};
    pv=[before,after];uci=["b3b2"];san=["Qb2#"];count=1;
    row.color="black";row.fen_before=before.fen;row.uci="b3a3";
  }
  row.best={...row.best,uci:uci[0],san:san[0],pv_frames:structuredClone(pv.slice(0,count+1)),pv_uci:uci.slice(0,count),pv_san:san.slice(0,count)};
  const packet={line:"best",target_plies:6,available_plies:count,complete:count===6 || terminal,terminal,
    intro:"Let's follow the stronger move and the replies it allows.",summary:"Keep your king covered while your rook joins the fight.",
    steps:uci.slice(0,count).map((move,index)=>({ply_index:index+1,uci:move,san:san[index],color:pv[index].turn,
      text:`${san[index]} changes the position. Check how the rook and king work together.`,squares:[move.slice(0,2),move.slice(2,4)]}))};
  row.coach.continuation=packet;
  row.coach.perspectives={white:{dialogue:"Look at the stronger move.",hint:"Keep your king covered.",continuation:packet},
    black:{dialogue:"Look at White's stronger move.",hint:"Watch your opponent's threats.",continuation:{...structuredClone(packet),intro:"Let's follow your opponent's stronger move."}}};
  return result;
}
function annotation(row,kind=row.classification) {
  const symbols={great:"!!",best:"★",good:"✓",inaccuracy:"?!",mistake:"?",blunder:"??",allows_mate:"??",missed_mate:"?",uncertain:"…"};
  return {symbol:symbols[kind],kind,label:kind==="great" ? "탁월 후보" : row.label,square:row.uci.slice(2,4),
    scope:"actual_move",provisional:true,ply:row.ply,uci:row.uci,fen_after:row.fen_after,reason:"제한 시간 분석 결과입니다."};
}

function strategySnapshot(positive=false) {
  const report=continuationSnapshot(),row=report.moves[0],line=positive ? "played" : "best";
  if(positive){row.classification="good";row.uci=row.best.uci;row.san=row.best.san;row.played=structuredClone(row.best);row.fen_after=row.played.pv_frames[1].fen;report.frames=row.played.pv_frames.slice(0,2);}
  const insight={theme:"rook_file",title:"Activate the rook",summary:"The rook can work along the open file; the opponent can contest it.",
    sections:[{id:"idea",title:"The useful file",text:"The rook now has an open file. Look for entry squares before committing it.",line,ply_index:1,squares:["f1"]},
      {id:"plan",title:"Bring the other rook",text:"The line brings the other rook to b1, preparing to use that file too.",line,ply_index:3,squares:["a1","b1"]},
      {id:"counterplay",title:"The reply",text:"Black castles in this line, bringing the rook to f8 to face your rook.",line,ply_index:2,squares:["f8"]}]};
  row.coach.insight=insight;
  for(const side of ["white","black"]){row.coach.perspectives[side].insight=structuredClone(insight);if(side==="black")row.coach.perspectives[side].insight.summary="White can use the open file; your rook can contest it.";}
  return report;
}

// UI-only packet over the existing legal board fixture; these names and grades
// are synthetic, not opening-theory or engine-strength assertions.
function openingSnapshot() {
  const report=strategySnapshot(),row=report.moves[0];
  row.coach.opening={title:"A shared opening position",summary:"Both sides have plans; assess the moves, not a fixed matchup.",
    white:{name:"White test setup"},black:{name:"Black test response"},ply_index:1,fen:row.fen_after,focus_squares:["g1"],
    sections:[{id:"white",title:"White's plan",text:"White develops toward the center and watches for the opponent's reply."},
      {id:"black",title:"Black's reply",text:"Black can challenge the central squares after completing development."},
      {id:"balance",title:"Who got the better start?",text:"It is too early to choose a winner from these opening names."}],
    assessment:{status:"early",side:null,score_cp_white:null}};
  return report;
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
  const source=fs.readFileSync(path.join(__dirname,"../web/human-play/coaching.js"),"utf8").replace("  const timer = setInterval", "  window.testApi={enter,leave,action,poll,loadSource,loadFile,accept,acceptForeign,render,choosePly,seek,showLine,startRetry,submitRetry,retrySquare,showEvidence};\n  const timer = setInterval");
  vm.runInNewContext(source,context);
  const ui={get,requests,events,lifecycle,timeouts,motion,motionCancels,speech,document:context.document,api:context.window.testApi,
    async click(id){get(id).dispatch("click");await flush();},
    async finish(data,ok=true){requests.at(-1).finish(data,ok);await flush();},
    square(name){return get("coaching-board").children.find((x)=>x.dataset.coachingSquare===name);},
    badges(){return get("coaching-board").children.flatMap((cell)=>cell.children).filter((child)=>child.classList.contains("coaching-move-badge"));},
    async clickSquare(name){get("coaching-board").dispatch("click",{target:ui.square(name)});await flush();},
    async replayStep(){const item=[...timeouts.entries()].find(([,value])=>value.ms===650);if(!item)return false;timeouts.delete(item[0]);item[1].callback();await flush();return true;},
    async voiceStep(){const item=[...timeouts.entries()].find(([,value])=>value.ms===300);if(!item)return false;timeouts.delete(item[0]);item[1].callback();await flush();return true;},
    async continuationStep(){const item=[...timeouts.entries()].find(([,value])=>value.ms===600 || (value.ms>=2200 && value.ms<=9000));if(!item)return false;timeouts.delete(item[0]);item[1].callback();await flush();return true;},
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
  assert.match(ui.get("coaching-score").textContent,/별도로 평가하지/);
  assert.equal(ui.get("coaching-evaluation-bar").classList.contains("unknown"),true);
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

test("board evaluation follows the displayed root through replay, not the selected lesson or previous played score",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));
  // After White's move, the displayed board is Black's next search root.
  assert.match(ui.get("coaching-score").textContent,/-1\.20.*백 기준/);
  assert.notEqual(ui.get("coaching-evaluation-white").style.height,"60%");
  const height=ui.get("coaching-evaluation-white").style.height;
  await ui.click("coaching-flip");
  assert.equal(ui.get("coaching-evaluation-white").style.height,height);
  assert.match(ui.get("coaching-evaluation-bar").attributes["aria-label"],/-1\.20/);
  ui.api.choosePly(3);
  assert.match(ui.get("coaching-score").textContent,/-1\.20/);
  await ui.replayStep();
  assert.match(ui.get("coaching-score").textContent,/\+1\.00/);
  assert.equal(ui.get("coaching-evaluation-white").style.height,"60%");
  await ui.replayStep();
  // The final nonterminal board has no independent root search in this report.
  assert.equal(ui.get("coaching-evaluation-bar").classList.contains("unknown"),true);
  assert.match(ui.get("coaching-score").textContent,/현재 위치 평가 대기/);
  assert.match(ui.get("coaching-played").textContent,/-2\.50/);
  await ui.click("coaching-before");
  assert.match(ui.get("coaching-score").textContent,/\+1\.00/);
});

test("board evaluation requires both row and search root FEN identity and an analyzed row",async()=>{
  for(const change of [
    row=>row.fen_before="wrong position",
    row=>row.best={...row.best,pv_frames:[{...row.best.pv_frames[0],fen:"wrong root"},...row.best.pv_frames.slice(1)]},
    row=>row.classification="pending"
  ]) {
    const ui=browser(),data=snapshot({status:"completed"});change(data.moves[1]);await ui.start(data);
    assert.equal(ui.get("coaching-evaluation-bar").classList.contains("unknown"),true);
    assert.equal(ui.get("coaching-evaluation-white").style.height,"50%");
  }
});

test("hypothetical positions do not inherit a root score even when their FEN occurs in the mainline",async()=>{
  const ui=browser(),data=snapshot({status:"completed"});
  data.moves[0].played={...data.moves[0].played,score_cp_white:-300};
  await ui.start(data);await ui.click("coaching-played-line");
  assert.match(ui.get("coaching-score").textContent,/\+1\.00/);
  await ui.click("coaching-line-next");
  assert.equal(ui.square("g1").children[0].textContent,"♔");
  assert.equal(ui.get("coaching-evaluation-bar").classList.contains("unknown"),true);
  assert.doesNotMatch(ui.get("coaching-score").textContent,/\+1\.00|-3\.00|-1\.20/);
  await ui.click("coaching-line-prev");
  assert.match(ui.get("coaching-score").textContent,/\+1\.00/);
});

test("terminal frame results override search values while zero mate without a winner remains unknown",async()=>{
  for(const [result,height,label] of [["1-0","100%","백 승"],["0-1","0%","흑 승"],["1/2-1/2","50%","무승부"]]) {
    const ui=browser(),data=snapshot({status:"completed"});data.frames=[data.frames[0],{...data.frames[1],result},...data.frames.slice(2)];
    await ui.start(data);
    assert.equal(ui.get("coaching-evaluation-white").style.height,height);
    assert.equal(ui.get("coaching-score").textContent,"대국 결과 · "+label);
    assert.equal(ui.get("coaching-evaluation-bar").classList.contains("unknown"),false);
    data.moves[0].best={...data.moves[0].best,pv_frames:[data.frames[0],data.frames[1]]};
    await ui.click("coaching-best-line");await ui.click("coaching-line-next");
    assert.equal(ui.get("coaching-evaluation-white").style.height,height);
    assert.equal(ui.get("coaching-score").textContent,"대국 결과 · "+label);
  }
  const ui=browser(),data=snapshot({status:"completed"});
  data.moves[1].best={...data.moves[1].best,score_cp_white:null,mate_white:0,winner:null};
  await ui.start(data);assert.equal(ui.get("coaching-evaluation-bar").classList.contains("unknown"),true);
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
  await ui.click("coach-retry-best-line");
  assert.match(ui.get("coaching-score").textContent,/\+2.75/);
  await ui.click("coaching-line-next");
  assert.match(ui.get("coaching-position").textContent,/재도전의 새 추천/);
  assert.equal(ui.square("c1").children[0].textContent,"♔");
  assert.match(ui.get("coaching-score").textContent,/별도로 평가하지/);
  assert.equal(ui.get("coaching-evaluation-bar").classList.contains("unknown"),true);
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

test("teaching dialogue automatically reads the better move, reason and principle from either player's perspective",async()=>{
  const ui=browser({voices:[{name:"Local English",lang:"en-US",localService:true}]}),report=teachingSnapshot();
  const original=JSON.stringify(report),row=report.moves[2];
  await ui.start(report);ui.api.seek(3);await ui.voiceStep();
  assert.equal(ui.get("coach-dialogue").textContent,row.coach.perspectives.white.dialogue);
  assert.equal(ui.speech.spoken.at(-1).text,row.coach.perspectives.white.dialogue);
  for(const text of [row.coach.teaching.alternative.san,row.coach.teaching.alternative.reason,row.coach.teaching.principle])
    assert.ok(ui.speech.spoken.at(-1).text.includes(text),text);
  ui.get("coaching-side").value="black";ui.get("coaching-side").dispatch("change");await ui.voiceStep();
  assert.equal(ui.get("coach-dialogue").textContent,row.coach.perspectives.black.dialogue);
  assert.equal(ui.speech.spoken.at(-1).text,row.coach.perspectives.black.dialogue);
  ui.api.render();assert.equal(await ui.voiceStep(),false,"unchanged progress must not repeat the full lesson");
  assert.equal(JSON.stringify(report),original,"selected perspective cannot rewrite stored teaching metadata");
});

test("unsolved retry hides nested teaching and cancels pending solution speech until explicit reveal",async()=>{
  for(const side of ["white","black"]) {
    const ui=browser({voices:[{name:"Local English",lang:"en-US",localService:true}]}),report=teachingSnapshot();
    const original=JSON.stringify(report),row=report.moves[2],secrets=[row.coach.teaching.alternative.san,row.coach.teaching.alternative.reason,row.coach.teaching.principle,"verified_move_effect"];
    await ui.start(report);ui.get("coaching-side").value=side;ui.get("coaching-side").dispatch("change");ui.api.seek(3);
    const staleLesson=[...ui.timeouts.values()].find(item=>item.ms===300).callback;
    await ui.click("coach-retry");await ui.voiceStep();staleLesson();
    assert.equal(ui.speech.spoken.length,1,"only the retry prompt, never the pending solution, is spoken");
    assert.equal(ui.speech.spoken[0].text,row.coach.perspectives[side].hint);
    const checkHidden=()=>{
      for(const id of ["coaching-summary-card","coaching-moves-card","coaching-comparison","coach-evidence","coach-overview-box"])
        assert.equal(ui.get(id).hidden,true,`${side}: ${id}`);
      for(const id of ["coach-dialogue","coach-scene","coaching-move-title","coaching-position","coaching-classification","coaching-explanation","coaching-played","coaching-best","coaching-pv","coach-retry-feedback"])
        for(const secret of secrets)assert.equal((ui.get(id).textContent || "").includes(secret),false,`${side}: ${id}: ${secret}`);
      for(const utterance of ui.speech.spoken)for(const secret of secrets)
        assert.equal(utterance.text.includes(secret),false,`${side}: spoken ${secret}`);
      assert.equal(ui.get("coach-retry-revealed").hidden,true);
      assert.equal(ui.get("coaching-board").children.some(item=>item.tag==="svg"),false);
      assert.equal(ui.get("coaching-moves").children.length,0);
    };
    checkHidden();ui.api.render();assert.equal(await ui.voiceStep(),false,"polling cannot reschedule hidden teaching");
    await ui.clickSquare("a1");await ui.clickSquare("b1");
    assert.equal(await ui.voiceStep(),false,"candidate submission waits for its own feedback");
    await ui.finish(retryResult({ply:3,uci:"a1b1",san:"Rb1",verdict:"try_again",message:"Check which pieces can be captured before trying again.",
      line:row.played,best:row.best,teaching:structuredClone(row.coach.teaching)}));
    await ui.voiceStep();checkHidden();
    await ui.click("coach-retry-reveal");await ui.voiceStep();
    assert.equal(ui.get("coach-dialogue").textContent,row.coach.perspectives[side].dialogue);
    assert.equal(ui.speech.spoken.at(-1).text,row.coach.perspectives[side].dialogue,"only explicit reveal restores the complete lesson");
    assert.equal(JSON.stringify(report),original,"retry keeps both teaching perspectives intact");
  }
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

test("coach wording and automatic narration follow the selected human side",async()=>{
  const report=taughtSnapshot();
  report.moves[0].coach.perspectives={white:{dialogue:"You've developed your rook.",hint:"Look for a safe square for your king."},
    black:{dialogue:"Your opponent has developed the rook.",hint:"Look for a safe square for their king."}};
  const ui=browser({voices:[{name:"Local",lang:"en-US",localService:true}]});
  await ui.start(report);
  assert.equal(ui.get("coach-dialogue").textContent,"You've developed your rook.");
  await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,"You've developed your rook.");
  ui.get("coaching-side").value="black";ui.get("coaching-side").dispatch("change");
  assert.equal(ui.get("coach-dialogue").textContent,"Your opponent has developed the rook.");
  await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,"Your opponent has developed the rook.");
  await ui.click("coach-retry");
  assert.equal(ui.get("coach-dialogue").textContent,"Look for a safe square for their king.");
});

test("six-ply explanation animates every server frame, highlights SAN and keeps the final board for its summary",async()=>{
  const ui=browser(),report=continuationSnapshot(),best=report.moves[0].best;
  best.pv_frames.push({fen:"5rk1/1r6/8/8/8/8/1R6/5R1K b - - 7 45",board:{h1:"K",f1:"R",b2:"R",g8:"k",f8:"r",b7:"r"},turn:"black",last_move:"g1h1",ply:7});
  best.pv_san.push("Kh1");best.pv_uci.push("g1h1");const saved=JSON.stringify(report);await ui.start(report);
  assert.equal(ui.get("coach-continuation").hidden,false);assert.match(ui.get("coach-continuation").textContent,/6수/);
  const requests=ui.requests.length;await ui.click("coach-continuation");
  assert.equal(ui.get("coach-dialogue").textContent,report.moves[0].coach.continuation.intro);
  assert.match(ui.get("coaching-variation-title").textContent,/0\/6수/);
  assert.match(ui.get("coach-continuation-note").textContent,/상대가 이 응수를.*양쪽 합계 6수/);
  for(let i=1;i<=6;i++) {
    assert.equal(await ui.continuationStep(),true);assert.equal(ui.motion.at(-1).ply,i);assert.equal(ui.motion.at(-1).animate,true);
    assert.equal(ui.get("coach-dialogue").textContent,report.moves[0].coach.continuation.steps[i-1].text);
    assert.match(ui.get("coaching-position").textContent,new RegExp(i+"/6수"));
    assert.equal(ui.get("coaching-pv").children[i-1].attributes["aria-current"],"step");
    for(const square of report.moves[0].coach.continuation.steps[i-1].squares)assert.ok(ui.square(square).classList.contains("coaching-evidence-square"));
    assert.equal(ui.badges().length,0);
  }
  await ui.continuationStep();assert.equal(ui.motion.at(-1).ply,6);assert.equal(ui.motion.at(-1).animate,false);
  assert.equal(ui.get("coach-dialogue").textContent,report.moves[0].coach.continuation.summary);
  assert.equal(ui.square("b7").children[0].textContent,"♜");
  await ui.continuationStep();assert.equal(ui.get("coaching-line-play").textContent,"설명 완료");
  assert.equal(await ui.continuationStep(),false);assert.equal(ui.requests.length,requests);assert.equal(JSON.stringify(report),saved);
  await ui.click("coaching-mainline");assert.equal(ui.square("a2").children[0].textContent,"♖");assert.equal(ui.get("coaching-variation").hidden,true);
});

test("guided speech waits for voice completion without interrupting each step and reads the final summary",async()=>{
  const ui=browser({voices:[{lang:"en-US",localService:true}]}),report=continuationSnapshot(2);await ui.start(report);
  await ui.click("coach-continuation");await ui.voiceStep();
  assert.equal(ui.speech.spoken.length,1);const intro=ui.speech.spoken[0];
  ui.api.render();assert.equal(await ui.continuationStep(),false);assert.equal(ui.speech.cancelled,0);
  intro.onend();await ui.continuationStep();assert.equal(ui.motion.at(-1).ply,1);await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,report.moves[0].coach.continuation.steps[0].text);
  assert.equal(await ui.continuationStep(),false);assert.equal(ui.speech.cancelled,0);
  ui.speech.spoken.at(-1).onend();await ui.continuationStep();await ui.voiceStep();
  ui.speech.spoken.at(-1).onend();await ui.continuationStep();await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,report.moves[0].coach.continuation.summary);
  ui.speech.spoken.at(-1).onend();await ui.continuationStep();assert.equal(ui.get("coaching-line-play").textContent,"설명 완료");
  const count=ui.motion.length;intro.onend();intro.onerror();assert.equal(await ui.continuationStep(),false);assert.equal(ui.motion.length,count);
});

test("guided pause and manual stepping cancel stale work, resume the current explanation and retain the original review",async()=>{
  const ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(continuationSnapshot());await ui.click("coach-continuation");await ui.voiceStep();
  const oldSpeech=ui.speech.spoken.at(-1);await ui.click("coaching-line-play");
  oldSpeech.onend();assert.equal(await ui.continuationStep(),false);assert.equal(ui.get("coaching-line-play").textContent,"이어서 보기");
  await ui.click("coaching-line-next");assert.equal(ui.motion.at(-1).ply,1);assert.equal(await ui.voiceStep(),true);
  await ui.click("coaching-line-play");await ui.voiceStep();assert.equal(ui.speech.spoken.length,3);
  ui.speech.spoken.at(-1).onend();const stale=[...ui.timeouts.values()].find((timer)=>timer.ms===600).callback;
  await ui.click("coaching-line-prev");const count=ui.motion.length;stale();assert.equal(ui.motion.length,count);assert.equal(ui.motion.at(-1).ply,0);
  await ui.click("coaching-line-play");await ui.voiceStep();assert.equal(ui.speech.spoken.at(-1).text,"Let's follow the stronger move and the replies it allows.");
});

test("manual continuation steps read once while paused, never auto advance, and reject stale speech or timers",async()=>{
  const ui=browser({voices:[{lang:"en-US",localService:true}]}),report=continuationSnapshot(2);await ui.start(report);
  await ui.click("coach-continuation");await ui.click("coaching-line-play");assert.equal(await ui.voiceStep(),false,"pause itself stays silent");
  await ui.click("coaching-line-next");const staleTimer=[...ui.timeouts.values()].find((item)=>item.ms===300).callback;
  await ui.voiceStep();const first=ui.speech.spoken.at(-1);
  assert.equal(first.text,report.moves[0].coach.continuation.steps[0].text);first.onend();assert.equal(ui.get("coach-speech-stop").hidden,true);ui.api.render();
  assert.equal(await ui.voiceStep(),false);assert.equal(await ui.continuationStep(),false);assert.equal(ui.motion.at(-1).ply,1);
  await ui.click("coaching-line-next");await ui.voiceStep();const second=ui.speech.spoken.at(-1),count=ui.speech.spoken.length;
  staleTimer();first.onerror();assert.equal(ui.speech.spoken.length,count);assert.equal(ui.motion.at(-1).ply,2);
  second.onerror();assert.equal(await ui.continuationStep(),false,"manual speech failure cannot start automatic playback");
  await ui.click("coaching-line-next");await ui.voiceStep();const summary=ui.speech.spoken.at(-1);
  assert.equal(summary.text,report.moves[0].coach.continuation.summary);summary.onend();assert.equal(await ui.continuationStep(),false);
  await ui.click("coaching-line-prev");await ui.voiceStep();const manual=ui.speech.spoken.at(-1);
  await ui.click("coach-speech-stop");manual.onend();assert.equal(await ui.continuationStep(),false);assert.equal(await ui.voiceStep(),false);
  await ui.click("coaching-line-play");await ui.voiceStep();assert.equal(ui.speech.spoken.at(-1).text,report.moves[0].coach.continuation.steps[1].text);
  ui.speech.spoken.at(-1).onend();assert.equal(await ui.continuationStep(),true,"resume restores deliberate automatic playback");
});

test("manual steps with muted or absent speech remain paused without reading timers",async()=>{
  for(const voices of [undefined,[{lang:"en-US",localService:true}]]) {
    const ui=browser({voices});await ui.start(continuationSnapshot());await ui.click("coach-continuation");
    if(voices)await ui.click("coach-speak");
    await ui.click("coaching-line-next");ui.api.render();assert.equal(ui.motion.at(-1).ply,1);
    assert.equal(ui.get("coach-speech-stop").hidden,true);
    assert.equal(await ui.voiceStep(),false);assert.equal(await ui.continuationStep(),false);
  }
});

test("short confirmed lines and terminal lines never promise six available moves",async()=>{
  for(const terminal of [false,true]) {
    const report=continuationSnapshot(2,terminal),count=terminal ? 1 : 2,ui=browser();await ui.start(report);await ui.click("coach-continuation");
    assert.match(ui.get("coach-continuation").textContent,new RegExp(count+"수"));
    assert.match(ui.get("coach-continuation-note").textContent,terminal ? /대국이 끝납니다/ : /2수까지만 확인/);
    for(let i=0;i<count+2;i++)await ui.continuationStep();
    assert.equal(ui.motion.at(-1).ply,count);assert.equal(ui.get("coaching-line-next").disabled,true);assert.equal(await ui.continuationStep(),false);
  }
});

test("continuation gate rejects untrusted mismatches, noncritical rows and actual-equals-best",async()=>{
  for(const mutate of [
    row=>{row.classification="good";}, row=>{row.uci=row.best.uci;}, row=>{row.best.bound="upper";},
    row=>{row.best.pv_frames[0].fen="another-root";},row=>{row.best.pv_uci[0]="e1c1";},
    row=>{row.best.pv_san[1]="different";},row=>{row.best.pv_frames[1].last_move="e1c1";},
    row=>{row.coach.continuation.steps[0].color="black";},row=>{row.coach.continuation.steps[0].ply_index=2;},
    row=>{row.coach.continuation.steps[0].text="";},row=>{row.coach.continuation.available_plies=7;},
    row=>{row.coach.continuation.complete=false;},row=>{row.coach.continuation.steps[0].squares=["z9"];},
    row=>{row.coach.perspectives.white={dialogue:"No verified line."};}
  ]) {
    const report=continuationSnapshot();mutate(report.moves[0]);const ui=browser();await ui.start(report);
    assert.equal(ui.get("coach-continuation").hidden,true);await ui.click("coach-continuation");
    assert.equal(await ui.continuationStep(),false);assert.equal(ui.get("coaching-variation").hidden,true);
  }
});

test("guided callbacks cannot revive after navigation, overview, evidence, retry, job replacement or leaving",async()=>{
  for(const operation of [
    ui=>ui.api.seek(0),ui=>ui.click("coaching-best-line"),ui=>ui.click("coaching-mainline"),
    ui=>{ui.get("coach-overview-box").open=true;ui.get("coach-overview-box").dispatch("toggle");},
    ui=>ui.api.showEvidence(0),ui=>ui.click("coach-retry"),ui=>ui.api.acceptForeign(snapshot({revision:2,job_id:"foreign"})),
    ui=>ui.click("coaching-start"),ui=>{void ui.api.loadSource("another");},ui=>ui.click("coaching-return"),
    ui=>ui.lifecycle.pagehide(),ui=>{ui.document.hidden=true;ui.lifecycle["document-visibilitychange"]();},
    ui=>{ui.get("coaching-side").value="black";ui.get("coaching-side").dispatch("change");}
  ]) {
    const ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(continuationSnapshot());await ui.click("coach-continuation");await ui.voiceStep();
    const stale=ui.speech.spoken.at(-1);stale.onend();const timer=[...ui.timeouts.values()].find((item)=>item.ms===600).callback;
    await operation(ui);const count=ui.motion.length;stale.onend();stale.onerror();timer();await flush();
    assert.equal(ui.motion.length,count);assert.equal(await ui.continuationStep(),false);
  }
});

test("retry hides the continuation completely, and revealed retry feedback never starts the original solution",async()=>{
  const report=continuationSnapshot(),ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(report);
  await ui.click("coach-continuation");const stale=[...ui.timeouts.values()].find((item)=>item.ms===300).callback;
  await ui.click("coach-retry");stale();assert.equal(ui.get("coach-continuation").hidden,true);assert.equal(await ui.continuationStep(),false);
  await ui.click("coach-continuation");await ui.voiceStep();assert.equal(ui.speech.spoken.at(-1).text,report.moves[0].coach.perspectives.white.hint);
  await ui.click("coach-retry-reveal");assert.equal(ui.get("coach-continuation").hidden,true);await ui.click("coach-continuation");assert.equal(await ui.continuationStep(),false);
  await ui.click("coach-retry-cancel");assert.equal(ui.get("coach-continuation").hidden,false);
});

test("muting or unavailable speech uses bounded reading time, errors recover, and selected perspective is spoken",async()=>{
  const ui=browser({voices:[{lang:"en-US",localService:true}]}),report=continuationSnapshot();
  report.moves[0].coach.perspectives.black.continuation.intro=Array(40).fill("word").join(" ");
  await ui.start(report);ui.get("coaching-side").value="black";ui.get("coaching-side").dispatch("change");await ui.click("coach-continuation");await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,report.moves[0].coach.perspectives.black.continuation.intro);
  ui.speech.spoken.at(-1).onerror();assert.ok([...ui.timeouts.values()].some((item)=>item.ms===8800));
  await ui.click("coach-speak");assert.ok([...ui.timeouts.values()].some((item)=>item.ms===8800));
  await ui.continuationStep();assert.equal(ui.motion.at(-1).ply,1);assert.equal(await ui.voiceStep(),false);
  assert.ok([...ui.timeouts.values()].some((item)=>item.ms>=2200 && item.ms<=9000));
});

test("strategic overview and three deeper parts speak the selected perspective and show exact evidence frames",async()=>{
  const report=strategySnapshot(),original=JSON.stringify(report),ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(report);
  const insight=report.moves[0].coach.perspectives.white.insight;
  assert.equal(ui.get("coach-strategy").hidden,false);assert.equal(ui.get("coach-dialogue").textContent,insight.summary);
  await ui.voiceStep();assert.equal(ui.speech.spoken.at(-1).text,insight.summary);
  for(const section of insight.sections){
    await ui.click("coach-strategy-"+section.id);await ui.voiceStep();
    assert.equal(ui.get("coach-dialogue").textContent,section.text);assert.equal(ui.speech.spoken.at(-1).text,section.text);
    assert.deepEqual(JSON.parse(JSON.stringify(ui.motion.at(-1).pieces)),report.moves[0].best.pv_frames[section.ply_index].board);
    assert.equal(ui.motion.at(-1).ply,section.ply_index);
    assert.equal(ui.get("coach-strategy-"+section.id).attributes["aria-pressed"],"true");
    assert.ok(ui.square(section.squares[0]).classList.contains("coaching-evidence-square"));assert.equal(ui.badges().length,0);
  }
  await ui.click("coach-strategy-return");assert.deepEqual(JSON.parse(JSON.stringify(ui.motion.at(-1).pieces)),report.frames[1].board);assert.equal(ui.get("coach-dialogue").textContent,insight.summary);
  ui.get("coaching-side").value="black";ui.get("coaching-side").dispatch("change");await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,report.moves[0].coach.perspectives.black.insight.summary);assert.equal(JSON.stringify(report),original);
});

test("positive lessons explain the actual move and reject missing, bounded or mismatched evidence",async()=>{
  const positive=strategySnapshot(true),ui=browser();await ui.start(positive);await ui.click("coach-strategy-plan");
  assert.deepEqual(JSON.parse(JSON.stringify(ui.motion.at(-1).pieces)),positive.moves[0].played.pv_frames[3].board);assert.equal(ui.get("coach-continuation").hidden,true);
  for(const mutate of [row=>row.classification="uncertain",row=>row.best.bound="upper",row=>row.best.pv_frames[0].fen="wrong root",
    row=>row.coach.perspectives.white.insight.sections[1].ply_index=50,row=>row.coach.perspectives.white.insight.sections[0].line="played",
    row=>row.coach.perspectives.white.insight.sections[1].id="idea",row=>row.coach.perspectives.white.insight.sections[2].squares=["z9"],
    row=>row.coach.perspectives.white.insight.summary="",row=>row.coach.perspectives.white.insight=null]){
    const report=strategySnapshot();mutate(report.moves[0]);const rejected=browser();await rejected.start(report);
    assert.equal(rejected.get("coach-strategy").hidden,true);await rejected.click("coach-strategy-idea");assert.equal(rejected.get("coaching-variation").hidden,true);
  }
});

test("strategy speech and evidence stop on replay, retry, new selection, overview and page lifecycle",async()=>{
  for(const operation of [ui=>ui.click("coach-continuation"),ui=>ui.click("coach-retry"),ui=>ui.api.seek(0),
    ui=>{ui.get("coach-overview-box").open=true;ui.get("coach-overview-box").dispatch("toggle");},
    ui=>ui.api.acceptForeign(snapshot({revision:2,job_id:"another"})),ui=>ui.click("coaching-return"),
    ui=>{ui.document.hidden=true;ui.lifecycle["document-visibilitychange"]();}]){
    const ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(strategySnapshot());await ui.click("coach-strategy-plan");
    const stale=[...ui.timeouts.values()].find(timer=>timer.ms===300).callback;await operation(ui);ui.api.render();
    const spoken=ui.speech.spoken.length;stale();assert.equal(ui.speech.spoken.length,spoken);
    assert.equal(ui.get("coach-strategy-return").hidden,true);
  }
  const ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(strategySnapshot());await ui.click("coach-retry");
  assert.equal(ui.get("coach-strategy").hidden,true);await ui.click("coach-strategy-plan");await ui.voiceStep();
  assert.doesNotMatch(ui.speech.spoken.at(-1).text,/open file|rook to b1/);
});

test("opening the game overview from a deep explanation reads the overview, not the move summary",async()=>{
  const ui=browser({voices:[{lang:"en-US",localService:true}]}),report=strategySnapshot();await ui.start(report);
  await ui.click("coach-strategy-plan");await ui.voiceStep();
  ui.get("coach-overview-box").open=true;ui.get("coach-overview-box").dispatch("toggle");await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,report.coach.summary);assert.equal(ui.get("coach-strategy").hidden,true);
  ui.get("coach-overview-box").open=false;ui.get("coach-overview-box").dispatch("toggle");await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,report.moves[0].coach.perspectives.white.insight.summary);
});

test("strategy selection cancels six-ply replay and normal variation stepping leaves the deep explanation",async()=>{
  const ui=browser();await ui.start(strategySnapshot());await ui.click("coach-continuation");
  const stale=[...ui.timeouts.values()].find(timer=>timer.ms>=2200 && timer.ms<=9000).callback;
  await ui.click("coach-strategy-plan");const position=JSON.stringify(ui.motion.at(-1));stale();assert.equal(JSON.stringify(ui.motion.at(-1)),position);
  assert.equal(await ui.continuationStep(),false);await ui.click("coaching-line-next");assert.equal(ui.get("coach-strategy-return").hidden,true);
  assert.equal(ui.get("coach-dialogue").textContent,strategySnapshot().moves[0].coach.perspectives.white.insight.summary);
});

test("opening choices explain both plans and the measured balance on the actual board with speech",async()=>{
  const report=openingSnapshot(),original=JSON.stringify(report),ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(report);
  const opening=report.moves[0].coach.opening;
  assert.equal(ui.get("coach-opening").hidden,false);assert.match(ui.get("coach-opening-sides").textContent,/White test setup.*Black test response/);
  const primary=ui.get("coach-dialogue").textContent;
  await ui.click("coach-strategy-plan");
  for(const part of opening.sections){
    await ui.click("coach-opening-"+part.id);await ui.voiceStep();
    assert.equal(ui.get("coach-dialogue").textContent,part.text);assert.equal(ui.speech.spoken.at(-1).text,part.text);
    assert.equal(ui.get("coaching-variation").hidden,true);assert.equal(ui.get("coach-strategy-return").hidden,true);
    assert.equal(JSON.stringify(ui.motion.at(-1).pieces),JSON.stringify(report.frames[1].board));
    assert.ok(ui.square("g1").classList.contains("coaching-evidence-square"));
    assert.equal(ui.get("coach-opening-"+part.id).attributes["aria-pressed"],"true");
  }
  await ui.click("coach-opening-return");assert.equal(ui.get("coach-dialogue").textContent,primary);
  assert.equal(ui.get("coach-opening-return").hidden,true);assert.equal(JSON.stringify(report),original);
});

test("opening packets reject stale positions, malformed sections and absent knowledge",async()=>{
  for(const mutate of [row=>row.coach.opening=null,row=>row.classification="pending",row=>row.coach.opening.fen="different position",
    row=>row.coach.opening.ply_index=25,row=>row.coach.opening.ply_index=2,row=>row.coach.opening.white.name="",
    row=>row.coach.opening.sections.pop(),row=>row.coach.opening.sections[2].id="white",row=>row.coach.opening.sections[0].text="",
    row=>row.coach.opening.focus_squares=["z9"],row=>row.uci="e1c1",row=>row.fen_before="wrong root"]){
    const report=openingSnapshot();mutate(report.moves[0]);const ui=browser();await ui.start(report);
    const primary=ui.get("coach-dialogue").textContent;
    assert.equal(ui.get("coach-opening").hidden,true);await ui.click("coach-opening-white");
    assert.equal(ui.get("coach-dialogue").textContent,primary);assert.equal(ui.get("coach-opening-return").hidden,true);
  }
});

test("opening narration cancels on navigation, side, source, strategy, guide, practice and lifecycle",async()=>{
  const operations=[ui=>ui.click("coach-strategy-idea"),ui=>ui.click("coach-continuation"),ui=>ui.click("coach-retry"),ui=>ui.api.seek(0),
    ui=>ui.api.showLine("best"),ui=>{ui.get("coaching-side").value="black";ui.get("coaching-side").dispatch("change");},
    ui=>{ui.get("coach-overview-box").open=true;ui.get("coach-overview-box").dispatch("toggle");},
    ui=>ui.api.acceptForeign(snapshot({revision:2,job_id:"another"})),ui=>ui.click("coaching-return"),
    ui=>{ui.document.hidden=true;ui.lifecycle["document-visibilitychange"]();}];
  for(const operation of operations){
    const ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(openingSnapshot());await ui.click("coach-opening-balance");
    const stale=[...ui.timeouts.values()].find(timer=>timer.ms===300).callback;
    await operation(ui);ui.api.render();const count=ui.speech.spoken.length;stale();
    assert.equal(ui.speech.spoken.length,count);assert.equal(ui.get("coach-opening-return").hidden,true);
  }
  const ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(openingSnapshot());await ui.click("coach-retry");
  assert.equal(ui.get("coach-opening").hidden,true);await ui.click("coach-opening-white");await ui.voiceStep();
  assert.notEqual(ui.speech.spoken.at(-1).text,openingSnapshot().moves[0].coach.opening.sections[0].text);
});

test("opening to overview reads the overview and returning keeps ordinary move feedback",async()=>{
  const report=openingSnapshot(),ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(report);
  await ui.click("coach-opening-black");await ui.voiceStep();
  ui.get("coach-overview-box").open=true;ui.get("coach-overview-box").dispatch("toggle");await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,report.coach.summary);assert.equal(ui.get("coach-opening").hidden,true);
  ui.get("coach-overview-box").open=false;ui.get("coach-overview-box").dispatch("toggle");await ui.voiceStep();
  assert.equal(ui.speech.spoken.at(-1).text,report.moves[0].coach.perspectives.white.insight.summary);
  assert.equal(ui.get("coach-opening").hidden,false);
});

test("opening review entry remains discoverable from later lessons and finds the latest valid opening checkpoint",async()=>{
  const report=taughtSnapshot();report.moves[0].coach.opening=structuredClone(openingSnapshot().moves[0].coach.opening);
  report.moves[0].coach.opening.fen=report.moves[0].fen_after;
  const ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(report);ui.api.seek(3);
  assert.equal(ui.get("coach-opening").hidden,true);assert.equal(ui.get("coach-opening-jump").hidden,false);
  await ui.click("coach-opening-jump");await ui.voiceStep();
  assert.equal(ui.get("coach-opening").hidden,false);assert.equal(ui.speech.spoken.at(-1).text,report.moves[0].coach.opening.sections[0].text);
  assert.equal(JSON.stringify(ui.motion.at(-1).pieces),JSON.stringify(report.frames[1].board));
  await ui.click("coach-retry");assert.equal(ui.get("coach-opening-jump").hidden,true);
  const none=browser();await none.start(strategySnapshot());assert.equal(none.get("coach-opening-jump").hidden,true);
  const later=structuredClone(report);later.moves[2].coach.opening=structuredClone(later.moves[0].coach.opening);
  later.moves[2].coach.opening.ply_index=3;later.moves[2].coach.opening.fen=later.moves[2].fen_after;
  const latest=browser();await latest.start(later);await latest.click("coach-opening-jump");
  assert.equal(JSON.stringify(latest.motion.at(-1).pieces),JSON.stringify(later.frames[3].board));
});

test("unrelated analysis progress keeps the opening lesson speaking while changed evidence cancels it",async()=>{
  const report=openingSnapshot();report.status="running";report.progress={completed:1,total:3};
  const ui=browser({voices:[{lang:"en-US",localService:true}]});await ui.start(report);await ui.click("coach-opening-white");await ui.voiceStep();
  const text=ui.get("coach-dialogue").textContent,cancelled=ui.speech.cancelled;
  const next=structuredClone(report);next.progress.completed=2;void ui.api.poll();await ui.finish(next);
  assert.equal(ui.get("coach-dialogue").textContent,text);assert.equal(ui.get("coach-opening-return").hidden,false);
  assert.equal(ui.speech.cancelled,cancelled);assert.equal(await ui.voiceStep(),false);
  const changed=structuredClone(next);changed.progress.completed=3;changed.moves[0].coach.opening.sections[0].text="New verified opening explanation.";
  void ui.api.poll();await ui.finish(changed);
  assert.equal(ui.get("coach-opening-return").hidden,true);assert.notEqual(ui.get("coach-dialogue").textContent,text);
});

test("all UI ids exist and output has no HTML interpolation sink",()=>{
  const js=fs.readFileSync(path.join(__dirname,"../web/human-play/coaching.js"),"utf8");
  const html=fs.readFileSync(path.join(__dirname,"../web/human-play/index.html"),"utf8");
  for(const match of js.matchAll(/get\("([a-z][a-z-]+)"\)/g))assert.ok(html.includes('id="'+match[1]+'"'),match[1]);
  assert.doesNotMatch(js,/innerHTML|insertAdjacentHTML/);
  assert.match(html,/src="\/coaching.js"/);
});

(async()=>{for(const {name,run} of cases){await run();console.log("PASS "+name);}console.log("Browser coaching: "+cases.length+" checks passed.");})().catch((error)=>{console.error(error);process.exitCode=1;});
