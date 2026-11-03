"use strict";

// Execute the shipped script with a controlled DOM and state payloads.
// This checks UI behavior, not browser rendering or opening chess strength.
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
    this.className = "";
    this.value = "";
    this.open = false;
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/), ...names])].join(" ").trim(); },
      contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, enabled) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        if (enabled ?? !names.has(name)) names.add(name); else names.delete(name);
        this.className = [...names].join(" ");
      }
    };
  }
  set textContent(value) { this.valueText = String(value); this.children = []; }
  get textContent() { return this.valueText || ""; }
  set innerHTML(_) { throw new Error("Opening content must never use HTML injection"); }
  setAttribute(name, value) { this.attributes[name] = value; }
  append(...items) { for (const item of items) this.children.push(...(item.tag === "fragment" ? item.children : [item])); }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  querySelector(selector) { return this.children.find(item => item.dataset.square === selector.match(/"(.*?)"/)?.[1]) || null; }
  focus() {}
}

const source = fs.readFileSync(path.join(__dirname, "../web/human-play/app.js"), "utf8");
const flush = async () => { for (let i = 0; i < 25; i++) await Promise.resolve(); };
const contents = element => [element.textContent, ...element.children.map(contents)].join(" ");
const descendants = element => [element, ...element.children.flatMap(descendants)];
const opening = extra => ({
  status:"in_catalog", history_scope:"standard", history_plies:0, position_key:"start",
  current_matches:[], current_matches_total:0, current_matches_truncated:false, matched_opening:null,
  continuations:[{uci:"e2e4", san:"e4", immediate_openings:[{name:"King's Pawn Opening", eco:"B00"}],
    immediate_total:1, immediate_truncated:false,
    reachable_openings:[{name:"Ruy Lopez Opening: Morphy Defense", eco:"C70", distance_plies:6,
      remaining_uci:["e2e4","e7e5","g1f3","b8c6","f1b5","a7a6"],
      remaining_san:"1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"}],
    reachable_total:1, reachable_lines_total:1, reachable_truncated:false}],
  continuations_total:1, continuations_truncated:false,
  source:{repository_url:"https://github.com/lichess-org/chess-openings",
    license_url:"https://creativecommons.org/publicdomain/zero/1.0/",
    license:"CC0-1.0", commit:"a6189a30dc273ccb21fc2536a9a2fefd5592a67a",
    attribution:"Lichess chess-openings"},
  disclaimer:"공개된 오프닝 수순의 분류입니다. 승률이나 엔진의 추천 수가 아닙니다.", ...extra
});
function state(extra = {}) {
  return {game_id:"live", revision:0, fen:"start", human_color:"white", turn:"white",
    human_to_move:true, engine_to_move:false, result:"*", error:null,
    board:{e2:"P", e7:"p"}, legal_moves:["e2e4", "d2d4"], moves:[], hint_enabled:false,
    engine:{name:"Regicide", movetime_ms:1000, settings_revision:0},
    opening_guide:opening(), ...extra};
}

async function browser(initial = state()) {
  const elements = new Map();
  const requests = [];
  const context = {
    document:{querySelector:() => ({content:"test-token"}),
      getElementById:id => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
      }, createElement:tag => new Element(tag), createDocumentFragment:() => new Element("fragment"),
      activeElement:null},
    fetch:async (url, options) => {
      requests.push({url, options});
      assert.equal(url, "/api/state", "Opening previews must not issue requests or play moves");
      return {ok:true, json:async () => initial};
    },
    window:{addEventListener() {}}, URL, AbortController,
    setTimeout:() => 1, clearTimeout() {}, setInterval:() => 1, clearInterval() {}
  };
  vm.runInNewContext(source.replace("  void initialize();",
    "  window.testApi = {applyState, render, renderOpeningGuide, selection: () => selected};\n  void initialize();"), context);
  await flush();
  return {elements, requests, api:context.window.testApi, element:id => elements.get(id)};
}

const cases = [];
const test = (name, run) => cases.push({name, run});

