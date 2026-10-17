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
  let closed = false;
  let errorMessage = "";
  let pendingPromotion = [];
  let renderedMoveCount = -1;

  function showError(message) {
    errorMessage = message || "";
    byId("error-text").textContent = errorMessage;
    byId("error-panel").hidden = !errorMessage;
  }

  async function request(path, payload) {
    let response;
    try {
      response = await fetch(path, {
        method: payload === undefined ? "GET" : "POST",
        headers: {"X-Regicide-Token": token, ...(payload === undefined ? {} : {"Content-Type": "application/json"})},
        ...(payload === undefined ? {} : {body: JSON.stringify(payload)}),
        cache: "no-store"
      });
    } catch (_) {
      throw new Error("대국 프로그램과 연결이 끊겼습니다. 실행 창을 확인하고 페이지를 새로 열어주세요.");
    }
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error("대국 상태를 읽지 못했습니다. 페이지를 새로 열어주세요."); }
    if (!response.ok) {
      if (data.state) applyState(data.state);
      throw new Error(data.error || "요청을 처리하지 못했습니다. 다시 시도해주세요.");
    }
    return data;
  }

  function applyState(next) {
    state = next;
    selected = null;
    render();
  }

  function renderBoard() {
    const blackView = state?.human_color === "black";
    const files = blackView ? "hgfedcba" : "abcdefgh";
    const ranks = blackView ? "12345678" : "87654321";
    const legal = state?.legal_moves || [];
    const destinations = new Set(selected ? legal.filter((move) => move.slice(0,2) === selected).map((move) => move.slice(2,4)) : []);
    const lastSquares = state?.last_move ? [state.last_move.slice(0,2), state.last_move.slice(2,4)] : [];
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
        if (destinations.has(square)) button.classList.add("legal-target");
        if (piece) button.classList.add("occupied");
        const pieceColor = piece && piece === piece.toUpperCase() ? "white" : "black";
        if (state?.in_check && piece?.toLowerCase() === "k" && pieceColor === state.turn) button.classList.add("in-check");
        const label = piece ? `${square}, ${colorName(pieceColor)} ${pieceNames[piece.toLowerCase()]}` : `${square}, 빈 칸`;
        button.setAttribute("aria-label", label + (destinations.has(square) ? ", 이동 가능" : ""));
        button.setAttribute("aria-pressed", String(square === selected));
        button.disabled = busy || closed || !state?.human_to_move;
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
    const ready = Boolean(state) && !closed;
    const active = ready && state.result === "*";
    const humanColor = state?.human_color || "white";
    byId("human-color").textContent = colorName(humanColor);
    byId("opponent-color").textContent = colorName(humanColor === "white" ? "black" : "white");
    if (state) {
      const ms = state.engine.movetime_ms;
      const seconds = Number(ms) / 1000;
      byId("opponent-detail").textContent = `한 수에 ${seconds}초 생각 · ${colorName(humanColor === "white" ? "black" : "white")}`;
      byId("engine-version").textContent = state.engine.name || "Regicide v0.5.0";
      byId("human-detail").textContent = active ? `${colorName(humanColor)}으로 두고 있어요. 기물과 도착 칸을 눌러주세요.` : "이번 대국이 끝났습니다.";
    }
    byId("new-game").disabled = !ready || busy;
    byId("human-side").disabled = !ready || busy;
    byId("think-time").disabled = !ready || busy;
    byId("undo").disabled = !ready || busy || !state.can_undo;
    byId("resign").disabled = !ready || busy || !active;
    byId("download-pgn").disabled = !ready || busy;
    byId("shutdown").disabled = !ready || busy;
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
    if (busy || closed || !state) return;
    busy = true;
    showError("");
    selected = null;
    render();
    try {
      applyState(await request(path, {...payload, revision: state.revision}));
      await engineMove();
    } catch (error) { showError(error.message); }
    finally { busy = false; render(); }
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
    if (!button || busy || closed || !state?.human_to_move) return;
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
  byId("undo").addEventListener("click", () => { void mutate("/api/undo"); });
  byId("resign").addEventListener("click", () => { void mutate("/api/resign"); });
  byId("shutdown").addEventListener("click", async () => {
    if (busy || closed || !state) return;
    busy = true;
    showError("");
    render();
    try {
      const result = await request("/api/shutdown", {revision: state.revision});
      if (result.closed !== true) throw new Error("종료를 확인하지 못했습니다. 다시 시도해주세요.");
      closed = true;
      selected = null;
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
  void initialize();
})();
