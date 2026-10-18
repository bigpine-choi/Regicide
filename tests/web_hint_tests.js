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
  setAttribute(key, value) { this.attributes[key] = value; }
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
    .replace("  void initialize();", "  window.testApi = {applyState, pollHint, render, currentHintMove};\n  void initialize();");
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

test("a legal recommendation labels SAN, origin and destination in both orientations", async () => {
  for (const color of ["white", "black"]) {
    const state = position({human_color:color});
    const ui = await browser(state);
    await ui.click("hint-toggle");
    await ui.finish(ui.requests[0], snapshot(state));
    assert.match(ui.element("hint-text").textContent, /e4 · e2 → e4/);
    const squares = ui.element("board").children;
    assert.equal(squares.length, 64);
    assert.equal(squares[0].dataset.square, color === "white" ? "a8" : "h1");
    assert.ok(squares.find((square) => square.dataset.square === "e2").classList.contains("hint-from"));
    assert.ok(squares.find((square) => square.dataset.square === "e4").classList.contains("hint-to"));
    assert.match(squares.find((square) => square.dataset.square === "e4").attributes["aria-label"], /추천 수 도착/);
    assert.equal(ui.requests.some((item) => item.url === "/api/move"), false);
  }
});

test("promotion recommendation names the promotion piece", async () => {
  const state = position({legal_moves:["e7e8q"]});
  const ui = await browser(state);
  await ui.click("hint-toggle");
  await ui.finish(ui.requests[0], snapshot(state, {move:"e7e8q", san:"e8=Q+"}));
  assert.match(ui.element("hint-text").textContent, /e8=Q\+ · e7 → e8 · 퀸으로 승격/);
});

test("foreign game, revision, FEN and illegal moves never appear", async () => {
  const state = position();
  for (const extra of [{game_id:"old"}, {revision:7}, {fen:"old"}, {move:"e2e5"}]) {
    const ui = await browser(state);
    await ui.click("hint-toggle");
    await ui.finish(ui.requests[0], snapshot(state, extra));
    assert.equal(ui.api.currentHintMove(), null);
    assert.equal(ui.element("hint-panel").classList.contains("hint-active"), false);
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
  await ui.finish(ui.requests[0], snapshot(state));
  const poll = ui.requests[1];
  await ui.click("hint-toggle");
  assert.equal(ui.api.currentHintMove(), null);
  assert.equal(ui.element("hint-panel").classList.contains("hint-active"), false);
  await ui.finish(poll, snapshot(state));
  assert.equal(ui.api.currentHintMove(), null);
  assert.deepEqual(JSON.parse(ui.requests[2].options.body), {enabled:false});
});

test("new games, undo, terminal states and errors clear existing or late hints", async () => {
  for (const change of [{game_id:"new"}, {revision:2}, {result:"1-0"}, {error:"engine problem"}, {human_to_move:false, engine_to_move:true}]) {
    const state = position();
    const ui = await browser(state);
    await ui.click("hint-toggle");
    await ui.finish(ui.requests[0], snapshot(state));
    assert.equal(ui.api.currentHintMove(), "e2e4");
    ui.api.applyState(position(change));
    await ui.finish(ui.requests[1], snapshot(state));
    assert.equal(ui.api.currentHintMove(), null);
    assert.equal(ui.element("hint-panel").classList.contains("hint-active"), false);
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
  }
});

(async () => {
  for (const {name, run} of cases) {
    await run();
    console.log(`PASS ${name}`);
  }
  console.log(`${cases.length} browser hint behavior cases passed.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
