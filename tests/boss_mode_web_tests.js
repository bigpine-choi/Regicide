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
const pending = [], calls = [], intervals = [];
const document = {
  getElementById: byId, querySelector: () => ({content: "test-session-token"}),
  createElement: () => new Element(), createDocumentFragment: () => Object.assign(new Element(), {fragment: true}),
  activeElement: null, body: new Element()
};
const html = fs.readFileSync(path.join(__dirname, "../web/human-play/index.html"), "utf8");
assert.match(html, /<option value="normal" selected>기본<\/option>/);
assert.match(html, /<option value="sparring">스파링 모드<\/option>/);
assert.match(html, /id="boss-mode"[^>]*aria-describedby="boss-mode-help"/);
const timeSelect = html.match(/<select id="think-time"[\s\S]*?<\/select>/)[0];
assert.equal(Math.min(...Array.from(timeSelect.matchAll(/option value="(\d+)"/g), match => Number(match[1])).filter(ms => ms >= 600)), 1000,
  "normal and boss modes retain their manual search-time options");
byId("human-side").value = "white";
byId("boss-mode").value = "normal";
const sandbox = {
  document, console, setTimeout: () => {}, clearTimeout: () => {}, URL: {},
  setInterval: (callback, ms) => { intervals.push({callback, ms}); return intervals.length; }, clearInterval: () => {}, window: {addEventListener: () => {}}, AbortController,
  fetch: (url, options) => new Promise(resolve => {
    const call = {url, body: options.body ? JSON.parse(options.body) : null, resolve};
    calls.push(call); pending.push(call);
  })
};
const clientSource = fs.readFileSync(path.join(__dirname, "../web/human-play/app.js"), "utf8");
assert.match(clientSource, /\}\)\(\);\s*$/);
// State injection exists only in this in-memory test copy, never the shipped client.
vm.runInNewContext(clientSource.replace(/\}\)\(\);\s*$/, "globalThis.__clientState = () => JSON.parse(JSON.stringify(state)); globalThis.__applyState = applyState;\n})();"), sandbox);
const clientState = () => sandbox.__clientState();
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
    fen: "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1",
    status: "당신 차례입니다", can_undo: false, in_check: false,
    engine: {name: "Regicide test", movetime_ms: ms, settings_revision: settings,
      active_movetime_ms: null, extra_budget_ms: 0, active_extra_budget_ms: null,
      ...(phase ? {boss_mode: true, boss_phase: phase} : {})}};
}
const submit = () => byId("new-game-form").listeners.submit({preventDefault() {}});
const changeTime = ms => { byId("think-time").value = String(ms); byId("think-time").listeners.change(); };
const clickSquare = square => byId("board").listeners.click({target: {closest: () => ({dataset: {square}})}});
function sparringFixture(revision, phase, observations = 3, status = "adapting") {
  const value = fixture(revision);
  value.game_id = "sparring-game"; value.can_undo = true;
  value.engine.sparring_mode = true;
  value.engine.extra_budget_ms = 250;
  value.engine.sparring = {phase, status, observations, level: .65, confidence: "high", target_loss_cp: 72};
  return value;
}
const selectMode = mode => { byId("boss-mode").value = mode; byId("boss-mode").listeners.change(); };
function ratingFixture(revision, estimate = null, delta = null, label = null) {
  const value = sparringFixture(revision, "sparring");
  value.engine.sparring_rating = {estimate, delta, label, target: "chess_com_rapid",
    status: estimate == null ? "awaiting_observations" : "provisional_uncalibrated",
    observations: estimate == null ? 0 : 2, calibration_games: 0, source: "heuristic_prior",
    updated_human_ply: estimate == null ? null : 1};
  return value;
}
const saveRating = value => { byId("rating-label-input").value = String(value); byId("rating-label-form").listeners.submit({preventDefault() {}}); };

