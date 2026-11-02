"use strict";

(() => {
  const token = document.querySelector('meta[name="regicide-token"]').content;
  const byId = (id) => document.getElementById(id);
  const boardElement = byId("board");
  const dialog = byId("promotion-dialog");
  const pieceSymbols = {K:"♔",Q:"♕",R:"♖",B:"♗",N:"♘",P:"♙",k:"♚",q:"♛",r:"♜",b:"♝",n:"♞",p:"♟"};
  const pieceNames = {k:"왕",q:"퀸",r:"룩",b:"비숍",n:"나이트",p:"폰"};
  const colorName = (color) => color === "white" ? "백" : "흑";
  const hintColors = ["#ef5350", "#ff9800", "#fdd835"];
  let state = null;
  let selected = null;
  let busy = true;
  let engineThinking = false;
  let timeSaving = false;
  let timeNotice = "";
  let closed = false;
  let errorMessage = "";
  let pendingPromotion = [];
  let renderedMoveCount = -1;
  let evaluation = null;
  let evaluationPollInFlight = false;
  let hint = null;
  let hintEnabled = false;
  let hintPreferenceLoaded = false;
  let hintEpoch = 0;
  let hintPollInFlight = false;
  let hintToggleInFlight = false;
  let hintToggleDirty = false;
  let renderedOpeningKey = null;
  let analysisMode = false;
  let coachingMode = false;

  function clearHint() {
    hintEpoch++;
    hint = null;
  }

  function showError(message) {
    errorMessage = message || "";
    byId("error-text").textContent = errorMessage;
    byId("error-panel").hidden = !errorMessage;
  }

  async function request(path, payload, signal, preserveSelection = false) {
    let response;
    try {
      response = await fetch(path, {
        method: payload === undefined ? "GET" : "POST",
        headers: {"X-Regicide-Token": token, ...(payload === undefined ? {} : {"Content-Type": "application/json"})},
        ...(payload === undefined ? {} : {body: JSON.stringify(payload)}),
        cache: "no-store",
        ...(signal ? {signal} : {})
      });
    } catch (_) {
      throw new Error("대국 프로그램과 연결이 끊겼습니다. 실행 창을 확인하고 페이지를 새로 열어주세요.");
    }
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error("대국 상태를 읽지 못했습니다. 페이지를 새로 열어주세요."); }
    if (!response.ok) {
      if (data.state) applyState(data.state, preserveSelection);
      throw new Error(data.error || "요청을 처리하지 못했습니다. 다시 시도해주세요.");
    }
    return data;
  }

  function applyState(next, preserveSelection = false) {
    // A settings response may arrive after the engine response. Never move
    // the board or its independently versioned time setting backwards.
    if (state && next.revision < state.revision) {
      if (state.game_id === next.game_id &&
          Number(next.engine.settings_revision || 0) > Number(state.engine.settings_revision || 0)) {
        state.engine.movetime_ms = next.engine.movetime_ms;
        state.engine.settings_revision = next.engine.settings_revision;
        state.engine.extra_budget_ms = next.engine.extra_budget_ms;
        state.engine.extra_budget_schedule = next.engine.extra_budget_schedule;
        render();
      }
      return;
    }
    if (state?.game_id === next.game_id &&
        Number(next.engine.settings_revision || 0) < Number(state.engine.settings_revision || 0)) {
      next.engine.movetime_ms = state.engine.movetime_ms;
      next.engine.settings_revision = state.engine.settings_revision;
      next.engine.extra_budget_ms = state.engine.extra_budget_ms;
      next.engine.extra_budget_schedule = state.engine.extra_budget_schedule;
    }
    const unchangedPosition = state?.game_id === next.game_id && state.revision === next.revision;
    if (!samePosition(state, next)) {
      evaluation = null;
      clearHint();
    }
    if (!hintPreferenceLoaded) {
      hintEnabled = next.hint_enabled === true;
      hintPreferenceLoaded = true;
    }
    state = next;
    if (!hintPositionAvailable()) clearHint();
    if (samePosition(state, next.evaluation)) evaluation = next.evaluation;
    if (!preserveSelection || !unchangedPosition) selected = null;
    render();
  }

  function samePosition(first, second) {
    return Boolean(first && second && first.game_id != null && first.revision != null && first.fen &&
      first.game_id === second.game_id &&
      first.revision === second.revision && first.fen === second.fen);
  }

  function openingText(value, limit = 250) {
    return typeof value === "string" ? value.trim().slice(0, limit) : "";
  }

  function openingLabel(item) {
    const name = openingText(item?.name);
    const eco = /^[A-E][0-9]{2}$/.test(item?.eco) ? item.eco : "";
    return name ? `${name}${eco ? ` · ${eco}` : ""}` : "";
  }

  function renderOpeningSide(id, heading, item, unavailable, note) {
    const status = openingText(item?.status);
    let name = openingText(item?.name);
    let detail = openingText(item?.detail, 500);
    if (unavailable || status === "unavailable") {
      name = "";
      detail = "안내 없음";
    } else if (!["confirmed", "position", "last_known", "pending", "unknown"].includes(status)) {
      name = "";
      detail = "확인 정보 없음";
    }
    const marker = status === "last_known" ? "마지막 확인: " : status === "position" ? "현재 배치: " : "";
    const label = `${heading} · ${name ? marker + name : detail || "확인 정보 없음"}`;
    byId(id).textContent = label;
    byId(id).title = [label, name && detail, openingText(note, 500)].filter(Boolean).join("\n");
  }

  function openingLink(url, label) {
    if (typeof URL !== "function" || typeof url !== "string") return null;
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== "https:" || parsed.username || parsed.password) return null;
      const link = document.createElement("a");
      link.textContent = label;
      link.setAttribute("href", parsed.href);
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      return link;
    } catch (_) { return null; }
  }

  function renderOpeningGuide() {
    const guide = state?.opening_guide;
    const key = JSON.stringify([state?.game_id, state?.revision, state?.fen, guide || null]);
    // Board selection and time-setting renders must not collapse a reader's
    // open previews. A new position or catalog payload rebuilds the examples.
    if (key === renderedOpeningKey) return;
    renderedOpeningKey = key;
    const statuses = {in_catalog:"등록 수순 안", beyond_catalog:"등록 수순 이후",
      unknown:"등록 이름 없음", unavailable:"안내 없음"};
    const status = guide && Object.hasOwn(statuses, guide.status) ? guide.status : "unavailable";
    byId("opening-badge").textContent = !state ? "준비 중" : statuses[status];
    renderOpeningSide("opening-white", "백의 오프닝", guide?.sides?.white, status === "unavailable", guide?.sides?.note);
    renderOpeningSide("opening-black", "흑의 대응", guide?.sides?.black, status === "unavailable", guide?.sides?.note);
    const matches = Array.isArray(guide?.current_matches) ? guide.current_matches.filter(openingLabel) : [];
    const matched = openingLabel(guide?.matched_opening);
    byId("opening-name").textContent = !state ? "오프닝 자료를 기다리고 있어요." :
      matches.length ? openingLabel(matches[0]) : matched ? `마지막 확인: ${matched}` :
      status === "in_catalog" ? "이어서 오프닝을 살펴보세요." :
      status === "unavailable" ? "오프닝 안내를 불러오지 못했습니다." : "현재 위치의 등록 이름이 없습니다.";
    let description = !state ? "수를 두면 등록된 이름과 이어지는 수순을 확인할 수 있습니다." :
      status === "unavailable" ? "오프닝 자료를 읽지 못했습니다. 대국은 계속할 수 있습니다." :
      status === "beyond_catalog" ? "등록된 수순을 벗어났습니다. 마지막으로 확인한 이름을 표시합니다." :
      status === "unknown" ? "이 자료에 일치하는 이름을 찾지 못했습니다. 계속 두어도 됩니다." :
      matches[0]?.match_type === "transposition" ? "다른 수순으로 같은 오프닝 위치에 도착했습니다." :
      matches[0]?.match_type === "position" ? "현재 배치에 일치하는 등록 이름입니다." :
      matches.length ? "현재 수순에 일치하는 등록 이름입니다." : "다음 수를 펼쳐 이어지는 등록 수순을 확인하세요.";
    if (guide?.history_scope === "custom" && status !== "unavailable")
      description += " 직접 불러온 위치는 확인 가능한 배치와 이후 수순을 기준으로 안내합니다.";
    byId("opening-status").textContent = description;
    const aliases = matches.slice(1).map(openingLabel);
    byId("opening-aliases").hidden = aliases.length === 0 && !guide?.current_matches_truncated;
    byId("opening-aliases").textContent = `${aliases.length ? `함께 등록된 이름: ${aliases.join(" / ")}` : ""}${guide?.current_matches_truncated ? " · 다른 등록 이름도 있습니다." : ""}`;

    const fragment = document.createDocumentFragment();
    const branches = status === "unavailable" || !Array.isArray(guide?.continuations) ? [] : guide.continuations;
    let visible = 0;
    for (const branch of branches) {
      if (!branch || !/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(branch.uci) || !openingText(branch.san)) continue;
      // The server validates every continuation. Where the live board also
      // exposes human legal moves, reject a stale/foreign continuation locally.
      // An opponent's turn intentionally exposes an empty human move list.
      if (state?.human_to_move && Array.isArray(state.legal_moves) && !state.legal_moves.includes(branch.uci)) continue;
      const details = document.createElement("details");
      details.className = "opening-branch";
      details.dataset.uci = branch.uci;
      const summary = document.createElement("summary");
      const san = document.createElement("span");
      san.className = "opening-next-san";
      san.textContent = openingText(branch.san);
      const immediate = Array.isArray(branch.immediate_openings) ? branch.immediate_openings.map(openingLabel).filter(Boolean) : [];
      const label = document.createElement("span");
      label.textContent = immediate.length ? immediate.join(" / ") : "이어지는 이름 살펴보기";
      const count = document.createElement("span");
      count.className = "opening-branch-count";
      count.textContent = Number.isInteger(branch.reachable_total) && branch.reachable_total >= 0 ?
        `도달 가능한 등록 이름 ${branch.reachable_total}개 · 수순 펼치기` : "등록 수순 펼치기";
      summary.append(san, label, count);
      const lines = document.createElement("div");
      lines.className = "opening-lines";
      const reachable = Array.isArray(branch.reachable_openings) ? branch.reachable_openings : [];
      let lineCount = 0;
      for (const line of reachable) {
        const name = openingLabel(line);
        const sequence = openingText(line?.remaining_san, 2500);
        if (!name || !sequence) continue;
        const item = document.createElement("div");
        item.className = "opening-line";
        const heading = document.createElement("p");
        heading.className = "opening-line-name";
        heading.textContent = name;
        const distance = document.createElement("p");
        distance.className = "opening-line-distance";
        distance.textContent = Number.isInteger(line.distance_plies) && line.distance_plies > 0 ?
          `이 수를 포함해 ${line.distance_plies}번 이동 뒤의 등록 이름` : "현재 위치에서 이어지는 등록 수순";
        const preview = document.createElement("p");
        preview.className = "opening-line-san";
        preview.textContent = sequence;
        item.append(heading, distance, preview);
        lines.append(item);
        lineCount++;
      }
      if (lineCount === 0 || branch.reachable_truncated || branch.immediate_truncated) {
        const note = document.createElement("p");
        note.className = "opening-status";
        note.textContent = lineCount === 0 ? "더 이어지는 등록 수순을 표시할 수 없습니다." : "등록 이름과 수순 중 일부를 표시합니다.";
        lines.append(note);
      }
      details.append(summary, lines);
      fragment.append(details);
      visible++;
    }
    byId("opening-branches").replaceChildren(fragment);
    byId("opening-empty").hidden = !state || status === "unavailable" || visible > 0;
    byId("opening-empty").textContent = "현재 위치에서 이어지는 등록 수순이 없습니다. 오프닝 자료의 범위는 한정되어 있습니다.";
    byId("opening-coverage").hidden = !guide?.continuations_truncated;
    byId("opening-coverage").textContent = Number.isInteger(guide?.continuations_total) ?
      `등록된 다음 수 ${guide.continuations_total}개 중 ${visible}개를 표시합니다.` : "등록된 다음 수 중 일부를 표시합니다.";
    byId("opening-disclaimer").textContent = openingText(guide?.disclaimer, 500) ||
      "공개된 오프닝 수순의 분류입니다. 승률이나 엔진의 추천 수가 아닙니다.";
    const source = guide?.source;
    const sourceItems = [];
    const repository = openingLink(source?.repository_url, "오프닝 원자료");
    const license = openingLink(source?.license_url, "이용 조건");
    if (repository) sourceItems.push(repository);
    if (license) sourceItems.push(license);
    const attribution = document.createElement("p");
    attribution.textContent = openingText(source?.attribution, 500) || "원자료의 출처를 현재 표시할 수 없습니다.";
    const metadata = document.createElement("span");
    metadata.className = "opening-source-meta";
    metadata.textContent = [openingText(source?.license, 80),
      /^[a-f0-9]{40}$/i.test(source?.commit) ? `자료 버전 ${source.commit.slice(0, 7)}` : ""].filter(Boolean).join(" · ");
    byId("opening-sources").replaceChildren(...sourceItems, attribution, metadata);
  }

  function evaluationView() {
    const note = "Regicide의 평가 · 백 기준 점수 · 승률이 아닙니다.";
    const result = state?.result !== "*" ? state?.result : evaluation?.status === "terminal" ? evaluation.result : "*";
    if (result && result !== "*") {
      const label = result === "1-0" ? "백 승리" : result === "0-1" ? "흑 승리" : result === "1/2-1/2" ? "무승부" : "대국 종료";
      const share = result === "1-0" ? 1 : result === "0-1" ? 0 : result === "1/2-1/2" ? .5 : null;
      return {label, score:result, detail:"확정된 대국 결과", share};
    }
    if (closed) return {label:"대국 종료", score:"—", detail:note, share:null};
    if (!evaluation || evaluation.status === "waiting") {
      return {label:"분석 대기 중", score:"—", detail:note, share:null};
    }
    if (evaluation.status === "unavailable") {
      return {label:"평가를 사용할 수 없습니다", score:"—", detail:"대국은 계속할 수 있습니다. " + note, share:null};
    }
    const hasCp = Number.isFinite(evaluation.cp);
    const hasMate = Number.isInteger(evaluation.mate);
    if (!hasCp && !hasMate) {
      return {label:"분석 중…", score:"—", detail:note, share:null};
    }
    const prefix = evaluation.bound === "lower" ? "≥ " : evaluation.bound === "upper" ? "≤ " : "";
    let score;
    let label;
    if (hasMate) {
      score = evaluation.mate === 0 ? "M0" : `${evaluation.mate > 0 ? "+" : "−"}M${Math.abs(evaluation.mate)}`;
      label = evaluation.mate === 0 ? (evaluation.winner ? `${colorName(evaluation.winner)} 체크메이트 승리` : "체크메이트 평가") : `${evaluation.mate > 0 ? "백" : "흑"} 메이트 예상`;
    } else {
      score = `${evaluation.cp > 0 ? "+" : evaluation.cp < 0 ? "−" : ""}${(Math.abs(evaluation.cp) / 100).toFixed(2)}`;
      label = evaluation.cp > 0 ? "백 우세" : evaluation.cp < 0 ? "흑 우세" : "균형";
    }
    // A bound is not an exact assessment of which side is ahead.
    if (prefix) label = evaluation.bound === "lower" ? "백 평가 하한" : "백 평가 상한";
    const details = [];
    if (evaluation.status === "searching") details.push("분석 중");
    if (evaluation.source === "previous_search") details.push("직전 탐색 평가");
    if (Number.isInteger(evaluation.depth)) details.push(`탐색 깊이 ${evaluation.depth}`);
    if (prefix) details.push("확정 평가가 아닌 탐색 경계값");
    details.push(note);
    const share = Number.isFinite(evaluation.white_share) ? Math.max(0, Math.min(1, evaluation.white_share)) : null;
    return {label, score:prefix + score, detail:details.join(" · "), share};
  }

  function renderEvaluation() {
    const view = evaluationView();
    const blackView = state?.human_color === "black";
    const bar = byId("evaluation-bar");
    bar.classList.toggle("black-view", blackView);
    bar.classList.toggle("unknown", view.share === null);
    bar.setAttribute("aria-label", `${view.label}, ${view.score}. ${view.detail}`);
    byId("evaluation-white").style.height = `${(view.share ?? 0) * 100}%`;
    byId("evaluation-top").textContent = blackView ? "백" : "흑";
    byId("evaluation-bottom").textContent = blackView ? "흑" : "백";
    byId("evaluation-label").textContent = view.label;
    byId("evaluation-score").textContent = view.score;
    byId("evaluation-detail").textContent = view.detail;
  }

  async function pollEvaluation() {
    if (analysisMode || coachingMode || evaluationPollInFlight || closed || !state || state.result !== "*") return;
    evaluationPollInFlight = true;
    const position = {game_id:state.game_id, revision:state.revision, fen:state.fen};
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const next = await request("/api/evaluation", undefined, controller.signal);
      if (!closed && samePosition(state, position) && samePosition(state, next)) {
        evaluation = next;
        renderEvaluation();
      }
    } catch (_) {
      if (samePosition(state, position) && !closed) {
        evaluation = {...position, status:"unavailable"};
        renderEvaluation();
      }
    } finally {
      clearTimeout(timeout);
      evaluationPollInFlight = false;
    }
  }

  function hintLegalMoves() {
    // Opponent candidates have their own validation list; legal_moves remains
    // the list of moves the person may actually submit.
    if (state && Object.hasOwn(state, "hint_legal_moves")) {
      return Array.isArray(state.hint_legal_moves) ? state.hint_legal_moves : [];
    }
    return state?.human_to_move && Array.isArray(state.legal_moves) ? state.legal_moves : [];
  }

  function hintPositionAvailable() {
    return Boolean(!closed && !analysisMode && !coachingMode && state &&
      (state.human_to_move || state.engine_to_move) && !state.handicap_mode &&
      state.result === "*" && !state.error &&
      (!busy || (engineThinking && state.engine_to_move)) && hintLegalMoves().length);
  }

  function currentHintCandidates() {
    if (!hintEnabled || !hintPositionAvailable() || hint?.enabled !== true || hint.status !== "ready" ||
        !samePosition(state, hint)) return [];
    // An explicit list is authoritative, including an empty or malformed list.
    const supplied = Object.hasOwn(hint, "candidates");
    const candidates = supplied ? (Array.isArray(hint.candidates) ? hint.candidates : []) : [{...hint, rank:1}];
    const legal = new Set(hintLegalMoves());
    const seenMoves = new Set(), seenRanks = new Set();
    return candidates.filter((item) => item && Number.isInteger(item.rank) && item.rank >= 1 && item.rank <= 3)
      .sort((a,b) => a.rank - b.rank).filter((item) => {
        const move = item.move;
        if (typeof move !== "string" || !/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move) || !legal.has(move) ||
            move.slice(0,2) === move.slice(2,4) || seenMoves.has(move) || seenRanks.has(item.rank) ||
            (item.from != null && item.from !== move.slice(0,2)) ||
            (item.to != null && item.to !== move.slice(2,4)) ||
            (item.promotion != null && item.promotion !== (move[4] || ""))) return false;
        seenMoves.add(move); seenRanks.add(item.rank);
        return true;
      }).slice(0,3).map((item) => ({...item, from:item.move.slice(0,2), to:item.move.slice(2,4),
        san:typeof item.san === "string" && item.san.trim() ? item.san.trim().slice(0,64) : item.move}));
  }

  function currentHintMove() {
    return currentHintCandidates()[0]?.move || null;
  }

  function hintMoveLabel(candidate) {
    return candidate.san + " · " + candidate.from + " → " + candidate.to + hintPromotionLabel(candidate.move);
  }

  function hintPromotionLabel(move) {
    return move[4] ? " · " + pieceNames[move[4]] + (move[4] === "n" ? "로 승격" : "으로 승격") : "";
  }

  function hintArrows(candidates, files, ranks) {
    if (!candidates.length) return null;
    const make = (tag, attributes = {}) => {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key,value] of Object.entries(attributes)) node.setAttribute(key, String(value));
      return node;
    };
    const svg = make("svg", {class:"live-hint-arrows", viewBox:"0 0 800 800", "aria-hidden":"true",
      focusable:"false", "pointer-events":"none"});
    const point = (square) => [files.indexOf(square[0])*100+50, ranks.indexOf(square[1])*100+50];
    const moves = candidates.map((candidate) => {
      const [x1,y1] = point(candidate.from), [x2,y2] = point(candidate.to);
      return {...candidate, x1,y1,x2,y2,dx:x2-x1,dy:y2-y1};
    });
    // Keep endpoints on square centers. Only rank badges use separate lanes;
    // wider lower-priority arrows keep coincident recommendations visible.
    for (const item of moves) {
      const peers = moves.filter((other) => (other.from === item.from || other.to === item.to) &&
        item.dx * other.dy === item.dy * other.dx);
      const peerIndex = peers.findIndex((other) => other.rank === item.rank);
      item.labelLane = (peerIndex - (peers.length-1)/2) * 32;
      item.width = 12 + peerIndex * 10;
      item.headWidth = 16 + peerIndex * 5;
      item.headLength = 31 + peerIndex * 6;
      const length = Math.hypot(item.dx,item.dy);
      item.ux = item.dx/length; item.uy = item.dy/length;
    }
    for (const item of [...moves].reverse()) {
      const {ux,uy,x1,y1,x2,y2,width,headWidth,headLength} = item;
      const group = make("g", {"data-hint-rank":item.rank, "data-move":item.move});
      const color = hintColors[item.rank-1], shaft = {x1,y1,x2:x2-ux*(headLength-9),y2:y2-uy*(headLength-9),"stroke-linecap":"butt"};
      group.append(make("line", {...shaft, stroke:"#203b32", "stroke-opacity":".65", "stroke-width":width+6}),
        make("line", {...shaft, stroke:color, "stroke-width":width}),
        make("polygon", {points:[x2+","+y2, (x2-ux*headLength-uy*headWidth)+","+(y2-uy*headLength+ux*headWidth),
          (x2-ux*headLength+uy*headWidth)+","+(y2-uy*headLength-ux*headWidth)].join(" "),
          fill:color, stroke:"#203b32", "stroke-width":2}));
      svg.append(group);
    }
    // Draw ranks last so crossing arrows cannot cover their numbers.
    for (const item of moves) {
      const x = item.x1+item.dx*.56-item.uy*item.labelLane, y = item.y1+item.dy*.56+item.ux*item.labelLane;
      const label = make("g", {"data-hint-label":item.rank});
      label.append(make("circle", {cx:x,cy:y,r:15,fill:hintColors[item.rank-1],stroke:"#203b32","stroke-width":2}));
      const number = make("text", {x,y:y+1,"text-anchor":"middle","dominant-baseline":"middle",
        fill:"#172d24","font-size":22,"font-weight":800,"font-family":"system-ui, sans-serif"});
      number.textContent = String(item.rank); label.append(number); svg.append(label);
    }
    return svg;
  }

  function renderHint() {
    const button = byId("hint-toggle");
    button.textContent = `추천 수: ${hintEnabled ? "켬" : "끔"}`;
    button.setAttribute("aria-pressed", String(hintEnabled));
    // This setting stays available while the opponent is thinking.
    button.disabled = !state || closed;
    const candidates = currentHintCandidates();
    let text = "켜면 양쪽 차례의 최대 3개 후보를 표시합니다.";
    if (closed) text = "대국 프로그램을 종료했습니다.";
    else if (analysisMode || coachingMode) text = "직접 대국으로 돌아오면 추천을 이어갑니다.";
    else if (hintEnabled && state?.result !== "*") text = "대국이 끝났습니다. 새 대국에서 추천을 이어갑니다.";
    else if (hintEnabled && (state?.error || (samePosition(state, hint) && (hint?.status === "unavailable" || hint?.enabled === false)))) text = "추천 수를 불러오지 못했습니다. 버튼을 껐다 켜서 다시 시도하세요. 대국은 계속할 수 있습니다.";
    else if (hintEnabled && state?.handicap_mode) text = "Regicide의 수 바꾸기를 마치면 추천을 이어갑니다.";
    else if (candidates.length) text = state.engine_to_move
      ? "Regicide 후보 수 " + candidates.length + "개 · 실제 착수는 Regicide가 선택합니다."
      : "내 추천 수 " + candidates.length + "개 · 번호 순서로 살펴보세요.";
    else if (hintEnabled && !hintPositionAvailable()) text = "현재 대국 위치의 추천 수를 기다리고 있습니다.";
    else if (hintEnabled) text = state.engine_to_move ? "Regicide 후보 수를 계산하고 있어요…" : "내 추천 수를 계산하고 있어요…";
    byId("hint-text").textContent = text;
    const rows = document.createDocumentFragment();
    for (const candidate of candidates) {
      const row = document.createElement("li");
      row.className = "hint-candidate";
      const rank = document.createElement("span");
      rank.className = "hint-candidate-rank";
      rank.style.backgroundColor = hintColors[candidate.rank-1];
      rank.textContent = String(candidate.rank);
      rank.setAttribute("aria-hidden", "true");
      const label = document.createElement("span");
      label.className = "hint-candidate-label";
      label.textContent = hintMoveLabel(candidate);
      row.setAttribute("aria-label", candidate.rank + "순위 · " + label.textContent);
      row.setAttribute("title", candidate.rank + "순위 · " + label.textContent);
      row.append(rank,label); rows.append(row);
    }
    byId("hint-candidates").replaceChildren(rows);
    byId("hint-panel").classList.toggle("hint-active", candidates.length > 0);
  }

  function acceptHint(next, position, epoch) {
    if (!hintEnabled || !hintPositionAvailable() || epoch !== hintEpoch ||
        !samePosition(state, position) || !samePosition(state, next) ||
        (next?.turn && state.turn && next.turn !== state.turn)) return;
    hint = next;
    renderHint();
    renderBoard();
  }

  async function pollHint() {
    if (hintPollInFlight || hintToggleInFlight || !hintEnabled || !hintPositionAvailable()) return;
    hintPollInFlight = true;
    const epoch = hintEpoch;
    const position = {game_id:state.game_id, revision:state.revision, fen:state.fen};
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      acceptHint(await request("/api/hint", undefined, controller.signal), position, epoch);
    } catch (_) {
      acceptHint({...position, enabled:true, status:"unavailable"}, position, epoch);
    } finally {
      clearTimeout(timeout);
      hintPollInFlight = false;
    }
  }

  async function syncHintToggle() {
    if (hintToggleInFlight || closed) return;
    hintToggleInFlight = true;
    try {
      while (hintToggleDirty && !closed) {
        hintToggleDirty = false;
        const enabled = hintEnabled;
        const epoch = hintEpoch;
        const position = state && {game_id:state.game_id, revision:state.revision, fen:state.fen};
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 5000);
        try {
          const next = await request("/api/hint", {enabled}, controller.signal);
          acceptHint(next, position, epoch);
        } catch (_) {
          acceptHint({...position, enabled:true, status:"unavailable"}, position, epoch);
        } finally { clearTimeout(timeout); }
      }
    } finally { hintToggleInFlight = false; }
    void pollHint();
  }

  function renderBoard() {
    const blackView = state?.human_color === "black";
    const files = blackView ? "hgfedcba" : "abcdefgh";
    const ranks = blackView ? "12345678" : "87654321";
    const legal = state?.legal_moves || [];
    const destinations = new Set(selected ? legal.filter((move) => move.slice(0,2) === selected).map((move) => move.slice(2,4)) : []);
    const lastSquares = state?.last_move ? [state.last_move.slice(0,2), state.last_move.slice(2,4)] : [];
    const hints = currentHintCandidates();
    const focusedSquare = document.activeElement?.dataset?.square;
    const fragment = document.createDocumentFragment();
    for (const rank of ranks) {
      for (const file of files) {
        const square = file + rank;
        const piece = state?.board?.[square];
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.square = square;
        button.className = "square";
        if ((file.charCodeAt(0) - 97 + Number(rank)) % 2 === 1) button.classList.add("dark");
        if (lastSquares.includes(square)) button.classList.add("last-move");
        if (square === selected) button.classList.add("selected");
        const hintLabels = hints.filter((item) => item.from === square || item.to === square)
          .map((item) => item.rank + "순위 추천 수 " + (item.from === square ? "출발" : "도착") +
            hintPromotionLabel(item.move));
        if (destinations.has(square)) button.classList.add("legal-target");
        if (piece) button.classList.add("occupied");
        const pieceColor = piece && piece === piece.toUpperCase() ? "white" : "black";
        if (state?.in_check && piece?.toLowerCase() === "k" && pieceColor === state.turn) button.classList.add("in-check");
        const label = piece ? `${square}, ${colorName(pieceColor)} ${pieceNames[piece.toLowerCase()]}` : `${square}, 빈 칸`;
        button.setAttribute("aria-label", label + (destinations.has(square) ? ", 이동 가능" : "") +
          (hintLabels.length ? ", " + hintLabels.join(", ") : ""));
        button.setAttribute("aria-pressed", String(square === selected));
        button.disabled = busy || timeSaving || closed || !state?.human_to_move;
        if (piece) {
          const symbol = document.createElement("span");
          symbol.className = "piece " + pieceColor;
          symbol.textContent = pieceSymbols[piece];
          symbol.setAttribute("aria-hidden", "true");
          button.append(symbol);
        }
        if (file === files[0]) {
          const coordinate = document.createElement("span");
          coordinate.className = "coordinate rank-coordinate";
          coordinate.textContent = rank;
          coordinate.setAttribute("aria-hidden", "true");
          button.append(coordinate);
        }
        if (rank === ranks[7]) {
          const coordinate = document.createElement("span");
          coordinate.className = "coordinate file-coordinate";
          coordinate.textContent = file;
          coordinate.setAttribute("aria-hidden", "true");
          button.append(coordinate);
        }
        fragment.append(button);
      }
    }
    const arrows = hintArrows(hints, files, ranks);
    if (arrows) fragment.append(arrows);
    if (window.RegicideMotion) window.RegicideMotion.replace(boardElement, fragment, {
      scope:state?.game_id, ply:state?.moves?.length || 0, pieces:state?.board || {},
      turn:state?.turn, lastMove:state?.last_move, legalMoves:state?.legal_moves,
      orientation:blackView ? "black" : "white", enabled:!analysisMode && !coachingMode && !closed
    });
    else boardElement.replaceChildren(fragment);
    if (focusedSquare) boardElement.querySelector(`[data-square="${focusedSquare}"]`)?.focus({preventScroll:true});
  }

  function renderMoves() {
    const moves = state?.moves || [];
    byId("move-count").textContent = `${moves.length}수`;
    byId("empty-moves").hidden = moves.length > 0;
    const fragment = document.createDocumentFragment();
    for (let i = 0; i < moves.length; i += 2) {
      const row = document.createElement("li");
      row.className = "move-row";
      const number = document.createElement("span");
      number.className = "move-number";
      number.textContent = `${Math.floor(i / 2) + 1}.`;
      row.append(number);
      for (let j = i; j < i + 2; j++) {
        const item = document.createElement("span");
        item.className = "move-san" + (j === moves.length - 1 ? " latest" : "");
        item.textContent = moves[j]?.san || "";
        if (moves[j]) item.setAttribute("aria-label", `${Math.floor(j/2)+1}수 ${j%2 === 0 ? "백" : "흑"} ${moves[j].san}`);
        row.append(item);
      }
      fragment.append(row);
    }
    byId("move-list").replaceChildren(fragment);
    if (moves.length !== renderedMoveCount) {
      byId("moves-scroll").scrollTop = byId("moves-scroll").scrollHeight;
      renderedMoveCount = moves.length;
    }
  }

  function render() {
    renderBoard();
    renderMoves();
    renderEvaluation();
    renderHint();
    renderOpeningGuide();
    const ready = Boolean(state) && !closed;
    const active = ready && state.result === "*";
    const humanColor = state?.human_color || "white";
    byId("human-color").textContent = colorName(humanColor);
    byId("opponent-color").textContent = colorName(humanColor === "white" ? "black" : "white");
    if (state) {
      const ms = state.engine.movetime_ms;
      const seconds = Number(ms) / 1000;
      byId("opponent-detail").textContent = `한 수에 ${seconds}초 생각 · ${colorName(humanColor === "white" ? "black" : "white")}`;
      if (state.engine.boss_mode === true) {
        const phaseLabels = {opening: "변칙 오프닝", trap_hunt: "전술 사냥", full_strength: "전력 대국"};
        const phase = Object.hasOwn(phaseLabels, state.engine.boss_phase) ? phaseLabels[state.engine.boss_phase] : null;
        byId("opponent-detail").textContent += phase ? ` · 보스 모드 · ${phase}` : " · 보스 모드";
      }
      const engineName = state.engine.name || "Regicide";
      byId("engine-version").textContent = engineName;
      byId("header-engine-version").textContent = engineName.startsWith("Regicide ") ? "v" + engineName.slice("Regicide ".length) : "";
      byId("human-detail").textContent = active ? `${colorName(humanColor)}으로 두고 있어요. 기물과 도착 칸을 눌러주세요.` : "이번 대국이 끝났습니다.";
    }
    byId("new-game").disabled = !ready || busy || timeSaving;
    byId("human-side").disabled = !ready || busy || timeSaving;
    byId("boss-mode").disabled = !ready || busy || timeSaving || state?.handicap_mode;
    byId("think-time").disabled = !ready || timeSaving || state?.handicap_mode || (busy && !state?.engine_to_move);
    if (state && !timeSaving) byId("think-time").value = String(state.engine.movetime_ms);
    const currentMS = state?.engine.active_movetime_ms;
    byId("think-time-help").textContent = timeSaving ? "생각 시간을 저장하고 있습니다…" :
      currentMS != null && currentMS !== state.engine.movetime_ms ?
      `현재 수는 ${currentMS / 1000}초로 생각 중 · 다음 Regicide 수부터 ${state.engine.movetime_ms / 1000}초` :
      timeNotice || "대국 중에도 변경할 수 있습니다. 다음 Regicide 수부터 적용됩니다.";
    // Settings and board replies can cross a phase boundary out of order.
    // Use the latest setting's schedule with the current board's move count.
    const extraSchedule = state?.engine.extra_budget_schedule;
    const plannedExtraMS = state?.engine.dynamic_play_enabled && Array.isArray(extraSchedule) ?
      (extraSchedule.find(stage => (state.moves?.length ?? 0) < stage.until_ply)?.budget_ms ?? 0) :
      (state?.engine.extra_budget_ms ?? 0);
    const extraMS = currentMS != null ? (state?.engine.active_extra_budget_ms ?? plannedExtraMS) : plannedExtraMS;
    const extraLabel = state?.engine.dynamic_play_enabled ? "구상 검토" : "오프닝 후보 검토";
    if (extraMS > 0) byId("think-time-help").textContent += ` ${extraLabel} 시 최대 ${extraMS / 1000}초 더 생각할 수 있습니다.`;
    byId("undo").disabled = !ready || busy || timeSaving || !state.can_undo;
    byId("resign").disabled = !ready || busy || timeSaving || !active;
    byId("download-pgn").disabled = !ready || busy;
    byId("shutdown").disabled = !ready || busy || timeSaving;
    byId("connection-label").textContent = closed ? "프로그램 종료됨" : "내 컴퓨터에서 대국 중";
    const thinking = busy && ready && state.engine_to_move;
    byId("status-dot").classList.toggle("thinking", thinking);
    byId("status-dot").classList.toggle("finished", ready && !active);
    byId("status-text").textContent = closed ? "대국 프로그램을 종료했습니다." : thinking ? "Regicide가 다음 수를 생각하고 있어요…" : state?.status || "체스판을 준비하고 있어요.";
    byId("status-detail").textContent = closed ? "진행한 기보는 자동 저장했습니다. 이 창을 닫아도 됩니다." : !ready ? "잠시만 기다려주세요." : thinking ? "엔진이 수를 두면 바로 이어서 둘 수 있습니다." : !active ? `결과 ${state.result} · 기보를 저장하거나 새 대국을 시작하세요.` : state.in_check ? "체크입니다. 왕을 안전하게 지키는 수를 두세요." : state.human_to_move ? (selected ? `${selected} 기물을 선택했습니다. 표시된 칸으로 이동하세요.` : "내 차례입니다. 기물을 선택해보세요.") : "Regicide의 차례입니다.";
    showError(closed ? "" : errorMessage || state?.error || "");
  }

  async function engineMove() {
    if (closed || engineThinking || !state?.engine_to_move || state.handicap_mode || state.result !== "*" || state.error) return;
    engineThinking = true;
    clearHint();
    render();
    try {
      // Advice polls run independently while the original move request waits.
      // They never delay or choose the opponent's actual move.
      const pendingMove = request("/api/engine", {revision: state.revision});
      void pollHint();
      applyState(await pendingMove);
    } finally {
      engineThinking = false;
      render();
    }
  }

  async function mutate(path, payload = {}) {
    if (busy || timeSaving || closed || !state) return;
    busy = true;
    showError("");
    selected = null;
    clearHint();
    render();
    try {
      applyState(await request(path, {...payload, revision: state.revision}));
      await engineMove();
    } catch (error) {
      clearHint();
      showError(error.message);
    }
    finally { busy = false; render(); }
  }

  async function changeThinkTime() {
    if (!state || closed || timeSaving || state.handicap_mode || (busy && !state.engine_to_move)) return;
    const movetime = Number(byId("think-time").value);
    timeSaving = true;
    timeNotice = "";
    showError("");
    render();
    try {
      applyState(await request("/api/think-time", {
        game_id: state.game_id, settings_revision: state.engine.settings_revision,
        movetime_ms: movetime
      }, undefined, true), true);
      timeNotice = `${movetime / 1000}초로 저장했습니다. 다음 Regicide 수부터 적용됩니다.`;
    } catch (error) { showError(error.message); }
    finally { timeSaving = false; render(); }
  }

  function choosePromotion(moves) {
    pendingPromotion = moves;
    const choices = document.createDocumentFragment();
    for (const type of ["q", "r", "b", "n"]) {
      const move = moves.find((item) => item[4] === type);
      if (!move) continue;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "promotion-choice";
      button.dataset.move = move;
      button.setAttribute("aria-label", `${pieceNames[type]}으로 승격`);
      const symbol = document.createElement("span");
      symbol.className = "promotion-symbol";
      symbol.setAttribute("aria-hidden", "true");
      symbol.textContent = pieceSymbols[state.human_color === "white" ? type.toUpperCase() : type];
      const label = document.createElement("span");
      label.textContent = pieceNames[type];
      button.append(symbol, label);
      choices.append(button);
    }
    byId("promotion-choices").replaceChildren(choices);
    dialog.showModal();
  }

  boardElement.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-square]");
    if (!button || busy || timeSaving || closed || !state?.human_to_move) return;
    const square = button.dataset.square;
    if (selected === square) { selected = null; render(); return; }
    if (selected) {
      const candidates = state.legal_moves.filter((move) => move.startsWith(selected + square));
      if (candidates.length > 1 || candidates[0]?.length === 5) { choosePromotion(candidates); return; }
      if (candidates.length === 1) { void mutate("/api/move", {move: candidates[0]}); return; }
    }
    selected = state.legal_moves.some((move) => move.startsWith(square)) ? square : null;
    render();
  });

  byId("promotion-choices").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-move]");
    if (!button || !pendingPromotion.includes(button.dataset.move)) return;
    const move = button.dataset.move;
    dialog.close();
    pendingPromotion = [];
    void mutate("/api/move", {move});
  });
  byId("cancel-promotion").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { pendingPromotion = []; });
  byId("new-game-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void mutate("/api/new", {color: byId("human-side").value, movetime_ms: Number(byId("think-time").value),
      boss_mode: byId("boss-mode").value === "boss"});
  });
  byId("think-time").addEventListener("change", () => { void changeThinkTime(); });
  byId("undo").addEventListener("click", () => { void mutate("/api/undo"); });
  byId("resign").addEventListener("click", () => { void mutate("/api/resign"); });
  byId("hint-toggle").addEventListener("click", () => {
    if (closed || !state) return;
    hintEnabled = !hintEnabled;
    clearHint();
    hintToggleDirty = true;
    renderHint();
    renderBoard();
    void syncHintToggle();
  });
  byId("shutdown").addEventListener("click", async () => {
    if (busy || timeSaving || closed || !state) return;
    busy = true;
    showError("");
    clearHint();
    render();
    try {
      const result = await request("/api/shutdown", {revision: state.revision});
      if (result.closed !== true) throw new Error("종료를 확인하지 못했습니다. 다시 시도해주세요.");
      closed = true;
      selected = null;
      clearHint();
    } catch (error) { showError(error.message); }
    finally { busy = false; render(); }
  });
  byId("download-pgn").addEventListener("click", async () => {
    try {
      const response = await fetch("/api/pgn", {headers: {"X-Regicide-Token": token}, cache: "no-store"});
      if (!response.ok) throw new Error(" 기보를 저장하지 못했습니다. 다시 시도해주세요.");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `Regicide-${new Date().toISOString().slice(0,10)}.pgn`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { showError(error.message); }
  });

  async function initialize() {
    render();
    try {
      applyState(await request("/api/state"));
      byId("human-side").value = state.human_color;
      byId("think-time").value = String(state.engine.movetime_ms);
      await engineMove();
    } catch (error) { showError(error.message); }
    finally { busy = false; render(); }
  }
  // Analysis polling has its own request and keeps running while /api/engine waits.
  const evaluationTimer = setInterval(() => { void pollEvaluation(); }, 200);
  const hintTimer = setInterval(() => { void pollHint(); }, 300);
  window.addEventListener("pagehide", () => {
    closed = true;
    clearHint();
    renderHint();
    renderBoard();
    clearInterval(evaluationTimer);
    clearInterval(hintTimer);
  });
  function changeHintMode() {
    clearHint();
    if (analysisMode || coachingMode) window.RegicideMotion?.cancel(boardElement, true);
    renderHint();
    renderBoard();
    if (!analysisMode && !coachingMode) void pollHint();
  }
  window.addEventListener("regicide-analysis-mode", (event) => { analysisMode = event.detail === true; changeHintMode(); });
  window.addEventListener("regicide-coaching-mode", (event) => { coachingMode = event.detail === true; changeHintMode(); });
  void initialize();
})();
