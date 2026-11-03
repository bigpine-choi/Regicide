"use strict";

(() => {
  const get = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name="regicide-token"]').content;
  const symbols = {K:"♔",Q:"♕",R:"♖",B:"♗",N:"♘",P:"♙",k:"♚",q:"♛",r:"♜",b:"♝",n:"♞",p:"♟"};
  const names = {k:"왕",q:"퀸",r:"룩",b:"비숍",n:"나이트",p:"폰"};
  const initialFen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const castlingRights = {K:["e1","K","h1","R"],Q:["e1","K","a1","R"],k:["e8","k","h8","r"],q:["e8","k","a8","r"]};
  let visible = false, editing = true, busy = false, polling = false, blackView = false;
  let epoch = 0, snapshot = null, draft = {}, halfmove = "0", fullmove = "1";
  let brush = "K", selected = null, failure = "", promotion = [];
  let drag = null, dragSource = null, dragTarget = null, suppressClickUntil = 0;
  let returnFailure = "";
  const returnNotice = document.createElement("p");
  returnNotice.className = "error-panel";
  returnNotice.setAttribute("role", "alert");
  returnNotice.hidden = true;
  get("live-view").before(returnNotice);

  function readFen(fen) {
    const fields = fen.trim().split(/\s+/);
    if (fields.length !== 6 || !/^[wb]$/.test(fields[1]) || !/^(?:-|K?Q?k?q?)$/.test(fields[2]) ||
        !/^(?:-|[a-h][36])$/.test(fields[3]) || !/^\d+$/.test(fields[4]) || !/^[1-9]\d*$/.test(fields[5]))
      throw new Error("차례와 특수 규칙을 포함한 FEN 6개 항목을 입력하세요.");
    const rows = fields[0].split("/"), pieces = {};
    if (rows.length !== 8) throw new Error("FEN에는 8개 가로줄이 필요합니다.");
    rows.forEach((row, index) => {
      let file = 0;
      for (const char of row) {
        if (/^[1-8]$/.test(char)) file += Number(char);
        else if (Object.hasOwn(symbols, char) && file < 8) pieces["abcdefgh"[file++] + (8-index)] = char;
        else throw new Error("FEN의 기물 배치를 확인하세요.");
      }
      if (file !== 8) throw new Error("FEN의 각 가로줄은 8칸이어야 합니다.");
    });
    return {fields, pieces};
  }

  function loadDraft(fen) {
    cancelDrag();
    const parsed = readFen(fen);
    draft = parsed.pieces;
    get("analysis-turn").value = parsed.fields[1];
    get("analysis-castling").value = parsed.fields[2];
    get("analysis-ep").value = parsed.fields[3];
    halfmove = parsed.fields[4]; fullmove = parsed.fields[5];
    selected = null;
    updateFen();
  }

  function cancelDrag() {
    if (drag) suppressClickUntil = Date.now() + 250;
    drag = null;
    dragSource?.classList.remove("analysis-drag-source");
    dragTarget?.classList.remove("analysis-drag-hover");
    dragSource = dragTarget = null;
    get("analysis-view").classList.remove("analysis-dragging");
  }

  function validDrag() {
    if (!visible || busy || !drag || drag.epoch !== epoch) return false;
    if (drag.kind === "palette") return editing;
    if (drag.kind === "board") return editing && draft[drag.square] === drag.piece;
    return !editing && !failure && !promotion.length && snapshot?.active &&
      drag.game_id === snapshot.game_id && drag.revision === snapshot.revision && drag.fen === snapshot.fen &&
      snapshot.board[drag.square] === drag.piece && movableSquare(drag.square);
  }

  function beginDrag(event, kind) {
    const source = event.target.closest(kind === "palette" ? "button[data-piece]" : "button[data-analysis-square]");
    const square = source?.dataset.analysisSquare;
    const piece = kind === "palette" ? source?.dataset.piece : (editing ? draft : snapshot?.board || {})[square];
    if (!visible || busy || !piece || !source || (kind === "palette" ? !editing : !editing && !movableSquare(square))) { event.preventDefault(); return; }
    cancelDrag();
    drag = {kind:kind === "board" && !editing ? "play" : kind, piece, square, epoch,
      game_id:snapshot?.game_id,revision:snapshot?.revision,fen:snapshot?.fen};
    dragSource = source;
    source.classList.add("analysis-drag-source");
    get("analysis-view").classList.add("analysis-dragging");
    event.dataTransfer.effectAllowed = kind === "palette" ? "copy" : "move";
    event.dataTransfer.setData("text/plain", piece);
  }

  function hoverDrag(event, target) {
    if (!validDrag()) return;
    event.preventDefault();
    const square = target?.dataset.analysisSquare;
    const allowed = square && (drag.kind !== "play" || moveCandidates(drag.square,square).length);
    event.dataTransfer.dropEffect = allowed ? (drag.kind === "palette" ? "copy" : "move") : !square && drag.kind === "board" ? "move" : "none";
    dragTarget?.classList.remove("analysis-drag-hover");
    dragTarget = allowed ? target : !square && drag.kind === "board" ? get("analysis-trash") : null;
    dragTarget?.classList.add("analysis-drag-hover");
  }

  function changedDraft(before, touched) {
    const previousRights = get("analysis-castling").value;
    get("analysis-castling").value = Object.keys(castlingRights).filter((right) => {
      const [kingSquare,,rookSquare] = castlingRights[right];
      return previousRights.includes(right) && castlingPiecesPresent(right,before) && castlingPiecesPresent(right) &&
        !touched.includes(kingSquare) && !touched.includes(rookSquare);
    }).join("") || "-";
    get("analysis-ep").value = "-";
    halfmove = "0"; fullmove = "1";
    failure = ""; selected = null;
    updateFen(); render();
  }

  function dropDrag(event, square = null) {
    if (!validDrag()) return;
    event.preventDefault();
    event.stopPropagation();
    const item = drag;
    cancelDrag();
    if (item.kind === "play") {
      const candidates = square ? moveCandidates(item.square,square) : [];
      if (candidates.length && candidates[0].length === 5) { promotion = candidates; renderPromotion(); }
      else if (candidates.length) void action("move",{move:candidates[0]});
      else render();
      return;
    }
    const before = {...draft};
    if (square) {
      if (item.kind === "board" && square === item.square) return;
      if (item.kind === "palette" && draft[square] === item.piece) return;
      if (item.kind === "board") delete draft[item.square];
      draft[square] = item.piece;
      changedDraft(before,item.kind === "board" ? [item.square,square] : [square]);
    } else if (item.kind === "board") {
      delete draft[item.square];
      changedDraft(before,[item.square]);
    }
  }

  function updateFen() {
    const rows = [];
    for (const rank of "87654321") {
      let row = "", empty = 0;
      for (const file of "abcdefgh") {
        const piece = draft[file+rank];
        if (!piece) empty++;
        else { if (empty) row += empty; empty = 0; row += piece; }
      }
      if (empty) row += empty;
      rows.push(row);
    }
    get("analysis-fen").value = `${rows.join("/")} ${get("analysis-turn").value} ${get("analysis-castling").value.trim() || "-"} ${get("analysis-ep").value.trim() || "-"} ${halfmove} ${fullmove}`;
    return get("analysis-fen").value;
  }

  function castlingPiecesPresent(right, pieces = draft) {
    const [kingSquare,king,rookSquare,rook] = castlingRights[right];
    return pieces[kingSquare] === king && pieces[rookSquare] === rook;
  }

  function renderCastling() {
    const rights = editing ? get("analysis-castling").value : snapshot?.fen?.trim().split(/\s+/)[2] || "-";
    const pieces = editing ? draft : snapshot?.board || {};
    for (const right of Object.keys(castlingRights)) {
      const control = get("analysis-right-" + right);
      control.checked = rights.includes(right);
      control.disabled = !visible || !editing || busy || !castlingPiecesPresent(right, pieces);
      control.title = castlingPiecesPresent(right, pieces) ? "왕과 룩이 아직 움직이지 않은 경우에 선택하세요. 실제 착수 가능 여부는 현재 합법 수로 확인합니다." : "해당 색의 왕과 룩을 원래 칸에 놓아야 선택할 수 있습니다.";
    }
    get("analysis-castle-actions").hidden = editing;
    for (const side of ["kingside","queenside"]) {
      const button = get("analysis-castle-"+side), reason = castlingReason(side);
      button.disabled = !castleMove(side);
      button.title = reason;
      get("analysis-castle-"+side+"-reason").textContent = reason;
    }
  }

  function castleMove(side) {
    if (!visible || editing || busy || failure || !snapshot?.active || promotion.length || get("analysis-promotion").open) return null;
    const rank = snapshot.turn === "white" ? "1" : snapshot.turn === "black" ? "8" : null;
    if (!rank) return null;
    const move = `e${rank}${side === "kingside" ? "g" : "c"}${rank}`;
    return snapshot.legal_moves.includes(move) ? move : null;
  }

  function castlingReason(side) {
    if (busy) return "착수를 반영하고 있습니다.";
    if (failure) return "연결 오류로 현재 캐슬링 가능 여부를 확인할 수 없습니다.";
    if (promotion.length || get("analysis-promotion").open) return "승격 선택을 마치거나 취소한 뒤 캐슬링할 수 있습니다.";
    if (editing || !snapshot?.active) return "분석을 시작한 뒤 현재 차례에서 캐슬링할 수 있습니다.";
    return snapshot.castling?.[side === "kingside" ? "king" : "queen"]?.reason ||
      (castleMove(side) ? "현재 차례에서 캐슬링할 수 있습니다." : "현재 위치의 합법 수에 이 캐슬링이 없습니다.");
  }

  function moveCandidates(from, to) {
    if (!from || !to || !snapshot?.active || failure || busy || promotion.length) return [];
    const direct = snapshot.legal_moves.filter((move) => move.startsWith(from+to));
    if (direct.length) return direct;
    const rank = snapshot.turn === "white" ? "1" : "8", king = "e"+rank;
    for (const side of ["kingside","queenside"]) {
      const rook = (side === "kingside" ? "h" : "a")+rank;
      if ((from === king && to === rook) || (from === rook && to === king)) {
        const kingPiece = snapshot.turn === "white" ? "K" : "k", rookPiece = snapshot.turn === "white" ? "R" : "r";
        const move = castleMove(side);
        if (move && snapshot.board[king] === kingPiece && snapshot.board[rook] === rookPiece) return [move];
      }
    }
    return [];
  }

  function movableSquare(square) {
    if (!visible || editing || busy || failure || !snapshot?.active || promotion.length || get("analysis-promotion").open) return false;
    if (snapshot.legal_moves.some((move) => move.startsWith(square))) return true;
    return moveCandidates(square,snapshot.turn === "white" ? "e1" : "e8").length > 0;
  }

  async function request(payload, path = "/api/analysis") {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path, {
        method: payload ? "POST" : "GET", cache:"no-store", signal:controller.signal,
        headers:{"X-Regicide-Token":token, ...(payload ? {"Content-Type":"application/json"} : {})},
        ...(payload ? {body:JSON.stringify(payload)} : {})
      });
      const result = await response.json();
      if (!response.ok) {
        if (result.analysis) accept(result.analysis);
        throw new Error(result.error || "분석 요청을 처리하지 못했습니다.");
      }
      return result;
    } catch (error) {
      if (error?.name === "AbortError")
        throw new Error("분석 요청 시간이 초과됐습니다. 연결을 확인하고 다시 시도해 주세요.");
      if (error instanceof TypeError)
        throw new Error("분석 관리기와 연결할 수 없습니다. 실행 창을 확인해 주세요.");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function accept(next) {
    if (snapshot && next.revision < snapshot.revision) return;
    if (snapshot?.revision !== next.revision) selected = null;
    snapshot = next;
  }

  function hintMove() {
    const hint = snapshot?.hint;
    return visible && !editing && !busy && snapshot?.active && hint?.enabled && hint.status === "ready" &&
      hint.game_id === snapshot.game_id && hint.revision === snapshot.revision && hint.fen === snapshot.fen &&
      snapshot.legal_moves.includes(hint.move) ? hint.move : null;
  }

  function evaluationView() {
    const unknown = (label, detail) => ({label, detail, score:"—", share:null, bounded:false});
    if (!visible || editing) return unknown("형세 분석 대기", "배치를 마치고 분석을 시작하세요.");
    if (busy) return unknown("새 위치 분석 대기", "현재 체스판의 새 평가를 기다립니다.");
    if (failure) return unknown("형세 분석 없음", "연결을 확인하고 현재 위치를 다시 분석해 주세요.");
    const value = snapshot?.evaluation;
    if (!snapshot?.active || !value || value.game_id !== snapshot.game_id || value.revision !== snapshot.revision ||
        value.fen !== snapshot.fen || (value.source_fen && value.source_fen !== snapshot.fen))
      return unknown("형세 분석 대기", "현재 체스판의 평가를 기다립니다.");
    if (value.status === "terminal" && ["1-0","0-1","1/2-1/2"].includes(value.result)) {
      const label = value.result === "1-0" ? "백 승리" : value.result === "0-1" ? "흑 승리" : "무승부";
      return {label, score:value.result, share:value.result === "1-0" ? 1 : value.result === "0-1" ? 0 : .5,
        detail:"현재 위치의 종료 판정", bounded:false};
    }
    if (value.status === "unavailable") return unknown("형세 분석 없음", "현재 위치의 평가값을 사용할 수 없습니다.");
    if (!["ready","searching"].includes(value.status)) return unknown("형세 분석 대기", "현재 체스판의 평가를 기다립니다.");
    const hasMate = Number.isInteger(value.mate), hasCp = Number.isFinite(value.cp);
    if (!hasMate && !hasCp) return unknown("형세 계산 중…", "첫 탐색 점수를 기다립니다.");
    const bound = value.bound === "lower" ? "lower" : value.bound === "upper" ? "upper" : "exact";
    const prefix = bound === "lower" ? "≥ " : bound === "upper" ? "≤ " : "";
    let label, score, share;
    if (hasMate) {
      const winner = value.winner === "white" || value.winner === "black" ? value.winner :
        value.mate === 0 ? (snapshot.turn === "white" ? "black" : "white") : value.mate > 0 ? "white" : "black";
      label = `${winner === "white" ? "백" : "흑"} ${value.mate === 0 ? "체크메이트" : "메이트 예상"}`;
      score = value.mate === 0 ? "M0" : `${value.mate > 0 ? "+" : "−"}M${Math.abs(value.mate)}`;
      share = winner === "white" ? 1 : 0;
    } else {
      label = value.cp > 0 ? "백 우세" : value.cp < 0 ? "흑 우세" : "균형";
      score = `${value.cp > 0 ? "+" : value.cp < 0 ? "−" : ""}${(Math.abs(value.cp) / 100).toFixed(2)}`;
      share = Number.isFinite(value.white_share) ? Math.max(0, Math.min(1, value.white_share)) :
        .5 + .5 * value.cp / (Math.abs(value.cp) + 400);
    }
    if (prefix) label = bound === "lower" ? "백 평가 하한" : "백 평가 상한";
    const detail = [];
    if (value.status === "searching") detail.push("계산 중");
    if (Number.isInteger(value.depth) && value.depth >= 0) detail.push(`탐색 깊이 ${value.depth}`);
    if (prefix) detail.push("경계값이며 확정 형세가 아닙니다");
    return {label, score:prefix + score, share, detail:detail.join(" · ") || "현재 위치의 탐색 평가", bounded:Boolean(prefix)};
  }

  function renderEvaluation() {
    const view = evaluationView(), bar = get("analysis-evaluation-bar");
    bar.classList.toggle("black-view", blackView);
    bar.classList.toggle("unknown", view.share === null);
    bar.classList.toggle("bounded", view.bounded);
    bar.setAttribute("aria-label", `${view.label}, ${view.score}. ${view.detail}. 자체 엔진 추정, 백 기준. 막대는 승률이 아닙니다.`);
    get("analysis-evaluation-white").style.height = `${(view.share ?? 0) * 100}%`;
    get("analysis-evaluation-top").textContent = blackView ? "백" : "흑";
    get("analysis-evaluation-bottom").textContent = blackView ? "흑" : "백";
    get("analysis-evaluation-label").textContent = view.label;
    get("analysis-evaluation-score").textContent = view.score;
    get("analysis-evaluation-detail").textContent = view.detail;
  }

  function renderBoard() {
    const pieces = editing ? draft : snapshot?.board || {};
    const files = blackView ? "hgfedcba" : "abcdefgh", ranks = blackView ? "12345678" : "87654321";
    const legal = editing ? [] : snapshot?.legal_moves || [];
    const targets = selected ? legal.filter((m) => m.startsWith(selected)).map((m) => m.slice(2,4)) : [];
    const recommended = hintMove(), fragment = document.createDocumentFragment();
    const focus = document.activeElement?.dataset?.analysisSquare;
    for (const rank of ranks) for (const file of files) {
      const square = file+rank, piece = pieces[square], button = document.createElement("button");
      button.type = "button"; button.className = "square"; button.dataset.analysisSquare = square;
      button.draggable = Boolean(piece) && (editing ? visible && !busy : movableSquare(square));
      if ((file.charCodeAt(0)-97+Number(rank))%2 === 1) button.classList.add("dark");
      if (selected === square) button.classList.add("selected");
      if (targets.includes(square)) button.classList.add("legal-target");
      if (piece) button.classList.add("occupied");
      if (recommended?.slice(0,2) === square) button.classList.add("hint-from");
      if (recommended?.slice(2,4) === square) button.classList.add("hint-to");
      button.disabled = busy || (!editing && !snapshot?.active);
      button.setAttribute("aria-label", `${square}, ${piece ? (piece === piece.toUpperCase() ? "백 " : "흑 ") + names[piece.toLowerCase()] : "빈 칸"}`);
      if (piece) {
        const span = document.createElement("span"); span.className = "piece " + (piece === piece.toUpperCase() ? "white" : "black");
        span.textContent = symbols[piece]; button.append(span);
      }
      if (file === files[0] || rank === ranks[7]) {
        for (const [show, value, klass] of [[file === files[0],rank,"rank-coordinate"],[rank === ranks[7],file,"file-coordinate"]]) {
          if (!show) continue;
          const label = document.createElement("span"); label.className = "coordinate " + klass; label.textContent = value; button.append(label);
        }
      }
      fragment.append(button);
    }
    if (window.RegicideMotion) window.RegicideMotion.replace(get("analysis-board"), fragment, {
      scope:snapshot?.game_id, ply:snapshot?.moves?.length || 0, pieces, turn:snapshot?.turn,
      lastMove:snapshot?.last_move, legalMoves:snapshot?.legal_moves,
      orientation:blackView ? "black" : "white", enabled:visible && !editing
    });
    else get("analysis-board").replaceChildren(fragment);
    if (recommended) {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 800 800"); svg.setAttribute("class", "analysis-arrow"); svg.setAttribute("aria-hidden", "true");
      const point = (square) => [files.indexOf(square[0])*100+50, ranks.indexOf(square[1])*100+50];
      const [x1,y1] = point(recommended.slice(0,2)), [x2,y2] = point(recommended.slice(2,4));
      const angle = Math.atan2(y2-y1,x2-x1), tipX = x2-Math.cos(angle)*13, tipY = y2-Math.sin(angle)*13;
      const line = document.createElementNS(svg.namespaceURI, "line");
      for (const [key,value] of Object.entries({x1,y1,x2:tipX,y2:tipY})) line.setAttribute(key, String(value));
      const head = document.createElementNS(svg.namespaceURI, "polygon");
      head.setAttribute("points", `${tipX},${tipY} ${tipX-Math.cos(angle-.5)*40},${tipY-Math.sin(angle-.5)*40} ${tipX-Math.cos(angle+.5)*40},${tipY-Math.sin(angle+.5)*40}`);
      svg.append(line,head); get("analysis-board").append(svg);
    }
    if (focus) get("analysis-board").querySelector(`[data-analysis-square="${focus}"]`)?.focus();
  }

  function render() {
    window.RegicideExplorer?.update("analysis", {fen:snapshot?.fen, active:Boolean(visible && !editing && !busy && snapshot?.active)});
    if (!visible || busy || (drag && !validDrag())) cancelDrag();
    returnNotice.textContent = returnFailure;
    returnNotice.hidden = visible || !returnFailure;
    get("analysis-view").hidden = !visible; get("live-view").hidden = visible;
    get("live-mode-button").setAttribute("aria-pressed", String(!visible));
    get("analysis-mode-button").setAttribute("aria-pressed", String(visible));
    get("analysis-title").textContent = editing ? "분석 모드 · 기물 배치" : `분석 모드 · ${snapshot?.turn === "black" ? "흑" : "백"} 차례`;
    get("analysis-status").textContent = busy ? "배치를 확인하고 있습니다…" : editing ? "기물을 고른 뒤 원하는 칸을 누르세요. 지우개로 기물을 제거할 수 있습니다." : snapshot?.message || "분석 중";
    get("analysis-error").textContent = failure; get("analysis-error").hidden = !failure;
    const move = hintMove(), hint = snapshot?.hint;
    get("analysis-hint").textContent = editing ? "배치를 마치고 분석을 시작하세요." : move ?
      `${hint.san || move} · ${move.slice(0,2)} → ${move.slice(2,4)}${move[4] ? ` · ${names[move[4]]}으로 승격` : ""}${hint.depth != null ? ` · 깊이 ${hint.depth}` : ""}` :
      busy ? "새 위치를 기다리고 있습니다…" : snapshot?.active ? (hint?.message || "Regicide가 추천 수를 찾고 있습니다…") : snapshot?.message || "추천할 수가 없습니다.";
    get("analysis-apply").disabled = !move;
    get("analysis-start").disabled = busy || !editing || !snapshot;
    get("analysis-undo").disabled = busy || editing || !snapshot?.can_undo;
    get("analysis-edit").disabled = busy || editing;
    get("analysis-time").disabled = busy || !editing;
    for (const id of ["analysis-turn","analysis-castling","analysis-ep","analysis-import","analysis-standard","analysis-clear","analysis-current"])
      get(id).disabled = busy || !editing;
    get("analysis-fen").readOnly = busy || !editing;
    get("analysis-trash").hidden = !editing;
    get("analysis-editor").hidden = !editing;
    if (!editing && snapshot) get("analysis-fen").value = snapshot.fen;
    get("analysis-moves").textContent = editing ? "" : (snapshot?.moves || []).map((item) => item.san).join("  ");
    for (const button of get("analysis-palette").children) {
      button.disabled = busy || !editing;
      button.draggable = visible && editing && !busy && Boolean(button.dataset.piece);
      button.setAttribute("aria-pressed", String(button.dataset.piece === brush));
    }
    get("analysis-brush-label").textContent = brush ? `선택: ${brush === brush.toUpperCase() ? "백" : "흑"} ${names[brush.toLowerCase()]}` : "선택: 지우개 · 누른 칸의 기물을 지웁니다.";
    renderCastling();
    renderEvaluation();
    // Keep the native drag source in the DOM while a same-position score poll arrives.
    if (!drag) renderBoard();
  }

  async function action(type, extra = {}) {
    if (busy || !snapshot) return false;
    cancelDrag();
    busy = true; failure = ""; selected = null; promotion = []; ++epoch;
    if (get("analysis-promotion").open) get("analysis-promotion").close();
    render();
    try { accept(await request({action:type, revision:snapshot.revision, ...extra})); return true; }
    catch (error) { failure = error.message || "분석 프로그램과 연결을 확인해 주세요."; return false; }
    finally { busy = false; render(); }
  }

  async function enter() {
    if (visible || busy) return;
    cancelDrag();
    returnFailure = "";
    visible = true; editing = true; busy = true; failure = ""; const ticket = ++epoch; render();
    window.dispatchEvent(new CustomEvent("regicide-analysis-mode", {detail:true}));
    try { const next = await request(); if (ticket === epoch) accept(next); }
    catch (error) { failure = error.message; }
    finally { busy = false; render(); }
  }

  async function leave() {
    if (!visible || busy) return;
    cancelDrag();
    ++epoch;
    if (snapshot?.active && !await action("stop"))
      returnFailure = "분석 종료를 확인하지 못했습니다. 대국 화면으로 돌아왔습니다. " + failure;
    ++epoch; visible = false; selected = null; render();
    window.dispatchEvent(new CustomEvent("regicide-analysis-mode", {detail:false}));
  }

  async function poll() {
    if (!visible || editing || busy || polling || !snapshot?.active) return;
    const ticket = epoch; polling = true;
    try { const next = await request(); if (ticket === epoch && visible && !editing && !busy) { failure = ""; accept(next); render(); } }
    catch (error) { if (ticket === epoch) { failure = error.message; if(snapshot) snapshot.hint = null; render(); } }
    finally { polling = false; }
  }

  async function squareClick(square) {
    if (busy) return;
    if (editing) {
      if ((draft[square] || "") === brush) return;
      const before = {...draft};
      if (brush) draft[square] = brush; else delete draft[square];
      changedDraft(before,[square]); return;
    }
    if (!snapshot?.active) return;
    const moves = moveCandidates(selected,square);
    if (selected && moves.length) {
      if (moves[0].length === 5) { promotion = moves; renderPromotion(); return; }
      await action("move", {move:moves[0]}); return;
    }
    selected = selected !== square && movableSquare(square) ? square : null;
    render();
  }

  function renderPromotion() {
    const choices = get("analysis-promotion-choices"); choices.replaceChildren();
    for (const move of promotion) {
      const button = document.createElement("button"); button.className = "promotion-choice";
      button.textContent = names[move[4]]; button.addEventListener("click", () => { promotion = []; get("analysis-promotion").close(); void action("move",{move}); });
      choices.append(button);
    }
    get("analysis-promotion").showModal();
    render();
  }

  for (const piece of [..."KQRBNPkqrbnp", ""]) {
    const button = document.createElement("button"); button.type = "button"; button.dataset.piece = piece;
    button.className = "analysis-palette-piece"; button.textContent = symbols[piece] || "지우개";
    button.setAttribute("aria-label", piece ? `${piece === piece.toUpperCase() ? "백" : "흑"} ${names[piece.toLowerCase()]}` : "지우개");
    button.addEventListener("click", () => { if (!busy && editing && !drag && Date.now() >= suppressClickUntil) { brush = piece; render(); } });
    get("analysis-palette").append(button);
  }
  get("analysis-mode-button").addEventListener("click", () => { void enter(); });
  window.addEventListener("regicide-explorer-analyze", async (event) => {
    const fen = event.detail?.fen;
    if (typeof fen !== "string" || busy) return;
    try { readFen(fen); } catch (_) { return; }
    if (!visible) await enter();
    if (!visible || busy || !snapshot) return;
    if (await action("start", {fen,movetime_ms:Number(get("analysis-time").value)})) {
      editing = false; render();
      get("analysis-view").scrollIntoView?.({block:"start", behavior:"instant"});
    }
  });
  get("live-mode-button").addEventListener("click", () => { void leave(); });
  get("analysis-flip").addEventListener("click", () => { cancelDrag(); blackView = !blackView; render(); });
  get("analysis-board").addEventListener("click", (event) => {
    if (drag || Date.now() < suppressClickUntil) return;
    const button = event.target.closest("button[data-analysis-square]");
    if (button) void squareClick(button.dataset.analysisSquare);
  });
  get("analysis-palette").addEventListener("dragstart", (event) => beginDrag(event, "palette"));
  get("analysis-board").addEventListener("dragstart", (event) => beginDrag(event, "board"));
  get("analysis-board").addEventListener("dragover", (event) => {
    if (!validDrag()) return;
    const square = event.target.closest("button[data-analysis-square]");
    hoverDrag(event, square); event.stopPropagation();
  });
  get("analysis-board").addEventListener("drop", (event) => {
    const square = event.target.closest("button[data-analysis-square]");
    if (square) dropDrag(event, square.dataset.analysisSquare);
    else if (validDrag()) { event.preventDefault(); event.stopPropagation(); cancelDrag(); render(); }
  });
  get("analysis-view").addEventListener("dragover", (event) => hoverDrag(event, null));
  get("analysis-view").addEventListener("drop", (event) => {
    if (get("analysis-board").contains(event.target)) { cancelDrag(); render(); return; }
    dropDrag(event);
  });
  // Escape, release outside this view/window, and invalid native drops cancel.
  // Off-board deletion is available only in the setup editor.
  get("analysis-view").addEventListener("dragend", () => { cancelDrag(); render(); });
  get("analysis-standard").addEventListener("click", () => { loadDraft(initialFen); failure = ""; render(); });
  get("analysis-clear").addEventListener("click", () => { loadDraft("8/8/8/8/8/8/8/8 w - - 0 1"); failure = ""; render(); });
  get("analysis-current").addEventListener("click", async () => {
    if (busy || !editing) return;
    cancelDrag();
    busy = true; render();
    try { const current = await request(undefined, "/api/state"); loadDraft(current.fen); failure = ""; }
    catch(error) { failure = error.message; } finally { busy = false; render(); }
  });
  get("analysis-import").addEventListener("click", () => { try { loadDraft(get("analysis-fen").value); failure = ""; } catch(error) { failure = error.message; } render(); });
  for (const id of ["analysis-turn","analysis-castling","analysis-ep"]) get(id).addEventListener("change", () => { cancelDrag(); updateFen(); render(); });
  for (const right of Object.keys(castlingRights)) get("analysis-right-" + right).addEventListener("change", () => {
    if (!visible || !editing || busy || !castlingPiecesPresent(right)) { renderCastling(); return; }
    cancelDrag();
    get("analysis-castling").value = Object.keys(castlingRights).filter((key) => get("analysis-right-" + key).checked && castlingPiecesPresent(key)).join("") || "-";
    updateFen(); render();
  });
  get("analysis-start").addEventListener("click", async () => {
    if(busy || !editing) return;
    try { loadDraft(get("analysis-fen").value); }
    catch(error) { failure = error.message; render(); return; }
    if (await action("start", {fen:updateFen(),movetime_ms:Number(get("analysis-time").value)})) { editing = false; render(); }
  });
  get("analysis-apply").addEventListener("click", () => { const move = hintMove(); if (move) void action("move",{move}); });
  get("analysis-undo").addEventListener("click", () => { void action("undo"); });
  for (const side of ["kingside","queenside"]) get("analysis-castle-" + side).addEventListener("click", () => {
    const move = castleMove(side); if (move) void action("move",{move});
  });
  get("analysis-edit").addEventListener("click", async () => { if(await action("stop")) { loadDraft(snapshot.fen); editing = true; render(); } });
  get("analysis-promotion-cancel").addEventListener("click", () => { promotion = []; get("analysis-promotion").close(); render(); });
  get("analysis-promotion").addEventListener("close", () => { promotion = []; render(); });
  const timer = setInterval(() => { void poll(); }, 300);
  window.addEventListener("pagehide", () => { cancelDrag(); ++epoch; visible = false; clearInterval(timer); });
  loadDraft(initialFen);
  render();
})();