(async () => {
  await reply("/api/state", fixture(1));
  assert.doesNotMatch(byId("opponent-detail").textContent, /갬빗 모드/);
  submit();
  assert.equal(pending[0].body.boss_mode, false, "default request explicitly selects normal mode");
  assert.equal(pending[0].body.sparring_mode, false);
  await reply("/api/new", fixture(2));
  byId("boss-mode").value = "boss";
  changeTime(3000);
  assert.equal(byId("boss-mode").disabled, true);
  assert.deepEqual(pending[0].body, {game_id: "legacy-game", settings_revision: 0, movetime_ms: 3000});
  await reply("/api/think-time", fixture(2, null, 1, 3000));
  assert.equal(byId("boss-mode").value, "boss", "render does not overwrite prospective mode");
  assert.doesNotMatch(byId("opponent-detail").textContent, /갬빗 모드/, "draft does not change the live game");
  submit();
  assert.equal(pending[0].body.boss_mode, true);
  assert.equal(pending[0].body.sparring_mode, false, "boss excludes sparring");
  await reply("/api/new", fixture(3, "opening", 0, 3000));
  assert.match(byId("opponent-detail").textContent, /갬빗 모드 · 갬빗 준비/);
  clickSquare("e2"); clickSquare("e4");
  const human = fixture(4, "initiative", 0, 3000);
  human.human_to_move = false; human.engine_to_move = true;
  human.moves = [{ply: 1, san: "e4", uci: "e2e4", actor: "human"}];
  await reply("/api/move", human);
  assert.match(byId("opponent-detail").textContent, /갬빗 모드 · 공격 전개/);
  assert.equal(byId("boss-mode").disabled, true, "no mode edits during an active board request");
  changeTime(1000);
  const done = fixture(5, "trap_hunt", 0, 3000);
  done.moves = [...human.moves, {ply: 2, san: "e5", uci: "e7e5", actor: "engine"}];
  await reply("/api/engine", done);
  assert.match(byId("opponent-detail").textContent, /갬빗 모드 · 전술 사냥/);
  const lateSetting = structuredClone(human);
  lateSetting.engine.settings_revision = 1;
  lateSetting.engine.movetime_ms = 1000;
  await reply("/api/think-time", lateSetting);
  assert.match(byId("opponent-detail").textContent, /갬빗 모드 · 전술 사냥/,
    "late time response cannot replace the phase from the newer board");
  assert.equal(byId("think-time").value, "1000");
  changeTime(3000);
  await reply("/api/think-time", fixture(6, "full_strength", 2, 3000));
  assert.match(byId("opponent-detail").textContent, /갬빗 모드 · 전력 대국/);
  byId("boss-mode").value = "normal";
  assert.match(byId("opponent-detail").textContent, /전력 대국/, "drafting basic mode does not stop the boss game");
  changeTime(1000);
  await reply("/api/think-time", fixture(6, "constructor", 3, 1000));
  assert.match(byId("opponent-detail").textContent, /갬빗 모드$/);
  assert.doesNotMatch(byId("opponent-detail").textContent, /constructor|function|Object/);
  submit();
  assert.equal(pending[0].body.boss_mode, false);
  const normal = fixture(7); normal.game_id = "normal-game";
  await reply("/api/new", normal);
  assert.doesNotMatch(byId("opponent-detail").textContent, /갬빗 모드/);
  assert.equal(pending.length, 0);
  changeTime(100);
  const fastNormal = structuredClone(normal);
  fastNormal.engine.movetime_ms = 100; fastNormal.engine.settings_revision = 1;
  await reply("/api/think-time", fastNormal);
  const requestsBeforeDraft = calls.length;
  selectMode("sparring");
  assert.match(byId("boss-mode-help").textContent, /20수까지 ±1점 범위를 목표로 형세를 조절하고, 21~49수에는 매 수 일정한 폭으로 강도를 높여 50번째 수부터 전력으로 둡니다\./);
  assert.match(byId("boss-mode-help").title, /21~49수.*양쪽 모두 50번째 수부터 전력/);
  assert.match(byId("boss-mode-help").textContent, /20수까지 형세가 벌어지면 균형 회복에 계산 시간을 우선 배분/);
  assert.match(byId("boss-mode-help").textContent, /자동으로 조절/);
  assert.equal(byId("think-time-field").hidden, false);
  assert.equal(byId("auto-think-time").hidden, true);
  assert.equal(byId("think-time").disabled, false);
  assert.match(byId("boss-mode-help").title, /각각 20번/);
  assert.equal(calls.length, requestsBeforeDraft, "draft mode does not send time mutation");
  assert.equal(byId("think-time").value, "100", "draft mode does not replace selected time");
  assert.doesNotMatch(byId("think-time-help").textContent, /맞춤 조절을 쉬고/, "draft mode does not change current normal-game behavior");
  assert.doesNotMatch(byId("opponent-detail").textContent, /스파링/, "draft leaves current normal game intact");
  submit();
  assert.equal(pending[0].body.boss_mode, false);
  assert.equal(pending[0].body.sparring_mode, true);
  assert.equal(Object.hasOwn(pending[0].body, "movetime_ms"), false, "new sparring delegates its search budget to automatic management");
  const fastSparring = sparringFixture(8, "calibration", 0, "calibrating");
  fastSparring.engine.movetime_ms = 100; fastSparring.engine.extra_budget_ms = 0;
  await reply("/api/new", fastSparring);
  assert.match(byId("opponent-detail").textContent, /스파링 모드$/);
  assert.equal(clientState().engine.sparring.observations, 0);
  assert.match(byId("think-time-help").textContent, /사람 권장시간.*5분 이하/);
  const beforeBlocked = calls.length; changeTime(5000);
  assert.equal(calls.length, beforeBlocked, "active sparring rejects forged manual control events");
  for (const [revision, phase, label] of [[9, "sparring", "맞춤 스파링"], [10, "ramp", "압박 높이는 중"], [11, "finish", "전력 마무리"]]) {
    sandbox.__applyState(sparringFixture(revision, phase, 8));
    assert.match(byId("opponent-detail").textContent, /스파링 모드$/);
    assert.equal(clientState().engine.sparring.phase, phase);
    assert.doesNotMatch(byId("opponent-detail").textContent, /관측|파악|압박|마무리/);
  }
  assert.doesNotMatch(byId("think-time-help").textContent, /맞춤 조절을 쉬고/, "eligible current time clears short-time notice");
  assert.match(byId("think-time-help").textContent, /사람 권장시간/);
  assert.doesNotMatch(byId("opponent-detail").textContent, /0\.65|72|%|Elo/);
  const late = sparringFixture(9, "calibration", 1);
  late.engine.settings_revision = 1; late.engine.movetime_ms = 3000;
  sandbox.__applyState(late);
  assert.equal(clientState().engine.sparring.phase, "finish", "stale settings cannot replace profile");
  assert.equal(clientState().engine.sparring.observations, 8);
  assert.equal(byId("think-time").value, "3000");
  byId("undo").listeners.click();
  await reply("/api/undo", sparringFixture(12, "calibration", 1, "holding"));
  assert.equal(clientState().engine.sparring.observations, 1, "undo may legitimately decrease observations");
  assert.equal(clientState().engine.sparring.phase, "calibration");
  assert.equal(byId("opponent-detail").title, "", "holding and observation diagnostics stay out of live tooltips");
  sandbox.__applyState(sparringFixture(13, "", null, "holding"));
  assert.match(byId("opponent-detail").textContent, /스파링 모드$/);
  const missing = sparringFixture(14, ""); missing.engine.sparring = null;
  sandbox.__applyState(missing);
  assert.match(byId("opponent-detail").textContent, /스파링 모드$/);
  selectMode("boss");
  assert.match(byId("boss-mode-help").textContent, /기물 미끼/);
  assert.match(byId("opponent-detail").textContent, /스파링 모드$/, "prospective boss explanation leaves current sparring intact");
  const blockedWithBossDraft = calls.length; changeTime(1000);
  assert.equal(calls.length, blockedWithBossDraft, "changing draft mode cannot enable current sparring settings");
  assert.equal(byId("think-time-field").hidden, true);
  const current = sparringFixture(15, "finish", 6);
  sandbox.__applyState(current);
  const poll = intervals.find(row => row.ms === 200).callback;
  const score = {game_id: current.game_id, revision: current.revision, fen: current.fen,
    status: "ready", cp: 125, mate: null, depth: 8, bound: "exact", white_share: .6};
  score.reference_evaluation = {game_id:current.game_id, revision:current.revision, fen:current.fen,turn:current.turn,
    status:"ready",source:"stockfish_gauge",cp:125,mate:null,depth:16,bound:"exact",white_share:.6};
  poll(); await reply("/api/evaluation", score);
  assert.match(byId("evaluation-label").textContent, /Stockfish.*백 우세/, "Stockfish evaluation stays separate from opponent-profile confidence");
  for (const [reliability, label] of [["provisional", "잠정 평가"], ["stable", "탐색 안정"], ["volatile", "평가 변동 큼"], ["bounded", "범위 평가"]]) {
    poll();
    await reply("/api/evaluation", {...score, reliability, recent_cp_range_white: [-900, -300], score_swing_cp: 600, stable_depths: 3});
    assert.equal(byId("evaluation-label").textContent.includes(label), false, "native reliability does not label the Stockfish gauge");
    assert.match(byId("evaluation-detail").textContent, /Stockfish/);
    assert.match(byId("evaluation-bar").title, /Stockfish/);
    assert.equal(byId("evaluation-score").textContent, "+1.25", "reliability range never replaces latest score");
    assert.equal(byId("evaluation-white").style.height, "60%", "reliability never changes bar fill");
    if (reliability === "stable") assert.match(byId("evaluation-detail").textContent, /정답의 보장이 아닙니다/);
  }
  poll(); await reply("/api/evaluation", {...score, fen: "wrong position", reliability: "stable"});
  assert.match(byId("evaluation-label").textContent, /Stockfish.*백 우세/, "wrong-position native reliability is rejected");
  poll(); await reply("/api/evaluation", {...score, reliability: "constructor"});
  assert.match(byId("evaluation-label").textContent, /Stockfish.*백 우세/, "unknown native reliability is ignored");
  poll();
  selectMode("normal");
  byId("think-time").value = ""; // A server-selected budget need not be a manual option.
  submit();
  assert.equal(Object.hasOwn(pending.find(call => call.url === "/api/new").body, "movetime_ms"), false, "new timed normal mode also delegates time management");
  assert.equal(pending.find(call => call.url === "/api/new").body.sparring_mode, false);
  assert.equal(pending.find(call => call.url === "/api/new").body.boss_mode, false);
  const finalNormal = fixture(16); finalNormal.game_id = "final-normal";
  await reply("/api/new", finalNormal);
  assert.equal(byId("think-time-field").hidden, false);
  assert.equal(byId("auto-think-time").hidden, true);
  assert.equal(byId("think-time").disabled, false);
  await reply("/api/evaluation", {...score, reliability: "stable"});
  assert.doesNotMatch(byId("opponent-detail").textContent, /스파링|갬빗/);
  assert.doesNotMatch(byId("evaluation-label").textContent, /탐색 안정/, "late previous-game analysis cannot revive score or reliability");
  selectMode("sparring"); submit();
  await reply("/api/new", ratingFixture(17));
  assert.match(byId("sparring-rating").textContent, /표본 부족/);
  assert.equal(byId("rating-label-panel").hidden, true, "live game cannot submit a calibration label");
  clickSquare("e2"); clickSquare("e4");
  const humanRating = ratingFixture(18);
  humanRating.human_to_move = false; humanRating.engine_to_move = true;
  await reply("/api/move", humanRating);
  assert.match(byId("sparring-rating").textContent, /분석 중/);
  await reply("/api/engine", ratingFixture(19, 1250, 20));
  assert.equal(byId("sparring-rating").textContent, "추정 래피드 ~1250 (+20) · 실험");
  assert.match(byId("sparring-rating").title, /공식 레이팅이 아닙니다/);
  const calibratedV2 = ratingFixture(19, 1450, -8);
  Object.assign(calibratedV2.engine.sparring_rating, {method: "regicide-sparring-rating-v2",
    observations: 9, calibration_games: 2, evidence_level: "low"});
  sandbox.__applyState(calibratedV2);
  assert.equal(byId("sparring-rating").textContent, "추정 래피드 ~1450 (-8) · 표본 적음");
  assert.match(byId("sparring-rating").title, /분석한 수 9개 · 실제 점수 보정 2판/);
  sandbox.__applyState(ratingFixture(19, 1250, 20));
  const staleRating = ratingFixture(18);
  staleRating.engine.settings_revision = 1; staleRating.engine.movetime_ms = 3000;
  sandbox.__applyState(staleRating);
  assert.match(byId("sparring-rating").textContent, /~1250 \(\+20\)/, "stale settings do not erase a verified estimate");
  byId("undo").listeners.click();
  await reply("/api/undo", ratingFixture(20));
  assert.match(byId("sparring-rating").textContent, /표본 부족/, "undo can remove the last verified observation");
  sandbox.__applyState(ratingFixture(21, 1210, -40));
  assert.match(byId("sparring-rating").textContent, /~1210 \(-40\)/);
  byId("resign").listeners.click();
  const terminalRating = ratingFixture(22, 1210, -40);
  terminalRating.result = "0-1"; terminalRating.human_to_move = false;
  await reply("/api/resign", terminalRating);
  assert.equal(byId("rating-label-panel").hidden, false);
  const beforeInvalid = calls.length;
  saveRating(99);
  assert.equal(calls.length, beforeInvalid);
  assert.match(byId("error-text").textContent, /100~4000/);
  saveRating(1200);
  const failedLabelIndex = pending.findIndex(call => call.url === "/api/rating-label");
  const [failedLabel] = pending.splice(failedLabelIndex, 1);
  failedLabel.resolve({ok: false, json: async () => ({error: "저장에 실패했습니다.", state: structuredClone(terminalRating)})});
  await flush();
  assert.doesNotMatch(byId("rating-label-note").textContent, /저장됨/, "failed request is not displayed as saved");
  assert.match(byId("error-text").textContent, /저장에 실패/);
  saveRating(1420);
  assert.deepEqual(pending.find(call => call.url === "/api/rating-label").body,
    {game_id: "sparring-game", rating: 1420, revision: 22});
  assert.equal(byId("rating-label-save").disabled, true);
  const savedRating = structuredClone(terminalRating); savedRating.revision = 23;
  savedRating.engine.sparring_rating.label = {rating: 1420, source: "user_reported", recorded_at: "synthetic"};
  await reply("/api/rating-label", savedRating);
  assert.match(byId("rating-label-note").textContent, /1420 저장됨/);
  assert.match(byId("rating-label-note").textContent, /다음 대국/);
  assert.equal(byId("rating-label-save").textContent, "레이팅 수정");
  assert.match(byId("sparring-rating").textContent, /~1210 \(-40\)/, "label does not rewrite the current estimated curve");
  saveRating(1500);
  const corrected = structuredClone(savedRating); corrected.revision = 24;
  corrected.engine.sparring_rating.label.rating = 1500;
  corrected.engine.sparring_rating.label.eligible_for_calibration = false;
  await reply("/api/rating-label", corrected);
  assert.equal(byId("rating-label-input").value, "1500");
  assert.match(byId("rating-label-note").textContent, /1500 저장됨/);
  assert.match(byId("rating-label-note").textContent, /이번 대국은 추정 보정에 쓰지 않습니다/);
  const staleLabel = structuredClone(terminalRating); staleLabel.engine.settings_revision = 2;
  sandbox.__applyState(staleLabel);
  assert.match(byId("rating-label-note").textContent, /1500 저장됨/, "late terminal settings cannot erase a saved correction");
  selectMode("normal"); submit();
  const afterRating = fixture(25); afterRating.game_id = "after-rating-normal";
  await reply("/api/new", afterRating);
  assert.equal(byId("sparring-rating").hidden, true);
  assert.equal(byId("rating-label-panel").hidden, true);
  assert.equal(byId("rating-label-input").value, "", "new game resets the previous label draft");
  assert.equal(pending.length, 0);
  byId("rapid-time-control").value = "10+0";
  for (const [index, mode] of ["normal", "boss"].entries()) {
    const commonRating = ratingFixture(50 + index, null, null);
    commonRating.result = "1/2-1/2";
    commonRating.engine.sparring_mode = false;
    commonRating.engine.boss_mode = mode === "boss";
    commonRating.engine.rating = commonRating.engine.sparring_rating;
    delete commonRating.engine.sparring_rating;
    sandbox.__applyState(commonRating);
    assert.equal(byId("rating-label-panel").hidden, false, `${mode} finished game accepts a rating without an estimate`);
    saveRating(1600 + index);
    assert.equal(pending.some(call => call.url === "/api/rating-label"), true);
    const confirmed = structuredClone(commonRating); confirmed.revision += 1;
    confirmed.engine.rating.label = {rating:1600 + index};
    await reply("/api/rating-label", confirmed);
    assert.match(byId("rating-label-note").textContent, /저장됨/);
  }
  for (const [index, mode] of ["normal","boss","sparring"].entries()) {
    const timed = fixture(100 + index);
    timed.game_id = "shared-auto-" + mode;
    Object.assign(timed.engine,{automatic_think_time:true,time_management:"automatic",boss_mode:mode==="boss",sparring_mode:mode==="sparring"});
    timed.clock = {enabled:true,control_id:"10+0",label:"10분 래피드",white_ms:600000,black_ms:600000,running_color:"white",status:"running",turn_elapsed_ms:0};
    selectMode(mode);submit();
    assert.equal(Object.hasOwn(pending[0].body,"movetime_ms"),false,"every timed mode omits a fixed search duration: "+mode);
    assert.equal(pending[0].body.time_control,"10+0");
    await reply("/api/new",timed);
    assert.equal(byId("think-time-field").hidden,true);
    assert.equal(byId("think-time").disabled,true);
    assert.match(byId("think-time-help").textContent,/기본·갬빗·스파링.*5분 이하/);
    assert.match(byId("opponent-detail").textContent,/생각시간 자동 조절/);
    selectMode(mode==="normal" ? "boss" : "normal");
    const before=calls.length;changeTime(5000);
    assert.equal(calls.length,before,"draft mode cannot enable a timed game's manual control");
  }
  const untimed=fixture(110);untimed.game_id="explicit-untimed";
  Object.assign(untimed.engine,{automatic_think_time:false,time_management:"fixed_legacy"});untimed.clock={enabled:false};
  sandbox.__applyState(untimed);selectMode("sparring");
  assert.equal(byId("think-time-field").hidden,false,"a draft timed mode does not change a live untimed API game");
  const clockOnly=fixture(111);clockOnly.game_id="clock-only-timing";clockOnly.clock={enabled:true};
  sandbox.__applyState(clockOnly);
  assert.equal(byId("think-time-field").hidden,true,"timed normal games remain automatic without optional metadata");
  console.log("Browser mode/reliability checks: PASS (three exclusive modes, phases, draft, stale settings, undo, separate reliability, identity guards)");
})().catch(error => { console.error(error); process.exitCode = 1; });
