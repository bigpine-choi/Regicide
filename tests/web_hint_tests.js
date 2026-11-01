"use strict";

// Browser behavior tests without a browser dependency. Run: node tests/web_hint_tests.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.style = {};
    this.listeners = {};
    this.textContent = "";
    this.className = "";
    this.disabled = false;
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(" "); },
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, yes) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        if (yes ?? !names.has(name)) names.add(name); else names.delete(name);
        this.className = [...names].join(" ");
      }
    };
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  append(...items) { for (const item of items) this.children.push(...(item.tag === "fragment" ? item.children : [item])); }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  dispatch(name) { return this.listeners[name]?.({preventDefault() {}}); }
  querySelector(selector) { return this.children.find((item) => item.dataset.square === selector.match(/"(.*?)"/)?.[1]) || null; }
  focus() {}
}

function position(extra = {}) {
  return {game_id:"first", revision:0, fen:"start", human_color:"white", human_to_move:true,
    engine_to_move:false, result:"*", board:{e2:"P",e7:"p"}, moves:[], legal_moves:["e2e4","d2d4"],
    engine:{name:"Regicide", movetime_ms:1000}, hint_enabled:false, ...extra};
}
function snapshot(state, extra = {}) {
  return {enabled:true, game_id:state.game_id, revision:state.revision, fen:state.fen,
    status:"ready", move:"e2e4", san:"e4", ...extra};
}
const response = (data) => ({ok:true, json:async () => data});
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

async function browser(initial = position()) {
  const elements = new Map();
  const requests = [];
  const lifecycle = {};
  const context = {
    document:{
      querySelector:() => ({content:"token"}),
      getElementById:(id) => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
      },
      createElement:(tag) => new Element(tag),
      createElementNS:(_ns, tag) => new Element(tag),
      createDocumentFragment:() => new Element("fragment"),
      activeElement:null
    },
    fetch:(url, options) => {
      if (url === "/api/state") return Promise.resolve(response(initial));
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      requests.push({url, options, resolve:(data) => resolve(response(data)), reject});
      return promise;
    },
    window:{addEventListener:(name, fn) => { lifecycle[name] = fn; }},
    AbortController,
    setTimeout:() => 1, clearTimeout() {}, setInterval:() => 1, clearInterval() {}
  };
  const source = fs.readFileSync(path.join(__dirname, "../web/human-play/app.js"), "utf8")
    .replace("  void initialize();", "  window.testApi = {applyState, pollHint, render, currentHintMove, currentHintCandidates};\n  void initialize();");
  vm.runInNewContext(source, context);
  await flush();
  return {elements, requests, lifecycle, api:context.window.testApi,
    element:(id) => elements.get(id),
    click:async (id) => { elements.get(id).dispatch("click"); await flush(); },
    finish:async (request, data) => { request.resolve(data); await flush(); }};
}

const cases = [];
const test = (name, run) => cases.push({name, run});

test("default off and reload restores the saved preference", async () => {
  const off = await browser();
  await off.api.pollHint();
  assert.equal(off.requests.length, 0);
  assert.equal(off.element("hint-toggle").attributes["aria-pressed"], "false");
  const on = await browser(position({hint_enabled:true}));
  void on.api.pollHint();
  assert.equal(on.requests[0].url, "/api/hint");
  assert.equal(on.element("hint-toggle").attributes["aria-pressed"], "true");
});

test("a legacy legal recommendation draws a ranked arrow in both orientations", async () => {
  for (const color of ["white", "black"]) {
    const state = position({human_color:color});
    const ui = await browser(state);
    await ui.click("hint-toggle");
    await ui.finish(ui.requests[0], snapshot(state));
    assert.match(ui.element("hint-candidates").children[0].attributes["aria-label"], /1순위 · e4 · e2 → e4/);
    const squares = ui.element("board").children.filter((item) => item.dataset.square);
    assert.equal(squares.length, 64);
    assert.equal(squares[0].dataset.square, color === "white" ? "a8" : "h1");
    const svg = arrows(ui);
    assert.equal(svg.attributes["pointer-events"], "none");
    assert.equal(svg.attributes["aria-hidden"], "true");
    const group = svg.children.find((item) => item.attributes["data-hint-rank"] === "1");
    const line = group.children[1];
    assert.equal(line.attributes.stroke, "#ef5350");
    assert.equal(Number(line.attributes.x1), color === "white" ? 450 : 350);
    assert.equal(Number(line.attributes.y1), color === "white" ? 650 : 150);
    assert.equal(group.children[2].attributes.points.split(" ")[0], color === "white" ? "450,450" : "350,350");
    assert.equal(line.attributes["stroke-linecap"], "butt");
    assert.equal(Math.sign(Number(line.attributes.y2)-Number(line.attributes.y1)), color === "white" ? -1 : 1);
    assert.match(squares.find((square) => square.dataset.square === "e4").attributes["aria-label"], /추천 수 도착/);
    assert.equal(ui.requests.some((item) => item.url === "/api/move"), false);
  }
});

