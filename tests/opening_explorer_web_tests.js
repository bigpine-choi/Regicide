"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.style = {}; this.value = ""; this.checked = false; this.disabled = false; this.hidden = false; this.textContent = ""; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, action) { this.listeners[name] = action; }
  focus() { this.focused = true; }
  dispatch(name, event = {}) { return this.listeners[name]?.(event); }
}
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const find = (root, predicate) => predicate(root) ? root : root.children.map((child) => find(child, predicate)).find(Boolean);
const all = (root, predicate) => [...(predicate(root) ? [root] : []), ...root.children.flatMap((child) => all(child, predicate))];
const content = (root) => [root.textContent || "", ...root.children.map(content)].join(" ");
const fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const nextFen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1";

function browser() {
  const hosts = new Map(["live", "analysis"].map((name) => [`${name}-opening-explorer`, new Element()]));
  const requests = [], timers = new Map(), events = [], lifecycle = {};
  let timerSequence = 0;
  const window = {addEventListener:(name, action) => { lifecycle[name] = action; }, dispatchEvent:(event) => { events.push(event); return lifecycle[event.type]?.(event); }};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../web/human-play/explorer.js"), "utf8"), {
    document:{querySelector:() => ({content:"local-session-token"}), getElementById:(id) => hosts.get(id), createElement:(tag) => new Element(tag)},
    window, URL, Date, CustomEvent:class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    fetch:(url, options) => new Promise((resolve, reject) => requests.push({url, options,
      finish:(data, ok = true) => resolve({ok, json:async () => data}), fail:() => reject(new Error("network"))})),
    setTimeout:(action) => { const id = ++timerSequence; timers.set(id, action); return id; }, clearTimeout:(id) => timers.delete(id)
  });
  const host = (name = "live") => hosts.get(`${name}-opening-explorer`);
  const control = (id, name = "live") => find(host(name), (element) => element.id === `${name}-explorer-${id}`);
  const ui = {hosts, host, control, requests, timers, events, lifecycle, api:window.RegicideExplorer,
    section(key, name = "live") { find(host(name), (node) => node.id === `${name}-explorer-tab-${key}`).dispatch("click"); },
    toggle(name = "live", checked = true) { const toggle = find(host(name), (element) => element.attributes["aria-label"] === "리체스 오프닝 통계 켜기"); toggle.checked = checked; toggle.dispatch("change"); },
    async finish(value, ok = true, entry = requests.at(-1)) { entry.finish(value, ok); await flush(); },
    update(value = fen, active = true, name = "live") { window.RegicideExplorer.update(name, {fen:value, active}); },
    async poll() { const [id, action] = [...timers.entries()][0]; timers.delete(id); action(); await flush(); }
  };
  return ui;
}

function snapshot(extra = {}) {
  return {status:"ready", request_key:"first", fen, source:"masters", ratings:[], speeds:[], configured:true,
    total:100, white:40, draws:35, black:25, fetched_at:"2026-11-03T03:00:00+00:00", cached:false,
    moves:[{uci:"e2e4", san:"e4", count:80, play_pct:80, white:35, draws:25, black:20, white_pct:43.75, draw_pct:31.25, black_pct:25, fen_after:nextFen}],
    games:[{id:"abcd1234", white:"Alice", black:"Bob", year:2025, url:"https://lichess.org/import/master/abcd1234/white"}], ...extra};
}

const cases = [], test = (name, action) => cases.push({name, action});

test("no external query until enabled; toggling sends only FEN and selected filters", async () => {
  const ui = browser(); ui.update(); assert.equal(ui.requests.length, 0);
  ui.toggle(); assert.equal(ui.requests.length, 1);
  const request = ui.requests[0]; assert.equal(request.url, "/api/opening-explorer");
  assert.equal(request.options.headers["X-Regicide-Token"], "local-session-token");
  const payload = JSON.parse(request.options.body);
  assert.equal(payload.fen, fen); assert.equal(payload.action, "query"); assert.equal(payload.source, "masters");
  assert.deepEqual(Object.keys(payload).sort(), ["action", "fen", "ratings", "source", "speeds"]);
  await ui.finish(snapshot());
  assert.match(content(ui.host()), /100판/); assert.match(content(ui.host()), /80판 · 80.0%/);
  assert.match(content(ui.host()), /Alice — Bob · 2025/); assert.doesNotMatch(content(ui.host()), /2,025/);
  const bar = find(ui.host(), (node) => node.className === "explorer-result-bar");
  assert.equal(bar.attributes["aria-label"], "백승 43.8% / 무 31.3% / 흑승 25.0%");
});

test("query follows new board; stale responses cannot restore old rows", async () => {
  const ui = browser(); ui.update(); ui.toggle(); const old = ui.requests[0];
  ui.update(nextFen); assert.equal(ui.requests.length, 2);
  await ui.finish(snapshot({fen:nextFen, request_key:"second", total:10, moves:[], games:[]}));
  await ui.finish(snapshot(), true, old);
  assert.match(content(ui.host()), /10판/); assert.doesNotMatch(content(ui.host()), /80판/);
});

