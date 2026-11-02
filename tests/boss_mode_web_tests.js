"use strict";

// Execute the shipped client with controlled HTTP replies; no native game runs.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor() {
    this.children = []; this.style = {}; this.dataset = {}; this.listeners = {};
    this.attributes = {}; this.classes = new Set(); this.value = "";
    this.textContent = ""; this.disabled = false;
    this.classList = {add: value => this.classes.add(value),
      toggle: (value, enabled) => enabled ? this.classes.add(value) : this.classes.delete(value)};
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items.flatMap(item => item.fragment ? item.children : [item]); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector() { return null; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const elements = new Map();
const byId = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const pending = [], calls = [];
const document = {
  getElementById: byId, querySelector: () => ({content: "test-session-token"}),
  createElement: () => new Element(), createDocumentFragment: () => Object.assign(new Element(), {fragment: true}),
  activeElement: null, body: new Element()
};
const html = fs.readFileSync(path.join(__dirname, "../web/human-play/index.html"), "utf8");
assert.match(html, /<option value="normal" selected>기본<\/option>/);
assert.match(html, /id="boss-mode"[^>]*aria-describedby="boss-mode-help"/);
byId("human-side").value = "white";
byId("boss-mode").value = "normal";
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../web/human-play/app.js"), "utf8"), {
  document, console, setTimeout: () => {}, clearTimeout: () => {}, URL: {},
  setInterval: () => 1, clearInterval: () => {}, window: {addEventListener: () => {}}, AbortController,
  fetch: (url, options) => new Promise(resolve => {
    const call = {url, body: options.body ? JSON.parse(options.body) : null, resolve};
    calls.push(call); pending.push(call);
  })
});
const flush = async () => { for (let i = 0; i < 12; ++i) await Promise.resolve(); };
async function reply(url, state) {
  const index = pending.findIndex(call => call.url === url);
  assert.ok(index >= 0, "expected HTTP request " + url);
  const [call] = pending.splice(index, 1);
  call.resolve({ok: true, json: async () => structuredClone(state)});
  await flush();
  return call;
}
function fixture(revision, phase, settings = 0, ms = 1000) {
  return {revision, game_id: phase ? "boss-game" : "legacy-game", board: {e2: "P", e1: "K", e8: "k"},
    human_color: "white", human_to_move: true, engine_to_move: false,
    legal_moves: ["e2e4"], moves: [], last_move: null, result: "*", error: null,
    status: "당신 차례입니다", can_undo: false, in_check: false,
    engine: {name: "Regicide test", movetime_ms: ms, settings_revision: settings,
      active_movetime_ms: null, extra_budget_ms: 0, active_extra_budget_ms: null,
      ...(phase ? {boss_mode: true, boss_phase: phase} : {})}};
}
const submit = () => byId("new-game-form").listeners.submit({preventDefault() {}});
const changeTime = ms => { byId("think-time").value = String(ms); byId("think-time").listeners.change(); };
const clickSquare = square => byId("board").listeners.click({target: {closest: () => ({dataset: {square}})}});

(async () => {
  await reply("/api/state", fixture(1));
  assert.doesNotMatch(byId("opponent-detail").textContent, /보스 모드/);
  submit();
  assert.equal(pending[0].body.boss_mode, false, "default request explicitly selects normal mode");
  await reply("/api/new", fixture(2));
  byId("boss-mode").value = "boss";
  changeTime(3000);
  assert.equal(byId("boss-mode").disabled, true);
  assert.deepEqual(pending[0].body, {game_id: "legacy-game", settings_revision: 0, movetime_ms: 3000});
  await reply("/api/think-time", fixture(2, null, 1, 3000));
  assert.equal(byId("boss-mode").value, "boss", "render does not overwrite prospective mode");
  assert.doesNotMatch(byId("opponent-detail").textContent, /보스 모드/, "draft does not change the live game");
  submit();
  assert.equal(pending[0].body.boss_mode, true);
  await reply("/api/new", fixture(3, "opening", 0, 3000));
  assert.match(byId("opponent-detail").textContent, /보스 모드 · 변칙 오프닝/);
  clickSquare("e2"); clickSquare("e4");
  const human = fixture(4, "opening", 0, 3000);
  human.human_to_move = false; human.engine_to_move = true;
  human.moves = [{ply: 1, san: "e4", uci: "e2e4", actor: "human"}];
  await reply("/api/move", human);
  assert.match(byId("opponent-detail").textContent, /보스 모드 · 변칙 오프닝/);
  assert.equal(byId("boss-mode").disabled, true, "no mode edits during an active board request");
  changeTime(1000);
  const done = fixture(5, "trap_hunt", 0, 3000);
  done.moves = [...human.moves, {ply: 2, san: "e5", uci: "e7e5", actor: "engine"}];
  await reply("/api/engine", done);
  assert.match(byId("opponent-detail").textContent, /보스 모드 · 전술 사냥/);
  const lateSetting = structuredClone(human);
  lateSetting.engine.settings_revision = 1;
  lateSetting.engine.movetime_ms = 1000;
  await reply("/api/think-time", lateSetting);
  assert.match(byId("opponent-detail").textContent, /보스 모드 · 전술 사냥/,
    "late time response cannot replace the phase from the newer board");
  assert.equal(byId("think-time").value, "1000");
  changeTime(3000);
  await reply("/api/think-time", fixture(6, "full_strength", 2, 3000));
  assert.match(byId("opponent-detail").textContent, /보스 모드 · 전력 대국/);
  byId("boss-mode").value = "normal";
  assert.match(byId("opponent-detail").textContent, /전력 대국/, "drafting basic mode does not stop the boss game");
  changeTime(1000);
  await reply("/api/think-time", fixture(6, "constructor", 3, 1000));
  assert.match(byId("opponent-detail").textContent, /보스 모드$/);
  assert.doesNotMatch(byId("opponent-detail").textContent, /constructor|function|Object/);
  submit();
  assert.equal(pending[0].body.boss_mode, false);
  const normal = fixture(7); normal.game_id = "normal-game";
  await reply("/api/new", normal);
  assert.doesNotMatch(byId("opponent-detail").textContent, /보스 모드/);
  assert.equal(pending.length, 0);
  console.log("Browser boss-mode checks: PASS (default, draft, payload, opening/trap-hunt/full-strength, stale settings, compatibility)");
})().catch(error => { console.error(error); process.exitCode = 1; });
