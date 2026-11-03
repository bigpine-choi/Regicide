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
  let ratingInputGame = null;
  let ratingSavedValue = null;
  let clockPollInFlight = false;
  let clockReceivedAt = 0;
  let clockAdjustSaving = false;
  let clockAdjustExpanded = false;
  let clockAdjustGame = null;
  let clockAdjustNotice = "";
  const clockSteps = [["minus-minute", -60000], ["minus-ten", -10000], ["plus-ten", 10000], ["plus-minute", 60000]];
  const monotonicNow = () => typeof performance !== "undefined" ? performance.now() : Date.now();
  const rapidControls = {"10+0": "각자 10분 · 추가 시간 없음", "10+5": "각자 10분 · 착수마다 5초 추가", "15+10": "각자 15분 · 착수마다 10초 추가"};

  function clearHint() {
    hintEpoch++;
    hint = null;
  }

  function showError(message) {
    errorMessage = message || "";
    byId("error-text").textContent = errorMessage;
    byId("error-panel").hidden = !errorMessage;
  }

  async function request(path, payload, signal, preserveSelection = false, expectedGame = null) {
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
      if (data.state && (expectedGame === null || (!closed && state?.game_id === expectedGame && data.state.game_id === expectedGame))) {
        if (expectedGame === null) applyState(data.state, preserveSelection);
        else applyClockRefresh(data.state);
      }
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
    if (unchangedPosition && state.clock && next.clock && !newerClock(next.clock, state.clock)) {
      next.clock = state.clock;
      next.decision_support = state.decision_support;
      next.engine.pacing = state.engine.pacing;
    } else {
      clockReceivedAt = nextClockAnchor(next.clock, state?.clock, unchangedPosition);
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

  function evaluationScoreNote(value) {
    let text = "평가 대기";
    if (["ready", "searching"].includes(value?.status)) {
      if (Number.isInteger(value.mate)) text = `M${value.mate}`;
      else if (Number.isInteger(value.cp)) {
        text = `${value.cp > 0 ? "+" : value.cp < 0 ? "−" : ""}${(Math.abs(value.cp) / 100).toFixed(2)}`;
        if (value.bound === "lower") text = "≥" + text;
        if (value.bound === "upper") text = "≤" + text;
      }
    }
    return text + (Number.isInteger(value?.depth) ? ` (깊이 ${value.depth})` : "");
  }

  function refereePresentation() {
    const unchanged = {value:evaluation, prefix:"", note:"Stockfish 평가 · 수는 Regicide가 선택합니다. 백 기준 점수 · 승률이 아닙니다."};
    const meta = evaluation?.stockfish_reference ?? state?.engine?.stockfish_reference ??
      evaluation?.sparring_referee ?? state?.engine?.sparring_referee ?? {enabled:true,available:true,status:"loading"};
    if (analysisMode || coachingMode || state?.result !== "*" || evaluation?.status === "terminal" ||
        !meta) return unchanged;
    const reference = evaluation?.reference_evaluation;
    const identity = samePosition(state, reference) && reference.turn === state.turn;
    const ready = meta.enabled === true && meta.available === true && identity && reference.source === "stockfish_gauge" && reference.status === "ready" && reference.bound === "exact" &&
      Number.isInteger(reference.depth) && reference.depth > 0 && (Number.isInteger(reference.cp) || Number.isInteger(reference.mate));
    const own = `Regicide 자체 ${evaluationScoreNote(evaluation)}`;
    if (ready) return {value:reference, prefix:"형세 · Stockfish 심판 · ", reference:true,
      note:`${reference.engine_name || meta.name || "Stockfish"} 보조 심판 · ${own} · 백 기준 점수 · 승률이나 정답의 보장이 아닙니다.`};
    let warning = "Stockfish 형세 계산 중";
    const unavailable = meta.enabled !== true || meta.available !== true || ["disabled", "unavailable", "error"].includes(reference?.status);
    if (unavailable) warning = "Stockfish 평가 사용 불가";
    else if (reference && !identity) warning = "다른 위치의 심판 평가 제외";
    else if (reference?.status === "ready" && reference.bound !== "exact") warning = "심판 범위 평가 제외";
    return {value:null, pending:warning, note:`${warning} · 현재 위치의 Stockfish 평가를 기다립니다. 수는 Regicide가 선택합니다.`};
  }

  function evaluationView() {
    const selected = refereePresentation();
    const value = selected.value;
    const note = selected.note;
    const result = state?.result !== "*" ? state?.result : evaluation?.status === "terminal" ? evaluation.result : "*";
    if (result && result !== "*") {
      const label = result === "1-0" ? "백 승리" : result === "0-1" ? "흑 승리" : result === "1/2-1/2" ? "무승부" : "대국 종료";
      const share = result === "1-0" ? 1 : result === "0-1" ? 0 : result === "1/2-1/2" ? .5 : null;
      return {label, score:result, detail:"확정된 대국 결과", share};
    }
    if (closed) return {label:"대국 종료", score:"—", detail:note, share:null};
    if (selected.pending) return {label:selected.pending, score:"—", detail:note, share:null};
    if (!value || value.status === "waiting") {
      return {label:"분석 대기 중", score:"—", detail:note, share:null};
    }
    if (value.status === "unavailable") {
      return {label:"평가를 사용할 수 없습니다", score:"—", detail:"대국은 계속할 수 있습니다. " + note, share:null};
    }
    const hasCp = Number.isFinite(value.cp);
    const hasMate = Number.isInteger(value.mate);
    if (!hasCp && !hasMate) {
      return {label:"분석 중…", score:"—", detail:note, share:null};
    }
    const prefix = value.bound === "lower" ? "≥ " : value.bound === "upper" ? "≤ " : "";
    let score;
    let label;
    if (hasMate) {
      score = value.mate === 0 ? "M0" : `${value.mate > 0 ? "+" : "−"}M${Math.abs(value.mate)}`;
      label = value.mate === 0 ? (value.winner ? `${colorName(value.winner)} 체크메이트 승리` : "체크메이트 평가") : `${value.mate > 0 ? "백" : "흑"} 메이트 예상`;
    } else {
      score = `${value.cp > 0 ? "+" : value.cp < 0 ? "−" : ""}${(Math.abs(value.cp) / 100).toFixed(2)}`;
      label = value.cp > 0 ? "백 우세" : value.cp < 0 ? "흑 우세" : "균형";
    }
    // A bound is not an exact assessment of which side is ahead.
    if (prefix) label = value.bound === "lower" ? "백 평가 하한" : "백 평가 상한";
    const details = [];
    if (value.status === "searching") details.push("분석 중");
    if (value.status === "searching" && value.refinement?.phase === "refining") details.push("정밀 분석 중");
    if (value.source === "previous_search") details.push("직전 탐색 평가");
    if (Number.isInteger(value.depth)) details.push(`탐색 깊이 ${value.depth}`);
    if (prefix) details.push("확정 평가가 아닌 탐색 경계값");
    const reliabilityLabels = {provisional: "잠정 평가", stable: "탐색 안정", volatile: "평가 변동 큼", bounded: "범위 평가"};
    const reliabilityDetails = {
      provisional: "탐색이 더 진행되면 평가가 달라질 수 있습니다.",
      stable: "최근 탐색값이 비슷합니다. 정답을 보장하지는 않습니다.",
      volatile: "최근 탐색에서 평가가 크게 달라졌습니다.",
      bounded: "상한·하한 형태로 얻은 탐색 평가입니다."
    };
    if (Object.hasOwn(reliabilityLabels, value.reliability)) {
      label += ` · ${reliabilityLabels[value.reliability]}`;
      details.push(reliabilityLabels[value.reliability], reliabilityDetails[value.reliability]);
    }
    details.push(note);
    let share = Number.isFinite(value.white_share) ? Math.max(0, Math.min(1, value.white_share)) : null;
    if (selected.reference && share === null) {
      const mateWinner = value.winner || (value.turn === "white" ? "black" : "white");
      share = hasMate ? (value.mate > 0 || (value.mate === 0 && mateWinner === "white") ? 1 : 0) : .5 + .5 * value.cp / (Math.abs(value.cp) + 400);
    }
    return {label:selected.prefix + label, score:prefix + score, detail:details.join(" · "), share};
  }

  function renderEvaluation() {
    const view = evaluationView();
    const blackView = state?.human_color === "black";
    const bar = byId("evaluation-bar");
    bar.classList.toggle("black-view", blackView);
    bar.classList.toggle("unknown", view.share === null);
    bar.setAttribute("aria-label", `${view.label}, ${view.score}. ${view.detail}`);
    bar.title = `${view.label}, ${view.score}. ${view.detail}`;
    byId("evaluation-white").style.height = `${(view.share ?? .5) * 100}%`;
    byId("evaluation-top").textContent = blackView ? "백" : "흑";
    byId("evaluation-bottom").textContent = blackView ? "흑" : "백";
    byId("evaluation-label").textContent = view.label;
    byId("evaluation-score").textContent = view.score;
    byId("evaluation-detail").textContent = view.detail;
    byId("evaluation-detail").title = view.detail;
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
    byId("hint-panel").hidden = !hintEnabled;
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

  function renderModeHelp() {
    const descriptions = {
      normal: "기본 기풍으로 두며, 남은 시간과 국면에 맞춰 생각 시간을 조절합니다. 새 대국부터 적용됩니다.",
      boss: "갬빗 오프닝을 적극적으로 선택하고, 성립 후 전개·중앙 돌파·왕 압박을 이어갑니다. 기물 미끼는 상대의 포획 뒤 응징을 검증한 경우에만 선택합니다. 새 대국부터 적용됩니다.",
      sparring: "20수까지 ±1점 범위를 목표로 형세를 조절하고, 21~49수에는 매 수 일정한 폭으로 강도를 높여 50번째 수부터 전력으로 둡니다. 20수까지 형세가 벌어지면 균형 회복에 계산 시간을 우선 배분합니다. 생각시간은 자동으로 조절합니다. 새 대국부터 적용됩니다."
    };
    const mode = byId("boss-mode").value;
    byId("boss-mode-help").textContent = Object.hasOwn(descriptions, mode) ? descriptions[mode] : descriptions.normal;
    const referee = state?.engine?.sparring_referee;
    const sparringOption = byId("boss-mode").querySelector('option[value="sparring"]');
    if (sparringOption) sparringOption.textContent = "스파링 모드";
    if (referee && typeof referee.available === "boolean") {
      if (sparringOption) sparringOption.textContent = referee.available ? "스파링 · Stockfish 심판" : "스파링 · 자체 평가";
      if (mode === "sparring") byId("boss-mode-help").textContent += referee.available ?
        " 수는 Regicide가 선택하고 Stockfish가 보조 심판을 맡습니다." : " Stockfish 심판을 사용할 수 없어 Regicide 자체 평가를 사용합니다.";
    }
    byId("boss-mode-help").title = mode === "sparring" ? "20수는 백과 흑이 각각 20번 둔 시점입니다. 21~49수에 매 수 일정한 폭으로 강도를 높이고, 양쪽 모두 50번째 수부터 전력으로 둡니다." : "";
    renderThinkTimeControls();
  }

  function usesAutoThinkTime() {
    if (state) {
      if (state.clock?.enabled === true) return true;
      if (typeof state.engine?.automatic_think_time === "boolean") return state.engine.automatic_think_time;
      if (typeof state.engine?.time_management === "string") return state.engine.time_management === "automatic";
      if (typeof state.clock?.enabled === "boolean") return state.clock.enabled;
      return state.engine?.sparring_mode === true;
    }
    return Object.hasOwn(rapidControls, byId("rapid-time-control").value);
  }

  function renderThinkTimeControls() {
    const automatic = usesAutoThinkTime();
    const ready = Boolean(state) && !closed;
    byId("think-time-field").hidden = automatic;
    byId("think-time").hidden = automatic;
    byId("think-time-label").textContent = automatic ? "AI 시간 관리" : "Regicide가 한 수를 생각할 시간";
    byId("auto-think-time").hidden = !automatic;
    byId("think-time").disabled = automatic || !ready || timeSaving || state?.handicap_mode || (busy && !state?.engine_to_move);
    if (state && !timeSaving && [100, 1000, 3000, 5000].includes(state.engine.movetime_ms)) byId("think-time").value = String(state.engine.movetime_ms);
    if (automatic) {
      byId("think-time-help").textContent = "기본·갬빗·스파링 모두 사람 권장시간을 바탕으로 익숙하거나 명백한 수는 빠르게, 복잡한 국면은 더 고민해 둡니다. 남은 시간이 5분 이하부터는 점차 빠르게 둡니다.";
      return;
    }
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
  }

  function renderSparringRating() {
    const rating = state?.engine.rating ?? state?.engine.sparring_rating;
    const supported = rating && rating.target === "chess_com_rapid";
    const label = byId("sparring-rating");
    label.hidden = !supported || closed || analysisMode || coachingMode;
    label.textContent = "";
    label.title = "내 수를 바탕으로 한 실험용 추정이며 공식 레이팅이 아닙니다. 검증된 수 분석이 있을 때만 갱신합니다.";
    const validEstimate = supported && ["provisional_uncalibrated", "provisional_calibrated"].includes(rating.status) &&
      Number.isInteger(rating.estimate) && rating.estimate >= 100 && rating.estimate <= 4000;
    if (validEstimate) {
      const delta = Number.isInteger(rating.delta) ? ` (${rating.delta > 0 ? "+" : ""}${rating.delta})` : "";
      label.textContent = `추정 래피드 ~${rating.estimate}${delta} · 실험`;
      label.title += " 괄호는 마지막으로 분석한 내 수의 변화입니다.";
      if (rating.method === "regicide-sparring-rating-v2") {
        if (rating.evidence_level === "low") label.textContent = `추정 래피드 ~${rating.estimate}${delta} · 표본 적음`;
        if (Number.isInteger(rating.observations) && rating.observations >= 0 &&
            Number.isInteger(rating.calibration_games) && rating.calibration_games >= 0) {
          label.title += ` 분석한 수 ${rating.observations}개 · 실제 점수 보정 ${rating.calibration_games}판. 쉬운 정답과 얕은 분석은 적게 반영합니다.`;
        }
        if (rating.calibration_games === 0) label.title += " 실제 점수로 보정하기 전에는 Chess.com 레이팅과 크게 다를 수 있습니다.";
      }
    } else if (supported) {
      label.textContent = `추정 래피드 · ${engineThinking || state.engine_thinking ? "분석 중" : "표본 부족"}`;
    }
    const terminal = supported && ["1-0", "0-1", "1/2-1/2"].includes(state.result) && !state.handicap_mode && !closed;
    byId("rating-label-panel").hidden = !terminal;
    const saved = supported && Number.isInteger(rating.label?.rating) && rating.label.rating >= 100 && rating.label.rating <= 4000 ? rating.label.rating : null;
    if (ratingInputGame !== state?.game_id || ratingSavedValue !== saved) {
      byId("rating-label-input").value = saved == null ? "" : String(saved);
      ratingInputGame = state?.game_id;
      ratingSavedValue = saved;
    }
    byId("rating-label-input").disabled = !terminal || busy || timeSaving;
    byId("rating-label-save").disabled = !terminal || busy || timeSaving;
    byId("rating-label-save").textContent = saved == null ? "레이팅 저장" : "레이팅 수정";
    byId("rating-label-note").textContent = saved == null ? "실제 레이팅을 기록합니다. 다음 대국 추정에는 보정 가능한 대국만 반영합니다." :
      rating.label.eligible_for_calibration === false ? `${saved} 저장됨 · 이번 대국은 추정 보정에 쓰지 않습니다. 수정할 수 있습니다.` :
      `${saved} 저장됨 · 수정할 수 있습니다. 다음 대국 추정에는 보정 가능한 대국만 반영합니다.`;
  }

  function nextClockAnchor(incoming, previous, sameTurn) {
    const now = monotonicNow();
    // Server and client monotonic origins differ. Only compare their deltas.
    // A delayed fresher packet must not put already elapsed seconds back.
    if (sameTurn && previous?.status === "running" && incoming?.status === "running" &&
        previous.running_color === incoming.running_color &&
        Number.isFinite(previous.sampled_monotonic_ms) && Number.isFinite(incoming.sampled_monotonic_ms)) {
      return Math.min(now, clockReceivedAt + Math.max(0, incoming.sampled_monotonic_ms - previous.sampled_monotonic_ms));
    }
    return now;
  }

  function newerClock(incoming, previous) {
    const incomingEdit = Number(incoming?.edit_revision || 0);
    const previousEdit = Number(previous?.edit_revision || 0);
    if (incomingEdit !== previousEdit) return incomingEdit > previousEdit;
    return Number(incoming?.sampled_monotonic_ms ?? incoming?.turn_elapsed_ms ?? 0) >
      Number(previous?.sampled_monotonic_ms ?? previous?.turn_elapsed_ms ?? 0);
  }

  function clockText(ms) {
    if (!Number.isFinite(ms)) return "—";
    ms = Math.max(0, ms);
    if (ms > 0 && ms < 10000) return `0:${(Math.ceil(ms / 100) / 10).toFixed(1).padStart(4, "0")}`;
    const seconds = Math.ceil(ms / 1000);
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function clockRemaining(color) {
    const clock = state?.clock;
    let remaining = Number(clock?.[`${color}_ms`] ?? 0);
    if (!closed && clock?.status === "running" && clock.running_color === color) {
      remaining -= Math.max(0, monotonicNow() - clockReceivedAt);
    }
    return Math.max(0, remaining);
  }

  function canAdjustClock() {
    return Boolean(state && !closed && !analysisMode && !coachingMode &&
      !state.handicap_mode && state.result === "*" &&
      state.clock?.enabled === true && state.clock.editable === true);
  }

  function renderClockAdjustment() {
    if (clockAdjustGame !== state?.game_id) {
      clockAdjustGame = state?.game_id;
      clockAdjustExpanded = false;
      clockAdjustNotice = "";
    }
    byId("clock-adjust").hidden = !canAdjustClock();
    byId("clock-adjust-panel").hidden = !clockAdjustExpanded;
    byId("clock-adjust-toggle").setAttribute("aria-expanded", String(clockAdjustExpanded));
    byId("clock-adjust-toggle").disabled = !canAdjustClock();
    const human = state?.human_color || "white";
    for (const [side, color] of [["human", human], ["engine", human === "white" ? "black" : "white"]]) {
      const remaining = clockRemaining(color);
      byId(`clock-adjust-${side}-time`).textContent = clockText(remaining);
      for (const [name, delta] of clockSteps) {
        byId(`clock-${side}-${name}`).disabled = !canAdjustClock() || clockAdjustSaving ||
          (delta < 0 && remaining <= 0) || (delta > 0 && remaining >= 86400000);
      }
    }
    byId("clock-adjust-notice").textContent = clockAdjustSaving ? "시간을 반영하고 있어요…" :
      state?.result !== "*" && state ? "대국이 끝나 시간 조정을 마쳤습니다." : clockAdjustNotice;
  }

  async function adjustClock(side, delta) {
    if (!canAdjustClock() || clockAdjustSaving || !["human", "engine"].includes(side) ||
        !clockSteps.some(([, step]) => step === delta)) return;
    const requestedGame = state.game_id;
    const clockRevision = Number(state.clock.edit_revision || 0);
    clockAdjustSaving = true;
    clockAdjustNotice = "";
    showError("");
    renderClockAdjustment();
    try {
      const next = await request("/api/clock-adjust", {
        game_id: requestedGame, clock_revision: clockRevision, side, delta_ms: delta
      }, undefined, true, requestedGame);
      if (!closed && state?.game_id === requestedGame && next.game_id === requestedGame) {
        applyClockRefresh(next);
        clockAdjustNotice = `${side === "human" ? "내" : "Regicide의"} 시간을 조정했습니다.`;
      }
    } catch (error) {
      if (!closed && state?.game_id === requestedGame) showError(error.message);
    } finally {
      clockAdjustSaving = false;
      renderClockAdjustment();
    }
  }

  function renderClockAndAdvice() {
    renderClockAdjustment();
    const clock = state?.clock;
    const human = state?.human_color || "white";
    const humanOvertime = clock?.timeout_policy === "engine_only" && clock.overtime_color === human && state?.result === "*";
    for (const [id, color] of [["human-clock", human], ["opponent-clock", human === "white" ? "black" : "white"]]) {
      const element = byId(id);
      element.hidden = clock?.enabled !== true && (id !== "human-clock" || !state);
      element.classList.toggle("untimed", clock?.enabled !== true);
      if (clock?.enabled !== true) {
        element.textContent = "새 대국에서 시간 선택";
        element.title = "현재 대국은 시간 제한 없이 진행합니다.";
        element.setAttribute("aria-label", "현재 대국은 시간 제한 없음 · 새 대국에서 시간 선택");
        element.classList.toggle("running", false);
        element.classList.toggle("low-time", false);
        continue;
      }
      const running = !closed && clock?.status === "running" && clock.running_color === color;
      const remaining = clockRemaining(color);
      element.textContent = `${running ? "▶ " : ""}${clockText(remaining)}`;
      element.classList.toggle("running", running);
      element.classList.toggle("low-time", remaining <= 30000);
      element.setAttribute("aria-label", `${colorName(color)} 남은 시간 ${clockText(remaining)}${humanOvertime && color === human ? " · 내 시간 소진 · 계속 진행" : ""}`);
      element.title = (clock?.label || "") + (humanOvertime && color === human ? " · 내 시간 소진 · 계속 진행" : "");
    }
    byId("rapid-time-help").textContent = `${rapidControls[byId("rapid-time-control").value] || rapidControls["10+0"]}. 새 대국부터 적용됩니다.`;
    const panel = byId("decision-support");
    panel.hidden = !state || closed || analysisMode || coachingMode;
    const support = state?.decision_support;
    const complexity = support?.complexity;
    const ready = support?.status === "ready" && state?.human_to_move && state?.result === "*";
    byId("human-complexity").textContent = state?.result !== "*" && state ? "대국 종료 · 시간 안내 마침" :
      !state?.human_to_move && state ? "상대 차례 · 다음 판단 준비" : "사람 기준 복잡도 · 대기";
    byId("complexity-reasons").textContent = support?.note || "내 차례에 판단할 요소와 생각 시간을 안내합니다.";
    byId("recommended-time").textContent = humanOvertime ? "내 시간 소진 · 계속 진행" : "";
    byId("time-recommendation-reason").textContent = humanOvertime ? "시간이 없어도 수를 둘 수 있습니다. Regicide의 시간 제한은 유지됩니다." : "";
    byId("recommended-time").classList.toggle("low-time", humanOvertime);
    if (!ready) return;
    if (Number.isInteger(complexity?.score) && complexity.score >= 0 && complexity.score <= 100) {
      byId("human-complexity").textContent = `사람 기준 복잡도 · ${complexity.label} ${complexity.score}/100 · 실험`;
    }
    byId("complexity-reasons").textContent = Array.isArray(complexity?.reasons) ? complexity.reasons.slice(0, 3).join(" · ") : "";
    if (humanOvertime) return;
    const advice = support.recommendation;
    if (Number.isFinite(advice?.remaining_ms) && Number.isFinite(advice?.total_ms)) {
      const spent = clock?.status === "running" && clock.running_color === human ? Math.max(0, monotonicNow() - clockReceivedAt) : 0;
      const remaining = Math.ceil(Math.min(clockRemaining(human), Math.max(0, advice.remaining_ms - spent)) / 1000);
      byId("recommended-time").textContent = remaining > 0 ?
        `이번 수 총 ${Math.ceil(advice.total_ms / 1000)}초 권장 · 앞으로 ${remaining}초` : "권장 생각 시간 사용 · 이제 결정하세요";
      byId("time-recommendation-reason").textContent = advice.reason || "";
      byId("recommended-time").classList.toggle("low-time", ["critical", "time_pressure", "budget_used"].includes(advice.urgency) || remaining === 0);
    } else {
      byId("recommended-time").textContent = advice?.reason || "시간제 대국에서 생각 시간을 추천합니다.";
    }
  }

  function applyClockRefresh(next) {
    const unchanged = samePosition(state, next) && state.result === next.result && state.error === next.error &&
      state.status === next.status && state.handicap_mode === next.handicap_mode &&
      Number(state.engine.settings_revision || 0) === Number(next.engine.settings_revision || 0);
    if (!unchanged) { applyState(next, true); return; }
    if (!newerClock(next.clock, state.clock)) return;
    clockReceivedAt = nextClockAnchor(next.clock, state.clock, true);
    state.clock = next.clock;
    state.decision_support = next.decision_support;
    state.engine.pacing = next.engine.pacing;
    // Never rebuild squares or interrupt drag/promotion for a timer-only update.
    renderClockAndAdvice();
  }

  async function pollClock() {
    // The host adjudicates timeout; analysis views must not pause a live clock.
    if (clockPollInFlight || closed || !state?.clock?.enabled || state.result !== "*") return;
    const requestedGame = state.game_id;
    clockPollInFlight = true;
    try {
      const next = await request("/api/state", undefined, undefined, true);
      if (!closed && state?.game_id === requestedGame && next.game_id === requestedGame) applyClockRefresh(next);
    } catch (_) {
      // Keep counting from the last authoritative snapshot; do not invent a result.
    } finally { clockPollInFlight = false; }
  }

  function updateSidebarExplorer() {
    window.RegicideExplorer?.update("live", {fen:state?.fen, active:Boolean(state && !closed && !analysisMode && !coachingMode)});
    const analyze = byId("live-analyze-current");
    if (analyze) analyze.disabled = !state || closed || analysisMode || coachingMode;
  }

  function render() {
    updateSidebarExplorer();
    renderClockAndAdvice();
    renderModeHelp();
    renderSparringRating();
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
      byId("opponent-detail").textContent = `${usesAutoThinkTime() ? "생각시간 자동 조절" : `한 수에 ${seconds}초 생각`} · ${colorName(humanColor === "white" ? "black" : "white")}`;
      byId("opponent-detail").title = "";
      if (state.engine.boss_mode === true) {
        const phaseLabels = {opening: "갬빗 준비", initiative: "공격 전개", trap_hunt: "전술 사냥", full_strength: "전력 대국"};
        const phase = Object.hasOwn(phaseLabels, state.engine.boss_phase) ? phaseLabels[state.engine.boss_phase] : null;
        byId("opponent-detail").textContent += phase ? ` · 갬빗 모드 · ${phase}` : " · 갬빗 모드";
      } else if (state.engine.sparring_mode === true) {
        // Keep adjustment diagnostics in state/records, outside the live presentation.
        const referee = state.engine.sparring_referee;
        byId("opponent-detail").textContent += referee?.enabled === true ? (referee.available === true ? " · Stockfish 보조 스파링" : " · 스파링 · 자체 평가 사용") : " · 스파링 모드";
      }
      const engineName = state.engine.name || "Regicide";
      byId("engine-version").textContent = engineName;
      byId("header-engine-version").textContent = engineName.startsWith("Regicide ") ? "v" + engineName.slice("Regicide ".length) : "";
      byId("human-detail").textContent = active ? `${colorName(humanColor)}으로 두고 있어요. 기물과 도착 칸을 눌러주세요.` : "이번 대국이 끝났습니다.";
    }
    byId("new-game").disabled = !ready || busy || timeSaving;
    byId("human-side").disabled = !ready || busy || timeSaving;
    byId("boss-mode").disabled = !ready || busy || timeSaving || state?.handicap_mode;
    byId("rapid-time-control").disabled = !ready || busy || timeSaving || state?.handicap_mode;
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
    if (!state || closed || timeSaving || usesAutoThinkTime() || state.handicap_mode || (busy && !state.engine_to_move)) return;
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
    const payload = {color: byId("human-side").value,
      boss_mode: byId("boss-mode").value === "boss", sparring_mode: byId("boss-mode").value === "sparring",
      time_control: Object.hasOwn(rapidControls, byId("rapid-time-control").value) ? byId("rapid-time-control").value : "10+0"};
    void mutate("/api/new", payload);
  });
  byId("boss-mode").addEventListener("change", renderModeHelp);
  byId("live-analyze-current")?.addEventListener("click", () => {
    if (!state || closed || analysisMode || coachingMode) return;
    window.dispatchEvent(new CustomEvent("regicide-current-analysis", {detail:{fen:state.fen}}));
  });
  byId("rapid-time-control").addEventListener("change", renderClockAndAdvice);
  byId("clock-adjust-toggle").addEventListener("click", () => {
    if (!canAdjustClock()) return;
    clockAdjustExpanded = !clockAdjustExpanded;
    renderClockAdjustment();
  });
  for (const side of ["human", "engine"]) {
    for (const [name, delta] of clockSteps) {
      byId(`clock-${side}-${name}`).addEventListener("click", () => { void adjustClock(side, delta); });
    }
  }
  byId("rating-label-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (byId("rating-label-panel").hidden || busy || timeSaving || closed || !state) return;
    const rating = Number(byId("rating-label-input").value);
    if (!Number.isInteger(rating) || rating < 100 || rating > 4000) {
      showError("100~4000 사이의 정수 레이팅을 입력해 주세요.");
      return;
    }
    void mutate("/api/rating-label", {game_id: state.game_id, rating});
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
      if ([100, 1000, 3000, 5000].includes(state.engine.movetime_ms)) byId("think-time").value = String(state.engine.movetime_ms);
      await engineMove();
    } catch (error) { showError(error.message); }
    finally { busy = false; render(); }
  }
  // Analysis polling has its own request and keeps running while /api/engine waits.
  const evaluationTimer = setInterval(() => { void pollEvaluation(); }, 200);
  const hintTimer = setInterval(() => { void pollHint(); }, 300);
  const clockTimer = setInterval(() => { void pollClock(); }, 500);
  const clockDrawTimer = setInterval(renderClockAndAdvice, 100);
  window.addEventListener("pagehide", () => {
    closed = true;
    clearHint();
    renderHint();
    renderBoard();
    clearInterval(evaluationTimer);
    clearInterval(hintTimer);
    clearInterval(clockTimer);
    clearInterval(clockDrawTimer);
  });
  function changeHintMode() {
    updateSidebarExplorer();
    clearHint();
    if (analysisMode || coachingMode) window.RegicideMotion?.cancel(boardElement, true);
    renderHint();
    renderBoard();
    renderClockAndAdvice();
    if (!analysisMode && !coachingMode) void pollHint();
  }
  window.addEventListener("regicide-analysis-mode", (event) => { analysisMode = event.detail === true; changeHintMode(); });
  window.addEventListener("regicide-coaching-mode", (event) => { coachingMode = event.detail === true; changeHintMode(); });
  window.addEventListener("regicide-sidebar-change", (event) => { if (event.detail?.name === "live") updateSidebarExplorer(); });
  void initialize();
})();