test("polls only matching generation and stops on disabling or leaving view", async () => {
  const ui = browser(); ui.update(); ui.toggle();
  await ui.finish(snapshot({status:"loading", moves:[], games:[]})); assert.equal(ui.timers.size, 1);
  await ui.poll(); const pending = ui.requests.at(-1);
  assert.equal(pending.options.method, "GET");
  ui.toggle("live", false); await ui.finish(snapshot(), true, pending);
  assert.equal(ui.timers.size, 0); assert.equal(find(ui.host(), (node) => node.className === "explorer-body").hidden, true);
  ui.toggle(); await ui.finish(snapshot({status:"loading"}));
  ui.update(fen, false); assert.equal(ui.timers.size, 0);
});

test("poll identity rejects another client's request key", async () => {
  const ui = browser(); ui.update(); ui.toggle();
  await ui.finish(snapshot({status:"loading", moves:[], games:[]})); await ui.poll();
  await ui.finish(snapshot({request_key:"foreign"}));
  assert.equal(find(ui.host(), (node) => node.className === "explorer-move explorer-explore"), undefined);
  assert.match(content(ui.host()), /다른 위치의 조회로 바뀌었습니다/);
  assert.equal(ui.timers.size, 0);
  find(ui.host(), (node) => node.attributes["aria-label"] === "현재 위치 다시 조회").dispatch("click");
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).action, "query");
});

test("source and rating/speed changes are query parameters and reject stale filters", async () => {
  const ui = browser(); ui.update(); ui.toggle(); await ui.finish(snapshot());
  ui.control("source").value = "lichess"; ui.control("source").dispatch("change");
  let payload = JSON.parse(ui.requests.at(-1).options.body);
  assert.deepEqual(payload.ratings, [1600, 1800, 2000, 2200, 2500]); assert.deepEqual(payload.speeds, ["rapid", "classical"]);
  const old = ui.requests.at(-1);
  ui.control("ratings").value = "2000"; ui.control("ratings").dispatch("change");
  payload = JSON.parse(ui.requests.at(-1).options.body);
  assert.deepEqual(payload.ratings, [2000, 2200, 2500]);
  await ui.finish(snapshot({source:"lichess", ratings:payload.ratings, speeds:payload.speeds, total:15}));
  await ui.finish(snapshot({source:"lichess", ratings:[1600, 1800, 2000, 2200, 2500], speeds:payload.speeds}), true, old);
  assert.match(content(ui.host()), /15판/);
  ui.control("speeds").value = "blitz"; ui.control("speeds").dispatch("change");
  assert.deepEqual(JSON.parse(ui.requests.at(-1).options.body).speeds, ["blitz"]);
});

test("explore opens separate analysis context and never sends a game move", async () => {
  const ui = browser(); ui.update(); ui.toggle(); await ui.finish(snapshot());
  const button = find(ui.host(), (node) => node.className === "explorer-move explorer-explore");
  button.dispatch("click"); assert.equal(ui.events.at(-1).type, "regicide-explorer-analyze");
  assert.equal(ui.events.at(-1).detail.fen, nextFen);
  assert.equal(ui.requests.length, 1); assert.equal(ui.requests.some((request) => /\/move/.test(request.url)), false);
  assert.equal(find(ui.host("analysis"), (node) => node.className === "explorer-body").hidden, false);
  ui.update(nextFen); button.dispatch("click"); assert.equal(ui.events.length, 1);
});

test("reference URLs are allowlisted, invalid rows are skipped, names are text", async () => {
  const ui = browser(); ui.update(); ui.toggle();
  await ui.finish(snapshot({games:[{id:"abcd1234", white:"<script>alert(1)</script>", black:"Bob", url:"https://lichess.org/abcd1234"},
    {id:"abcd1234", white:"evil", url:"https://evil.test/abcd1234"}, {id:"abcd1234", white:"bad-path", url:"https://lichess.org/account/oauth/token"}],
    moves:[...snapshot().moves, {...snapshot().moves[0], uci:"e2e9"}]}));
  const game = find(ui.host(), (node) => node.className === "explorer-game");
  assert.match(game.textContent, /<script>/); assert.equal(game.rel, "noopener noreferrer");
  assert.equal(ui.api.gameLink({id:"abcd1234", url:"https://lichess.org/abcd1234?token=bad"}), null);
  assert.equal(ui.api.gameLink({id:"abcd1234", url:"https://lichess.org/import/master/abcd1234/white"}), "https://lichess.org/import/master/abcd1234/white");
  assert.equal(ui.api.gameLink({id:"abcd1234", url:"javascript:alert(1)"}), null);
  assert.equal(all(ui.host(), (node) => node.className === "explorer-move explorer-explore").length, 1);
  assert.equal(all(ui.host(), (node) => node.className === "explorer-game").length, 1);
  assert.doesNotMatch(content(ui.host()), /evil|bad-path/);
});

