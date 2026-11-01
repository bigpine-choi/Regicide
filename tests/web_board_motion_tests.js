"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
function environment({reduced=false,stored=null}={}) {
  const animations=[],sounds=[],events={},media={matches:reduced,addEventListener(_name,fn){this.change=fn;}};
  let storageReads=0,storageWrites=0;
  class Element {
    constructor(tag="div"){this.tag=tag;this.children=[];this.dataset={};this.style={};this.listeners={};this.attributes={};this.className="";this.checked=true;this.disabled=false;}
    append(...items){for(const item of items)for(const child of item.tag==="fragment" ? [...item.children] : [item]){child.remove();child.parentElement=this;this.children.push(child);}}
    remove(){if(this.parentElement){this.parentElement.children=this.parentElement.children.filter((x)=>x!==this);this.parentElement=null;}}
    replaceChildren(...items){for(const child of [...this.children])child.remove();this.append(...items);}
    setAttribute(name,value){this.attributes[name]=String(value);}
    addEventListener(name,fn){this.listeners[name]=fn;}
    querySelector(selector){return this.children.find((child)=>selector.startsWith(".") && child.className.split(" ").includes(selector.slice(1)));}
    animate(keyframes,options){
      let finish;const animation={element:this,keyframes,options,cancelled:false,finished:new Promise((resolve)=>{finish=resolve;}),finish:()=>finish(),cancel(){this.cancelled=true;finish();}};
      animations.push(animation);return animation;
    }
    getBoundingClientRect(){return {width:400,height:400};}
  }
  const context={window:{RegicideMoveSound:{play:(kind)=>sounds.push(kind)},matchMedia:()=>media,addEventListener:(name,fn)=>{events[name]=fn;}},
    document:{createElement:(tag)=>new Element(tag),hidden:false,addEventListener:(name,fn)=>{events["document-"+name]=fn;}},
    localStorage:{getItem:()=>{storageReads++;return stored;},setItem(_key,value){storageWrites++;stored=value;}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../web/human-play/board_motion.js"),"utf8"),context);
  const board=new Element(),frame=(pieces,badgeSquare)=>{
    const fragment=new Element("fragment");
    for(const square of Object.keys(pieces)){
      const cell=new Element();cell.dataset.square=square;const glyph=new Element("span");glyph.className="piece";glyph.textContent=pieces[square];cell.append(glyph);
      if(square===badgeSquare){const badge=new Element("span");badge.className="coaching-move-badge";badge.textContent="??";cell.append(badge);}
      fragment.append(cell);
    }
    return fragment;
  };
  return {api:context.window.RegicideMotion,board,frame,animations,sounds,events,media,document:context.document,get stored(){return stored;},
    get storageReads(){return storageReads;},get storageWrites(){return storageWrites;},
    glyph(square){return board.children.find((child)=>child.dataset.square===square)?.querySelector(".piece");},
    badge(){return board.children.flatMap((cell)=>cell.children).find((child)=>child.className==="coaching-move-badge");},
    render(state,badgeSquare){context.window.RegicideMotion.replace(board,frame(state.pieces,badgeSquare),state);},
    layers(){return board.children.filter((child)=>child.className==="board-motion-layer");}};
}
const position=(pieces,extra={})=>({scope:"game-a",ply:0,turn:"white",lastMove:null,orientation:"white",pieces,enabled:true,...extra});
const simpleBefore=()=>position({e1:"K",e8:"k",e2:"P"},{legalMoves:["e2e4"]});
const simpleAfter=()=>position({e1:"K",e8:"k",e4:"P"},{ply:1,turn:"black",lastMove:"e2e4",legalMoves:["e8d8"]});
const cases=[];const test=(name,run)=>cases.push({name,run});
test("single known forward move plans from authoritative board difference",()=>{
  const {api}=environment(),before=simpleBefore(),after=simpleAfter(),plan=api.plan(before,after);
  assert.equal(plan.moves.length,1);assert.equal(plan.moves[0].from,"e2");assert.equal(plan.moves[0].to,"e4");
  assert.equal(plan.captures.length,0);
  assert.equal(before.pieces.e2,"P");assert.equal(after.pieces.e4,"P");
});
test("mismatched metadata, jumps, flip and editor replacements do not animate",()=>{
  const {api}=environment(),before=simpleBefore(),after=simpleAfter();
  for(const change of [{scope:"other"},{ply:3},{ply:-1},{orientation:"black"},{turn:"white"},{lastMove:"e2e5"},{pieces:{...after.pieces,a4:"Q"}}])
    assert.equal(api.plan(before,{...after,...change}),null,JSON.stringify(change));
  assert.equal(api.plan({...before,legalMoves:["e2e3"]},after),null);
  assert.equal(api.plan(after,before).reverse,true);
});
test("opponent reply animates when the live host omits selectable moves",()=>{
  const {api}=environment(),before={...simpleAfter(),legalMoves:[]};
  const after=position({e1:"K",d8:"k",e4:"P"},{ply:2,turn:"white",lastMove:"e8d8",legalMoves:["e1d1"]});
  assert.equal(api.plan(before,after).moves[0].piece,"k");
});
test("ordinary capture fades only the actual captured piece",()=>{
  const {api}=environment();
  const before=position({e1:"K",e8:"k",c4:"B",f7:"p"}),after=position({e1:"K",e8:"k",f7:"B"},{ply:1,turn:"black",lastMove:"c4f7"});
  const plan=api.plan(before,after);assert.equal(plan.captures.length,1);assert.equal(plan.captures[0].piece,"p");assert.equal(plan.captures[0].square,"f7");
});
test("en passant removes the pawn on its actual square in both colors",()=>{
  const {api}=environment();
  for(const [before,after,square] of [
    [position({e1:"K",e8:"k",e5:"P",d5:"p"}),position({e1:"K",e8:"k",d6:"P"},{ply:1,turn:"black",lastMove:"e5d6"}),"d5"],
    [position({e1:"K",e8:"k",d4:"p",e4:"P"},{turn:"black"}),position({e1:"K",e8:"k",e3:"p"},{ply:1,turn:"white",lastMove:"d4e3"}),"e4"]]){
    const plan=api.plan(before,after);assert.equal(plan.captures[0].square,square);
  }
});
test("all four castles animate the king and rook together",()=>{
  const {api}=environment();
  for(const side of ["white","black"])for(const kingside of [true,false]){
    const rank=side==="white"?"1":"8",king=side==="white"?"K":"k",rook=side==="white"?"R":"r";
    const from="e"+rank,to=(kingside?"g":"c")+rank,rookFrom=(kingside?"h":"a")+rank,rookTo=(kingside?"f":"d")+rank;
    const before=position({[from]:king,[rookFrom]:rook},{turn:side}),after=position({[to]:king,[rookTo]:rook},{ply:1,turn:side==="white"?"black":"white",lastMove:from+to});
    const plan=api.plan(before,after);assert.equal(plan.moves.length,2);assert.equal(plan.moves[1].from,rookFrom);assert.equal(plan.moves[1].to,rookTo);
  }
});
test("promotion and promotion capture retain pawn origin and final piece",()=>{
  const {api}=environment();
  for(const promoted of ["q","r","b","n"]){
    const before=position({e1:"K",h8:"k",e7:"P",f8:"r"}),after=position({e1:"K",h8:"k",f8:promoted.toUpperCase()},{ply:1,turn:"black",lastMove:"e7f8"+promoted});
    const plan=api.plan(before,after);assert.equal(plan.promotion,true);assert.equal(plan.moves[0].piece,"P");assert.equal(plan.moves[0].arrives,promoted.toUpperCase());assert.equal(plan.captures[0].square,"f8");
  }
});
test("impossible transition shapes and blocked slides safely snap",()=>{
  const {api}=environment();
  const cases=[
    [position({e1:"K",e8:"k",e2:"P"}),position({e1:"K",e8:"k",e5:"P"},{ply:1,turn:"black",lastMove:"e2e5"})],
    [position({e1:"K",e8:"k",c4:"B",d5:"P"}),position({e1:"K",e8:"k",d5:"P",f7:"B"},{ply:1,turn:"black",lastMove:"c4f7"})],
    [position({e1:"K",h1:"R",f1:"B"}),position({g1:"K",f1:"R"},{ply:1,turn:"black",lastMove:"e1g1"})]
  ];
  for(const [before,after]of cases)assert.equal(api.plan(before,after),null);
});
test("state commits immediately while overlays have no input semantics",async()=>{
  const ui=environment();ui.render(simpleBefore());ui.render(simpleAfter());
  assert.equal(ui.glyph("e2"),undefined);assert.equal(ui.glyph("e4").textContent,"P");
  assert.equal(ui.glyph("e4").style.visibility,"hidden");
  assert.equal(ui.layers().length,1);assert.equal(ui.layers()[0].attributes["aria-hidden"],"true");
  assert.equal(ui.animations.length,1);assert.equal(ui.animations[0].options.duration,240);
  assert.match(ui.animations[0].keyframes[0].transform,/0%,200%/);
  ui.animations[0].finish();await flush();
  assert.equal(ui.layers().length,0);assert.equal(ui.glyph("e4").style.visibility,"");
});
test("same-position polling preserves the current animation without retriggering",async()=>{
  const ui=environment();ui.render(simpleBefore());ui.render(simpleAfter());const layer=ui.layers()[0];
  ui.render({...simpleAfter(),animate:false});ui.render(simpleAfter());
  assert.equal(ui.layers()[0],layer);assert.equal(ui.animations.length,1);assert.equal(ui.glyph("e4").style.visibility,"hidden");
  ui.animations[0].finish();await flush();assert.equal(ui.glyph("e4").style.visibility,"");
});

test("actual move badge stays hidden until all moving pieces land",async()=>{
  const ui=environment(),before=position({e1:"K",h1:"R"}),after=position({g1:"K",f1:"R"},{ply:1,turn:"black",lastMove:"e1g1"});
  ui.render(before);ui.render(after,"g1");
  assert.equal(ui.badge().style.visibility,"hidden");
  ui.animations[0].finish();await flush();assert.equal(ui.badge().style.visibility,"hidden");
  ui.animations[1].finish();await flush();assert.equal(ui.badge().style.visibility,"");
});

test("repaint and rapid reverse movement cannot reveal a badge from the unfinished animation",async()=>{
  const ui=environment();ui.render(simpleBefore());ui.render(simpleAfter(),"e4");
  ui.render({...simpleAfter(),animate:false},"e4");
  assert.equal(ui.badge().style.visibility,"hidden");assert.equal(ui.animations.length,1);
  const reply=position({e1:"K",d8:"k",e4:"P"},{ply:2,turn:"white",lastMove:"e8d8"});
  ui.render(reply,"d8");assert.equal(ui.animations[0].cancelled,true);assert.equal(ui.badge().style.visibility,"hidden");
  ui.render(simpleAfter(),"e4");
  assert.equal(ui.animations[1].cancelled,true);assert.equal(ui.badge().style.visibility,"hidden");
  for(const animation of ui.animations)animation.finish();await flush();
  assert.equal(ui.badge().parentElement.dataset.square,"e4");assert.equal(ui.badge().style.visibility,"");
});

test("badges are immediate with reduced motion or cancelled transitions",()=>{
  const reduced=environment({reduced:true});reduced.render(simpleBefore());reduced.render(simpleAfter(),"e4");
  assert.notEqual(reduced.badge().style.visibility,"hidden");assert.equal(reduced.animations.length,0);
  for(const action of ["reduced","resize","flip"]){
    const ui=environment();ui.render(simpleBefore());ui.render(simpleAfter(),"e4");
    if(action==="reduced"){ui.media.matches=true;ui.media.change();}
    else if(action==="resize")ui.events.resize();
    else ui.render({...simpleAfter(),orientation:"black"},"e4");
    assert.notEqual(ui.badge().style.visibility,"hidden");assert.equal(ui.layers().length,0);
  }
});
test("rapid next move cancels the previous overlay and reveals its committed piece",async()=>{
  const ui=environment();ui.render(simpleBefore());ui.render(simpleAfter());
  ui.render(position({e1:"K",d8:"k",e4:"P"},{ply:2,turn:"white",lastMove:"e8d8"}));
  assert.equal(ui.animations[0].cancelled,true);assert.equal(ui.animations.length,2);
  assert.equal(ui.layers().length,1);assert.notEqual(ui.glyph("e4").style.visibility,"hidden");
  ui.animations[1].finish();await flush();assert.equal(ui.layers().length,0);
});
test("castling has simultaneous motion and promotion crossfades to final glyph",async()=>{
  const ui=environment();
  ui.render(position({e1:"K",h1:"R"}));ui.render(position({g1:"K",f1:"R"},{ply:1,turn:"black",lastMove:"e1g1"}));
  assert.equal(ui.animations.length,2);assert.equal(ui.glyph("g1").style.visibility,"hidden");assert.equal(ui.glyph("f1").style.visibility,"hidden");
  ui.api.cancel(ui.board,true);
  ui.render(position({e7:"P",e1:"K",h8:"k"}));ui.render(position({e8:"Q",e1:"K",h8:"k"},{ply:1,turn:"black",lastMove:"e7e8q"}));
  assert.equal(ui.animations.length,4);assert.equal(ui.animations[2].keyframes.at(-1).opacity,0);assert.equal(ui.animations[3].keyframes.at(-1).opacity,1);
  ui.animations[2].finish();ui.animations[3].finish();await flush();assert.equal(ui.glyph("e8").textContent,"Q");assert.equal(ui.glyph("e8").style.visibility,"");
});
test("flip, jumps, hidden mode and resize clean all ghosts",()=>{
  for(const action of ["flip","jump","hidden","resize","leave"]){
    const ui=environment();ui.render(simpleBefore());ui.render(simpleAfter());
    if(action==="flip")ui.render({...simpleAfter(),orientation:"black"});
    if(action==="jump")ui.render({...simpleBefore(),ply:8});
    if(action==="hidden")ui.render({...simpleAfter(),enabled:false});
    if(action==="resize")ui.events.resize();
    if(action==="leave")ui.events.pagehide();
    assert.equal(ui.layers().length,0,action);assert.equal(ui.animations[0].cancelled,true,action);
  }
});
test("one-ply undo reverses motion and restores captured pieces without ghosts",async()=>{
  const ui=environment(),before=position({e1:"K",e8:"k",c4:"B",f7:"p"}),after=position({e1:"K",e8:"k",f7:"B"},{ply:1,turn:"black",lastMove:"c4f7"});
  const reverse=ui.api.plan(after,before);
  assert.equal(reverse.reverse,true);assert.equal(reverse.moves[0].from,"f7");assert.equal(reverse.restores[0].piece,"p");
  ui.render(after);ui.render(before);
  assert.equal(ui.glyph("f7").textContent,"p");assert.equal(ui.glyph("f7").style.visibility,"hidden");
  assert.equal(ui.glyph("c4").textContent,"B");assert.equal(ui.animations.length,2);
  for(const animation of ui.animations)animation.finish();await flush();
  assert.equal(ui.layers().length,0);assert.equal(ui.glyph("f7").style.visibility,"");
});
test("undo preserves castling, en passant and promotion transitions",()=>{
  const {api}=environment();
  for(const [before,after,restore]of [
    [position({e1:"K",h1:"R"}),position({g1:"K",f1:"R"},{ply:1,turn:"black",lastMove:"e1g1"}),0],
    [position({e1:"K",e8:"k",e5:"P",d5:"p"}),position({e1:"K",e8:"k",d6:"P"},{ply:1,turn:"black",lastMove:"e5d6"}),1],
    [position({e1:"K",h8:"k",e7:"P",f8:"r"}),position({e1:"K",h8:"k",f8:"N"},{ply:1,turn:"black",lastMove:"e7f8n"}),1]
  ]){
    const plan=api.plan(after,before);assert.equal(plan.reverse,true);assert.equal(plan.restores.length,restore);
    if(after.lastMove==="e1g1")assert.equal(plan.moves.length,2);
    if(after.lastMove==="e7f8n"){assert.equal(plan.moves[0].piece,"N");assert.equal(plan.moves[0].arrives,"P");}
  }
});
test("document hiding removes animation overlays and cancels effects",()=>{
  const ui=environment();ui.render(simpleBefore());ui.render(simpleAfter());
  ui.document.hidden=true;ui.events["document-visibilitychange"]();
  assert.equal(ui.layers().length,0);assert.equal(ui.animations[0].cancelled,true);
});
test("animation ignores legacy saved preferences while respecting system reduced motion",()=>{
  for(const stored of [null,"off","on"]){
    const ui=environment({stored});ui.render(simpleBefore());ui.render(simpleAfter());
    assert.equal(ui.animations.length,1);assert.equal(ui.layers().length,1);
    assert.equal(ui.storageReads,0);assert.equal(ui.storageWrites,0);assert.equal(ui.stored,stored);
  }
  const ui=environment({reduced:true,stored:"off"});ui.render(simpleBefore());ui.render(simpleAfter());
  assert.equal(ui.animations.length,0);assert.equal(ui.layers().length,0);
  ui.media.matches=false;ui.media.change();ui.render(simpleBefore());ui.render(simpleAfter());
  assert.ok(ui.animations.length>0);assert.equal(ui.layers().length,1);
  ui.media.matches=true;ui.media.change();assert.equal(ui.layers().length,0);assert.equal(ui.animations.at(-1).cancelled,true);
});
test("move sounds land once for human and opponent moves despite repeated polling",async()=>{
  const ui=environment();ui.render(simpleBefore());assert.deepEqual(ui.sounds,[]);
  ui.render(simpleAfter());ui.render(simpleAfter());assert.deepEqual(ui.sounds,[]);
  ui.animations[0].finish();await flush();ui.render(simpleAfter());assert.deepEqual(ui.sounds,["move"]);
  ui.render(position({e1:"K",d8:"k",e4:"P"},{ply:2,turn:"white",lastMove:"e8d8"}));
  ui.animations[1].finish();await flush();assert.deepEqual(ui.sounds,["move","move"]);
});
test("captures, en passant and promotion captures sound once; castle is one tap",async()=>{
  for(const [before,after,sound]of [
    [position({e1:"K",e8:"k",c4:"B",f7:"p"}),position({e1:"K",e8:"k",f7:"B"},{ply:1,turn:"black",lastMove:"c4f7"}),"capture"],
    [position({e1:"K",e8:"k",e5:"P",d5:"p"}),position({e1:"K",e8:"k",d6:"P"},{ply:1,turn:"black",lastMove:"e5d6"}),"capture"],
    [position({e1:"K",h8:"k",e7:"P",f8:"r"}),position({e1:"K",h8:"k",f8:"N"},{ply:1,turn:"black",lastMove:"e7f8n"}),"capture"],
    [position({e1:"K",h1:"R"}),position({g1:"K",f1:"R"},{ply:1,turn:"black",lastMove:"e1g1"}),"move"]
  ]) {
    const ui=environment();ui.render(before);ui.render(after);
    for(const animation of ui.animations)animation.finish();await flush();assert.deepEqual(ui.sounds,[sound]);
  }
});
test("cancel, undo, jumps, editor updates and hidden boards never emit stale sounds",async()=>{
  for(const action of ["cancel","undo","jump","editor","hidden","flip","resize","leave"]){
    const ui=environment();ui.render(simpleBefore());ui.render(simpleAfter());
    if(action==="cancel")ui.api.cancel(ui.board,true);
    if(action==="undo")ui.render(simpleBefore());
    if(action==="jump")ui.render({...simpleBefore(),ply:9});
    if(action==="editor")ui.render({...simpleBefore(),animate:false});
    if(action==="hidden")ui.render({...simpleBefore(),enabled:false});
    if(action==="flip")ui.render({...simpleAfter(),orientation:"black"});
    if(action==="resize")ui.events.resize();
    if(action==="leave")ui.events.pagehide();
    for(const animation of ui.animations)animation.finish();await flush();assert.deepEqual(ui.sounds,[],action);
  }
});
test("reduced motion sounds immediately and a fast reply preserves both taps without late duplicates",async()=>{
  const reduced=environment({reduced:true});reduced.render(simpleBefore());reduced.render(simpleAfter());reduced.render(simpleAfter());
  assert.equal(reduced.animations.length,0);assert.deepEqual(reduced.sounds,["move"]);
  const ui=environment();ui.render(simpleBefore());ui.render(simpleAfter());
  ui.render(position({e1:"K",d8:"k",e4:"P"},{ply:2,turn:"white",lastMove:"e8d8"}));
  assert.deepEqual(ui.sounds,["move"]);
  ui.animations[0].finish();await flush();assert.deepEqual(ui.sounds,["move"]);
  ui.animations[1].finish();await flush();assert.deepEqual(ui.sounds,["move","move"]);
});
test("CSS overlays cannot intercept moves and all three boards use the shared motion layer",()=>{
  const css=fs.readFileSync(path.join(__dirname,"../web/human-play/style.css"),"utf8");
  assert.match(css,/\.board-motion-layer\s*\{[^}]*pointer-events:none/);
  assert.match(css,/\.board-motion-glyph\s*\{[^}]*pointer-events:none/);
  assert.doesNotMatch(css,/\.motion-toggle/);
  assert.doesNotMatch(fs.readFileSync(path.join(__dirname,"../web/human-play/index.html"),"utf8"),/piece-animation|motion-toggle/);
  for(const file of ["app.js","analysis.js","coaching.js"])assert.match(fs.readFileSync(path.join(__dirname,"../web/human-play",file),"utf8"),/RegicideMotion\.replace/);
});
(async()=>{for(const {name,run}of cases){await run();console.log("PASS "+name);}console.log("Browser board motion: "+cases.length+" checks passed.");})().catch((error)=>{console.error(error);process.exitCode=1;});
