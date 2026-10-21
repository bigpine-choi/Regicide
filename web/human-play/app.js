"use strict";

(() => {
  const token = document.querySelector('meta[name="regicide-token"]').content;
  const byId = (id) => document.getElementById(id);
  const boardElement = byId("board");
  const dialog = byId("promotion-dialog");
  const pieceSymbols = {K:"♔",Q:"♕",R:"♖",B:"♗",N:"♘",P:"♙",k:"♚",q:"♛",r:"♜",b:"♝",n:"♞",p:"♟"};
  const pieceNames = {k:"왕",q:"퀸",r:"룩",b:"비숍",n:"나이트",p:"폰"};
  const colorName = (color) => color === "white" ? "백" : "흑";
  let state = null;
  let selected = null;
  let busy = true;
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
        render();
      }
      return;
    }
    if (state?.game_id === next.game_id &&
        Number(next.engine.settings_revision || 0) < Number(state.engine.settings_revision || 0)) {
      next.engine.movetime_ms = state.engine.movetime_ms;
      next.engine.settings_revision = state.engine.settings_revision;
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
    if (state.result !== "*" || state.error) clearHint();
    if (samePosition(state, next.evaluation)) evaluation = next.evaluation;
    if (!preserveSelection || !unchangedPosition) selected = null;
    render();
  }

  function samePosition(first, second) {
    return Boolean(first && second && first.game_id != null && first.revision != null && first.fen &&
      first.game_id === second.game_id &&
      first.revision === second.revision && first.fen === second.fen);
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
    if (evaluationPollInFlight || closed || !state || state.result !== "*") return;
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

  function currentHintMove() {
    return !closed && !busy && hintEnabled && state?.human_to_move && state.result === "*" && !state.error &&
      hint?.enabled === true && hint.status === "ready" && samePosition(state, hint) &&
      (state.legal_moves || []).includes(hint.move) ? hint.move : null;
  }

  function renderHint() {
    const button = byId("hint-toggle");
    button.textContent = `추천 수: ${hintEnabled ? "켬" : "끔"}`;
    button.setAttribute("aria-pressed", String(hintEnabled));
    // This setting stays available while the opponent is thinking.
    button.disabled = !state || closed;
    const move = currentHintMove();
    let text = "켜면 내 차례에 추천 수를 표시합니다.";
    if (closed) text = "대국 프로그램을 종료했습니다.";
    else if (hintEnabled && state?.result !== "*") text = "대국이 끝났습니다. 새 대국에서 추천을 이어갑니다.";
    else if (hintEnabled && (state?.error || (samePosition(state, hint) && (hint?.status === "unavailable" || hint?.enabled === false)))) text = "추천 수를 불러오지 못했습니다. 버튼을 껐다 켜서 다시 시도하세요. 대국은 계속할 수 있습니다.";
    else if (hintEnabled && !state?.human_to_move) text = "내 차례가 되면 추천 수를 찾습니다.";
    else if (move) {
      const promotion = move[4] ? ` · ${pieceNames[move[4]]}으로 승격` : "";
      text = `${hint.san || move} · ${move.slice(0,2)} → ${move.slice(2,4)}${promotion}`;
    } else if (hintEnabled) text = "다음 수를 생각하고 있어요…";
    byId("hint-text").textContent = text;
    byId("hint-panel").classList.toggle("hint-active", Boolean(move));
  }

  function acceptHint(next, position, epoch) {
    if (closed || !hintEnabled || epoch !== hintEpoch || !samePosition(state, position) || !samePosition(state, next)) return;
    hint = next;
    renderHint();
    renderBoard();
  }

  async function pollHint() {
    if (hintPollInFlight || hintToggleInFlight || closed || !hintEnabled || !state || state.result !== "*" || state.error ||
      !state.human_to_move || busy) return;
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
    const hintMove = currentHintMove();
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
        const hintRole = hintMove && (square === hintMove.slice(0,2) ? "from" : square === hintMove.slice(2,4) ? "to" : null);
        if (hintRole) button.classList.add(`hint-${hintRole}`);
        if (destinations.has(square)) button.classList.add("legal-target");
        if (piece) button.classList.add("occupied");
        const pieceColor = piece && piece === piece.toUpperCase() ? "white" : "black";
        if (state?.in_check && piece?.toLowerCase() === "k" && pieceColor === state.turn) button.classList.add("in-check");
        const label = piece ? `${square}, ${colorName(pieceColor)} ${pieceNames[piece.toLowerCase()]}` : `${square}, 빈 칸`;
        button.setAttribute("aria-label", label + (destinations.has(square) ? ", 이동 가능" : "") + (hintRole ? `, 추천 수 ${hintRole === "from" ? "출발" : "도착"}` : ""));
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
        if (hintRole) {
          const marker = document.createElement("span");
          marker.className = "hint-square-label";
          marker.textContent = hintRole === "from" ? "출발" : "도착";
          marker.setAttribute("aria-hidden", "true");
          button.append(marker);
        }
        fragment.append(button);
      }
    }
    boardElement.replaceChildren(fragment);
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
    const ready = Boolean(state) && !closed;
    const active = ready && state.result === "*";
    const humanColor = state?.human_color || "white";
    byId("human-color").textContent = colorName(humanColor);
    byId("opponent-color").textContent = colorName(humanColor === "white" ? "black" : "white");
    if (state) {
      const ms = state.engine.movetime_ms;
      const seconds = Number(ms) / 1000;
      byId("opponent-detail").textContent = `한 수에 ${seconds}초 생각 · ${colorName(humanColor === "white" ? "black" : "white")}`;
      const engineName = state.engine.name || "Regicide";
      byId("engine-version").textContent = engineName;
      byId("header-engine-version").textContent = engineName.startsWith("Regicide ") ? "v" + engineName.slice("Regicide ".length) : "";
      byId("human-detail").textContent = active ? `${colorName(humanColor)}으로 두고 있어요. 기물과 도착 칸을 눌러주세요.` : "이번 대국이 끝났습니다.";
    }
    byId("new-game").disabled = !ready || busy || timeSaving;
    byId("human-side").disabled = !ready || busy || timeSaving;
    byId("think-time").disabled = !ready || timeSaving || state?.handicap_mode || (busy && !state?.engine_to_move);
    if (state && !timeSaving) byId("think-time").value = String(state.engine.movetime_ms);
    const currentMS = state?.engine.active_movetime_ms;
    byId("think-time-help").textContent = timeSaving ? "생각 시간을 저장하고 있습니다…" :
      currentMS != null && currentMS !== state.engine.movetime_ms ?
      `현재 수는 ${currentMS / 1000}초로 생각 중 · 다음 Regicide 수부터 ${state.engine.movetime_ms / 1000}초` :
      timeNotice || "대국 중에도 변경할 수 있습니다. 다음 Regicide 수부터 적용됩니다.";
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
    if (closed || !state?.engine_to_move || state.result !== "*" || state.error) return;
    applyState(await request("/api/engine", {revision: state.revision}));
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
    void mutate("/api/new", {color: byId("human-side").value, movetime_ms: Number(byId("think-time").value)});
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
    clearInterval(evaluationTimer);
    clearInterval(hintTimer);
  });
  void initialize();
})();
