"use strict";

// Execute the shipped browser script with a minimal DOM and controlled HTTP
// replies. No chess engine, browser rendering or human interaction is claimed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor() {
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.listeners = {};
    this.attributes = {};
    this.classes = new Set();
    this.classList = {
      add: value => this.classes.add(value),
      toggle: (value, enabled) => enabled ? this.classes.add(value) : this.classes.delete(value)
    };
    this.value = "";
    this.textContent = "";
    this.disabled = false;
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
const pending = [];
const calls = [];
const document = {
  getElementById: byId, querySelector: () => ({content: "test-session-token"}),
  createElement: () => new Element(), createDocumentFragment: () => Object.assign(new Element(), {fragment: true}),
  activeElement: null, body: new Element()
};
const source = fs.readFileSync(path.join(__dirname, "../web/human-play/app.js"), "utf8");
vm.runInNewContext(source, {
  document, console, setTimeout: () => {}, clearTimeout: () => {}, URL: {},
  setInterval: () => 1, clearInterval: () => {}, window: {addEventListener: () => {}}, AbortController,
  fetch: (url, options) => new Promise(resolve => {
    const call = {url, body: options.body ? JSON.parse(options.body) : null, resolve};
    calls.push(call);
    pending.push(call);
  })
});

const flush = async () => { for (let i = 0; i < 12; ++i) await Promise.resolve(); };
async function reply(url, state, ok = true) {
  const index = pending.findIndex(call => call.url === url);
  assert.ok(index >= 0, "expected pending HTTP request " + url);
  const [call] = pending.splice(index, 1);
  call.resolve({ok, json: async () => structuredClone(state)});
  await flush();
  return call;
}
function fixture(revision = 1, settingRevision = 0, ms = 1000) {
  return {revision, game_id: "game-one", board: {e2: "P", g1: "N", e1: "K", e8: "k"},
    human_color: "white", human_to_move: true, engine_to_move: false,
    legal_moves: ["e2e4", "g1f3"], moves: [], last_move: null,
    result: "*", error: null, status: "당신 차례입니다", can_undo: false, in_check: false,
    engine: {name: "Regicide test", movetime_ms: ms, settings_revision: settingRevision, active_movetime_ms: null,
      extra_budget_ms: ms >= 500 ? Math.min(ms, 2000) : 0, active_extra_budget_ms: null}};
}
function clickSquare(square) {
  byId("board").listeners.click({target: {closest: () => ({dataset: {square}})}});
}
function isSelected(square) {
  return byId("board").children.some(child => child.dataset.square === square && child.classes.has("selected"));
}
function dynamicFixture(revision, settingRevision, ms, ply) {
  const value = fixture(revision, settingRevision, ms);
  value.game_id = "dynamic-game";
  // These move entries exercise display order only; no chess legality claim.
  value.moves = Array.from({length: ply}, (_, index) => ({ply: index + 1, san: "Nf3", uci: "g1f3", actor: "human"}));
  value.engine.dynamic_play_enabled = true;
  value.engine.extra_budget_schedule = [
    {until_ply: 16, budget_ms: ms >= 500 ? Math.min(ms, 2000) : 0},
    {until_ply: 80, budget_ms: ms >= 500 ? Math.min(ms / 2, 750) : 0}
  ];
  value.engine.extra_budget_ms = value.engine.extra_budget_schedule.find(stage => ply < stage.until_ply)?.budget_ms ?? 0;
  return value;
}