test("authentication clears password before request and never displays the token", async () => {
  const ui = browser(); ui.update(); ui.toggle(); await ui.finish(snapshot({status:"auth_required", configured:false}));
  assert.match(content(ui.host()), /연결이 필요/);
  ui.section("connection");
  const secret = ui.control("token"); secret.value = "lip_secret_value";
  assert.match(content(ui.host()), /권한 항목은 아무것도 체크하지/);
  assert.match(content(ui.host()), /리체스 연결키\(토큰\)/);
  const save = find(ui.host(), (node) => node.textContent === "연결하기"); save.dispatch("click");
  assert.equal(secret.value, "");
  const request = ui.requests.at(-1); assert.equal(JSON.parse(request.options.body).token, "lip_secret_value");
  await ui.finish(snapshot({status:"idle", configured:true,message:"안전한 저장을 사용할 수 없어 이번 실행에서만 사용합니다."}), true, request);
  assert.doesNotMatch(content(ui.host()), /lip_secret_value/);
  assert.match(content(ui.host()), /이번 실행에서만 사용/);
  assert.match(content(ui.host()), /실제 통계 조회로 연결 상태를 확인/);
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).action, "query");
});

test("explorer tabs show one section and connection setup pauses remote polling", async () => {
  const ui = browser(); ui.update(); ui.toggle(); await ui.finish(snapshot({status:"loading", moves:[], games:[]}));
  await ui.poll(); const old = ui.requests.at(-1);
  ui.section("connection");
  assert.equal(find(ui.host(), (node) => node.id === "live-explorer-panel-stats").hidden, true);
  assert.equal(find(ui.host(), (node) => node.id === "live-explorer-panel-games").hidden, true);
  assert.equal(find(ui.host(), (node) => node.id === "live-explorer-panel-connection").hidden, false);
  await ui.finish(snapshot(), true, old); assert.equal(ui.timers.size, 0);
  const count = ui.requests.length; ui.update(nextFen); assert.equal(ui.requests.length, count);
  assert.equal(find(ui.host(), (node) => node.className === "explorer-status").textContent, "연결 설정 · 통계 탭으로 돌아가면 현재 위치를 조회합니다.");
  ui.section("stats"); assert.equal(ui.requests.length, count + 1);
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).fen, nextFen);
});

test("hidden parent tabs stop queries and retain filters and usable ready rows on return", async () => {
  const ui = browser(); ui.update(); ui.toggle(); await ui.finish(snapshot());
  const count = ui.requests.length;
  ui.update(fen, false); ui.update(fen, true);
  assert.equal(ui.requests.length, count);
  const row = find(ui.host(), (node) => node.className === "explorer-move explorer-explore"); row.dispatch("click");
  assert.equal(ui.events.at(-1).detail.fen, nextFen);
  ui.control("source").value = "lichess"; ui.control("source").dispatch("change");
  const payload = JSON.parse(ui.requests.at(-1).options.body);
  await ui.finish(snapshot({source:"lichess",ratings:payload.ratings,speeds:payload.speeds}));
  const readyCount = ui.requests.length;
  ui.update(fen, false); ui.update(nextFen, false); assert.equal(ui.requests.length, readyCount);
  ui.update(nextFen, true); assert.equal(ui.requests.length, readyCount + 1);
  assert.equal(ui.control("source").value, "lichess");
  assert.deepEqual(JSON.parse(ui.requests.at(-1).options.body).ratings, payload.ratings);
});

test("internal keyboard navigation uses roving focus and does not fetch while connection is selected", async () => {
  const ui = browser(); ui.update(); ui.toggle(); await ui.finish(snapshot());
  const stats = find(ui.host(), (node) => node.id === "live-explorer-tab-stats");
  let prevented = false; stats.dispatch("keydown", {key:"End",preventDefault(){prevented=true;}});
  const connection = find(ui.host(), (node) => node.id === "live-explorer-tab-connection");
  assert.equal(prevented,true);assert.equal(connection.tabIndex,0);assert.equal(connection.focused,true);assert.equal(stats.tabIndex,-1);
  const count=ui.requests.length;ui.update(nextFen);assert.equal(ui.requests.length,count);
  connection.dispatch("keydown",{key:"Home",preventDefault(){}});assert.equal(stats.tabIndex,0);assert.equal(stats.focused,true);
  assert.equal(ui.requests.length,count+1);
});

test("no data, rate limit and network failure have useful states without stale rows", async () => {
  const ui = browser(); ui.update(); ui.toggle(); await ui.finish(snapshot({total:0, moves:[], games:[]}));
  assert.match(content(ui.host()), /대국이 없습니다/);
  ui.update(nextFen); await ui.finish(snapshot({fen:nextFen, status:"rate_limited", retry_after:75}));
  assert.match(content(ui.host()), /75초/);
  ui.update(fen); ui.requests.at(-1).fail(); await flush();
  assert.match(content(ui.host()), /연결하지 못했습니다/);
  assert.equal(find(ui.host(), (node) => node.className === "explorer-move explorer-explore"), undefined);
});

(async () => {
  for (const entry of cases) { await entry.action(); process.stdout.write(`PASS ${entry.name}\n`); }
  process.stdout.write(`${cases.length} opening explorer web tests passed\n`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
