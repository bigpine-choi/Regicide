"use strict";
// Run the shipped client with controlled transport/time, including overlapping moves.
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
class Element {
  constructor() {
    this.children = []; this.style = {}; this.dataset = {}; this.listeners = {}; this.attributes = {};
    this.classes = new Set(); this.value = ""; this.textContent = ""; this.disabled = false;
    this.classList = {add: value => this.classes.add(value), toggle: (value, enabled) => enabled ? this.classes.add(value) : this.classes.delete(value)};
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items.flatMap(item => item.fragment ? item.children : [item]); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, callback) { this.listeners[key] = callback; }
  querySelector() { return null; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const elements = new Map();
const byId = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const events = {}, pending = [], calls = [];
let now = 1000;
const sandbox = {
  document: {getElementById: byId, querySelector: () => ({content: "fixture"}), createElement: () => new Element(),
    createDocumentFragment: () => Object.assign(new Element(), {fragment: true}), activeElement: null, body: new Element()},
  console, performance: {now: () => now}, setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {}, URL: {}, AbortController,
  window: {addEventListener: (key, callback) => { events[key] = callback; }},
  fetch: (url, options) => new Promise((resolve, reject) => {
    const call = {url, body: options.body ? JSON.parse(options.body) : null, resolve, reject};
    pending.push(call); calls.push(call);
  })
};
for (const [id, value] of [["human-side", "white"], ["boss-mode", "sparring"], ["rapid-time-control", "10+0"], ["think-time", "1000"]]) byId(id).value = value;
const source = fs.readFileSync(path.join(__dirname, "../web/human-play/app.js"), "utf8");
vm.runInNewContext(source.replace(/\}\)\(\);\s*$/, "globalThis.probe = {applyState, applyClockRefresh, renderClockAndAdvice, pollClock, state:()=>state};\n})();"), sandbox);
const probe = sandbox.probe;
function fixture(revision = 1, sample = 1000, edit = 0, game = "clock-adjust-game") {
  return {game_id: game, revision, fen: "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1", board: {e1: "K", e2: "P", e8: "k"},
    human_color: "white", human_to_move: true, engine_to_move: false, legal_moves: ["e2e4"], moves: [], last_move: null,
    result: "*", status: "내 차례", can_undo: false, in_check: false, error: null,
    engine: {name: "Regicide fixture", movetime_ms: 1000, settings_revision: 0, extra_budget_ms: 0, sparring_mode: true},
    clock: {enabled: true, editable: true, edit_revision: edit, control_id: "10+0", label: "10분 래피드", white_ms: 600000, black_ms: 600000,
      running_color: "white", status: "running", turn_elapsed_ms: 0, sampled_monotonic_ms: sample, timeout_policy: "engine_only"},
    decision_support: null};
}
async function flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
function take(url) { const index = pending.findIndex(call => call.url === url); assert.ok(index >= 0, url); return pending.splice(index, 1)[0]; }
async function reply(url, value, ok = true) { const call = take(url); call.resolve({ok, json: async () => structuredClone(value)}); await flush(); return call; }
function click(id) { assert.equal(byId(id).disabled, false, `${id} enabled`); byId(id).listeners.click(); }
function assertPayload(side, delta, revision, game = "clock-adjust-game") {
  const call = pending.find(item => item.url === "/api/clock-adjust");
  assert.deepEqual(call.body, {game_id: game, clock_revision: revision, side, delta_ms: delta});
}
(async () => {
  const html = fs.readFileSync(path.join(__dirname, "../web/human-play/index.html"), "utf8");
  const panel = html.match(/<section id="clock-adjust"[\s\S]*?<\/section>/)[0];
  assert.doesNotMatch(panel, /<input|<select/, "clock changes require only buttons");
  assert.equal((panel.match(/id="clock-(human|engine)-(minus|plus)-(minute|ten)"/g) || []).length, 8);
  assert.match(panel, /aria-controls="clock-adjust-panel"/);
  await reply("/api/state", fixture());
  assert.equal(byId("clock-adjust").hidden, false);
  assert.equal(byId("think-time-field").hidden, true, "sparring retains automatic thinking");
  assert.match(byId("think-time-help").textContent, /3분 미만/);
  click("clock-adjust-toggle");
  assert.equal(byId("clock-adjust-panel").hidden, false);
  assert.equal(byId("clock-adjust-toggle").attributes["aria-expanded"], "true");
  const board = byId("board").children;
  click("clock-human-minus-ten");
  assertPayload("human", -10000, 0);
  assert.equal(byId("clock-engine-plus-minute").disabled, true, "one clock write in flight");
  byId("clock-engine-plus-minute").listeners.click();
  assert.equal(pending.filter(call => call.url === "/api/clock-adjust").length, 1, "double clicks do not queue stale edits");
  const adjusted = fixture(1, 1000, 1); adjusted.clock.white_ms = 590000;
  await reply("/api/clock-adjust", adjusted);
  assert.equal(probe.state().revision, 1, "clock edits do not change the chess position");
  assert.equal(probe.state().clock.edit_revision, 1, "edit with identical sample timestamp still applies");
  assert.equal(byId("clock-adjust-human-time").textContent, "9:50");
  assert.equal(byId("clock-adjust-engine-time").textContent, "10:00", "other side is unchanged");
  assert.equal(byId("board").children, board, "adjustment preserves board DOM and drag targets");
  assert.equal(byId("clock-adjust-panel").hidden, false);
  now += 2000; probe.renderClockAndAdvice();
  assert.equal(byId("clock-adjust-human-time").textContent, "9:48", "expanded clock remains live");
  const staleClock = fixture(1, 4000, 0); staleClock.clock.white_ms = 599000;
  probe.applyClockRefresh(staleClock);
  assert.equal(probe.state().clock.edit_revision, 1, "poll cannot roll back a newer edit");
  probe.applyState(staleClock, true);
  assert.equal(byId("clock-adjust-human-time").textContent, "9:48", "ordinary state packets also preserve clock edit order");

  // Both roles remain independent when the human plays black.
  const black = fixture(2, 4000, 2); black.human_color = "black"; black.clock.running_color = "black";
  black.clock.white_ms = 123000; black.clock.black_ms = 456000; probe.applyState(black);
  assert.equal(byId("clock-adjust-human-time").textContent, "7:36");
  assert.equal(byId("clock-adjust-engine-time").textContent, "2:03");
  click("clock-engine-plus-minute"); assertPayload("engine", 60000, 2);
  const blackAdjusted = structuredClone(black); blackAdjusted.clock.edit_revision = 3; blackAdjusted.clock.white_ms += 60000;
  await reply("/api/clock-adjust", blackAdjusted);
  assert.equal(probe.state().clock.black_ms, 456000);
  click("clock-human-plus-ten"); assertPayload("human", 10000, 3);
  const conflict = structuredClone(blackAdjusted); conflict.clock.edit_revision = 4; conflict.clock.white_ms += 10000;
  await reply("/api/clock-adjust", {state: conflict, error: "다른 시간 조정이 반영되었습니다. 다시 눌러주세요."}, false);
  assert.equal(probe.state().clock.edit_revision, 4, "conflict refreshes authoritative clock");
  assert.match(byId("error-text").textContent, /다시 눌러/);
  click("clock-human-plus-ten"); assertPayload("human", 10000, 4);
  take("/api/clock-adjust").reject(new Error("offline")); await flush();
  assert.equal(byId("clock-human-plus-ten").disabled, false, "transport failure releases controls");
  assert.match(byId("error-text").textContent, /연결이 끊겼습니다/);

  // Overlap a human move/engine wait with an adjustment; no second engine POST.
  probe.applyState(fixture(3, 5000, 4));
  for (const square of ["e2", "e4"]) byId("board").listeners.click({target: {closest: () => ({dataset: {square}})}});
  const thinking = fixture(4, 6000, 4); thinking.human_to_move = false; thinking.engine_to_move = true; thinking.clock.running_color = "black";
  await reply("/api/move", thinking);
  assert.ok(pending.some(call => call.url === "/api/engine"));
  click("clock-engine-minus-minute"); assertPayload("engine", -60000, 4);
  const engineAdjusted = structuredClone(thinking); engineAdjusted.clock.edit_revision = 5; engineAdjusted.clock.black_ms = 540000;
  engineAdjusted.engine.pacing = {target_ms: 1200};
  await reply("/api/clock-adjust", engineAdjusted);
  assert.equal(probe.state().engine.pacing.target_ms, 1200, "active pacing follows the accepted clock edit");
  probe.applyState(thinking, true);
  assert.equal(probe.state().engine.pacing.target_ms, 1200, "old engine metadata cannot roll back adjusted pacing");
  assert.ok(pending.some(call => call.url === "/api/engine"), "editing does not cancel a waiting move");
  assert.equal(calls.filter(call => call.url === "/api/engine").length, 1);
  const moved = fixture(5, 7000, 5); moved.clock.black_ms = 539000;
  await reply("/api/engine", moved);
  probe.applyState(engineAdjusted, true);
  assert.equal(probe.state().revision, 5, "late clock response cannot undo the engine move");

  // Human zero stays playable; the server, not the client, decides engine timeout.
  const low = fixture(6, 8000, 5); low.clock.white_ms = 1000; probe.applyState(low);
  click("clock-human-minus-ten"); assertPayload("human", -10000, 5);
  const zero = structuredClone(low); zero.clock.white_ms = 0; zero.clock.edit_revision = 6; zero.clock.overtime_color = "white";
  await reply("/api/clock-adjust", zero);
  assert.equal(probe.state().result, "*");
  assert.equal(byId("clock-human-minus-ten").disabled, true);
  click("clock-human-plus-minute"); assertPayload("human", 60000, 6);
  const restored = structuredClone(zero); restored.clock.white_ms = 60000; restored.clock.edit_revision = 7; restored.clock.overtime_color = null;
  await reply("/api/clock-adjust", restored);
  assert.equal(byId("clock-human-minus-ten").disabled, false);
  const capped = structuredClone(restored); capped.clock.running_color = "black"; capped.clock.white_ms = 86400000; capped.clock.edit_revision = 8;
  probe.applyState(capped); assert.equal(byId("clock-human-plus-minute").disabled, true, "24-hour cap is visible");

  for (const event of ["regicide-analysis-mode", "regicide-coaching-mode"]) {
    events[event]({detail: true}); assert.equal(byId("clock-adjust").hidden, true);
    const count = calls.length; byId("clock-engine-plus-ten").listeners.click(); assert.equal(calls.length, count);
    events[event]({detail: false}); assert.equal(byId("clock-adjust").hidden, false);
  }
  const handicap = fixture(7, 9000, 8); handicap.handicap_mode = true; probe.applyState(handicap);
  assert.equal(byId("clock-adjust").hidden, true);
  const normal = fixture(8, 10000, 8); normal.engine.sparring_mode = false; probe.applyState(normal);
  assert.equal(byId("clock-adjust").hidden, true, "draft sparring selector cannot enable normal-game editing");
  const notEditable = fixture(9, 11000, 8); notEditable.clock.editable = false; probe.applyState(notEditable);
  assert.equal(byId("clock-human-plus-ten").disabled, true, "server eligibility is respected");

  // Responses from a previous game must not touch the new game or show its errors.
  probe.applyState(fixture(10, 12000, 8)); click("clock-engine-plus-ten");
  const replacement = fixture(11, 13000, 0, "replacement"); probe.applyState(replacement);
  await reply("/api/clock-adjust", fixture(10, 12500, 9));
  assert.equal(probe.state().game_id, "replacement");
  assert.equal(probe.state().clock.edit_revision, 0);
  assert.equal(byId("clock-adjust-panel").hidden, true, "new game collapses old controls");
  assert.equal(byId("clock-adjust-notice").textContent, "");
  click("clock-engine-minus-ten"); assertPayload("engine", -10000, 0, "replacement");
  probe.applyState(fixture(12, 14000, 0, "third"));
  await reply("/api/clock-adjust", {state: fixture(99, 15000, 9, "replacement"), error: "OLD GAME ERROR"}, false);
  assert.equal(probe.state().game_id, "third", "even a mismatched error state with higher revision is ignored");
  assert.doesNotMatch(byId("error-text").textContent, /OLD GAME ERROR/);
  click("clock-engine-minus-minute");
  const ended = fixture(13, 15000, 1, "third"); ended.result = "1-0"; ended.clock.black_ms = 0;
  ended.clock.running_color = null; ended.clock.status = "finished"; ended.clock.editable = false;
  await reply("/api/clock-adjust", ended);
  assert.equal(probe.state().result, "1-0");
  assert.equal(byId("clock-engine-plus-minute").disabled, true, "finished games cannot be revived");
  const finalCount = calls.length; byId("clock-engine-plus-minute").listeners.click(); assert.equal(calls.length, finalCount);
  console.log("Clock adjustment browser checks: PASS (buttons, colors, AI overlap, edit ordering, zero, caps, mode guards, errors, game identity)");
})().catch(error => { console.error(error); process.exitCode = 1; });
