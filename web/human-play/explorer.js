"use strict";

// Statistics are external observations. Regicide remains the move evaluator.
(() => {
  const endpoint = "/api/opening-explorer";
  const token = document.querySelector('meta[name="regicide-token"]').content;
  const components = new Map();
  let connectionBootstrap = null;
  const initialRatings = [1600, 1800, 2000, 2200, 2500];
  const initialSpeeds = ["rapid", "classical"];
  const position = (fen) => String(fen || "").trim().split(/\s+/).slice(0, 4).join(" ");
  const sameList = (a, b) => JSON.stringify([...(a || [])].sort()) === JSON.stringify([...(b || [])].sort());
  const number = (value) => Number.isFinite(Number(value)) ? Number(value).toLocaleString("ko-KR") : "—";
  const percent = (value) => Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)}%` : "—";
  const text = (value, limit = 150) => typeof value === "string" ? value.slice(0, limit) : "";

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  async function request(payload) {
    const response = await fetch(endpoint, {
      method: payload === undefined ? "GET" : "POST",
      headers: {"X-Regicide-Token": token, ...(payload === undefined ? {} : {"Content-Type": "application/json"})},
      ...(payload === undefined ? {} : {body: JSON.stringify(payload)}), cache: "no-store"
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "통계 조회에 실패했습니다. 다시 시도해 주세요.");
    return data.explorer || data;
  }

  function gameLink(game) {
    if (!game || !/^[A-Za-z0-9]{8}$/.test(game.id || "")) return null;
    try {
      const url = new URL(game.url);
      if (url.protocol !== "https:" || url.hostname !== "lichess.org" ||
          ![`/${game.id}`, `/import/master/${game.id}/white`].includes(url.pathname) || url.search || url.hash) return null;
      return url.href;
    } catch (_) { return null; }
  }

  class Explorer {
    constructor(host, name) {
      this.host = host; this.name = name; this.enabled = true; this.active = false; this.fen = "";
      this.epoch = 0; this.timer = null; this.state = null; this.signature = ""; this.requestKey = null;
      this.section = "stats"; this.pending = false;
      this.connectionChecked = false; this.connectionEpoch = 0;
      this.connectionRetryAt = 0;
      this.connectionConfigured = false;
      this.mount();
    }

    mount() {
      this.host.className = "card opening-explorer";
      this.host.setAttribute("aria-label", "통계형 오프닝 탐색기");
      const heading = element("div", "section-heading");
      heading.append(element("h2", "", "오프닝 탐색기"));
      this.host.append(heading, element("p", "help-note", "현재 위치의 실제 대국 통계를 자동으로 조회합니다."));
      this.body = element("div", "explorer-body");
      const tabs = element("div", "explorer-tabs"); tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "오프닝 탐색기 도구");
      this.sections = new Map(); this.sectionButtons = new Map();
      for (const [key, title] of [["stats", "통계"], ["games", "참고 기보"], ["connection", "연결 설정"]]) {
        const button = element("button", "", title); button.type = "button"; button.id = `${this.name}-explorer-tab-${key}`;
        button.setAttribute("role", "tab"); button.setAttribute("aria-controls", `${this.name}-explorer-panel-${key}`);
        const panel = element("div", `explorer-section explorer-section-${key}`); panel.id = `${this.name}-explorer-panel-${key}`;
        panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", button.id);
        this.sections.set(key, panel); this.sectionButtons.set(key, button); tabs.append(button);
        button.addEventListener("click", () => { this.selectSection(key); });
        button.addEventListener("keydown", (event) => {
          const keys = [...this.sections.keys()], index = keys.indexOf(key);
          const next = event.key === "ArrowRight" ? keys[(index + 1) % keys.length] : event.key === "ArrowLeft" ? keys[(index + keys.length - 1) % keys.length] : event.key === "Home" ? keys[0] : event.key === "End" ? keys.at(-1) : null;
          if (next) { event.preventDefault(); this.selectSection(next); this.sectionButtons.get(next).focus(); }
        });
      }
      const sourceLabel = element("label", "field-label", "대국 자료"); sourceLabel.htmlFor = `${this.name}-explorer-source`;
      this.source = element("select"); this.source.id = sourceLabel.htmlFor;
      for (const [value, label] of [["masters", "마스터 대국"], ["lichess", "리체스 일반 대국"]]) {
        const option = element("option", "", label); option.value = value; this.source.append(option);
      }
      this.source.value = "masters";
      this.filters = element("div", "explorer-filters"); this.filters.hidden = true;
      const ratingLabel = element("label", "field-label", "레이팅 구간"); ratingLabel.htmlFor = `${this.name}-explorer-ratings`;
      this.ratings = element("select"); this.ratings.id = ratingLabel.htmlFor;
      for (const [value, label] of [["all", "전체"], ["1600", "1600 이상"], ["1800", "1800 이상"], ["2000", "2000 이상"], ["2200", "2200 이상"]]) {
        const option = element("option", "", label); option.value = value; this.ratings.append(option);
      }
      this.ratings.value = "1600";
      const speedLabel = element("label", "field-label", "시간제"); speedLabel.htmlFor = `${this.name}-explorer-speeds`;
      this.speeds = element("select"); this.speeds.id = speedLabel.htmlFor;
      for (const [value, label] of [["rapid,classical", "래피드 · 클래시컬"], ["rapid", "래피드"], ["blitz", "블리츠"], ["classical", "클래시컬"], ["bullet,blitz,rapid,classical", "불릿 · 블리츠 · 래피드 · 클래시컬"]]) {
        const option = element("option", "", label); option.value = value; this.speeds.append(option);
      }
      this.speeds.value = initialSpeeds.join(",");
      const ratingField = element("div"), speedField = element("div");
      ratingField.append(ratingLabel, this.ratings); speedField.append(speedLabel, this.speeds); this.filters.append(ratingField, speedField);
      this.status = element("p", "explorer-status"); this.status.setAttribute("role", "status");
      this.refresh = element("button", "secondary-button explorer-refresh", "↻"); this.refresh.type = "button";
      this.refresh.setAttribute("aria-label", "현재 위치 다시 조회"); this.refresh.title = "현재 위치 다시 조회";
      this.summary = element("p", "help-note");
      this.results = element("div", "explorer-results");
      this.games = element("div", "explorer-games");
      const toolbar = element("div", "explorer-toolbar"), sourceField = element("div"); sourceField.append(sourceLabel, this.source); toolbar.append(sourceField, this.refresh);
      this.sections.get("stats").append(toolbar, this.filters, this.summary, this.results,
        element("p", "help-note explorer-footnote", "백승·무·흑승은 실전 결과이며 엔진 평가가 아닙니다. 표본 수를 함께 확인하세요."));
      this.sections.get("games").append(this.games);
      this.body.append(tabs, this.status, ...this.sections.values());
      this.mountAuthentication();
      this.host.append(this.body);
      this.selectSection("stats", false);
      for (const control of [this.source, this.ratings, this.speeds]) control.addEventListener("change", () => {
        this.filters.hidden = this.source.value !== "lichess"; this.signature = ""; this.sync();
      });
      this.refresh.addEventListener("click", () => { this.signature = ""; this.sync(); });
    }

    mountAuthentication() {
      const details = element("div", "explorer-auth");
      this.tokenForm = element("div", "explorer-token-form");
      this.changeToken = element("button", "secondary-button", "연결키 변경"); this.changeToken.type = "button"; this.changeToken.hidden = true;
      this.changeToken.addEventListener("click", () => { this.tokenForm.hidden = false; this.changeToken.hidden = true; });
      const link = element("a", "", "리체스 연결키 만들기 ↗");
      link.href = "https://lichess.org/account/oauth/token/create"; link.target = "_blank"; link.rel = "noopener noreferrer";
      const note = element("p", "help-note", "처음 한 번 연결하면 다음 실행부터 저장된 연결키를 사용합니다. 리체스 화면에서는 연결키를 ‘토큰’이라고 부릅니다.");
      const steps = element("ol", "explorer-auth-steps");
      steps.append(element("li", "", "위 링크를 열고 리체스에 로그인하세요."),
        element("li", "", "설명에는 Regicide를 적고, 권한 항목은 아무것도 체크하지 않은 채 생성하세요."),
        element("li", "", "만들어진 긴 문자열을 복사해 아래에 붙여 넣고 연결하기를 누르세요."));
      const privacy = element("p", "help-note", "연결키는 채팅이나 기보에 적지 마세요.");
      const label = element("label", "field-label", "리체스 연결키(토큰)"); label.htmlFor = `${this.name}-explorer-token`;
      this.secret = element("input", "analysis-input"); this.secret.type = "password"; this.secret.id = label.htmlFor;
      this.secret.autocomplete = "new-password"; this.secret.spellcheck = false; this.secret.maxLength = 512;
      const rememberLabel = element("label", "explorer-toggle");
      this.remember = element("input"); this.remember.type = "checkbox"; this.remember.checked = true;
      rememberLabel.append(this.remember, element("span", "", "이 컴퓨터에 안전하게 저장"));
      this.save = element("button", "secondary-button", "연결하기"); this.save.type = "button";
      this.connection = element("p", "help-note"); this.connection.setAttribute("role", "status");
      this.tokenForm.append(link, note, steps, privacy, label, this.secret, rememberLabel, this.save);
      details.append(this.connection, this.changeToken, this.tokenForm);
      this.sections.get("connection").append(details);
      this.save.addEventListener("click", () => { void this.saveToken(); });
    }

    invalidate() {
      this.epoch++; if (this.timer !== null) clearTimeout(this.timer); this.timer = null;
      this.pending = false;
      this.requestKey = null; this.state = null; this.results.replaceChildren(); this.games.replaceChildren(); this.summary.textContent = "";
    }

    pause() {
      this.epoch++; if (this.timer !== null) clearTimeout(this.timer); this.timer = null; this.pending = false;
    }

    selectSection(section, sync = true) {
      if (!this.sections.has(section)) return;
      this.section = section;
      for (const [key, panel] of this.sections) {
        const selected = key === section; panel.hidden = !selected;
        this.sectionButtons.get(key).setAttribute("aria-selected", String(selected)); this.sectionButtons.get(key).tabIndex = selected ? 0 : -1;
      }
      if (sync) this.sync();
    }

    update(value) {
      const changed = this.fen !== value.fen || this.active !== Boolean(value.active);
      this.fen = value.fen || ""; this.active = Boolean(value.active);
      if (this.fen && !this.connectionChecked && Date.now() >= this.connectionRetryAt) void this.restoreConnection();
      if (changed) this.sync();
    }

    async restoreConnection() {
      this.connectionChecked = true;
      const epoch = this.connectionEpoch;
      // Shared localhost metadata request; this never sends a position to Lichess.
      if (!connectionBootstrap) connectionBootstrap = request();
      const pending = connectionBootstrap;
      try {
        const value = await pending;
        if (epoch !== this.connectionEpoch) return;
        this.updateConnection(value);

      } catch (_) {
        if (epoch !== this.connectionEpoch) return;
        if (connectionBootstrap === pending) connectionBootstrap = null;
        this.connectionChecked = false; this.connectionRetryAt = Date.now() + 5000;
        // No retry timer: a later board/view update may retry after backoff.
      }
    }

    updateConnection(value) {
      const configured = value.configured === true;
      let message = text(value.connection?.message, 400);
      if (!message && configured && (!this.connectionConfigured || !this.connection.textContent))
        message = "연결키가 설정되어 있습니다. 다시 입력할 필요가 없습니다.";
      if (value.status === "auth_required") {
        message = text(value.message, 400) || "리체스 연결이 필요합니다. 연결키를 입력해 주세요.";
        this.tokenForm.hidden = false; this.changeToken.hidden = true;
      } else {
        this.tokenForm.hidden = configured; this.changeToken.hidden = !configured;
      }
      this.connectionConfigured = configured && value.status !== "auth_required";
      if (message) this.connection.textContent = message;
    }

    query() {
      const minimum = this.ratings.value;
      const ratings = minimum === "all" ? [0, 1000, 1200, 1400, ...initialRatings] : initialRatings.filter((rating) => rating >= Number(minimum));
      return {action:"query", fen:this.fen, source:this.source.value, ratings, speeds:this.speeds.value.split(",")};
    }

    sync() {
      if (!this.enabled) {
        this.invalidate(); this.refresh.disabled = true; return;
      }
      if (!this.active || !this.fen || this.section === "connection") {
        this.pause();
        if (this.section === "connection") this.status.textContent = "연결 설정 · 통계 탭으로 돌아가면 현재 위치를 조회합니다.";
        else if (!this.state) this.status.textContent = "보드가 활성화되면 현재 위치의 통계를 조회합니다.";
        this.refresh.disabled = true; return;
      }
      this.refresh.disabled = false;
      const payload = this.query(), signature = JSON.stringify(payload);
      if (this.signature === signature && (this.pending || this.state && this.state.status !== "loading")) {
        if (this.state && !this.pending) this.render(this.state);
        return;
      }
      this.signature = signature; this.invalidate();
      this.status.textContent = "리체스에서 현재 위치의 통계를 조회합니다…";
      this.pending = true;
      const ticket = this.epoch;
      void this.load(payload, ticket);
    }

    matches(value, payload, ticket) {
      return this.epoch === ticket && this.enabled && this.active && value && position(value.fen) === position(payload.fen) &&
        value.source === payload.source && (payload.source === "masters" || (sameList(value.ratings, payload.ratings) && sameList(value.speeds, payload.speeds))) &&
        (!this.requestKey || value.request_key === this.requestKey);
    }

    async load(payload, ticket, polling = false) {
      try {
        const value = await request(polling ? undefined : payload);
        if (!this.matches(value, payload, ticket)) {
          if (ticket === this.epoch && this.enabled && this.active) {
            this.state = null; this.results.replaceChildren(); this.games.replaceChildren(); this.summary.textContent = "";
            this.pending = false;
            this.status.textContent = "다른 위치의 조회로 바뀌었습니다. 현재 위치 다시 조회를 눌러 주세요.";
          }
          return;
        }
        this.requestKey = value.request_key || null; this.state = value; this.pending = value.status === "loading"; this.render(value);
        if (value.status === "loading") this.timer = setTimeout(() => { this.timer = null; void this.load(payload, ticket, true); }, 500);
      } catch (_) {
        if (ticket !== this.epoch || !this.enabled || !this.active) return;
        this.pending = false;
        this.results.replaceChildren(); this.games.replaceChildren(); this.summary.textContent = "";
        this.status.textContent = "통계 서비스와 연결하지 못했습니다. 연결을 확인하고 다시 조회해 주세요.";
      }
    }

    async saveToken() {
      const secret = this.secret.value.trim(); this.secret.value = "";
      if (!secret || this.save.disabled) { this.connection.textContent = "만든 연결키를 아래 입력칸에 붙여 넣으세요."; return; }
      for (const component of components.values()) component.connectionEpoch++;
      this.save.disabled = true; this.connection.textContent = "연결 설정을 저장합니다…";
      try {
        const value = await request({action:"token", token:secret, remember:this.remember.checked});
        connectionBootstrap = Promise.resolve(value);
        const message = text(value.message, 400);
        for (const component of components.values()) {
          component.updateConnection(value);
          if (component === this) this.connection.textContent = value.configured ? `${message || "연결키를 설정했습니다."} 실제 통계 조회로 연결 상태를 확인합니다.` : message || "연결 설정을 확인해 주세요.";
          component.signature = "";
          if (component === this && value.configured) component.selectSection("stats");
          else component.sync();
        }
      } catch (_) { this.connection.textContent = "연결키를 저장하지 못했습니다. 연결 설정을 확인해 주세요."; }
      finally { this.save.disabled = false; }
    }

    render(value) {
      this.updateConnection(value);
      this.results.replaceChildren(); this.games.replaceChildren(); this.summary.textContent = "";
      const messages = {idle:"현재 위치를 조회하세요.", loading:"리체스 통계를 불러오고 있습니다…", auth_required:"리체스 연결이 필요합니다. 연결 설정 탭에서 연결키를 입력하세요.",
        rate_limited:`리체스 조회 한도에 도달했습니다. ${Math.max(0, Number(value.retry_after) || 0)}초 뒤 다시 조회해 주세요.`, error:"통계를 읽지 못했습니다. 잠시 후 다시 조회해 주세요."};
      this.status.textContent = value.status === "auth_required" ? text(value.message, 400) || messages.auth_required : messages[value.status] || text(value.message) || "통계를 준비하고 있습니다.";
      if (value.status !== "ready") return;
      const source = value.source === "masters" ? "리체스 마스터 대국" : "리체스 일반 대국";
      const fetched = value.fetched_at ? new Date(value.fetched_at) : null;
      const date = fetched && Number.isFinite(fetched.getTime()) ? fetched.toLocaleString("ko-KR") : "조회 시각 확인 불가";
      this.status.textContent = Number(value.total) ? `${source} · ${number(value.total)}판` : "이 위치와 조건에 해당하는 대국이 없습니다.";
      this.summary.textContent = `${date}${value.cached ? " · 저장된 통계" : ""} · 백승 ${number(value.white)} / 무 ${number(value.draws)} / 흑승 ${number(value.black)}`;
      if (Number(value.total) > 0) this.results.append(element("p", "help-note", "다음 수 · 대국 수 / 선택률 · 백승 / 무 / 흑승"));
      for (const move of (value.moves || []).slice(0, 20)) {
        if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move.uci || "") || typeof move.fen_after !== "string") continue;
        const row = element("button", "explorer-move explorer-explore"); row.type = "button";
        const heading = element("span", "explorer-move-heading");
        heading.append(element("strong", "", text(move.san, 20) || move.uci), element("span", "", `${number(move.count)}판 · ${percent(move.play_pct)}`), element("span", "explorer-row-arrow", "↗"));
        row.append(heading, this.resultBar(move));
        row.setAttribute("aria-label", `${text(move.san, 20) || move.uci} 이후 위치를 분석 보드에서 탐색`);
        row.title = "이 수를 둔 위치를 분석 보드에서 탐색";
        const fen = move.fen_after, ticket = this.epoch, requestKey = this.requestKey;
        row.addEventListener("click", () => {
          if (ticket !== this.epoch || !this.active || !this.enabled || this.state?.request_key !== requestKey) return;
          const analysis = components.get("analysis");
          if (analysis) {
            analysis.enabled = true; analysis.body.hidden = false;
            analysis.source.value = this.source.value; analysis.ratings.value = this.ratings.value; analysis.speeds.value = this.speeds.value;
            analysis.filters.hidden = analysis.source.value !== "lichess"; analysis.signature = "";
            analysis.selectSection("stats", false);
          }
          window.RegicideSidebar?.select("analysis", "analysis");
          window.dispatchEvent(new CustomEvent("regicide-explorer-analyze", {detail:{fen}}));
        });
        this.results.append(row);
      }
      const games = (value.games || []).slice(0, 5).filter((game) => gameLink(game));
      if (games.length) this.games.append(element("h3", "", "참고 기보"));
      else this.games.append(element("p", "help-note", "현재 위치에서 제공되는 참고 기보가 없습니다."));
      for (const game of games) {
        const year = Number.isInteger(Number(game.year)) && Number(game.year) >= 1000 && Number(game.year) <= 9999 ? String(Number(game.year)) : "";
        const link = element("a", "explorer-game", `${text(game.white, 50)} — ${text(game.black, 50)}${year ? ` · ${year}` : ""}`);
        link.href = gameLink(game); link.target = "_blank"; link.rel = "noopener noreferrer"; this.games.append(link);
      }
    }

    resultBar(value) {
      const bar = element("span", "explorer-result-bar");
      const label = `백승 ${percent(value.white_pct)} / 무 ${percent(value.draw_pct)} / 흑승 ${percent(value.black_pct)}`;
      bar.setAttribute("role", "img"); bar.setAttribute("aria-label", label); bar.title = label;
      for (const [field, className] of [["white_pct", "white"], ["draw_pct", "draw"], ["black_pct", "black"]]) {
        const share = Math.max(0, Math.min(100, Number(value[field]) || 0));
        const segment = element("span", `explorer-result-${className}`, share >= 12 ? `${share.toFixed(0)}%` : "");
        segment.style.width = `${share}%`; segment.setAttribute("aria-hidden", "true"); bar.append(segment);
      }
      return bar;
    }
  }

  window.RegicideExplorer = {
    update(name, value) { components.get(name)?.update(value); },
    // Export pure validation for headless checks without contacting Lichess.
    gameLink, position
  };
  for (const name of ["live", "analysis"]) {
    const host = document.getElementById(`${name}-opening-explorer`);
    if (host) components.set(name, new Explorer(host, name));
  }
  window.addEventListener("pagehide", () => { for (const component of components.values()) { component.enabled = false; component.invalidate(); component.secret.value = ""; } });
})();
