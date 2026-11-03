"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag, attributes = {}) { this.tag = tag; this.attributes = attributes; this.children = []; this.listeners = {}; this.hidden = Object.hasOwn(attributes,"hidden"); this.tabIndex = Number(attributes.tabindex || 0); this.scrollTop = 0; }
  setAttribute(key,value) { this.attributes[key] = value; }
  addEventListener(name, action) { this.listeners[name] = action; }
  dispatch(name,event = {}) { return this.listeners[name]?.(event); }
  click() { this.dispatch("click"); }
  focus() { this.focused = true; }
}

function browser() {
  const source = fs.readFileSync(path.join(__dirname,"../web/human-play/index.html"),"utf8");
  const ids = new Map(), root = new Element("root"), stack = [root], duplicates = [];
  const voidTags = new Set(["area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"]);
  for (const token of source.matchAll(/<\/?[A-Za-z][^>]*>/g)) {
    const raw = token[0], tag = raw.match(/^<\/?([A-Za-z][\w-]*)/)[1].toLowerCase();
    if (raw.startsWith("</")) { assert.equal(stack.at(-1).tag,tag,`unbalanced ${tag}`); stack.pop(); continue; }
    const attributes = {};
    for (const match of raw.slice(tag.length+1,-1).matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) attributes[match[1]] = match[2] ?? "";
    const node = new Element(tag,attributes); node.parent = stack.at(-1); node.parent.children.push(node);
    if (attributes.id) { if(ids.has(attributes.id)) duplicates.push(attributes.id); ids.set(attributes.id,node); }
    if(!voidTags.has(tag) && !raw.endsWith("/>")) stack.push(node);
  }
  assert.equal(stack.length,1);assert.deepEqual(duplicates,[]);
  const events = [];
  const window = {dispatchEvent:(event)=>{events.push(event);}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../web/human-play/sidebar_tabs.js"),"utf8"),{
    document:{getElementById:(id)=>ids.get(id)},window,
    CustomEvent:class {constructor(type,value){this.type=type;this.detail=value.detail;}}
  });
  const get = (id)=>{const item=ids.get(id);assert.ok(item,`missing ${id}`);return item;};
  const parent = (id,predicate)=>{for(let node=get(id).parent;node;node=node.parent)if(predicate(node))return node;return null;};
  return {get,parent,ids,root,events,api:window.RegicideSidebar};
}

const cases=[],test=(name,run)=>cases.push({name,run});
test("only the selected tool panel occupies sidebar space and switching never changes game controls",()=>{
  const ui=browser();assert.equal(ui.api.selected("live"),"game");assert.equal(ui.api.selected("analysis"),"analysis");
  ui.get("live-sidebar-tab-opening").click();
  for(const tab of ["game","opening","records"]){assert.equal(ui.get(`live-sidebar-panel-${tab}`).hidden,tab!=="opening");assert.equal(ui.get(`live-sidebar-tab-${tab}`).attributes["aria-selected"],String(tab==="opening"));}
  assert.equal(ui.events.at(-1).type,"regicide-sidebar-change");assert.equal(ui.events.at(-1).detail.tab,"opening");
  const count=ui.events.length;assert.equal(ui.api.select("live","missing"),false);assert.equal(ui.events.length,count);
  assert.equal(ui.get("new-game").listeners.click,undefined);assert.equal(ui.get("resign").listeners.click,undefined);
});
test("keyboard tabs support arrows home end and preserve each panel scroll",()=>{
  const ui=browser();ui.get("live-sidebar-panel-game").scrollTop=160;
  let prevented=false;ui.get("live-sidebar-tab-game").dispatch("keydown",{key:"ArrowLeft",preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(ui.api.selected("live"),"records");assert.equal(ui.get("live-sidebar-tab-records").focused,true);
  assert.equal(ui.get("live-sidebar-tab-game").tabIndex,-1);assert.equal(ui.get("live-sidebar-tab-records").tabIndex,0);
  ui.get("live-sidebar-tab-records").dispatch("keydown",{key:"Home",preventDefault(){}});
  assert.equal(ui.api.selected("live"),"game");assert.equal(ui.get("live-sidebar-panel-game").scrollTop,160);
  ui.get("analysis-sidebar-tab-analysis").dispatch("keydown",{key:"End",preventDefault(){}});
  assert.equal(ui.api.selected("analysis"),"opening");assert.equal(ui.get("analysis-sidebar-panel-analysis").hidden,true);
});
test("frequent actions remain outside scrollable panels while clocks errors and promotion stay independent",()=>{
  const ui=browser();
  for(const id of ["undo","hint-toggle","live-analyze-current","analysis-start","analysis-apply","analysis-undo","analysis-edit"]){
    assert.equal(ui.parent(id,(node)=>node.attributes.role==="tabpanel"),null,`${id} must stay above panel scrolling`);
    assert.ok(ui.parent(id,(node)=>String(node.attributes.class||"").split(/\s+/).includes("sidebar-header")));
  }
  for(const id of ["human-clock","opponent-clock","error-panel","status-text","promotion-dialog","analysis-promotion"])
    assert.equal(ui.parent(id,(node)=>node.attributes.role==="tabpanel"),null);
  assert.equal(ui.parent("new-game",(node)=>node.tag==="form").attributes.id,"new-game-form");
  assert.equal(ui.parent("human-side",(node)=>node.tag==="form").attributes.id,"new-game-form");
  assert.equal(ui.parent("resign",(node)=>node.attributes.class==="sidebar-quick-actions"),null);
});
test("opening content and records have separate panels; coaching setup precedes long coaching content",()=>{
  const ui=browser();
  for(const id of ["opening-guide","live-opening-explorer"]) assert.equal(ui.parent(id,(node)=>node.attributes.role==="tabpanel").attributes.id,"live-sidebar-panel-opening");
  assert.equal(ui.parent("move-list",(node)=>node.attributes.role==="tabpanel").attributes.id,"live-sidebar-panel-records");
  const coachingParent=ui.get("coaching-setup").parent;
  assert.ok(coachingParent.children.indexOf(ui.get("coaching-setup"))<coachingParent.children.indexOf(ui.get("coach-card")));
  assert.equal(ui.get("analysis-fen-details").attributes.open,undefined);
});
test("archive shortcut uses the existing archive entry without affecting sidebar selection",()=>{
  const ui=browser();let opened=0;ui.get("archive-mode-button").addEventListener("click",()=>{opened++;});
  ui.api.select("live","records");ui.get("sidebar-archive-open").click();
  assert.equal(opened,1);assert.equal(ui.api.selected("live"),"records");
});
for(const item of cases){item.run();console.log(`PASS ${item.name}`);}console.log(`${cases.length} sidebar web checks passed`);