test("promotion recommendation names the promotion piece", async () => {
  const state = position({legal_moves:["e7e8q"]});
  const ui = await browser(state);
  await ui.click("hint-toggle");
  await ui.finish(ui.requests[0], snapshot(state, {move:"e7e8q", san:"e8=Q+"}));
  assert.match(ui.element("hint-candidates").children[0].attributes["aria-label"], /e8=Q\+ · e7 → e8 · 퀸으로 승격/);
});

test("foreign game, revision, FEN and illegal moves never appear", async () => {
  const state = position();
  for (const extra of [{game_id:"old"}, {revision:7}, {fen:"old"}, {move:"e2e5"}]) {
    const ui = await browser(state);
    await ui.click("hint-toggle");
    await ui.finish(ui.requests[0], snapshot(state, extra));
    assert.equal(ui.api.currentHintMove(), null);
    assert.equal(ui.element("hint-panel").classList.contains("hint-active"), false);
    assert.equal(arrows(ui), undefined);
  }
});

test("rapid on/off/on keeps preference and ignores earlier replies", async () => {
  const state = position();
  const ui = await browser(state);
  await ui.click("hint-toggle");
  await ui.click("hint-toggle");
  assert.equal(ui.element("hint-toggle").attributes["aria-pressed"], "false");
  await ui.click("hint-toggle");
  await ui.finish(ui.requests[0], snapshot(state));
  assert.equal(ui.api.currentHintMove(), null);
  assert.equal(ui.requests.length, 2);
  assert.deepEqual(JSON.parse(ui.requests[1].options.body), {enabled:true});
  await ui.finish(ui.requests[1], snapshot(state));
  assert.equal(ui.api.currentHintMove(), "e2e4");
});

test("turning off clears immediately and an in-flight poll cannot restore highlights", async () => {
  const state = position();
  const ui = await browser(state);
  await ui.click("hint-toggle");
  const next = snapshot(state,{candidates:[candidate(1,"e2e4"),candidate(2,"d2d4")]});
  await ui.finish(ui.requests[0], next);
  const poll = ui.requests[1];
  await ui.click("hint-toggle");
  assert.equal(ui.api.currentHintMove(), null);
  assert.equal(ui.element("hint-panel").classList.contains("hint-active"), false);
  assert.equal(arrows(ui), undefined);
  await ui.finish(poll, next);
  assert.equal(ui.api.currentHintMove(), null);
  assert.deepEqual(JSON.parse(ui.requests[2].options.body), {enabled:false});
});

test("new games, undo, terminal states and errors clear existing or late hints", async () => {
  for (const change of [{game_id:"new"}, {revision:2}, {result:"1-0"}, {error:"engine problem"}, {human_to_move:false, engine_to_move:true}]) {
    const state = position();
    const ui = await browser(state);
    await ui.click("hint-toggle");
    const next = snapshot(state,{candidates:[candidate(1,"e2e4"),candidate(2,"d2d4")]});
    await ui.finish(ui.requests[0], next);
    assert.equal(ui.api.currentHintMove(), "e2e4");
    ui.api.applyState(position(change));
    await ui.finish(ui.requests[1], next);
    assert.equal(ui.api.currentHintMove(), null);
    assert.equal(ui.element("hint-panel").classList.contains("hint-active"), false);
    assert.equal(arrows(ui), undefined);
    assert.equal(ui.element("hint-toggle").attributes["aria-pressed"], "true");
  }
});

