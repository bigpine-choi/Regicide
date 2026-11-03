"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class Element {
  constructor(tag="div") { this.tag=tag; this.children=[]; this.listeners={}; this.dataset={}; this.attributes={}; this.style={}; this.value=""; this.disabled=false; this.hidden=false; this.className=""; this.classList={add:(name)=>{this.className += " "+name;},remove:(name)=>{this.className=this.className.split(/\s+/).filter((item)=>item!==name).join(" ");},toggle:(name,on)=>{this.classList.remove(name);if(on)this.classList.add(name);}}; }
  append(...children) { for (const item of children) for(const child of item.tag === "fragment" ? item.children : [item]) {child.parentElement=this;this.children.push(child);} }
  replaceChildren(...children) { this.children=[]; this.append(...children); }
  before(element) { this.beforeElement=element; }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  setAttribute(name,value) { this.attributes[name]=value; }
  querySelector(selector) { return this.children.find((c)=>c.dataset.analysisSquare === selector.match(/"(.*?)"/)?.[1]); }
  closest(selector) { return this.tag==="button" && ((selector==="button[data-analysis-square]" && this.dataset.analysisSquare) || (selector==="button[data-piece]" && this.dataset.piece!==undefined)) ? this : this.parentElement?.closest(selector); }
  contains(element) { return this===element || this.children.some((child)=>child.contains(element)); }
  dispatch(name,event={}) { return this.listeners[name]?.(event); }
  focus() {} showModal() {this.open=true;} close() {this.open=false;}
}
const flush = async()=>{for(let i=0;i<40;i++) await Promise.resolve();};
function state(extra={}) {
  const fen="4k3/8/8/8/8/8/4P3/4K3 w - - 0 1";
  return {revision:0,game_id:"analysis-test",active:false,fen,board:{e1:"K",e8:"k",e2:"P"},turn:"white",legal_moves:[],can_undo:false,status:"editing",message:"기물 배치",moves:[],hint:{},...extra};
}
function active(extra={}) { return state({revision:1,active:true,status:"active",legal_moves:["e2e4","e1d1"],...extra}); }
function advised(extra={}) {
  const next=active(extra); next.hint={enabled:true,status:"ready",game_id:next.game_id,revision:next.revision,fen:next.fen,move:"e2e4",san:"e4",depth:3}; return next;
}
function evaluated(values={},position={}) {
  const next=advised(position);
  next.evaluation={game_id:next.game_id,revision:next.revision,fen:next.fen,turn:next.turn,
    status:"ready",cp:100,mate:null,depth:3,bound:"exact",white_share:null,winner:null,result:"*",source:"search",source_fen:next.fen,...values};
  next.reference_evaluation={...next.evaluation,source:"stockfish_gauge",...values};
  // The native recommendation score deliberately disagrees with the gauge.
  next.evaluation={...next.evaluation,cp:-7777,mate:null};
  return next;
}
async function browser() {
  const elements=new Map(),requests=[],events=[],lifecycle={},timeouts=new Map();
  let nextTimer=0, now=1000;
  const get=(id)=>{if(!elements.has(id)) elements.set(id,new Element());return elements.get(id);};
  get("analysis-time").value="1000";
  const context={
    document:{getElementById:get,querySelector:()=>({content:"token"}),createElement:(tag)=>new Element(tag),createElementNS:(_ns,tag)=>new Element(tag),createDocumentFragment:()=>new Element("fragment"),activeElement:null},
    window:{addEventListener:(name,fn)=>{lifecycle[name]=fn;},dispatchEvent:(event)=>events.push(event)},
    CustomEvent:class {constructor(type,init){this.type=type;this.detail=init.detail;}},
    fetch:(url,options)=>new Promise((resolve,reject)=>{
      options.signal?.addEventListener("abort",()=>reject(Object.assign(new Error("aborted"),{name:"AbortError"})),{once:true});
      requests.push({url,options,finish:(data,ok=true)=>resolve({ok,json:async()=>data})});
    }),
    AbortController,Date:{now:()=>now},setTimeout:(callback,ms)=>{const id=++nextTimer;timeouts.set(id,{callback,ms});return id;},
    clearTimeout:(id)=>timeouts.delete(id),setInterval:()=>1,clearInterval(){}
  };
  const source=fs.readFileSync(path.join(__dirname,"../web/human-play/analysis.js"),"utf8").replace("  loadDraft(initialFen);\n  render();", "  window.testApi={enter,leave,poll,action,accept,render,squareClick,readFen,loadDraft,hintMove};\n  loadDraft(initialFen);\n  render();");
  vm.runInNewContext(source,context);
  const ui={get,requests,events,lifecycle,timeouts,api:context.window.testApi,advance:(ms=300)=>{now+=ms;},async finish(data,ok=true){requests.at(-1).finish(data,ok);await flush();},async click(id){get(id).dispatch("click");await flush();}};
  ui.square=(name)=>get("analysis-board").children.find((item)=>item.dataset.analysisSquare===name);
  ui.piece=(name)=>get("analysis-palette").children.find((item)=>item.dataset.piece===name);
  ui.dragEvent=(target,transfer)=>({target,dataTransfer:transfer,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}});
  ui.beginDrag=(kind,name)=>{
    const transfer={dropEffect:"none",setData(type,value){this[type]=value;}};
    const event=ui.dragEvent(kind==="palette" ? ui.piece(name) : ui.square(name),transfer);
    get(kind==="palette" ? "analysis-palette" : "analysis-board").dispatch("dragstart",event);
    return {transfer,event};
  };
  ui.drop=(operation,square)=>{
    const element=ui.square(square),event=ui.dragEvent(element,operation.transfer);
    get("analysis-board").dispatch("dragover",event);
    get("analysis-board").dispatch("drop",event);
  };
  ui.dropOutside=(operation,target="analysis-trash")=>{
    const event=ui.dragEvent(get(target),operation.transfer);
    get("analysis-view").dispatch("dragover",event);
    get("analysis-view").dispatch("drop",event);
  };
  void ui.api.enter(); await ui.finish(state());
  return ui;
}
const cases=[]; const test=(name,run)=>cases.push({name,run});
test("editor paints and erases while preserving unrelated castling rights",async()=>{
  const ui=await browser();
  assert.equal(ui.get("live-view").hidden,true);
  assert.equal(ui.get("analysis-palette").children.length,13);
  await ui.api.squareClick("a4");
  assert.match(ui.get("analysis-fen").value,/ w KQkq - 0 1$/);
  assert.match(ui.get("analysis-fen").value,/K7/);
  ui.get("analysis-palette").children.at(-1).dispatch("click");
  await ui.api.squareClick("a4"); assert.doesNotMatch(ui.get("analysis-fen").value,/K7/);
  await ui.click("analysis-clear"); assert.equal(ui.get("analysis-fen").value,"8/8/8/8/8/8/8/8 w - - 0 1");
});
test("FEN import preserves side, castling, en passant and counters; malformed rows reject",async()=>{
  const ui=await browser(), fen="r3k2r/8/8/3pP3/8/8/8/R3K2R b KQkq e3 7 42";
  ui.api.loadDraft(fen); assert.equal(ui.get("analysis-fen").value,fen);
  for(const invalid of ["8/8 w - - 0 1","9/8/8/8/8/8/8/8 w - - 0 1","8/8/8/8/8/8/8/8 z - - 0 1"])
    assert.throws(()=>ui.api.readFen(invalid));
});
test("invalid server position stays editable with reason and no game mutations",async()=>{
  const ui=await browser(); await ui.click("analysis-clear"); await ui.click("analysis-start");
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).action,"start");
  await ui.finish({error:"백과 흑의 왕이 하나씩 필요합니다.",analysis:state()},false);
  assert.match(ui.get("analysis-error").textContent,/왕/); assert.equal(ui.get("analysis-start").disabled,false);
  assert.equal(ui.requests.some((r)=>r.url !== "/api/analysis"),false);
});
test("recommendation is shown as square markers and arrow without auto playing",async()=>{
  const ui=await browser(); await ui.click("analysis-start"); await ui.finish(advised());
  assert.match(ui.get("analysis-hint").textContent,/e4 · e2 → e4/);
  assert.equal(ui.get("analysis-apply").disabled,false);
  assert.ok(ui.get("analysis-board").children.find((e)=>e.tag === "svg"));
  assert.equal(ui.requests.length,2);
  await ui.click("analysis-apply");
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).move,"e2e4");
  assert.equal(ui.api.hintMove(),null);
  await ui.finish(active({revision:2,turn:"black",can_undo:true,board:{e1:"K",e8:"k",e4:"P"},legal_moves:["e8d8"]}));
  assert.match(ui.get("analysis-title").textContent,/흑/);
});
test("foreign or illegal recommendations never display",async()=>{
  for(const extra of [{revision:99},{fen:"old"},{game_id:"old"},{move:"e2e5"}]) {
    const ui=await browser(), next=advised(); Object.assign(next.hint,extra);
    await ui.click("analysis-start"); await ui.finish(next);
    assert.equal(ui.api.hintMove(),null); assert.equal(ui.get("analysis-apply").disabled,true);
  }
});
test("old poll after a move cannot restore the previous recommendation",async()=>{
  const ui=await browser(); await ui.click("analysis-start"); await ui.finish(advised());
  void ui.api.poll(); const old=ui.requests.at(-1);
  await ui.click("analysis-apply"); await ui.finish(active({revision:2,hint:{status:"searching"}}));
  old.finish(advised()); await flush();
  assert.equal(ui.api.hintMove(),null);
});
test("both sides move manually; returning cancels analysis and reveals live game",async()=>{
  const ui=await browser(); await ui.click("analysis-start"); await ui.finish(active({turn:"black",legal_moves:["e8d8"]}));
  await ui.api.squareClick("e8"); void ui.api.squareClick("d8"); await flush();
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).move,"e8d8");
  await ui.finish(active({revision:2})); await ui.click("live-mode-button");
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).action,"stop");
  await ui.finish(state({revision:3})); assert.equal(ui.get("live-view").hidden,false);
  assert.equal(ui.events.at(-1).detail,false);
});
test("terminal position has no recommendation and can undo",async()=>{
  const ui=await browser(); await ui.click("analysis-start");
  await ui.finish(active({status:"checkmate",message:"체크메이트",legal_moves:[],can_undo:true}));
  assert.equal(ui.get("analysis-apply").disabled,true); assert.equal(ui.get("analysis-undo").disabled,false);
  assert.match(ui.get("analysis-status").textContent,/체크메이트/);
});
test("promotion requires an explicit piece choice",async()=>{
  const ui=await browser(); await ui.click("analysis-start"); await ui.finish(active({legal_moves:["e7e8q","e7e8r","e7e8b","e7e8n"]}));
  await ui.api.squareClick("e7"); await ui.api.squareClick("e8");
  assert.equal(ui.get("analysis-promotion").open,true); assert.equal(ui.get("analysis-promotion-choices").children.length,4);
  assert.equal(ui.requests.length,2);
});
test("a successful poll clears a transient connection error and restores advice",async()=>{
  const ui=await browser(); await ui.click("analysis-start"); await ui.finish(advised());
  void ui.api.poll(); await ui.finish({error:"일시적인 연결 오류"},false);
  assert.equal(ui.get("analysis-error").hidden,false);
  assert.equal(ui.api.hintMove(),null);
  void ui.api.poll(); await ui.finish(advised());
  assert.equal(ui.get("analysis-error").textContent,"");
  assert.equal(ui.get("analysis-error").hidden,true);
  assert.equal(ui.api.hintMove(),"e2e4");
});
test("current-position loading times out and releases the editor controls",async()=>{
  const ui=await browser(); await ui.click("analysis-current");
  const request=ui.requests.at(-1), timer=[...ui.timeouts.values()].at(-1);
  assert.equal(request.url,"/api/state");
  assert.equal(timer.ms,10000);
  assert.equal(ui.get("analysis-start").disabled,true);
  assert.equal(request.options.signal.aborted,false);
  timer.callback(); await flush();
  assert.equal(request.options.signal.aborted,true);
  assert.equal(ui.get("analysis-current").disabled,false);
  assert.equal(ui.get("analysis-start").disabled,false);
  assert.match(ui.get("analysis-error").textContent,/시간이 초과/);
  assert.equal(ui.timeouts.size,0);
});
test("a failed stop still returns to the live screen and discards late advice",async()=>{
  const ui=await browser(); await ui.click("analysis-start"); await ui.finish(advised());
  void ui.api.poll(); const oldPoll=ui.requests.at(-1);
  await ui.click("live-mode-button"); await ui.finish({error:"연결이 끊겼습니다."},false);
  assert.equal(ui.get("live-view").hidden,false);
  assert.equal(ui.get("analysis-view").hidden,true);
  assert.equal(ui.events.at(-1).detail,false);
  assert.equal(ui.get("live-view").beforeElement.hidden,false);
  assert.match(ui.get("live-view").beforeElement.textContent,/분석 종료를 확인하지 못했습니다/);
  oldPoll.finish(advised()); await flush();
  assert.equal(ui.api.hintMove(),null);
  assert.equal(ui.get("analysis-view").hidden,true);
  assert.equal(ui.get("live-view").hidden,false);
});
test("palette drags copy in both orientations and preserve untouched home-piece rights",async()=>{
  for(const flipped of [false,true]) {
    const ui=await browser();
    ui.api.loadDraft("r3k2r/8/8/3pP3/8/8/8/R3K2R b KQkq e3 7 42");ui.api.render();
    if(flipped) await ui.click("analysis-flip");
    assert.equal(ui.get("analysis-board").children[0].dataset.analysisSquare,flipped ? "h1" : "a8");
    const before=ui.get("analysis-fen").value,operation=ui.beginDrag("palette","N");
    assert.equal(operation.transfer.effectAllowed,"copy");
    assert.equal(ui.get("analysis-fen").value,before);
    ui.drop(operation,"a4");
    assert.equal(operation.transfer.dropEffect,"copy");
    assert.equal(ui.api.readFen(ui.get("analysis-fen").value).pieces.a4,"N");
    assert.equal(ui.api.readFen(ui.get("analysis-fen").value).pieces.a1,"R");
    assert.match(ui.get("analysis-fen").value,/ b KQkq - 0 1$/);
    assert.equal(ui.piece("N").dataset.piece,"N");
  }
});
test("board dragging preserves its source until drop, moves and replaces without painting the brush",async()=>{
  const ui=await browser();ui.piece("q").dispatch("click");
  const before=ui.get("analysis-fen").value,operation=ui.beginDrag("board","e2");
  assert.equal(operation.transfer.effectAllowed,"move");
  assert.equal(ui.get("analysis-fen").value,before);
  assert.equal(ui.api.readFen(before).pieces.e2,"P");
  ui.drop(operation,"a1");
  assert.equal(operation.transfer.dropEffect,"move");
  const pieces=ui.api.readFen(ui.get("analysis-fen").value).pieces;
  assert.equal(pieces.e2,undefined);assert.equal(pieces.a1,"P");
  assert.match(ui.get("analysis-fen").value,/ w Kkq - 0 1$/);
});
test("completed off-board and trash drops remove board pieces while palette drops cancel",async()=>{
  const ui=await browser();
  ui.dropOutside(ui.beginDrag("board","e2"));
  assert.equal(ui.api.readFen(ui.get("analysis-fen").value).pieces.e2,undefined);
  ui.dropOutside(ui.beginDrag("board","e7"),"analysis-title");
  assert.equal(ui.api.readFen(ui.get("analysis-fen").value).pieces.e7,undefined);
  const before=ui.get("analysis-fen").value;
  ui.dropOutside(ui.beginDrag("palette","Q"));
  assert.equal(ui.get("analysis-fen").value,before);
});
test("Escape, release outside the window, same-square and invalid board drops preserve source and metadata",async()=>{
  const ui=await browser(),before=ui.get("analysis-fen").value;
  const cancelled=ui.beginDrag("board","e2");
  ui.get("analysis-view").dispatch("dragend",ui.dragEvent(ui.square("e2"),cancelled.transfer));
  assert.equal(ui.get("analysis-fen").value,before);
  ui.drop(ui.beginDrag("board","e2"),"e2");
  assert.equal(ui.get("analysis-fen").value,before);
  const invalid=ui.beginDrag("board","e2");
  ui.get("analysis-view").dispatch("drop",ui.dragEvent(ui.get("analysis-board"),invalid.transfer));
  assert.equal(ui.get("analysis-fen").value,before);
  const erase=ui.beginDrag("palette","");assert.equal(erase.event.prevented,true);
  ui.advance();ui.piece("P").dispatch("click");await ui.api.squareClick("e2");
  assert.equal(ui.get("analysis-fen").value,before);
  ui.piece("").dispatch("click");await ui.api.squareClick("a4");
  assert.equal(ui.get("analysis-fen").value,before);
});
test("rotation, FEN changes and busy requests cancel old editor drags",async()=>{
  const ui=await browser(),before=ui.get("analysis-fen").value;
  const rotated=ui.beginDrag("board","e2");await ui.click("analysis-flip");ui.drop(rotated,"e4");
  assert.equal(ui.get("analysis-fen").value,before);
  ui.drop(ui.beginDrag("board","e2"),"e4");
  assert.equal(ui.api.readFen(ui.get("analysis-fen").value).pieces.e4,"P");
  const imported=ui.beginDrag("board","e4");ui.api.loadDraft(before);ui.api.render();ui.dropOutside(imported);
  assert.equal(ui.get("analysis-fen").value,before);
  const pending=ui.beginDrag("board","e2");await ui.click("analysis-current");ui.dropOutside(pending);
  assert.equal(ui.get("analysis-fen").value,before);
  await ui.finish({error:"연결 오류"},false);
  assert.equal(ui.get("analysis-current").disabled,false);
});
test("leaving or starting cancels editor drags and running off-board drops never delete",async()=>{
  const ui=await browser(),before=ui.get("analysis-fen").value;
  const old=ui.beginDrag("board","e2");await ui.click("live-mode-button");ui.dropOutside(old);
  assert.equal(ui.get("analysis-fen").value,before);
  await ui.click("analysis-mode-button");await ui.finish(state());
  const starting=ui.beginDrag("board","e2");await ui.click("analysis-start");
  await ui.finish(active());ui.dropOutside(starting);
  const requestCount=ui.requests.length;
  assert.equal(ui.square("e2").draggable,true);assert.equal(ui.piece("Q").draggable,false);
  const running=ui.beginDrag("board","e2");assert.notEqual(running.event.prevented,true);
  ui.dropOutside(running);
  assert.equal(ui.beginDrag("palette","Q").event.prevented,true);
  assert.equal(ui.requests.length,requestCount);
  assert.equal(ui.get("analysis-fen").value,active().fen);
});
test("a drop suppresses its duplicate click while later click placement and erasing remain available",async()=>{
  const ui=await browser();ui.piece("q").dispatch("click");
  ui.drop(ui.beginDrag("board","e2"),"e4");
  const after=ui.get("analysis-fen").value;
  ui.get("analysis-board").dispatch("click",{target:ui.square("e4")});
  assert.equal(ui.get("analysis-fen").value,after);
  ui.advance();ui.get("analysis-board").dispatch("click",{target:ui.square("e2")});
  assert.equal(ui.api.readFen(ui.get("analysis-fen").value).pieces.e2,"q");
  ui.piece("").dispatch("click");ui.get("analysis-board").dispatch("click",{target:ui.square("e4")});
  assert.equal(ui.api.readFen(ui.get("analysis-fen").value).pieces.e4,undefined);
});
test("evaluation signs always use White's perspective and zero means balance",async()=>{
  for(const turn of ["white","black"]) for(const [cp,label,score] of [[175,"백 우세","+1.75"],[-230,"흑 우세","−2.30"],[0,"균형","0.00"]]) {
    const ui=await browser();await ui.click("analysis-start");
    const fen=state().fen.replace(" w ",turn==="white" ? " w " : " b ");
    await ui.finish(evaluated({cp},{turn,fen}));
    assert.equal(ui.get("analysis-evaluation-label").textContent,"Stockfish · "+label);
    assert.equal(ui.get("analysis-evaluation-score").textContent,score);
    const share=parseFloat(ui.get("analysis-evaluation-white").style.height);
    assert.ok(cp>0 ? share>50 : cp<0 ? share<50 : share===50);
    assert.match(ui.get("analysis-evaluation-bar").attributes["aria-label"],/Stockfish 평가, 백 기준.*승률이 아닙니다/);
  }
});
test("native scores cannot fill a missing Stockfish gauge",async()=>{
  const ui=await browser();await ui.click("analysis-start");
  const next=evaluated({cp:750});delete next.reference_evaluation;
  await ui.finish(next);
  assert.equal(ui.get("analysis-evaluation-score").textContent,"—");
  assert.equal(ui.get("analysis-evaluation-white").style.height,"50%");
  assert.match(ui.get("analysis-evaluation-label").textContent,/Stockfish.*계산 중/);
});
test("only completed Stockfish evaluations display while native advice remains independent",async()=>{
  const ui=await browser();await ui.click("analysis-start");
  await ui.finish(evaluated({status:"searching",cp:null,depth:null}));
  assert.equal(ui.get("analysis-evaluation-score").textContent,"—");
  void ui.api.poll();await ui.finish(evaluated({status:"searching",cp:-120,depth:1}));
  assert.equal(ui.get("analysis-evaluation-score").textContent,"—");
  assert.equal(ui.get("analysis-evaluation-white").style.height,"50%");
  void ui.api.poll();await ui.finish(evaluated({cp:160,depth:5}));
  assert.equal(ui.get("analysis-evaluation-score").textContent,"+1.60");
  assert.match(ui.get("analysis-evaluation-detail").textContent,/깊이 5/);
  assert.equal(ui.requests.filter((request)=>request.options.method==="POST").length,1);
  assert.ok(ui.requests.every((request)=>request.url==="/api/analysis"));
});
test("non-exact Stockfish bounds leave the gauge neutral",async()=>{
  for(const [bound,cp,label,score] of [["lower",-100,"백 평가 하한","≥ −1.00"],["upper",100,"백 평가 상한","≤ +1.00"]]) {
    const ui=await browser();await ui.click("analysis-start");await ui.finish(evaluated({cp,bound}));
    assert.equal(ui.get("analysis-evaluation-score").textContent,"—");
    assert.equal(ui.get("analysis-evaluation-white").style.height,"50%");
    assert.match(ui.get("analysis-evaluation-bar").className,/unknown/);
  }
});
test("mate scores including M0 preserve the winner and terminal draws remain balanced",async()=>{
  for(const [mate,winner,label,score,share] of [[3,"white","백 메이트 예상","+M3","100%"],[-2,"black","흑 메이트 예상","−M2","0%"],[0,"white","백 체크메이트","M0","100%"],[0,"black","흑 체크메이트","M0","0%"]]) {
    const ui=await browser();await ui.click("analysis-start");await ui.finish(evaluated({cp:null,mate,winner}));
    assert.equal(ui.get("analysis-evaluation-label").textContent,"Stockfish · "+label);
    assert.equal(ui.get("analysis-evaluation-score").textContent,score);
    assert.equal(ui.get("analysis-evaluation-white").style.height,share);
  }
  for(const [result,label,share] of [["1-0","백 승리","100%"],["0-1","흑 승리","0%"],["1/2-1/2","무승부","50%"]]) {
    const ui=await browser();await ui.click("analysis-start");
    await ui.finish(evaluated({status:"terminal",source:"chess_rules",result},{status:"draw",legal_moves:[]}));
    assert.equal(ui.get("analysis-evaluation-label").textContent,label);
    assert.equal(ui.get("analysis-evaluation-score").textContent,result);
    assert.equal(ui.get("analysis-evaluation-white").style.height,share);
  }
});
test("flipping the board swaps the bar ends while retaining the White POV score",async()=>{
  const ui=await browser();await ui.click("analysis-start");await ui.finish(evaluated({cp:100,white_share:.6}));
  assert.equal(ui.get("analysis-evaluation-top").textContent,"흑");
  assert.equal(ui.get("analysis-evaluation-bottom").textContent,"백");
  await ui.click("analysis-flip");
  assert.match(ui.get("analysis-evaluation-bar").className,/black-view/);
  assert.equal(ui.get("analysis-evaluation-top").textContent,"백");
  assert.equal(ui.get("analysis-evaluation-bottom").textContent,"흑");
  assert.equal(ui.get("analysis-evaluation-white").style.height,"60%");
  assert.equal(ui.get("analysis-evaluation-score").textContent,"+1.00");
  await ui.click("analysis-flip");assert.doesNotMatch(ui.get("analysis-evaluation-bar").className,/black-view/);
});
test("foreign and unavailable evaluation snapshots never display an old score",async()=>{
  for(const values of [{game_id:"old"},{revision:99},{fen:"old"},{turn:"black"},{source_fen:"old"},{source:"search"},{status:"unavailable"},{status:"waiting",cp:999},{cp:"200",mate:null}]) {
    const ui=await browser();await ui.click("analysis-start");await ui.finish(evaluated(values));
    assert.equal(ui.get("analysis-evaluation-score").textContent,"—");
    assert.match(ui.get("analysis-evaluation-bar").className,/unknown/);
    assert.equal(ui.get("analysis-evaluation-white").style.height,"50%");
  }
});
test("moves, connection errors and returning to the editor clear evaluation immediately",async()=>{
  const ui=await browser();await ui.click("analysis-start");await ui.finish(evaluated({cp:250}));
  await ui.click("analysis-apply");assert.equal(ui.get("analysis-evaluation-score").textContent,"—");
  await ui.finish(evaluated({cp:-50},{revision:2}));
  assert.equal(ui.get("analysis-evaluation-score").textContent,"−0.50");
  void ui.api.poll();await ui.finish({error:"연결 오류"},false);
  assert.equal(ui.get("analysis-evaluation-score").textContent,"—");
  void ui.api.poll();await ui.finish(evaluated({cp:-50},{revision:2}));
  assert.equal(ui.get("analysis-evaluation-score").textContent,"−0.50");
  await ui.click("analysis-edit");assert.equal(ui.get("analysis-evaluation-score").textContent,"—");
  await ui.finish(state({revision:3,evaluation:evaluated().evaluation}));
  assert.equal(ui.get("analysis-evaluation-score").textContent,"—");
});
test("a late pre-move poll cannot replace the current position's evaluation",async()=>{
  const ui=await browser();await ui.click("analysis-start");await ui.finish(evaluated({cp:900}));
  void ui.api.poll();const old=ui.requests.at(-1);
  await ui.click("analysis-apply");await ui.finish(evaluated({cp:-50},{revision:2}));
  old.finish(evaluated({cp:900}));await flush();
  assert.equal(ui.get("analysis-evaluation-score").textContent,"−0.50");
});
test("the analysis evaluation explains its units and distinguishes the bar from a win probability",async()=>{
  const html=fs.readFileSync(path.join(__dirname,"../web/human-play/index.html"),"utf8");
  assert.match(html,/id="analysis-evaluation-bar" class="evaluation-bar unknown"/);
  assert.match(html,/Stockfish 평가 · 백 기준 · \+는 백 \/ −는 흑 우세 · 1\.00 ≈ 폰 한 개/);
  assert.match(html,/막대는 승률이 아닙니다/);
});
test("four castling rights follow FEN imports, manual toggles and standard presets",async()=>{
  const ui=await browser();
  for(const key of "KQkq") {assert.equal(ui.get("analysis-right-"+key).checked,true);assert.equal(ui.get("analysis-right-"+key).disabled,false);}
  await ui.click("analysis-clear");
  for(const key of "KQkq") {assert.equal(ui.get("analysis-right-"+key).checked,false);assert.equal(ui.get("analysis-right-"+key).disabled,true);}
  ui.get("analysis-fen").value="r3k2r/8/8/8/8/8/8/R3K2R b Kq - 7 42";
  await ui.click("analysis-import");
  for(const key of "KQkq") assert.equal(ui.get("analysis-right-"+key).checked,"Kq".includes(key));
  for(const key of "Qk") {ui.get("analysis-right-"+key).checked=true;ui.get("analysis-right-"+key).dispatch("change");}
  assert.match(ui.get("analysis-fen").value,/ b KQkq - 7 42$/);
  for(const key of "Kq") {ui.get("analysis-right-"+key).checked=false;ui.get("analysis-right-"+key).dispatch("change");}
  assert.match(ui.get("analysis-fen").value,/ b Qk - 7 42$/);
  await ui.click("analysis-standard");
  for(const key of "KQkq") assert.equal(ui.get("analysis-right-"+key).checked,true);
});
test("rights need their home king and rook and survive unrelated editor changes",async()=>{
  const ui=await browser();ui.api.loadDraft("4k3/8/8/8/8/8/8/4K2R w - - 0 1");ui.api.render();
  assert.equal(ui.get("analysis-right-K").disabled,false);
  for(const key of "Qkq") assert.equal(ui.get("analysis-right-"+key).disabled,true);
  ui.get("analysis-right-K").checked=true;ui.get("analysis-right-K").dispatch("change");
  assert.match(ui.get("analysis-fen").value,/ w K - 0 1$/);
  ui.get("analysis-right-Q").checked=true;ui.get("analysis-right-Q").dispatch("change");
  assert.equal(ui.get("analysis-right-Q").checked,false);
  ui.drop(ui.beginDrag("palette","P"),"a4");
  assert.match(ui.get("analysis-fen").value,/ w K - 0 1$/);
  assert.equal(ui.get("analysis-right-K").checked,true);
  assert.equal(ui.get("analysis-right-K").disabled,false);
  ui.get("analysis-right-K").checked=true;ui.get("analysis-right-K").dispatch("change");
  assert.match(ui.get("analysis-fen").value,/ w K - 0 1$/);
  ui.api.loadDraft("4k3/8/8/8/8/8/8/3K3R w - - 0 1");ui.api.render();
  assert.equal(ui.get("analysis-right-K").disabled,true);
  ui.api.loadDraft("4k3/8/8/8/8/8/8/4K2N w - - 0 1");ui.api.render();
  assert.equal(ui.get("analysis-right-K").disabled,true);
});
test("an invalid imported right remains intact for the backend to reject",async()=>{
  const ui=await browser(),fen="4k3/8/8/8/8/8/8/4K3 w K - 0 1";
  ui.get("analysis-fen").value=fen;await ui.click("analysis-import");
  assert.equal(ui.get("analysis-right-K").checked,true);
  assert.equal(ui.get("analysis-right-K").disabled,true);
  assert.equal(ui.get("analysis-fen").value,fen);
  await ui.click("analysis-start");
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).fen,fen);
  await ui.finish({error:"캐슬링 권리의 룩이 원래 자리에 있어야 합니다.",analysis:state()},false);
  assert.equal(ui.get("analysis-fen").value,fen);
  assert.match(ui.get("analysis-error").textContent,/캐슬링/);
  assert.equal(ui.get("analysis-start").disabled,false);
});
test("both colors can castle on either side through a single move and undo",async()=>{
  for(const [turn,side,move,movedFen] of [
    ["white","kingside","e1g1","r3k2r/8/8/8/8/8/8/R4RK1 b kq - 1 1"],
    ["white","queenside","e1c1","r3k2r/8/8/8/8/8/8/2KR3R b kq - 1 1"],
    ["black","kingside","e8g8","r4rk1/8/8/8/8/8/8/R3K2R w KQ - 1 2"],
    ["black","queenside","e8c8","2kr3r/8/8/8/8/8/8/R3K2R w KQ - 1 2"],
  ]) {
    const ui=await browser(),fen=`r3k2r/8/8/8/8/8/8/R3K2R ${turn==="white"?"w":"b"} KQkq - 0 1`;
    const position=active({turn,fen,board:ui.api.readFen(fen).pieces,legal_moves:[move]});
    await ui.click("analysis-start");await ui.finish(position);
    assert.equal(ui.get("analysis-castle-"+side).disabled,false);
    assert.equal(ui.get("analysis-castle-"+(side==="kingside"?"queenside":"kingside")).disabled,true);
    for(const key of "KQkq") assert.equal(ui.get("analysis-right-"+key).disabled,true);
    await ui.click("analysis-castle-"+side);
    const payload=JSON.parse(ui.requests.at(-1).options.body);
    assert.equal(payload.action,"move");assert.equal(payload.move,move);
    assert.equal(ui.get("analysis-castle-kingside").disabled,true);assert.equal(ui.get("analysis-castle-queenside").disabled,true);
    await ui.finish(active({revision:2,turn:turn==="white"?"black":"white",fen:movedFen,board:ui.api.readFen(movedFen).pieces,legal_moves:[],can_undo:true}));
    for(const key of turn==="white"?"KQ":"kq") assert.equal(ui.get("analysis-right-"+key).checked,false);
    await ui.click("analysis-undo");assert.equal(JSON.parse(ui.requests.at(-1).options.body).action,"undo");
    await ui.finish({...position,revision:3});
    assert.equal(ui.get("analysis-castle-"+side).disabled,false);
    assert.equal(ui.get("analysis-fen").value,fen);
    assert.deepEqual(ui.requests.filter((request)=>request.options.method==="POST").map((request)=>JSON.parse(request.options.body).action),["start","move","undo"]);
  }
});
test("castling buttons obey legal moves even with rights during check, blocked or attacked paths",async()=>{
  for(const fen of [
    "4r1k1/8/8/8/8/8/8/R3K2R w KQ - 0 1",
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "k4r2/8/8/8/8/8/8/4K2R w K - 0 1",
    "k5r1/8/8/8/8/8/8/4K2R w K - 0 1",
    "r3k2r/8/8/8/8/8/8/R3K2R w - - 0 1",
  ]) {
    const ui=await browser();await ui.click("analysis-start");
    await ui.finish(active({fen,board:ui.api.readFen(fen).pieces,legal_moves:["e1d1"]}));
    assert.equal(ui.get("analysis-castle-kingside").disabled,true);assert.equal(ui.get("analysis-castle-queenside").disabled,true);
    const count=ui.requests.length;await ui.click("analysis-castle-kingside");await ui.click("analysis-castle-queenside");
    assert.equal(ui.requests.length,count);
  }
});
test("a pending promotion blocks castle buttons until selection is cancelled",async()=>{
  const ui=await browser();await ui.click("analysis-start");
  await ui.finish(active({legal_moves:["e1g1","e7e8q","e7e8r","e7e8b","e7e8n"]}));
  assert.equal(ui.get("analysis-castle-kingside").disabled,false);
  await ui.api.squareClick("e7");await ui.api.squareClick("e8");
  assert.equal(ui.get("analysis-castle-kingside").disabled,true);
  const count=ui.requests.length;await ui.click("analysis-castle-kingside");assert.equal(ui.requests.length,count);
  await ui.click("analysis-promotion-cancel");assert.equal(ui.get("analysis-castle-kingside").disabled,false);
  await ui.api.squareClick("e8");
  assert.equal(ui.get("analysis-promotion").open,true);
  assert.equal(ui.get("analysis-castle-kingside").disabled,true);
  ui.get("analysis-promotion").open=false;ui.get("analysis-promotion").dispatch("close");
  assert.equal(ui.get("analysis-castle-kingside").disabled,false);
});
test("editor clears only touched home-piece rights and never restores a lost flag",async()=>{
  const ui=await browser(),open="r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 7 42";
  ui.api.loadDraft(open);ui.api.render();
  ui.drop(ui.beginDrag("board","h1"),"h2");assert.equal(ui.get("analysis-castling").value,"Qkq");
  ui.drop(ui.beginDrag("board","h2"),"h1");assert.equal(ui.get("analysis-castling").value,"Qkq");
  ui.drop(ui.beginDrag("board","e8"),"e7");assert.equal(ui.get("analysis-castling").value,"Q");
  ui.drop(ui.beginDrag("board","e7"),"e8");assert.equal(ui.get("analysis-castling").value,"Q");
  ui.api.loadDraft(open);ui.api.render();
  ui.drop(ui.beginDrag("board","e1"),"e2");assert.equal(ui.get("analysis-castling").value,"kq");
  ui.api.loadDraft("r3k2r/8/8/8/8/8/7R/R3K2R w KQkq - 0 1");ui.api.render();
  ui.drop(ui.beginDrag("board","h2"),"h1");assert.equal(ui.get("analysis-castling").value,"Qkq");
  ui.api.loadDraft(open.replace("KQkq","-"));ui.api.render();
  ui.drop(ui.beginDrag("palette","P"),"a4");assert.equal(ui.get("analysis-castling").value,"-");
});
test("clearing bishop and knight paths and editing a pawn retain the existing right then standard drag castles",async()=>{
  const ui=await browser();ui.piece("").dispatch("click");
  await ui.api.squareClick("f1");await ui.api.squareClick("g1");
  ui.piece("q").dispatch("click");ui.drop(ui.beginDrag("board","e2"),"e4");
  const fen=ui.get("analysis-fen").value;
  assert.match(fen,/ w KQkq - 0 1$/);
  await ui.click("analysis-start");assert.equal(JSON.parse(ui.requests.at(-1).options.body).fen,fen);
  await ui.finish(active({fen,board:ui.api.readFen(fen).pieces,legal_moves:["e1g1"]}));
  assert.equal(ui.get("analysis-editor").hidden,true);
  ui.drop(ui.beginDrag("board","e1"),"g1");await flush();
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).move,"e1g1");
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).action,"move");
});
test("running two-square and king-rook drags use canonical castling in both colors and orientations",async()=>{
  for(const turn of ["white","black"]) for(const side of ["king","queen"]) for(const flipped of [false,true]) for(const gesture of ["two-square","king-rook","rook-king"]) {
    const ui=await browser(),rank=turn==="white"?"1":"8",king="e"+rank,rook=(side==="king"?"h":"a")+rank,to=(side==="king"?"g":"c")+rank;
    const fen=`r3k2r/8/8/8/8/8/8/R3K2R ${turn==="white"?"w":"b"} KQkq - 0 1`;
    ui.piece("q").dispatch("click");await ui.click("analysis-start");
    await ui.finish(active({turn,fen,board:ui.api.readFen(fen).pieces,legal_moves:[king+to]}));
    if(flipped) await ui.click("analysis-flip");
    const source=gesture==="rook-king"?rook:king,target=gesture==="two-square"?to:gesture==="king-rook"?rook:king;
    const operation=ui.beginDrag("board",source);assert.notEqual(operation.event.prevented,true);
    ui.drop(operation,target);await flush();
    const payload=JSON.parse(ui.requests.at(-1).options.body);
    assert.equal(payload.action,"move");assert.equal(payload.move,king+to);
    assert.equal(ui.requests.length,3);
  }
});
test("legal king-rook click aliases work both ways while missing rights remain rejected",async()=>{
  for(const turn of ["white","black"]) for(const rookFirst of [false,true]) {
    const ui=await browser(),rank=turn==="white"?"1":"8",fen=`r3k2r/8/8/8/8/8/8/R3K2R ${turn==="white"?"w":"b"} KQkq - 0 1`;
    await ui.click("analysis-start");await ui.finish(active({turn,fen,board:ui.api.readFen(fen).pieces,legal_moves:["e"+rank+"g"+rank]}));
    await ui.api.squareClick((rookFirst?"h":"e")+rank);void ui.api.squareClick((rookFirst?"e":"h")+rank);await flush();
    assert.equal(JSON.parse(ui.requests.at(-1).options.body).move,"e"+rank+"g"+rank);
  }
  const ui=await browser(),fen="r3k2r/8/8/8/8/8/8/R3K2R w - - 0 1";
  await ui.click("analysis-start");await ui.finish(active({fen,board:ui.api.readFen(fen).pieces,legal_moves:["e1d1","h1h2"],castling:{king:{reason:"이 시작 위치에는 백 킹사이드 캐슬링 권리가 없습니다."}}}));
  const count=ui.requests.length;
  await ui.api.squareClick("e1");await ui.api.squareClick("h1");
  ui.drop(ui.beginDrag("board","e1"),"g1");
  ui.drop(ui.beginDrag("board","h1"),"e1");
  assert.equal(ui.requests.length,count);
  assert.equal(ui.get("analysis-fen").value,fen);
  assert.match(ui.get("analysis-castle-kingside-reason").textContent,/권리가 없습니다/);
});
test("same-position polling keeps a running drag source alive and a position change cancels it",async()=>{
  const ui=await browser();await ui.click("analysis-start");await ui.finish(evaluated());
  const source=ui.square("e2"),operation=ui.beginDrag("board","e2");
  void ui.api.poll();await ui.finish(evaluated({cp:125}));
  assert.equal(ui.square("e2"),source);
  ui.drop(operation,"e4");await flush();assert.equal(JSON.parse(ui.requests.at(-1).options.body).move,"e2e4");
  await ui.finish(evaluated({cp:20},{revision:2}));
  const old=ui.beginDrag("board","e2"),count=ui.requests.length;
  ui.api.accept(evaluated({cp:30},{revision:3}));ui.api.render();
  ui.drop(old,"e4");assert.equal(ui.requests.length,count);
  assert.equal(ui.get("analysis-evaluation-score").textContent,"+0.30");
});
test("active illegal and off-board drops preserve position and promotion drag requires a choice",async()=>{
  const ui=await browser();await ui.click("analysis-start");await ui.finish(active({legal_moves:["e2e4","e7e8q","e7e8r","e7e8b","e7e8n"],board:{e1:"K",e8:"k",e2:"P",e7:"P"}}));
  const fen=ui.get("analysis-fen").value,count=ui.requests.length;
  ui.drop(ui.beginDrag("board","e2"),"e5");ui.dropOutside(ui.beginDrag("board","e2"));
  assert.equal(ui.requests.length,count);assert.equal(ui.get("analysis-fen").value,fen);
  ui.drop(ui.beginDrag("board","e7"),"e8");
  assert.equal(ui.get("analysis-promotion").open,true);assert.equal(ui.requests.length,count);
  ui.get("analysis-promotion-choices").children[3].dispatch("click");await flush();
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).move,"e7e8n");
});
test("backend diagnostics remain available and connection errors block castle gestures",async()=>{
  const ui=await browser(),position=active({fen:"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",board:{e1:"K",h1:"R",a1:"R",e8:"k",h8:"r",a8:"r"},legal_moves:["e1g1"],castling:{king:{reason:"백 킹사이드 캐슬링이 가능합니다."},queen:{reason:"c1·d1 사이에 기물이 있습니다."}}});
  await ui.click("analysis-start");await ui.finish(position);
  assert.equal(ui.get("analysis-editor").hidden,true);
  assert.match(ui.get("analysis-castle-queenside-reason").textContent,/c1·d1/);
  void ui.api.poll();await ui.finish({error:"연결 오류"},false);
  assert.equal(ui.get("analysis-castle-kingside").disabled,true);
  assert.match(ui.get("analysis-castle-kingside-reason").textContent,/연결 오류/);
  assert.equal(ui.beginDrag("board","e1").event.prevented,true);
  const count=ui.requests.length;await ui.click("analysis-castle-kingside");assert.equal(ui.requests.length,count);
  void ui.api.poll();await ui.finish(position);assert.equal(ui.get("analysis-castle-kingside").disabled,false);
});
test("normal opening input events reach both standard two-square castling drags",async()=>{
  // Position fixtures independently replayed through HTTP and actual Regicide core.
  const fens=[
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
    "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
    "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
    "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
    "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
    "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
    "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 b kq - 5 4",
    "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 w kq - 6 5",
    "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/3P1N2/PPP2PPP/RNBQ1RK1 b kq - 0 5",
    "r1bq1rk1/pppp1ppp/2n2n2/2b1p3/2B1P3/3P1N2/PPP2PPP/RNBQ1RK1 w - - 1 6",
  ],moves="e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 e1g1 f8c5 d2d3 e8g8".split(" ");
  const ui=await browser(),frame=(index)=>active({revision:index+1,fen:fens[index],turn:fens[index].split(" ")[1]==="w"?"white":"black",board:ui.api.readFen(fens[index]).pieces,legal_moves:moves[index]?[moves[index]]:[],can_undo:index>0});
  await ui.click("analysis-start");await ui.finish(frame(0));
  for(let index=0;index<moves.length;index++) {
    const move=moves[index];ui.advance();
    if(index%2 || move==="e1g1") ui.drop(ui.beginDrag("board",move.slice(0,2)),move.slice(2,4));
    else {ui.get("analysis-board").dispatch("click",{target:ui.square(move.slice(0,2))});ui.get("analysis-board").dispatch("click",{target:ui.square(move.slice(2,4))});}
    await flush();assert.equal(JSON.parse(ui.requests.at(-1).options.body).move,move);
    await ui.finish(frame(index+1));assert.equal(ui.get("analysis-fen").value,fens[index+1]);
  }
  assert.equal(ui.requests.filter((request)=>request.options.method==="POST").length,11);
});
test("explorer opens its resulting FEN in separate analysis without a live-game move",async()=>{
  const ui=await browser(),fen="4k3/8/8/8/4P3/8/8/4K3 b - - 0 1";
  let scrollOptions=null;ui.get("analysis-view").scrollIntoView=(options)=>{scrollOptions=options;};
  const operation=ui.lifecycle["regicide-explorer-analyze"]({detail:{fen}}); await flush();
  const request=ui.requests.at(-1),payload=JSON.parse(request.options.body);
  assert.equal(request.url,"/api/analysis");assert.equal(payload.action,"start");assert.equal(payload.fen,fen);
  await ui.finish(active({fen,turn:"black",board:{e1:"K",e8:"k",e4:"P"},legal_moves:["e8d8"]}));await operation;
  assert.equal(ui.get("analysis-fen").value,fen);assert.match(ui.get("analysis-title").textContent,/흑/);
  assert.equal(scrollOptions.block,"start");
  assert.equal(ui.requests.some((item)=>item.url!=="/api/analysis"),false);
});
test("invalid explorer positions cannot start analysis",async()=>{
  const ui=await browser(),count=ui.requests.length;
  await ui.lifecycle["regicide-explorer-analyze"]({detail:{fen:"bad FEN"}});
  await ui.lifecycle["regicide-explorer-analyze"]({detail:{fen:42}});
  assert.equal(ui.requests.length,count);
});
(async()=>{for(const item of cases){await item.run();console.log("PASS "+item.name);}console.log(`${cases.length} browser analysis behavior cases passed.`);})().catch((error)=>{console.error(error);process.exitCode=1;});