test("opening guidance lives in its own panel while common game tools precede the scrolling panels", () => {
  const html = fs.readFileSync(path.join(__dirname, "../web/human-play/index.html"), "utf8");
  assert.ok(html.indexOf('id="undo"') < html.indexOf('id="live-sidebar-panel-game"'));
  assert.ok(html.indexOf('id="live-sidebar-panel-opening"') < html.indexOf('id="opening-guide"'));
  assert.ok(html.indexOf('id="opening-guide"') < html.indexOf('id="live-sidebar-panel-records"'));
  assert.ok(html.indexOf('id="live-sidebar-panel-records"') < html.indexOf('class="card moves-card"'));
  assert.match(html, /id="opening-status"[^>]+aria-live="polite"/);
});

test("current exact and transposed names keep ECO and source labels", async () => {
  const guide = opening({current_matches:[{name:"Ruy Lopez Opening", eco:"C60", match_type:"exact"},
    {name:"Spanish Game", eco:"C60", match_type:"position"}]});
  const ui = await browser(state({opening_guide:guide}));
  assert.equal(ui.element("opening-name").textContent, "Ruy Lopez Opening · C60");
  assert.match(ui.element("opening-aliases").textContent, /Spanish Game · C60/);
  assert.match(ui.element("opening-status").textContent, /현재 수순에 일치/);
  const links = descendants(ui.element("opening-sources")).filter(item => item.tag === "a");
  assert.equal(links.length, 2);
  assert.equal(links[0].attributes.rel, "noopener noreferrer");
  assert.match(contents(ui.element("opening-sources")), /a6189a3/);
  ui.api.applyState(state({revision:1, fen:"transposed", opening_guide:opening({
    current_matches:[{name:"Ruy Lopez Opening", eco:"C60", match_type:"transposition"}]})}));
  assert.match(ui.element("opening-status").textContent, /다른 수순으로 같은 오프닝/);
});

test("a next-move preview shows future registered names, distance and remaining SAN without playing", async () => {
  const ui = await browser();
  const branch = ui.element("opening-branches").children[0];
  assert.equal(branch.tag, "details");
  assert.equal(branch.children[0].tag, "summary");
  assert.equal(branch.dataset.uci, "e2e4");
  assert.match(contents(branch.children[0]), /e4.*King's Pawn Opening.*B00/);
  assert.match(contents(branch), /Ruy Lopez Opening: Morphy Defense · C70/);
  assert.match(contents(branch), /이 수를 포함해 6번 이동 뒤/);
  assert.match(contents(branch), /1\. e4 e5 2\. Nf3 Nc6 3\. Bb5 a6/);
  branch.open = true; // Native details expansion changes only presentation.
  ui.api.render();
  assert.equal(ui.element("opening-branches").children[0], branch);
  assert.equal(branch.open, true);
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.api.selection(), null);
});

test("selection and same-position time refresh preserve expanded examples", async () => {
  const initial = state();
  const ui = await browser(initial);
  const branch = ui.element("opening-branches").children[0];
  branch.open = true;
  ui.element("board").listeners.click({target:{closest:() => ({dataset:{square:"e2"}})}});
  assert.equal(ui.api.selection(), "e2");
  ui.api.applyState(state({engine:{name:"Regicide", movetime_ms:5000, settings_revision:1}}), true);
  assert.equal(ui.api.selection(), "e2");
  assert.equal(ui.element("opening-branches").children[0], branch);
  assert.equal(branch.open, true);
  assert.equal(ui.element("think-time").value, "5000");
  assert.equal(ui.requests.length, 1);
});