test("toggle works during opponent search without starting a second game action", async () => {
  const state = position({human_to_move:false, engine_to_move:true});
  const ui = await browser(state);
  assert.equal(ui.requests[0].url, "/api/engine");
  assert.equal(ui.element("hint-toggle").disabled, false);
  await ui.click("hint-toggle");
  assert.equal(ui.requests[1].url, "/api/hint");
  assert.equal(ui.api.currentHintMove(), null);
  assert.match(ui.element("hint-text").textContent, /내 차례/);
});

test("hint request failures leave the game usable and remove the recommendation", async () => {
  const state = position();
  const ui = await browser(state);
  await ui.click("hint-toggle");
  await ui.finish(ui.requests[0], snapshot(state));
  ui.requests[1].reject(new Error("offline"));
  await flush();
  assert.equal(ui.api.currentHintMove(), null);
  assert.match(ui.element("hint-text").textContent, /대국은 계속/);
  assert.equal(ui.element("new-game").disabled, false);
});

test("shutdown and navigation discard delayed recommendations", async () => {
  for (const action of ["shutdown", "pagehide"]) {
    const state = position();
    const ui = await browser(state);
    await ui.click("hint-toggle");
    await ui.finish(ui.requests[0], snapshot(state));
    if (action === "shutdown") {
      await ui.click("shutdown");
      await ui.finish(ui.requests.find((item) => item.url === "/api/shutdown"), {closed:true});
    } else ui.lifecycle.pagehide();
    await ui.finish(ui.requests[1], snapshot(state));
    assert.equal(ui.api.currentHintMove(), null);
    assert.equal(arrows(ui), undefined);
  }
});

const arrows = (ui) => ui.element("board").children.find((item) => item.attributes.class === "live-hint-arrows");
const candidate = (rank, move, san = move) => ({rank,move,san,from:move.slice(0,2),to:move.slice(2,4),
  promotion:move[4] || null,depth:10});
const candidateMoves = (ui) => Array.from(ui.api.currentHintCandidates(), (item) => item.move);

test("three candidates are sorted, colored and numbered without choosing a move", async () => {
  const state = position({legal_moves:["e2e4","d2d4","g1f3"]});
  const ui = await browser(state);
  await ui.click("hint-toggle");
  await ui.finish(ui.requests[0], snapshot(state, {candidates:[
    candidate(3,"g1f3","Nf3"),candidate(1,"e2e4","e4"),candidate(2,"d2d4","d4")]}));
  assert.deepEqual(candidateMoves(ui), ["e2e4","d2d4","g1f3"]);
  assert.equal(ui.element("hint-candidates").children.length, 3);
  const svg = arrows(ui), colors = ["#ef5350","#ff9800","#fdd835"];
  for (let rank=1;rank<=3;rank++) {
    const line = svg.children.find((item) => item.attributes["data-hint-rank"] === String(rank)).children[1];
    const number = svg.children.find((item) => item.attributes["data-hint-label"] === String(rank));
    assert.equal(line.attributes.stroke, colors[rank-1]);
    assert.equal(number.children[0].attributes.fill, colors[rank-1]);
    assert.equal(number.children[1].textContent, String(rank));
    const centers = [[450,650,450,450],[350,650,350,450],[650,750,550,550]][rank-1];
    assert.equal(Number(line.attributes.x1), centers[0]);
    assert.equal(Number(line.attributes.y1), centers[1]);
    const head = svg.children.find((item) => item.attributes["data-hint-rank"] === String(rank)).children[2];
    assert.equal(head.attributes.points.split(" ")[0], centers.slice(2).join(","));
    assert.equal(ui.element("hint-candidates").children[rank-1].children[0].style.backgroundColor, colors[rank-1]);
  }
  assert.equal(svg.children.length, 6);
  assert.equal(ui.requests.some((item) => item.url === "/api/move"), false);
});

test("explicit empty or malformed lists never resurrect the legacy first move", async () => {
  for (const candidates of [[],null,{},"e2e4"]) {
    const state = position(), ui = await browser(state);
    await ui.click("hint-toggle");
    await ui.finish(ui.requests[0], snapshot(state,{candidates}));
    assert.deepEqual(candidateMoves(ui), []);
    assert.equal(arrows(ui), undefined);
    assert.equal(ui.element("hint-candidates").children.length, 0);
  }
});