(async () => {
  await reply("/api/state", fixture());
  assert.match(byId("think-time-help").textContent, /최대 1초 더/);
  assert.equal(byId("think-time").disabled, false);
  clickSquare("e2");
  assert.ok(isSelected("e2"));
  byId("human-side").value = "black"; // A draft color applies only to a new game.
  byId("think-time").value = "5000";
  byId("think-time").listeners.change();
  assert.equal(byId("new-game").disabled, true);
  assert.equal(pending[0].url, "/api/think-time");
  assert.deepEqual(pending[0].body, {game_id: "game-one", settings_revision: 0, movetime_ms: 5000});
  await reply("/api/think-time", fixture(1, 1, 5000));
  assert.ok(isSelected("e2"), "changing time preserves selected piece");
  assert.equal(byId("human-side").value, "black", "time does not apply the drafted color");
  assert.equal(byId("think-time").value, "5000");
  assert.match(byId("think-time-help").textContent, /최대 2초 더/);
  assert.ok(byId("think-time-help").textContent.includes("다음 Regicide 수부터"));
  assert.ok(!calls.some(call => call.url === "/api/new"));

  clickSquare("e4");
  const human = fixture(2, 1, 5000);
  human.human_to_move = false;
  human.engine_to_move = true;
  human.moves = [{ply: 1, san: "e4", uci: "e2e4", actor: "human"}];
  await reply("/api/move", human);
  assert.ok(pending.some(call => call.url === "/api/engine"));
  assert.equal(byId("think-time").disabled, false, "time remains editable while the engine thinks");
  byId("think-time").value = "3000";
  byId("think-time").listeners.change();
  const done = fixture(3, 1, 5000);
  done.can_undo = true;
  done.moves = [...human.moves, {ply: 2, san: "e5", uci: "e7e5", actor: "engine"}];
  await reply("/api/engine", done);
  const lateSetting = structuredClone(human);
  lateSetting.engine = {name: "Regicide test", movetime_ms: 3000, settings_revision: 2, active_movetime_ms: 5000,
    extra_budget_ms: 2000, active_extra_budget_ms: 2000};
  await reply("/api/think-time", lateSetting);
  assert.equal(byId("move-count").textContent, "2수", "late settings response cannot rewind the board");
  assert.equal(byId("think-time").value, "3000", "newer settings survive a late board snapshot");

  clickSquare("g1");
  assert.ok(isSelected("g1"));
  byId("think-time").value = "100";
  byId("think-time").listeners.change();
  const conflict = structuredClone(done);
  conflict.engine.movetime_ms = 1000;
  conflict.engine.extra_budget_ms = 1000;
  conflict.engine.settings_revision = 3;
  await reply("/api/think-time", {error: "생각 시간이 먼저 바뀌었습니다.", state: conflict}, false);
  assert.equal(byId("think-time").value, "1000");
  assert.ok(isSelected("g1"), "time conflict refresh preserves a legal piece selection");
  assert.ok(byId("error-text").textContent.includes("먼저 바뀌었습니다"));
  assert.match(byId("think-time-help").textContent, /최대 1초 더/);
  byId("think-time").value = "100";
  byId("think-time").listeners.change();
  await reply("/api/think-time", fixture(3, 4, 100));
  assert.doesNotMatch(byId("think-time-help").textContent, /오프닝 후보 검토/);
  assert.equal(pending.length, 0);

  byId("new-game-form").listeners.submit({preventDefault() {}});
  await reply("/api/new", dynamicFixture(4, 0, 1000, 14));
  assert.match(byId("think-time-help").textContent, /구상 검토 시 최대 1초/);
  assert.doesNotMatch(byId("think-time-help").textContent, /오프닝 후보/);
  clickSquare("e2");
  clickSquare("e4");
  const dynamicHuman = dynamicFixture(5, 0, 1000, 15);
  dynamicHuman.human_to_move = false;
  dynamicHuman.engine_to_move = true;
  dynamicHuman.engine.active_movetime_ms = 1000;
  dynamicHuman.engine.active_extra_budget_ms = 1000;
  await reply("/api/move", dynamicHuman);
  byId("think-time").value = "3000";
  byId("think-time").listeners.change();
  await reply("/api/engine", dynamicFixture(6, 0, 1000, 16));
  assert.match(byId("think-time-help").textContent, /구상 검토 시 최대 0.5초/);
  const lateDynamicSetting = dynamicFixture(5, 1, 3000, 15);
  lateDynamicSetting.engine.active_movetime_ms = 1000;
  lateDynamicSetting.engine.active_extra_budget_ms = 1000;
  await reply("/api/think-time", lateDynamicSetting);
  assert.equal(byId("move-count").textContent, "16수");
  assert.match(byId("think-time-help").textContent, /구상 검토 시 최대 0.75초/,
    "late opening snapshot uses the latest setting schedule with the current middlegame board");

  clickSquare("g1");
  clickSquare("f3");
  const thinkingDynamic = dynamicFixture(7, 1, 3000, 17);
  thinkingDynamic.human_to_move = false;
  thinkingDynamic.engine_to_move = true;
  thinkingDynamic.engine.active_movetime_ms = 3000;
  thinkingDynamic.engine.active_extra_budget_ms = 750;
  await reply("/api/move", thinkingDynamic);
  byId("think-time").value = "1000";
  byId("think-time").listeners.change();
  const freshSetting = structuredClone(thinkingDynamic);
  freshSetting.engine.movetime_ms = 1000;
  freshSetting.engine.settings_revision = 2;
  freshSetting.engine.extra_budget_ms = 500;
  freshSetting.engine.extra_budget_schedule = dynamicFixture(7, 2, 1000, 17).engine.extra_budget_schedule;
  await reply("/api/think-time", freshSetting);
  assert.match(byId("think-time-help").textContent, /구상 검토 시 최대 0.75초/,
    "an in-flight search retains its captured additional budget");
  await reply("/api/engine", dynamicFixture(8, 1, 3000, 18));
  assert.equal(byId("think-time").value, "1000");
  assert.match(byId("think-time-help").textContent, /구상 검토 시 최대 0.5초/,
    "late engine response must retain the newer settings schedule");
  assert.equal(pending.length, 0);
  console.log("Browser time-setting checks: PASS (legacy, dynamic wording, phase crossing, captured budget, selection, response order, CAS failure)");
})().catch(error => { console.error(error); process.exitCode = 1; });