test("state and replay payload changes replace old lines while late revisions stay ignored", async () => {
  const ui = await browser();
  const first = ui.element("opening-branches").children[0];
  const replay = state({game_id:"replay", revision:7, fen:"replay-position", human_to_move:false,
    legal_moves:[], opening_guide:opening({history_scope:"custom",
      current_matches:[{name:"French Defense", eco:"C00", match_type:"position"}]})});
  ui.api.applyState(replay);
  assert.equal(ui.element("opening-name").textContent, "French Defense · C00");
  assert.match(ui.element("opening-status").textContent, /직접 불러온 위치/);
  assert.notEqual(ui.element("opening-branches").children[0], first);
  assert.equal(ui.element("opening-branches").children.length, 1,
    "Opponent/replay positions need previews even with an empty human move list");
  ui.api.applyState(state({revision:6, fen:"late", opening_guide:opening({
    current_matches:[{name:"Late stale opening", eco:"A00"}]})}));
  assert.equal(ui.element("opening-name").textContent, "French Defense · C00");
  assert.equal(ui.requests.length, 1);
});

test("outside catalog, unknown and missing data stay useful without stale branches", async () => {
  const ui = await browser();
  ui.api.applyState(state({revision:1, fen:"after", opening_guide:opening({status:"beyond_catalog",
    current_matches:[], matched_opening:{name:"Italian Game", eco:"C50"}, continuations:[]})}));
  assert.equal(ui.element("opening-name").textContent, "마지막 확인: Italian Game · C50");
  assert.match(ui.element("opening-status").textContent, /마지막으로 확인한 이름/);
  assert.equal(ui.element("opening-branches").children.length, 0);
  assert.equal(ui.element("opening-empty").hidden, false);
  ui.api.applyState(state({revision:2, fen:"unknown", opening_guide:opening({status:"unknown", continuations:[]})}));
  assert.equal(ui.element("opening-badge").textContent, "등록 이름 없음");
  ui.api.applyState(state({revision:3, fen:"old-server", opening_guide:undefined}));
  assert.match(ui.element("opening-status").textContent, /자료를 읽지 못했습니다.*대국은 계속/);
  assert.equal(ui.element("error-panel").hidden, true);
  assert.equal(ui.element("new-game").disabled, false);
});

test("white setup and black response are distinct with exact combined classification retained", async () => {
  const sides = {white:{name:"King's Pawn Opening", status:"confirmed", detail:"백은 e폰을 먼저 전진했습니다."},
    black:{name:"Sicilian Defense", status:"confirmed", detail:"흑의 c5 응수입니다."}, note:"실제로 확인된 수순입니다."};
  const ui = await browser(state({opening_guide:opening({sides,
    current_matches:[{name:"Sicilian Defense: Najdorf Variation", eco:"B90", match_type:"exact"}]})}));
  assert.equal(ui.element("opening-white").textContent, "백의 오프닝 · King's Pawn Opening");
  assert.equal(ui.element("opening-black").textContent, "흑의 대응 · Sicilian Defense");
  assert.equal(ui.element("opening-name").textContent, "Sicilian Defense: Najdorf Variation · B90");
  assert.match(ui.element("opening-black").title, /흑의 c5 응수/);
  assert.equal(ui.requests.length, 1);
});

test("pending, historical and position-only sides do not claim current confirmed openings", async () => {
  const ui = await browser(state({opening_guide:opening({sides:{
    white:{name:"Réti Opening", status:"confirmed", detail:"첫 수 Nf3"},
    black:{name:null, status:"pending", detail:"아직 흑의 대응 전입니다."}}})}));
  assert.equal(ui.element("opening-black").textContent, "흑의 대응 · 아직 흑의 대응 전입니다.");
  ui.api.applyState(state({revision:1, fen:"past-opening", opening_guide:opening({sides:{
    white:{name:"Réti Opening", status:"last_known", detail:"앞선 수순에서 확인했습니다."},
    black:{name:"King's Indian Defense", status:"position", detail:"현재 배치 기준입니다."}}})}));
  assert.equal(ui.element("opening-white").textContent, "백의 오프닝 · 마지막 확인: Réti Opening");
  assert.equal(ui.element("opening-black").textContent, "흑의 대응 · 현재 배치: King's Indian Defense");
  ui.api.applyState(state({revision:2, fen:"legacy", opening_guide:opening()}));
  assert.equal(ui.element("opening-white").textContent, "백의 오프닝 · 확인 정보 없음");
  assert.equal(ui.element("opening-black").textContent, "흑의 대응 · 확인 정보 없음");
  ui.api.applyState(state({revision:3, fen:"missing", opening_guide:undefined}));
  assert.equal(ui.element("opening-white").textContent, "백의 오프닝 · 안내 없음");
  assert.equal(ui.element("opening-black").textContent, "흑의 대응 · 안내 없음");
});