test("illegal, duplicate, invalid-rank and mismatched-coordinate candidates are discarded and capped at three", async () => {
  const state = position({legal_moves:["e2e4","d2d4","g1f3","b1c3","e2e2"]}), ui = await browser(state);
  await ui.click("hint-toggle");
  await ui.finish(ui.requests[0], snapshot(state,{candidates:[
    null,candidate(0,"b1c3"),candidate(4,"b1c3"),candidate(1,"e2e5"),candidate(1,"e2e2"),candidate(1,"e2e4"),
    candidate(2,"e2e4"),{...candidate(2,"d2d4"),from:"a1"},
    {...candidate(2,"d2d4"),to:"a2"},{...candidate(2,"d2d4"),promotion:"q"},
    candidate(2,"d2d4"),candidate(2,"b1c3"),candidate(3,"g1f3"),candidate(3,"b1c3")]}));
  assert.deepEqual(candidateMoves(ui), ["e2e4","d2d4","g1f3"]);
  assert.equal(arrows(ui).children.length, 6);
});

test("all candidates obey game, revision and FEN identity", async () => {
  for (const extra of [{game_id:"foreign"},{revision:7},{fen:"different"}]) {
    const state = position(), ui = await browser(state);
    await ui.click("hint-toggle");
    await ui.finish(ui.requests[0], snapshot(state,{candidates:[candidate(1,"e2e4"),candidate(2,"d2d4")],...extra}));
    assert.deepEqual(candidateMoves(ui), []);
    assert.equal(arrows(ui), undefined);
  }
});

test("promotion alternatives share centered endpoints with distinct widths and badges, including edge files after flipping", async () => {
  for (const color of ["white","black"]) for (const file of ["a","h"]) {
    const moves = ["q","r","n"].map((piece) => file+"7"+file+"8"+piece);
    const state = position({human_color:color,legal_moves:moves}), ui = await browser(state);
    await ui.click("hint-toggle");
    await ui.finish(ui.requests[0], snapshot(state,{candidates:moves.map((move,index)=>candidate(index+1,move))}));
    const svg = arrows(ui), groups = svg.children.filter((item)=>item.attributes["data-hint-rank"]);
    const lines = groups.map((item)=>item.children[1]);
    const x = (file === "a") === (color === "white") ? 50 : 750;
    for (const group of groups) {
      assert.equal(Number(group.children[1].attributes.x1), x);
      assert.equal(Number(group.children[1].attributes.y1), color === "white" ? 150 : 650);
      assert.equal(group.children[2].attributes.points.split(" ")[0], `${x},${color === "white" ? 50 : 750}`);
    }
    assert.equal(new Set(lines.map((line)=>line.attributes["stroke-width"])).size, 3);
    const labels = svg.children.filter((item)=>item.attributes["data-hint-label"]);
    assert.equal(new Set(labels.map((label)=>label.children[0].attributes.cx)).size, 3);
    for (const label of labels) {
      const circle = label.children[0], x = Number(circle.attributes.cx), y = Number(circle.attributes.cy);
      assert.ok(x>=15 && x<=785 && y>=15 && y<=785);
    }
    assert.match(ui.element("hint-candidates").children[0].attributes["aria-label"], /퀸으로 승격/);
    assert.match(ui.element("hint-candidates").children[1].attributes["aria-label"], /룩으로 승격/);
    assert.match(ui.element("hint-candidates").children[2].attributes["aria-label"], /나이트로 승격/);
  }
});

test("collinear shared sources and destinations stay centered with distinct badges", async () => {
  for (const moves of [["e2e3","e2e4"],["e2e4","e3e4"]]) {
    const state = position({legal_moves:moves}), ui = await browser(state);
    await ui.click("hint-toggle");
    await ui.finish(ui.requests[0],snapshot(state,{candidates:moves.map((move,index)=>candidate(index+1,move))}));
    const svg = arrows(ui), groups = svg.children.filter((item)=>item.attributes["data-hint-rank"]);
    for (const group of groups) {
      const move = group.attributes["data-move"], line = group.children[1];
      assert.equal(Number(line.attributes.x1),450);
      assert.equal(Number(line.attributes.y1),(8-Number(move[1]))*100+50);
      assert.equal(group.children[2].attributes.points.split(" ")[0],`450,${(8-Number(move[3]))*100+50}`);
    }
    const labels = svg.children.filter((item)=>item.attributes["data-hint-label"]);
    assert.equal(new Set(labels.map((label)=>label.children[0].attributes.cx)).size,2);
  }
});

