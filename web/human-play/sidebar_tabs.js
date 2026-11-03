"use strict";

// Tool selection has no effect on the game, clock or analysis position.
(() => {
  const groups = new Map();
  const get = (id) => document.getElementById(id);
  function announce(name, tab) {
    window.dispatchEvent(new CustomEvent("regicide-sidebar-change", {detail:{name, tab}}));
  }
  function select(name, tab, focus = false) {
    const group = groups.get(name);
    if (!group || !group.items.some((item) => item.tab === tab)) return false;
    group.selected = tab;
    for (const item of group.items) {
      const active = item.tab === tab;
      item.button.setAttribute("aria-selected", String(active)); item.button.tabIndex = active ? 0 : -1;
      item.panel.hidden = !active;
      if (active && focus) item.button.focus();
    }
    announce(name, tab); return true;
  }
  for (const [name, tabs] of [["live", ["game", "opening", "records"]], ["analysis", ["analysis", "opening"]]]) {
    const items = tabs.map((tab) => ({tab, button:get(`${name}-sidebar-tab-${tab}`), panel:get(`${name}-sidebar-panel-${tab}`)})).filter((item) => item.button && item.panel);
    if (!items.length) continue;
    groups.set(name, {items, selected:tabs[0]});
    for (const item of items) {
      item.button.addEventListener("click", () => { select(name, item.tab); });
      item.button.addEventListener("keydown", (event) => {
        const index = items.indexOf(item);
        let next = null;
        if (event.key === "ArrowRight") next = items[(index + 1) % items.length];
        if (event.key === "ArrowLeft") next = items[(index + items.length - 1) % items.length];
        if (event.key === "Home") next = items[0];
        if (event.key === "End") next = items.at(-1);
        if (next) { event.preventDefault(); select(name, next.tab, true); }
      });
    }
  }
  window.RegicideSidebar = {select, selected:(name) => groups.get(name)?.selected || null};
  get("sidebar-archive-open")?.addEventListener("click", () => { get("archive-mode-button")?.click(); });
})();