test("long and hostile side names remain plain text with complete tooltip and no actions", async () => {
  const name = "Sicilian Defense: "+"A very long variation ".repeat(8);
  const ui = await browser(state({opening_guide:opening({sides:{
    white:{name, status:"confirmed", detail:"설명"},
    black:{name:"<svg onload=alert(1)>", status:"confirmed", detail:"<b>literal text</b>"}}})}));
  assert.equal(ui.element("opening-white").title.split("\n")[0], "백의 오프닝 · "+name.trim());
  assert.equal(ui.element("opening-black").textContent, "흑의 대응 · <svg onload=alert(1)>");
  assert.equal(ui.element("opening-black").children.length, 0);
  assert.equal(ui.requests.length, 1);
  const css = fs.readFileSync(path.join(__dirname, "../web/human-play/style.css"), "utf8");
  assert.match(css, /\.opening-side\s*\{[^}]*height:22px;[^}]*white-space:nowrap;[^}]*overflow:hidden;[^}]*text-overflow:ellipsis/s);
});

test("verified supplemental names without ECO display no invented code or trailing separator", async () => {
  const ui = await browser(state({opening_guide:opening({
    current_matches:[{name:"Modern Defense", eco:"", match_type:"exact"}],
    sides:{white:{name:"Queen's Pawn Opening", status:"confirmed", detail:"백의 d4 전개입니다."},
      black:{name:"Modern Defense", status:"confirmed", detail:"흑의 g6 대응입니다."}}})}));
  assert.equal(ui.element("opening-name").textContent, "Modern Defense");
  assert.equal(ui.element("opening-black").textContent, "흑의 대응 · Modern Defense");
});

test("finite coverage and malformed or human-illegal branches are explicit", async () => {
  const guide = opening();
  guide.continuations[0].reachable_truncated = true;
  guide.continuations.push({uci:"e2e5", san:"e5", reachable_openings:[]}, null,
    {uci:"<script>", san:"bad"});
  guide.continuations_total = 9;
  guide.continuations_truncated = true;
  const ui = await browser(state({opening_guide:guide}));
  assert.equal(ui.element("opening-branches").children.length, 1);
  assert.match(contents(ui.element("opening-branches")), /일부를 표시/);
  assert.match(ui.element("opening-coverage").textContent, /9개 중 1개/);
  assert.match(ui.element("opening-disclaimer").textContent, /승률이나 엔진의 추천 수가 아닙니다/);
});

test("source failure and hostile labels never inject markup, unsafe links or game actions", async () => {
  const guide = opening({status:"unavailable", current_matches:null,
    source:{repository_url:"javascript:alert(1)", license_url:"https://user:secret@example.com/",
      attribution:"<img src=x onerror=alert(1)>"}});
  const ui = await browser(state({opening_guide:guide}));
  assert.equal(ui.element("opening-branches").children.length, 0);
  assert.equal(descendants(ui.element("opening-sources")).filter(item => item.tag === "a").length, 0);
  assert.match(contents(ui.element("opening-sources")), /<img src=x onerror=alert\(1\)>/);
  assert.equal(ui.element("error-panel").hidden, true);
  const unsafe = opening({current_matches:[{name:"<svg onload=alert(2)>", eco:"C60"}],
    source:{repository_url:"not a URL"}});
  ui.api.applyState(state({revision:1, fen:"safe-text", opening_guide:unsafe}));
  assert.equal(ui.element("opening-name").textContent, "<svg onload=alert(2)> · C60");
  assert.equal(ui.requests.length, 1);
});

(async () => {
  for (const {name, run} of cases) { await run(); console.log(`PASS ${name}`); }
  console.log(`${cases.length} opening-guide browser behavior cases passed.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