test("analysis and coaching transitions clear arrows and invalidate in-flight replies even after returning", async () => {
  for (const name of ["regicide-analysis-mode","regicide-coaching-mode"]) {
    const state = position(), ui = await browser(state);
    await ui.click("hint-toggle");
    const next = snapshot(state,{candidates:[candidate(1,"e2e4"),candidate(2,"d2d4")]});
    await ui.finish(ui.requests[0],next);
    assert.ok(arrows(ui));
    ui.lifecycle[name]({detail:true});
    assert.equal(arrows(ui),undefined);
    assert.deepEqual(candidateMoves(ui),[]);
    await ui.api.pollHint();
    assert.equal(ui.requests.length,2);
    ui.lifecycle[name]({detail:false});
    await ui.finish(ui.requests[1],next);
    assert.equal(arrows(ui),undefined);
    void ui.api.pollHint();
    await ui.finish(ui.requests[2],next);
    assert.equal(candidateMoves(ui).length,2);
  }
});

test("hint arrows remain click-through and normal board input still submits only the selected legal move", async () => {
  const state = position(), ui = await browser(state);
  await ui.click("hint-toggle");
  await ui.finish(ui.requests[0],snapshot(state,{candidates:[candidate(1,"e2e4"),candidate(2,"d2d4")]}));
  for (const square of ["e2","e4"]) {
    const button = ui.element("board").children.find((item)=>item.dataset.square===square);
    ui.element("board").listeners.click({target:{closest:()=>button}});
    await flush();
  }
  const move = ui.requests.find((item)=>item.url==="/api/move");
  assert.equal(JSON.parse(move.options.body).move,"e2e4");
  assert.equal(arrows(ui),undefined);
  const css = fs.readFileSync(path.join(__dirname,"../web/human-play/style.css"),"utf8");
  assert.match(css,/\.live-hint-arrows\s*\{[^}]*position:absolute[^}]*pointer-events:none/);
  assert.match(css,/\.live-hint-arrows \*\s*\{[^}]*pointer-events:none/);
  assert.match(css,/\.hint-candidates\s*\{[^}]*height:72px[^}]*grid-template-rows:repeat\(3,24px\)/);
});

test("candidate SAN is plain text and cannot create markup", async () => {
  const state = position(), ui = await browser(state), san = "<img src=x onerror=alert(1)>";
  await ui.click("hint-toggle");
  await ui.finish(ui.requests[0],snapshot(state,{candidates:[candidate(1,"e2e4",san)]}));
  const label = ui.element("hint-candidates").children[0].children[1];
  assert.match(label.textContent, /^<img src=x onerror=alert\(1\)>/);
  assert.equal(label.children.length,0);
});

test("ranked hints preserve the current gameplay evaluation and board state", async () => {
  const state = position();
  state.evaluation = {game_id:state.game_id,revision:state.revision,fen:state.fen,
    status:"ready",cp:135,white_share:.62,depth:10};
  const ui = await browser(state);
  const before = ["evaluation-label","evaluation-score","evaluation-detail"].map((id)=>ui.element(id).textContent);
  await ui.click("hint-toggle");
  await ui.finish(ui.requests[0],snapshot(state,{candidates:[candidate(1,"e2e4"),candidate(2,"d2d4")]}));
  assert.deepEqual(["evaluation-label","evaluation-score","evaluation-detail"].map((id)=>ui.element(id).textContent),before);
  assert.equal(ui.element("evaluation-white").style.height,"62%");
  assert.equal(ui.element("board").children.filter((item)=>item.dataset.square).length,64);
  assert.equal(state.revision,0);
  assert.deepEqual(state.moves,[]);
});

(async () => {
  for (const {name, run} of cases) {
    await run();
    console.log(`PASS ${name}`);
  }
  console.log(`${cases.length} browser hint behavior cases passed.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
