"use strict";

(() => {
  const get = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name="regicide-token"]').content;
  const symbols = {K:"♔",Q:"♕",R:"♖",B:"♗",N:"♘",P:"♙",k:"♚",q:"♛",r:"♜",b:"♝",n:"♞",p:"♟"};
  const names = {k:"왕",q:"퀸",r:"룩",b:"비숍",n:"나이트",p:"폰"};
  const criticalKinds = new Set(["inaccuracy","mistake","blunder","missed_mate","allows_mate"]);
  const sideName = (color) => color === "white" ? "백" : "흑";
  let visible = false, busy = false, polling = false, analysisOpen = false, blackView = false;
  let libraryMode = false;
  let epoch = 0, session = null, report = null, source = null, failure = "";
  let ownedJob = null;
  let libraryGames = [];
  let cursor = 0, selectedPly = 1, variation = null, lineCursor = 0, viewBefore=false;
  let contextReplay = null, contextTimer = null, replayEpoch = 0, motionStep = false;
  let continuationReplay = null, continuationTimer = null, continuationEpoch = 0;
  let strategyView = null;
  let openingView = null;
  let retry = null, retryEpoch = 0, evidenceFocus = null;
  let retryPointer=null, pointerSource=null, pointerTarget=null, suppressRetryClickUntil=0;
  let speaking = false, speechEpoch = 0;
  let speechEnabled = true, speechTimer = null, pendingSpeechKey = "", lastSpeechKey = "";
  let speechOverview = false, speechSceneKey = "";
  const returnNotice = document.createElement("p");
  returnNotice.className = "error-panel";
  returnNotice.setAttribute("role", "alert");
  returnNotice.hidden = true;
  get("live-view").before(returnNotice);

  async function request(path, payload, asText = false) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {method:payload === undefined ? "GET" : "POST", cache:"no-store", signal:controller.signal,
        headers:{"X-Regicide-Token":token, ...(payload === undefined ? {} : {"Content-Type":"application/json"})},
        ...(payload === undefined ? {} : {body:JSON.stringify(payload)})});
      const result = asText && response.ok ? await response.text() : await response.json();
      if (!response.ok) {
        const error = new Error(result.error || "코칭 요청을 처리하지 못했습니다.");
        error.coaching = result.coaching;
        throw error;
      }
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("코칭 연결 시간이 초과됐습니다. 다시 시도해 주세요.");
      if (error instanceof TypeError) throw new Error("코칭 관리기와 연결할 수 없습니다. 실행 창을 확인해 주세요.");
      throw error;
    } finally { clearTimeout(timer); }
  }

  function accept(next) {
    if (!next || !Number.isInteger(next.revision)) return false;
    if (session && next.revision < session.revision) return false;
    if (session && next.revision === session.revision) {
      if (next.job_id !== session.job_id) return false;
      if ((next.progress?.completed || 0) < (session.progress?.completed || 0)) return false;
      if (session.status !== "running" && next.status === "running") return false;
    }
    session = next;
    return true;
  }

  function running() { return session?.status === "running" && session.job_id === ownedJob; }
  function acceptForeign(next) {
    if (!accept(next)) return;
    cancelExercise();
    stopContext();
    ownedJob = null; report = null; variation = null;
    failure = "다른 화면에서 코칭이 변경됐습니다. 선택한 기보로 다시 시작할 수 있습니다.";
  }
  function frames() { return report?.frames || source?.frames || []; }
  function rows() { return report?.moves || []; }
  function selectedRow() { return rows().find((row) => row.ply === selectedPly); }
  function line() { return variation === "retry_candidate" ? retry?.feedback?.line : variation === "retry_best" ? retry?.feedback?.best : variation ? selectedRow()?.[variation] : null; }
  function lineTitle() { return ({best:"추천 수",played:"실제 수",retry_candidate:"재도전에서 고른 수",retry_best:"재도전의 새 추천 수"})[variation] || ""; }
  function exerciseHidden() { return Boolean(retry && !retry.revealed); }
  function frame() { return exerciseHidden() ? retry.trialFrame || retry.base : variation ? line()?.pv_frames?.[lineCursor] : frames()[cursor]; }
  function mine() { return get("coaching-side").value; }
  function shown(row) {
    const filter = get("coaching-filter").value;
    return filter === "mine" ? row.color === mine() : filter === "opponent" ? row.color !== mine() : true;
  }
  function criticalRows() { return rows().filter((row) => criticalKinds.has(row.classification) && shown(row)); }
  function actor(row) { return (row.color === mine() ? "내 수" : "상대 수") + " · " + sideName(row.color); }
  function duration(seconds) { return seconds < 60 ? "약 " + Math.ceil(seconds) + "초" : "약 " + Math.ceil(seconds / 60) + "분"; }
  function recordedTime(summary, kind) {
    if (summary?.[kind+"_at_local"]) return summary[kind+"_at_local"];
    const value = summary?.[kind+"_at"], date = value ? new Date(value) : null;
    if (!date || !Number.isFinite(date.getTime())) return "시간 정보 없음";
    return new Intl.DateTimeFormat("sv-SE", {year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).format(date);
  }
  function score(value) {
    if (!value) return "평가 대기";
    if (Number.isInteger(value.mate_white)) {
      const winner = value.winner || (value.mate_white > 0 ? "white" : value.mate_white < 0 ? "black" : null);
      return winner ? sideName(winner) + " 메이트 " + Math.abs(value.mate_white) : "메이트";
    }
    if (!Number.isFinite(value.score_cp_white)) return "평가 없음";
    const bound = value.bound === "lower" ? "≥ " : value.bound === "upper" ? "≤ " : "";
    return bound + (value.score_cp_white > 0 ? "+" : "") + (value.score_cp_white / 100).toFixed(2);
  }
  function boardEvaluation(position) {
    if (exerciseHidden() || !position?.fen) return {value:null};
    const result = {"1-0":[1,"백 승"],"0-1":[0,"흑 승"],"1/2-1/2":[.5,"무승부"]}[position.result];
    if (result) return {value:null,share:result[0],label:"대국 결과 · "+result[1]};
    // A PV score belongs to its searched root, not every board along the line.
    if (variation && lineCursor > 0) return {value:null,unsearchedLine:true};
    const ply = variation ? selectedPly - 1 : cursor;
    if (position.ply !== ply) return {value:null};
    const row = rows().find((item) => item.ply === ply + 1);
    if (!row || row.fen_before !== position.fen) return {value:null};
    const freshRetry = retry?.revealed && retry.ply === row.ply && retry.base.fen === position.fen;
    const value = freshRetry ? retry.feedback?.best : row.classification === "pending" ? null : row.best;
    if (!value || value.pv_frames?.[0]?.fen !== position.fen || value.pv_frames[0].ply !== ply) return {value:null};
    if (Number.isInteger(value.mate_white) && value.mate_white === 0 && !["white","black"].includes(value.winner)) return {value:null};
    return {value};
  }
  function moveTitle(row) {
    if (!row) return "수를 선택하세요.";
    const number = row.fen_before?.split(/\s+/)[5] || Math.ceil(row.ply / 2);
    return number + (row.color === "black" ? "... " : ". ") + row.san + " · " + actor(row);
  }
  function positionTitle(row) {
    if (!row) return "수를 선택하세요.";
    return (row.fen_before?.split(/\s+/)[5] || Math.ceil(row.ply/2)) + "수 · " + actor(row);
  }
  function lessons() { return (report?.coach?.lesson_plies || []).filter((ply) => rows().some((row) => row.ply === ply)).slice(0,5); }
  function canRetry() {
    const row=selectedRow();
    return visible && !busy && !retry?.submitting && (!contextReplay || contextReplay.done) && ["completed","cancelled"].includes(report?.status) && row?.classification !== "pending" &&
      Array.isArray(row?.retry_legal_moves) && row.retry_legal_moves.length > 0;
  }
  function strategyPacket() {
    const row=selectedRow(), wording=row?.coach?.perspectives?.[mine()] || row?.coach, value=wording?.insight;
    if(!value || !["best","good",...criticalKinds].includes(row?.classification) ||
       ![value.title,value.summary,value.theme].every(text=>typeof text==="string" && text.trim()) ||
       !Array.isArray(value.sections) || value.sections.length!==3) return null;
    const expected=criticalKinds.has(row.classification) ? "best" : "played";
    if(expected==="best" && row.best?.uci===row.uci)return null;
    for(const [index,section] of value.sections.entries()) {
      const branch=row?.[section.line], frames=branch?.pv_frames;
      if(section.id!==["idea","plan","counterplay"][index] || section.line!==expected || branch?.bound!=="exact" ||
         !Number.isInteger(section.ply_index) || section.ply_index<0 || !Array.isArray(frames) ||
         section.ply_index>=frames.length || frames[0]?.fen!==row.fen_before ||
         !frames[section.ply_index]?.board || typeof frames[section.ply_index]?.fen!=="string" ||
         ![section.title,section.text].every(text=>typeof text==="string" && text.trim()) ||
         !Array.isArray(section.squares) || section.squares.some(square=>!/^[a-h][1-8]$/.test(square)))return null;
    }
    return value;
  }
  function openingPacket(row=selectedRow()) {
    const value=row?.coach?.opening, actual=frames()[row?.ply];
    if(!value || !row || row.classification==="pending" || !Number.isInteger(value.ply_index) ||
       value.ply_index!==row.ply || value.ply_index<1 || value.ply_index>24 ||
       value.fen!==row.fen_after || value.fen!==actual?.fen || frames()[row.ply-1]?.fen!==row.fen_before ||
       actual?.last_move!==row.uci || !actual?.board ||
       ![value.title,value.summary,value.white?.name,value.black?.name].every(text=>typeof text==="string" && text.trim()) ||
       !Array.isArray(value.sections) || value.sections.length!==3 || !Array.isArray(value.focus_squares) ||
       value.focus_squares.some(square=>!/^[a-h][1-8]$/.test(square)))return null;
    for(const [index,section] of value.sections.entries())if(section?.id!==["white","black","balance"][index] ||
      ![section.title,section.text].every(text=>typeof text==="string" && text.trim()))return null;
    return value;
  }
  function latestOpeningPly() {return rows().reduce((latest,row)=>openingPacket(row) ? Math.max(latest,row.ply) : latest,0);}
  function openingContextKey() {
    const row=selectedRow();
    return JSON.stringify([report?.job_id,selectedPly,mine(),row?.uci,row?.fen_before,row?.fen_after,openingPacket()]);
  }
  function jumpOpening() {
    const ply=latestOpeningPly();
    if(!ply || !visible || busy || retry || document.hidden)return;
    choosePly(ply,false);showOpening(ply>=12 ? "balance" : "white");
  }
  function openingCurrent() {
    return Boolean(openingView && visible && !busy && !retry && !continuationReplay && !strategyView &&
      openingView.key===openingContextKey() && !variation && !viewBefore && cursor===selectedPly);
  }
  function stopOpening() {if(openingView){openingView=null;evidenceFocus=null;stopSpeech();}}
  function showOpening(id) {
    const packet=openingPacket(),section=packet?.sections.find(item=>item.id===id);
    if(!section || !visible || busy || retry || (contextReplay && !contextReplay.done))return;
    stopContext();stopSpeech();speechOverview=false;get("coach-overview-box").open=false;
    variation=null;cursor=selectedPly;viewBefore=false;motionStep=false;
    openingView={packet,section,key:openingContextKey()};
    evidenceFocus={ply:selectedPly,line:null,actualIndex:selectedPly,squares:packet.focus_squares};render();
  }
  function leaveOpening() {stopOpening();render();}
  function strategyCurrent() {
    return Boolean(strategyView && visible && !busy && !retry && !continuationReplay &&
      strategyView.report===report && strategyView.ply===selectedPly && strategyView.side===mine() &&
      strategyView.packet===strategyPacket() && variation===strategyView.section.line && lineCursor===strategyView.section.ply_index);
  }
  function stopStrategy() {if(strategyView){strategyView=null;stopSpeech();}}
  function showStrategy(id) {
    const packet=strategyPacket(),section=packet?.sections.find(item=>item.id===id);
    if(!section || !visible || busy || retry || (contextReplay && !contextReplay.done))return;
    stopContext();stopSpeech();speechOverview=false;get("coach-overview-box").open=false;
    variation=section.line;lineCursor=section.ply_index;viewBefore=false;motionStep=false;
    strategyView={packet,section,report,ply:selectedPly,side:mine()};
    evidenceFocus={ply:selectedPly,line:section.line,index:section.ply_index,squares:section.squares};render();
  }
  function leaveStrategy() {
    stopStrategy();stopSpeech();evidenceFocus=null;variation=null;cursor=selectedPly;viewBefore=false;motionStep=false;render();
  }
  function continuationPacket() {
    const row=selectedRow(), coach=row?.coach?.perspectives?.[mine()] || row?.coach;
    const packet=coach?.continuation, best=row?.best, pv=best?.pv_frames;
    if (!packet || !criticalKinds.has(row.classification) || best?.bound!=="exact" || best.uci===row.uci ||
        packet.line!=="best" || packet.target_plies!==6 ||
        !Number.isInteger(packet.available_plies) || packet.available_plies<1 || packet.available_plies>6 ||
        typeof packet.terminal!=="boolean" || packet.complete!==(packet.available_plies===6 || packet.terminal) ||
        !Array.isArray(packet.steps) || packet.steps.length!==packet.available_plies ||
        ![packet.intro,packet.summary].every((text)=>typeof text==="string" && text.trim()) ||
        !Array.isArray(pv) || pv.length<=packet.available_plies || pv[0]?.fen!==row.fen_before || pv[0]?.turn!==row.color ||
        best.uci!==packet.steps[0]?.uci) return null;
    for(const [index,step] of packet.steps.entries()) {
      const before=pv[index],after=pv[index+1];
      if(step.ply_index!==index+1 || !/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(step.uci || "") ||
         typeof step.text!=="string" || !step.text.trim() || typeof step.san!=="string" || !step.san.trim() ||
         best.pv_uci?.[index]!==step.uci || best.pv_san?.[index]!==step.san ||
         !["white","black"].includes(step.color) || before?.turn!==step.color ||
         after?.turn!==(step.color==="white" ? "black" : "white") || after?.last_move!==step.uci ||
         typeof after?.fen!=="string" || !after.board || !before?.board ||
         !Array.isArray(step.squares) || step.squares.some((square)=>!/^[a-h][1-8]$/.test(square))) return null;
    }
    return packet;
  }
  function continuationCurrent(active=continuationReplay) {
    return Boolean(active && active===continuationReplay && visible && !document.hidden && !busy && !retry &&
      report===active.report && selectedPly===active.ply && mine()===active.side && variation==="best" &&
      continuationPacket()===active.packet);
  }
  function continuationText() {
    const active=continuationReplay;
    return active?.stage==="summary" ? active.packet.summary : lineCursor ? active?.packet.steps[lineCursor-1]?.text : active?.packet.intro;
  }
  function continuationReadingDelay() {
    return Math.max(2200,Math.min(9000,(continuationText() || "").trim().split(/\s+/).length*220));
  }
  function continuationFocus() {
    evidenceFocus=continuationReplay?.stage==="step" ? {ply:selectedPly,line:"best",index:lineCursor,
      squares:continuationReplay.packet.steps[lineCursor-1].squares} : null;
  }
  function clearContinuationTimer() {
    continuationEpoch++;
    if(continuationTimer!==null)clearTimeout(continuationTimer);
    continuationTimer=null;
  }
  function stopContinuation() {
    if(!continuationReplay && continuationTimer===null)return;
    clearContinuationTimer();continuationReplay=null;stopSpeech();
  }
  function continuationWait(milliseconds,callback,allowPaused=false) {
    const ticket=continuationEpoch,active=continuationReplay;
    continuationTimer=setTimeout(()=>{
      if(ticket!==continuationEpoch || !continuationCurrent(active) || (active.paused && !allowPaused))return;
      continuationTimer=null;callback();
    },milliseconds);
  }
  function advanceContinuation() {
    if(!continuationCurrent() || continuationReplay.paused)return;
    if(continuationReplay.stage==="summary") {continuationReplay.done=true;render();return;}
    clearContinuationTimer();stopSpeech();
    if(lineCursor<continuationReplay.packet.available_plies) {lineCursor++;continuationReplay.stage="step";motionStep=true;}
    else {continuationReplay.stage="summary";motionStep=false;}
    continuationReplay.narrated=null;continuationReplay.manualSpeech=false;continuationFocus();render();
  }
  function scheduleContinuation() {
    const active=continuationReplay;
    if(!continuationCurrent(active) || (active.paused && !active.manualSpeech) || active.done)return;
    const key=active.stage+":"+lineCursor;
    if(active.narrated===key)return;
    active.narrated=key;
    const voice=speechEnabled ? localEnglishVoice() : null;
    if(!voice) {
      active.manualSpeech=false;
      renderSpeech();
      if(!active.paused)continuationWait(continuationReadingDelay(),advanceContinuation);
      return;
    }
    const ticket=continuationEpoch;
    continuationWait(300,()=>{
      lastSpeechKey=speechContextKey();
      speakCoach(continuationText(),voice,(failed)=>{
        if(ticket!==continuationEpoch || !continuationCurrent(active) || active.narrated!==key)return;
        active.manualSpeech=false;
        if(active.paused){renderSpeech();return;}
        continuationWait(failed ? continuationReadingDelay() : 600,advanceContinuation);
      });
    },active.paused);
  }
  function startContinuation() {
    const packet=continuationPacket();
    if(!packet || !visible || busy || running() || retry || document.hidden)return;
    stopContext();stopSpeech();evidenceFocus=null;speechOverview=false;get("coach-overview-box").open=false;
    variation="best";lineCursor=0;viewBefore=false;motionStep=false;
    continuationReplay={packet,report,ply:selectedPly,side:mine(),stage:"intro",paused:false,done:false,narrated:null,manualSpeech:false};
    render();
  }
  function pauseContinuation() {
    if(!continuationCurrent() || continuationReplay.done)return;
    clearContinuationTimer();stopSpeech();continuationReplay.paused=!continuationReplay.paused;
    continuationReplay.narrated=null;continuationReplay.manualSpeech=false;render();
  }
  function stepLine(delta) {
    if(continuationReplay) {
      if(!continuationCurrent())return;
      clearContinuationTimer();stopSpeech();const active=continuationReplay,previous=lineCursor;
      if(delta<0 && active.stage==="summary")active.stage="step";
      else if(delta>0 && lineCursor===active.packet.available_plies)active.stage="summary";
      else {lineCursor=Math.max(0,Math.min(active.packet.available_plies,lineCursor+delta));active.stage=lineCursor ? "step" : "intro";}
      active.paused=true;active.done=false;active.narrated=null;active.manualSpeech=true;
      motionStep=lineCursor!==previous;continuationFocus();render();return;
    }
    stopSpeech();stopContext();
    if(variation){motionStep=true;lineCursor=Math.max(0,Math.min((line()?.pv_frames?.length || 1)-1,lineCursor+delta));render();}
  }
  function stopSpeech() {
    speechEpoch++;
    if (speechTimer !== null) clearTimeout(speechTimer);
    speechTimer=null;pendingSpeechKey="";
    if (speaking) window.speechSynthesis?.cancel();
    speaking=false;
    renderSpeech();
  }
  function localEnglishVoice() {
    if (!window.speechSynthesis || typeof window.SpeechSynthesisUtterance !== "function") return null;
    try { return window.speechSynthesis.getVoices().find((voice) => voice.localService === true && /^en(?:[-_]|$)/i.test(voice.lang || "")) || null; }
    catch (_) { return null; }
  }
  function renderSpeech() {
    const available=Boolean(localEnglishVoice());
    get("coach-speak").disabled=!available || !visible;
    get("coach-speak").textContent=available ? (speechEnabled ? "음성 켜짐" : "음성 꺼짐") : "영어 음성 없음";
    get("coach-speak").setAttribute("aria-pressed",String(available && speechEnabled));
    get("coach-speech-stop").hidden=!speaking && speechTimer===null && (!continuationReplay ||
      (continuationReplay.paused && !continuationReplay.manualSpeech) || continuationReplay.done);
    get("coach-voice-note").textContent=available ? "내 컴퓨터의 영어 음성 · 수를 넘기면 자동으로 읽어요." : "사용 가능한 영어 음성이 없어 글로 안내합니다.";
  }
  function spokenCoachText() {
    // Read only currently visible guidance, including retry feedback after submission.
    if (speechOverview && !exerciseHidden() && get("coach-overview-box").open) return get("coach-overview").textContent;
    return get("coach-dialogue").textContent;
  }
  function speechContextKey() {
    return JSON.stringify([epoch,report?.job_id || "",selectedPly,variation || "actual",viewBefore,
      exerciseHidden(),speechOverview,retry?.feedback?.uci || "",strategyView?.section.id || "",openingView?.section.id || "",spokenCoachText()]);
  }
  function canSpeak() {
    return visible && !document.hidden && speechEnabled && !busy && !retry?.submitting &&
      (!contextReplay || contextReplay.done) && Boolean(localEnglishVoice()) && Boolean(spokenCoachText());
  }
  function scheduleSpeech() {
    if(continuationReplay){scheduleContinuation();return;}
    if (!canSpeak()) { stopSpeech(); return; }
    const key=speechContextKey();
    if (key===pendingSpeechKey) return;
    if (key===lastSpeechKey) { if (speechTimer!==null) stopSpeech(); return; }
    stopSpeech();pendingSpeechKey=key;
    const ticket=speechEpoch;
    speechTimer=setTimeout(()=>{
      if (ticket!==speechEpoch) return;
      speechTimer=null;
      if (!canSpeak() || pendingSpeechKey!==speechContextKey()) { stopSpeech();return; }
      const text=spokenCoachText(),voice=localEnglishVoice();
      lastSpeechKey=pendingSpeechKey;pendingSpeechKey="";
      speakCoach(text,voice);
    },300);
    renderSpeech();
  }
  function speakCoach(text,voice,completed=null) {
    const utterance=new window.SpeechSynthesisUtterance(text), ticket=++speechEpoch;
    utterance.voice=voice;utterance.lang=voice.lang;utterance.rate=.95;
    let finished=false;
    const finish=(failed=false)=>{if(!finished && ticket===speechEpoch){finished=true;speaking=false;renderSpeech();completed?.(failed);}};
    utterance.onend=()=>finish();utterance.onerror=()=>finish(true);speaking=true;renderSpeech();
    try { window.speechSynthesis.speak(utterance); } catch (_) { finish(true); }
  }
  function toggleSpeech() {
    if (!localEnglishVoice()) return;
    speechEnabled=!speechEnabled;stopSpeech();
    if(continuationReplay){clearContinuationTimer();continuationReplay.narrated=null;scheduleContinuation();renderSpeech();return;}
    if (speechEnabled) {lastSpeechKey="";scheduleSpeech();}
    renderSpeech();
  }
  function stopCurrentSpeech() {
    if(continuationReplay && !continuationReplay.paused && !continuationReplay.done){pauseContinuation();return;}
    if(continuationReplay){clearContinuationTimer();continuationReplay.manualSpeech=false;}
    lastSpeechKey=speechContextKey();stopSpeech();
  }
  function cancelExercise() {
    stopOpening();
    stopStrategy();
    stopContinuation();
    clearRetryPointer();
    if(retry){cursor=selectedPly;viewBefore=false;}
    if(variation === "retry_candidate" || variation === "retry_best"){variation=null;lineCursor=0;}
    retryEpoch++;retry=null;evidenceFocus=null;stopSpeech();
  }
  function renderCoach() {
    const row=selectedRow(), hidden=exerciseHidden(), coach=row?.coach;
    const wording=coach?.perspectives?.[mine()] || coach;
    const insight=strategyPacket();
    const opening=openingPacket();
    const travelling=Boolean(contextReplay && !contextReplay.done);
    const mood=retry?.feedback ? ({recommended:"celebrate",good:"encourage",try_again:"think",uncertain:"think"})[retry.feedback.verdict] || "think" :
      hidden || travelling ? "think" : ["encourage","think","celebrate","warning"].includes(coach?.mood) ? coach.mood : "encourage";
    get("coach-mascot").dataset.mood=mood;
    get("coach-mascot").setAttribute("aria-label",({encourage:"응원하는",think:"생각하는",celebrate:"기뻐하는",warning:"주의할 점을 알려주는"})[mood]+" 체스 코치 루크");
    get("coach-scene").textContent=row ? positionTitle(row) + (hidden ? " · 다시 두기" : "") : "한 장면씩 함께 살펴봐요.";
    get("coach-dialogue").textContent=continuationReplay ? continuationText() : travelling ? "Following the moves actually played to the next key moment." :
      hidden ? retry.feedback?.message || wording?.hint || "Check for threats against the king and pieces that can be captured." :
      openingView ? openingView.section.text : strategyView ? strategyView.section.text :
      (!retry && insight?.summary) || wording?.dialogue || (report ? "I'm checking this move and the opponent's reply." : "Choose a game to review its key moments.");
    get("coach-strategy").hidden=!insight || Boolean(retry) || travelling || Boolean(continuationReplay) || speechOverview;
    get("coach-strategy-title").textContent=insight?.title || "";
    get("coach-strategy-note").textContent=strategyView ? strategyView.section.title+" · 분석 수순의 근거 장면" : "구상·계획·상대 대응을 나눠 살펴보세요.";
    for(const id of ["idea","plan","counterplay"]){
      get("coach-strategy-"+id).disabled=busy;
      get("coach-strategy-"+id).setAttribute("aria-pressed",String(strategyView?.section.id===id));
    }
    get("coach-strategy-return").hidden=!strategyView;
    get("coach-opening").hidden=!opening || Boolean(retry) || travelling || Boolean(continuationReplay) || speechOverview;
    get("coach-opening-title").textContent=opening ? "오프닝 해설 · "+opening.title : "";
    get("coach-opening-sides").textContent=opening ? "백 · "+opening.white.name+" / 흑 · "+opening.black.name : "";
    get("coach-opening-summary").textContent=opening?.summary || "";
    for(const id of ["white","black","balance"]){
      get("coach-opening-"+id).disabled=busy;
      get("coach-opening-"+id).setAttribute("aria-pressed",String(openingView?.section.id===id));
    }
    get("coach-opening-return").hidden=!openingView;
    get("coach-opening-jump").hidden=!latestOpeningPly() || Boolean(retry) || travelling;
    get("coach-opening-jump").disabled=busy;
    get("coach-overview").textContent=report?.coach?.summary || "Start the analysis to find the key moments.";
    get("coach-overview-box").hidden=hidden;
    const continuation=continuationPacket();
    get("coach-continuation").hidden=!continuation || Boolean(retry) || travelling;
    get("coach-continuation").disabled=busy || running() || Boolean(continuationReplay && !continuationReplay.done);
    get("coach-continuation").textContent="왜 더 좋은지 · "+(continuation?.available_plies || 6)+"수 보기";
    get("coach-lesson-controls").hidden=hidden;
    const lessonPlies=lessons();
    get("coach-lesson-prev").disabled=!lessonPlies.some((ply)=>ply<selectedPly);
    get("coach-lesson-next").disabled=!lessonPlies.some((ply)=>ply>selectedPly);
    const lessonFragment=document.createDocumentFragment();
    lessonPlies.forEach((ply,index)=>{
      const button=document.createElement("button");button.type="button";button.dataset.lessonPly=String(ply);
      button.textContent="장면 "+(index+1)+" · "+actor(rows().find((item)=>item.ply===ply));
      button.setAttribute("aria-pressed",String(selectedPly===ply));lessonFragment.append(button);
    });
    get("coach-lessons").replaceChildren(lessonFragment);
    const evidence=document.createDocumentFragment();
    if (!hidden && !travelling) for (const [index,item] of (coach?.evidence || []).entries()) {
      if (!["best","played"].includes(item.line) || !Number.isInteger(item.ply_index) || !row?.[item.line]?.pv_frames?.[item.ply_index]) continue;
      const button=document.createElement("button");button.type="button";button.dataset.evidenceIndex=String(index);
      button.textContent=item.text || "근거 수순 보기";evidence.append(button);
    }
    get("coach-evidence").replaceChildren(evidence);get("coach-evidence").hidden=hidden || travelling;
    get("coach-focus").hidden=hidden || travelling;get("coach-focus").disabled=!coach?.focus_squares?.length;
    get("coach-retry").hidden=Boolean(retry);get("coach-retry").disabled=!canRetry();
    get("coach-retry-panel").hidden=!retry;
    get("coach-retry-status").textContent=retry ? retry.submitting ? "고른 수를 Regicide가 비교하고 있어요. 다른 장면으로 이동해도 기보는 그대로입니다." :
      retry.feedback ? "내가 고른 수: "+(retry.feedback.san || retry.uci)+" · 다시 선택하거나 해설을 확인하세요." :
      retry.revealed ? "연습을 마쳤습니다. 실제 수와 추천 수순을 비교해 보세요." :
      retry.promotion.length ? "승격할 기물을 골라 주세요." : sideName(row?.color)+" 차례 · 기물을 클릭하거나 끌어서 한 수를 두세요. 해설과 점수는 가려 두었습니다." : "";
    get("coach-retry-feedback").textContent=retry?.feedback?.message || retry?.error || "";
    get("coach-retry-feedback").hidden=!retry?.feedback && !retry?.error;
    get("coach-retry-again").disabled=Boolean(retry?.submitting) || !canRetry();
    get("coach-retry-reveal").disabled=Boolean(retry?.submitting) || Boolean(retry?.revealed);
    get("coach-retry-revealed").hidden=!retry?.revealed || !retry?.feedback;
    get("coach-retry-revealed").textContent=retry?.feedback ? "이번 재도전 탐색의 추천: "+(retry.feedback.best?.san || "판단 보류")+
      (Number.isFinite(retry.feedback.loss_cp) ? " · 평가 차이 "+(retry.feedback.loss_cp/100).toFixed(2) : "")+
      ". 아래 대국 해설은 저장된 원래 분석 기준입니다." : "";
    get("coach-retry-lines").hidden=!retry?.revealed || !retry?.feedback;
    get("coach-retry-candidate-line").disabled=!retry?.revealed || (retry?.feedback?.line?.pv_frames?.length || 0)<2;
    get("coach-retry-best-line").disabled=!retry?.revealed || (retry?.feedback?.best?.pv_frames?.length || 0)<2;
    const choices=document.createDocumentFragment();
    for(const uci of retry?.promotion || []) {
      const button=document.createElement("button");button.type="button";button.dataset.retryPromotion=uci;
      button.textContent=names[uci[4]];button.addEventListener("click",()=>{void submitRetry(uci);});choices.append(button);
    }
    get("coach-retry-promotion").replaceChildren(choices);get("coach-retry-promotion").hidden=!retry?.promotion?.length;
    const sceneKey=JSON.stringify([epoch,report?.job_id,selectedPly,variation,viewBefore,exerciseHidden(),get("coach-dialogue").textContent]);
    if (sceneKey!==speechSceneKey) {speechOverview=false;speechSceneKey=sceneKey;}
    renderSpeech();scheduleSpeech();
  }
  function showEvidence(index) {
    if (exerciseHidden() || (contextReplay && !contextReplay.done)) return;
    const row=selectedRow(),item=row?.coach?.evidence?.[index];
    if (!item || !["best","played"].includes(item.line) || !row[item.line]?.pv_frames?.[item.ply_index]) return;
    stopContext();stopSpeech();variation=item.line;lineCursor=item.ply_index;motionStep=false;
    evidenceFocus={ply:row.ply,line:variation,index:lineCursor,squares:item.squares || []};render();
  }
  function showRetryLine(kind) {
    if(!retry?.revealed || !["retry_candidate","retry_best"].includes(kind))return;
    const candidate=kind==="retry_candidate" ? retry.feedback?.line : retry.feedback?.best;
    if((candidate?.pv_frames?.length || 0)<2)return;
    stopSpeech();stopContext();evidenceFocus=null;variation=kind;lineCursor=0;motionStep=false;render();
  }
  function startRetry() {
    if (!canRetry()) return;
    const row=selectedRow(),base=frames()[row.ply-1];
    if (!base) return;
    cancelExercise();stopContext();window.RegicideMotion?.cancel(get("coaching-board"),true);
    variation=null;cursor=row.ply-1;viewBefore=true;motionStep=false;
    retry={job_id:report.job_id,revision:report.revision,ply:row.ply,base:{...base,board:{...base.board}},
      legal:[...row.retry_legal_moves],selected:null,promotion:[],submitting:false,feedback:null,error:"",revealed:false,attempt:retryEpoch};
    get("coaching-setup").open=false;render();
  }
  function retryCandidates(from,to) {
    if (!retry || retry.submitting || retry.uci || retry.revealed) return [];
    const direct=retry.legal.filter((uci)=>uci.startsWith(from+to));
    if (direct.length) return direct;
    const rank=retry.base.turn==="white" ? "1" : "8", king="e"+rank;
    for(const [rook,destination]of [["h","g"],["a","c"]])
      if (((from===king && to===rook+rank)||(from===rook+rank && to===king)) && retry.legal.includes(king+destination+rank))
        return [king+destination+rank];
    return [];
  }
  function retryOrigin(square) {
    if(!retry)return false;
    return retry.legal.some((uci)=>uci.startsWith(square)) || retryCandidates(square,retry.base.turn==="white" ? "e1" : "e8").length>0;
  }
  function trialPosition(uci) {
    const pieces={...retry.base.board},from=uci.slice(0,2),to=uci.slice(2,4),piece=pieces[from];
    if (piece.toLowerCase()==="p" && from[0]!==to[0] && !pieces[to]) delete pieces[to[0]+from[1]];
    delete pieces[from];pieces[to]=uci[4] ? (piece===piece.toUpperCase() ? uci[4].toUpperCase() : uci[4]) : piece;
    if (piece.toLowerCase()==="k" && Math.abs(from.charCodeAt(0)-to.charCodeAt(0))===2) {
      const rookFrom=(to[0]==="g" ? "h" : "a")+from[1],rookTo=(to[0]==="g" ? "f" : "d")+from[1];
      pieces[rookTo]=pieces[rookFrom];delete pieces[rookFrom];
    }
    return {...retry.base,board:pieces,turn:retry.base.turn==="white" ? "black" : "white",last_move:uci,in_check:false};
  }
  async function submitRetry(uci) {
    if (!retry || retry.submitting || retry.uci || retry.revealed || !retry.legal.includes(uci)) return;
    const active=retry,ticket=retryEpoch;stopSpeech();clearRetryPointer();
    retry.uci=uci;retry.trialFrame=trialPosition(uci);retry.selected=null;retry.promotion=[];retry.submitting=true;retry.error="";
    motionStep=true;render();
    try {
      const result=await request("/api/coaching",{action:"retry",ply:active.ply,uci,revision:active.revision,job_id:active.job_id});
      if (!visible || retry!==active || retryEpoch!==ticket || report?.job_id!==active.job_id || selectedPly!==active.ply) return;
      if (result.job_id!==active.job_id || result.revision!==active.revision || result.ply!==active.ply || result.uci!==uci)
        throw new Error("다른 위치의 응답을 받아 표시하지 않았습니다. 다시 선택해 주세요.");
      if ((result.line && (result.line.uci!==uci || result.line.pv_frames?.[0]?.fen!==active.base.fen)) ||
          (result.best && result.best.pv_frames?.[0]?.fen!==active.base.fen))
        throw new Error("다른 위치의 수순을 받아 표시하지 않았습니다. 다시 선택해 주세요.");
      retry.feedback=result;
      if(result.line?.pv_frames?.[1])retry.trialFrame=result.line.pv_frames[1];
    } catch(error) {
      if (retry===active && retryEpoch===ticket) {
        if(error.coaching && (error.coaching.job_id!==active.job_id || error.coaching.revision!==active.revision)){acceptForeign(error.coaching);render();}
        else retry.error=error.message;
      }
    } finally {if(retry===active && retryEpoch===ticket){retry.submitting=false;render();}}
  }
  function retrySquare(square) {
    if (!exerciseHidden() || retry.submitting || retry.uci) return;
    if(retry.selected===square){retry.selected=null;render();return;}
    const candidates=retry.selected ? retryCandidates(retry.selected,square) : [];
    if(candidates.length>1 || candidates[0]?.length===5){retry.promotion=candidates;render();return;}
    if(candidates.length){void submitRetry(candidates[0]);return;}
    retry.selected=retryOrigin(square) ? square : null;retry.promotion=[];render();
  }
  function clearRetryPointer() {
    retryPointer=null;
    pointerSource?.classList.remove("coaching-retry-drag-source");
    pointerTarget?.classList.remove("coaching-retry-drag-target");
    pointerSource=pointerTarget=null;
  }
  function retryDrop(from,to) {
    const candidates=retryCandidates(from,to);
    if(candidates.length>1 || candidates[0]?.length===5){retry.promotion=candidates;render();}
    else if(candidates.length)void submitRetry(candidates[0]);
  }
  function pointerCell(event) {
    const target=typeof document.elementFromPoint==="function" ? document.elementFromPoint(event.clientX,event.clientY) : event.target;
    return target?.closest?.("[data-coaching-square]");
  }
  function beginRetryPointer(event,kind) {
    const cell=event.target.closest("[data-coaching-square]");
    if((event.button!==undefined && event.button!==0) || !exerciseHidden() || retry.submitting || retry.uci || !cell || !retryOrigin(cell.dataset.coachingSquare))return;
    // Browsers send compatibility mouse events after pointer events. Keep a single gesture owner.
    if(kind==="mouse" && retryPointer?.kind==="pointer")return;
    clearRetryPointer();
    retryPointer={kind,id:event.pointerId,from:cell.dataset.coachingSquare,x:event.clientX,y:event.clientY,attempt:retry.attempt,moved:false};
    pointerSource=cell;cell.classList.add("coaching-retry-drag-source");
  }
  function moveRetryPointer(event,kind) {
    if(!retryPointer || retryPointer.kind!==kind || (kind==="pointer" && retryPointer.id!==event.pointerId))return;
    if(!exerciseHidden() || retry.attempt!==retryPointer.attempt){clearRetryPointer();return;}
    if(Math.hypot(event.clientX-retryPointer.x,event.clientY-retryPointer.y)<6)return;
    retryPointer.moved=true;
    if(event.cancelable)event.preventDefault();
    const cell=pointerCell(event);
    pointerTarget?.classList.remove("coaching-retry-drag-target");
    pointerTarget=cell && retryCandidates(retryPointer.from,cell.dataset.coachingSquare).length ? cell : null;
    pointerTarget?.classList.add("coaching-retry-drag-target");
  }
  function endRetryPointer(event,kind) {
    if(!retryPointer || retryPointer.kind!==kind || (kind==="pointer" && retryPointer.id!==event.pointerId))return;
    const pointer=retryPointer,cell=pointerCell(event);
    clearRetryPointer();
    const moved=pointer.moved || Math.hypot(event.clientX-pointer.x,event.clientY-pointer.y)>=6;
    if(!moved || !exerciseHidden() || retry.attempt!==pointer.attempt)return;
    suppressRetryClickUntil=Date.now()+350;
    if(event.cancelable)event.preventDefault();
    if(cell)retryDrop(pointer.from,cell.dataset.coachingSquare);
  }
  function moveAnnotation(position) {
    if(retry || variation || viewBefore || !position || cursor<1 || position.ply!==cursor)return null;
    const row=rows()[cursor-1],destination=row?.uci?.slice(2,4);
    if(!row || row.ply!==cursor || row.classification==="pending" || !row.fen_after || position.fen!==row.fen_after ||
      position.last_move!==row.uci || !/^[a-h][1-8]$/.test(destination || "") || !position.board?.[destination])return null;
    const piece=position.board[destination];
    if((piece===piece.toUpperCase() ? "white" : "black")!==row.color)return null;
    const kinds={great:["!!","탁월 후보"],best:["★","추천 수"],good:["✓","좋은 수"],inaccuracy:["?!","아쉬운 수"],
      mistake:["?","실수"],blunder:["??","큰 실수"],allows_mate:["??","메이트 허용"],missed_mate:["?","메이트 기회 놓침"],uncertain:["…","판단 보류"]};
    const annotation=row.annotation;
    if(annotation) {
      if(annotation.scope!=="actual_move" || annotation.provisional!==true || annotation.ply!==row.ply || annotation.uci!==row.uci || annotation.fen_after!==position.fen ||
        annotation.square!==destination || !kinds[annotation.kind] || annotation.symbol!==kinds[annotation.kind][0] ||
        (annotation.kind==="great" ? row.classification!=="best" : annotation.kind!==row.classification))return null;
      return {...annotation,row};
    }
    const fallback=row.classification==="great" ? null : kinds[row.classification];
    return fallback ? {symbol:fallback[0],label:fallback[1],kind:row.classification,square:destination,provisional:true,
      reason:"Regicide 제한 탐색 기준입니다. 더 깊게 분석하면 평가가 달라질 수 있습니다.",row} : null;
  }

  function renderBoard() {
    const position = frame(), pieces = position?.board || {}, row = selectedRow();
    const hidden=exerciseHidden();
    const annotation=moveAnnotation(position);
    const recommendation = !hidden && viewBefore && !variation && (!contextReplay || contextReplay.done) && cursor === selectedPly - 1 ? row?.best?.uci : null;
    const fragment = document.createDocumentFragment();
    const ranks = blackView ? "12345678" : "87654321", files = blackView ? "hgfedcba" : "abcdefgh";
    for (let y = 0; y < 8; y++) for (let x = 0; x < 8; x++) {
      const square = files[x] + ranks[y], piece = pieces[square], cell = document.createElement("div");
      cell.className = "square " + ((x + y) % 2 ? "dark" : "light");
      cell.dataset.coachingSquare = square;
      const movable=Boolean(hidden && !retry.submitting && !retry.uci && retryOrigin(square));
      // Native HTML dragging suppresses pointerup. Board gestures use pointer/mouse events.
      cell.draggable=false;
      if(movable)cell.classList.add("coaching-retry-origin");
      if(hidden){cell.setAttribute("role","button");cell.setAttribute("tabindex",movable || (retry.selected && retryCandidates(retry.selected,square).length) ? "0" : "-1");}
      if(hidden && retry.selected===square)cell.classList.add("coaching-retry-selected");
      if(hidden && retry.selected && retryCandidates(retry.selected,square).length)cell.classList.add("coaching-retry-target");
      const focusMatches=evidenceFocus && evidenceFocus.ply===selectedPly && (evidenceFocus.line ?
        variation===evidenceFocus.line && lineCursor===evidenceFocus.index : !variation && cursor===(evidenceFocus.actualIndex ?? selectedPly-1));
      if(!hidden && focusMatches && evidenceFocus.squares.includes(square))cell.classList.add("coaching-evidence-square");
      cell.setAttribute("aria-label", square + (piece ? " " + (piece === piece.toUpperCase() ? "백" : "흑") + " " + names[piece.toLowerCase()] : " 빈칸"));
      if (position?.last_move?.slice(0,2) === square || position?.last_move?.slice(2,4) === square) cell.classList.add("last-move");
      if (recommendation?.slice(0,2) === square) cell.classList.add("coaching-best-from");
      if (recommendation?.slice(2,4) === square) cell.classList.add("coaching-best-to");
      if (piece) {
        const glyph = document.createElement("span");
        glyph.className = "piece " + (piece === piece.toUpperCase() ? "white" : "black");
        glyph.textContent = symbols[piece]; glyph.setAttribute("aria-hidden", "true"); cell.append(glyph);
      }
      if(annotation?.square===square) {
        const badge=document.createElement("span");
        badge.className="coaching-move-badge badge-"+annotation.kind;
        badge.textContent=annotation.symbol;badge.setAttribute("role","img");
        badge.setAttribute("aria-label",moveTitle(annotation.row)+" · "+annotation.label+" ("+annotation.symbol+") · Regicide 제한 탐색 평가");
        badge.title=annotation.label+" · "+(annotation.reason || "제한된 탐색 결과이며 평가가 달라질 수 있습니다.");
        cell.append(badge);
      }
      for (const kind of ["rank","file"]) if (kind === "rank" ? x === 0 : y === 7) {
        const coordinate = document.createElement("span");
        coordinate.className = "coordinate " + kind + "-coordinate";
        coordinate.textContent = kind === "rank" ? ranks[y] : files[x];
        coordinate.setAttribute("aria-hidden", "true"); cell.append(coordinate);
      }
      fragment.append(cell);
    }
    if (recommendation && /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(recommendation)) {
      const center = (square) => [files.indexOf(square[0]) * 100 + 50, ranks.indexOf(square[1]) * 100 + 50];
      const [x1,y1] = center(recommendation.slice(0,2)), [x2,y2] = center(recommendation.slice(2,4));
      const distance = Math.hypot(x2-x1,y2-y1), dx = (x2-x1)/distance, dy = (y2-y1)/distance;
      if (distance > 0) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 800 800"); svg.setAttribute("class", "analysis-arrow"); svg.setAttribute("aria-hidden", "true");
        const shaft = document.createElementNS("http://www.w3.org/2000/svg", "line");
        for (const [key,value] of Object.entries({x1,y1,x2:x2-dx*24,y2:y2-dy*24})) shaft.setAttribute(key,value);
        const head = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
        head.setAttribute("points", x2+","+y2+" "+(x2-dx*36-dy*20)+","+(y2-dy*36+dx*20)+" "+(x2-dx*36+dy*20)+","+(y2-dy*36-dx*20));
        svg.append(shaft,head); fragment.append(svg);
      }
    }
    if (window.RegicideMotion) window.RegicideMotion.replace(get("coaching-board"), fragment, {
      scope:hidden ? report.job_id+":retry:"+retry.ply+":"+retry.attempt : (report?.job_id || source?.summary?.game_id || "preview") + (variation ? ":" + selectedPly + ":" + variation : ":main"),
      ply:hidden ? (retry.uci ? 1 : 0) : variation ? lineCursor : cursor, pieces, turn:position?.turn, lastMove:position?.last_move,
      orientation:blackView ? "black" : "white", enabled:visible, animate:motionStep
    });
    else get("coaching-board").replaceChildren(fragment);
    get("coaching-board").setAttribute("aria-label",hidden ? "다시 두기 체스판 · 기물과 도착 칸을 선택하세요." : "코칭 체스판 · 읽기 전용");
    get("coaching-board").classList.toggle("coaching-retry-board",hidden);
    get("coaching-badge-status").textContent=annotation ? annotation.symbol+" "+annotation.label+" · 방금 실제로 둔 수" :
      hidden ? "연습 중에는 실제 수의 평가를 가립니다." : variation ? "예상 수순에는 실제 대국의 평가 배지를 붙이지 않습니다." :
      viewBefore ? "두기 전 위치 · 실제 착수 평가는 ‘둔 뒤’에서 확인하세요." : "배지는 실제 착수 뒤, 그 수를 둔 기물에 표시합니다.";
    get("coaching-badge-legend").hidden=hidden;
    motionStep = false;
    get("coaching-position").textContent = hidden ? "다시 두기 · "+sideName(row?.color)+"의 수를 골라 보세요." : continuationReplay ?
      "추천 수순 설명 · "+lineCursor+"/"+continuationReplay.packet.available_plies+"수" : variation ? lineTitle() + " 수순 · " + lineCursor + "/" + Math.max(0,(line()?.pv_frames?.length || 1)-1) + "반수" :
      position ? cursor + "/" + Math.max(0,frames().length-1) + "반수 · " + sideName(position.turn) + " 차례" + (position.in_check ? " · 체크" : "") : "대국을 선택하세요.";
    const evaluation = boardEvaluation(position), comparison = evaluation.value;
    let share = evaluation.share ?? null;
    if (Number.isFinite(comparison?.score_cp_white)) share = .5 + .5 * comparison.score_cp_white / (Math.abs(comparison.score_cp_white) + 400);
    if (Number.isInteger(comparison?.mate_white)) {
      const winner = comparison.winner || (comparison.mate_white > 0 ? "white" : comparison.mate_white < 0 ? "black" : null);
      if (winner) share = winner === "white" ? 1 : 0;
    }
    get("coaching-evaluation-bar").classList.toggle("unknown", share === null);
    get("coaching-evaluation-bar").classList.toggle("black-view", blackView);
    get("coaching-evaluation-white").style.height = ((share ?? .5) * 100) + "%";
    get("coaching-evaluation-top").textContent = blackView ? "백" : "흑";
    get("coaching-evaluation-bottom").textContent = blackView ? "흑" : "백";
    get("coaching-evaluation-bar").setAttribute("aria-label", hidden ? "연습 중 평가 숨김" : evaluation.label ||
      "현재 위치 탐색 평가, 백 기준 " + score(comparison) + ". 승률이 아닙니다.");
    get("coaching-score").textContent = hidden ? "스스로 생각할 수 있도록 점수와 추천을 가렸습니다." : evaluation.label ||
      (comparison ? "현재 위치 탐색 평가 · " + score(comparison) + " · 백 기준" : evaluation.unsearchedLine ?
        "이 수순 위치는 별도로 평가하지 않았습니다." : "현재 위치 평가 대기 · 백 기준 점수");
  }

  function render() {
    if(openingView && !openingCurrent())stopOpening();
    if(strategyView && !strategyCurrent())stopStrategy();
    if(continuationReplay && !continuationCurrent())stopContinuation();
    const locked = busy || running(), row = selectedRow(), allFrames = frames(), pv = line();
    const hidden=exerciseHidden();
    get("coaching-view").hidden = !visible;
    get("coaching-title").textContent = libraryMode && !report ? "대국 보관함 · 기보 복기" : "대국 코칭";
    get("coaching-mode-button").setAttribute("aria-pressed",String(visible && (!libraryMode || Boolean(report))));
    get("archive-mode-button").setAttribute("aria-pressed",String(visible && libraryMode && !report));
    get("archive-mode-button").disabled = visible || analysisOpen;
    get("coaching-error").hidden = !failure; get("coaching-error").textContent = failure;
    for (const id of ["coaching-source","coaching-file","coaching-side","coaching-time"]) get(id).disabled = locked;
    get("coaching-start").disabled = locked || !source?.pgn || !session;
    get("coaching-start").textContent = report ? "다시 분석" : "코칭 시작";
    get("coaching-cancel").disabled = busy || !running(); get("coaching-return").disabled = busy;
    get("live-mode-button").disabled = busy && visible; get("analysis-mode-button").disabled = visible;
    const summary = source?.summary;
    get("coaching-game").textContent = summary ? (summary.white || (summary.human_color === "white" ? "나" : summary.engine_name || "Regicide")) + " — " +
      (summary.black || (summary.human_color === "black" ? "나" : summary.engine_name || "Regicide")) + " · " + summary.result + " · " + summary.ply_count + "반수" : "끝난 대국이나 보관함의 기보를 선택하세요.";
    get("coaching-game-time").textContent = summary ? "시작 " + recordedTime(summary,"started") + " · 종료 " + recordedTime(summary,"ended") : "";
    const seconds = (summary?.ply_count || 0) * Number(get("coaching-time").value) / 500;
    get("coaching-estimate").textContent = seconds ? "실제 수와 최선수를 각각 탐색합니다. " + duration(seconds) + " + 준비 시간. 결과는 순서대로 표시됩니다." : "기보를 선택하면 예상 시간을 표시합니다.";
    const done = report?.progress?.completed || 0, total = report?.progress?.total || 0;
    get("coaching-progress").textContent = busy ? "요청을 처리하고 있습니다…" : report ?
      (({running:"분석 중",completed:report.cached ? "저장된 분석 불러옴" : "분석 완료",cancelled:"분석 중지 · 완료된 수까지 표시",error:"분석 오류 · 완료된 수까지 표시"})[report.status] || "분석 대기") + " · " + done + "/" + total + "반수" : "시작을 누르면 엔진이 기보를 분석합니다.";
    get("coaching-progress-bar").hidden = !report;
    get("coaching-progress-bar").max = total || 1; get("coaching-progress-bar").value = done;
    const analyzed = rows().filter((item) => item.classification !== "pending");
    const sideSummary = (color, label) => {
      const sideRows = analyzed.filter((item) => item.color === color);
      return label + " " + sideName(color) + " · " + sideRows.length + "수 분석 / 다시 볼 국면 " + sideRows.filter((item) => criticalKinds.has(item.classification)).length + "개";
    };
    get("coaching-summary").textContent = report ? sideSummary(mine(), "나") + "\n" + sideSummary(mine() === "white" ? "black" : "white", "상대") :
      "양쪽의 수를 함께 분석해 실수와 놓친 기회를 찾습니다.";
    get("coaching-notice").textContent = report?.notice || (session?.status === "running" && !running() ?
      "다른 화면의 코칭이 진행 중입니다. 여기서 코칭을 시작하면 그 분석을 대신합니다." :
      "Regicide 탐색 기준입니다. 제한된 시간 안에 찾은 추천이며 더 깊게 분석하면 평가가 달라질 수 있습니다.");
    get("coaching-summary-card").hidden=hidden;get("coaching-moves-card").hidden=hidden;
    get("coaching-comparison").hidden=hidden;get("coaching-critical-navigation").hidden=hidden;
    get("coaching-position-navigation").hidden=hidden;
    get("coaching-before").disabled=!row || busy || (viewBefore && !variation);
    get("coaching-after").disabled=!row || busy || (!variation && !viewBefore && cursor===selectedPly);
    get("coaching-move-title").textContent = hidden ? positionTitle(row)+" · 다시 두기" : moveTitle(row);
    get("coaching-played-label").textContent = row ? (row.color === mine() ? "내가 둔 수" : "상대가 둔 수") + " · " + sideName(row.color) : "실제 둔 수";
    get("coaching-classification").textContent = !hidden && row ? (row.label || "분석 대기") + (Number.isFinite(row.loss_cp) ? " · 손실 " + (row.loss_cp / 100).toFixed(2) : "") : "";
    get("coaching-explanation").textContent = hidden ? "" : row?.explanation || "수별 평가에서 복기할 수를 선택하세요.";
    for (const kind of ["played","best"]) {
      const value = row?.[kind];
      get("coaching-" + kind).textContent = hidden ? "" : value ? (value.san || value.uci || "—") + " · " + score(value) + " · 깊이 " + (value.depth ?? "—") +
        (value.bound && value.bound !== "exact" ? " · 평가 경계" : "") : "분석 대기";
      get("coaching-" + kind + "-line").disabled = hidden || !value?.pv_frames || value.pv_frames.length < 2;
    }
    get("coaching-variation").hidden = !variation;
    get("coaching-variation-title").textContent = continuationReplay ?
      (continuationReplay.stage==="summary" ? "마무리" : continuationReplay.paused ? "잠깐 멈춤" : lineCursor ? "추천 수순 설명" : "추천 수순 시작")+
      " · "+lineCursor+"/"+continuationReplay.packet.available_plies+"수" : variation ? lineTitle() + " 이후 예상 응수 · " + lineCursor + "반수 진행" : "";
    get("coach-continuation-note").hidden=!continuationReplay;
    get("coach-continuation-note").textContent=continuationReplay ? "상대가 이 응수를 택했을 때의 예상 수순입니다. 양쪽 합계 "+continuationReplay.packet.available_plies+"수"+
      (continuationReplay.packet.terminal ? "에서 대국이 끝납니다." : continuationReplay.packet.available_plies<6 ? "까지만 확인됐습니다." : "를 따라봅니다.") : "";
    const sanList=document.createDocumentFragment();
    if(continuationReplay)for(const step of continuationReplay.packet.steps) {
      const span=document.createElement("span");span.textContent=step.san;span.className=step.ply_index===lineCursor ? "current" : "";
      span.setAttribute("aria-current",step.ply_index===lineCursor ? "step" : "false");sanList.append(span);
    }
    get("coaching-pv").replaceChildren(sanList);
    if(!continuationReplay)get("coaching-pv").textContent = hidden ? "" : pv?.pv_san?.join(" ") || "";
    get("coaching-line-prev").disabled = !variation || lineCursor <= 0;
    get("coaching-line-next").disabled = continuationReplay ? continuationReplay.stage==="summary" : !variation || lineCursor >= (pv?.pv_frames?.length || 1) - 1;
    get("coaching-line-next").textContent=continuationReplay && lineCursor===continuationReplay.packet.available_plies ? "마무리 보기" : "다음 응수";
    get("coaching-line-play").hidden=!continuationReplay;
    get("coaching-line-play").disabled=Boolean(continuationReplay?.done);
    get("coaching-line-play").textContent=continuationReplay?.paused ? "이어서 보기" : continuationReplay?.done ? "설명 완료" : "잠깐 멈춤";
    for (const id of ["coaching-first","coaching-prev"]) get(id).disabled = !allFrames.length || (!variation && cursor === 0);
    for (const id of ["coaching-last","coaching-next"]) get(id).disabled = !allFrames.length || (!variation && cursor >= allFrames.length-1);
    get("coaching-prev-critical").disabled = !criticalRows().some((item) => item.ply < selectedPly);
    get("coaching-next-critical").disabled = !criticalRows().some((item) => item.ply > selectedPly);
    const fragment = document.createDocumentFragment();
    for (const item of rows()) {
      if(hidden)break;
      if (!shown(item)) continue;
      const li = document.createElement("li"), button = document.createElement("button");
      button.type = "button"; button.className = "coaching-move"; button.dataset.coachingPly = String(item.ply);
      button.setAttribute("aria-pressed", item.ply === selectedPly ? "true" : "false");
      button.classList.toggle("coaching-critical", criticalKinds.has(item.classification));
      button.textContent = moveTitle(item) + " · " + (item.label || "분석 대기") + (Number.isFinite(item.loss_cp) ? " (" + (item.loss_cp/100).toFixed(2) + ")" : "");
      li.append(button); fragment.append(li);
    }
    get("coaching-moves").replaceChildren(fragment);
    renderCoach();
    renderContext();
    renderBoard();
  }

  function stopContext() {
    stopOpening();
    stopStrategy();
    stopContinuation();
    replayEpoch++;
    if (contextTimer !== null) clearTimeout(contextTimer);
    contextTimer = null; contextReplay = null;
  }
  function scheduleContext() {
    if (!contextReplay || contextReplay.paused || contextReplay.done) return;
    const ticket = replayEpoch;
    contextTimer = setTimeout(() => {
      contextTimer = null;
      if (!visible || busy || !contextReplay || contextReplay.paused || ticket !== replayEpoch) return;
      cursor++; selectedPly = Math.min(rows().length, cursor); viewBefore=false;motionStep = true;
      if (cursor >= contextReplay.targetPly) {
        selectedPly = contextReplay.targetPly; contextReplay.done = true;
      }
      render(); scheduleContext();
    }, 650);
  }
  function renderContext() {
    get("coaching-context").hidden = !contextReplay;
    get("coaching-context-pause").disabled = !contextReplay || contextReplay.done;
    get("coaching-context-skip").disabled = !contextReplay || contextReplay.done;
    get("coaching-context-pause").textContent = contextReplay?.paused ? "이어서 보기" : "잠깐 멈춤";
    const fragment = document.createDocumentFragment();
    if (contextReplay) {
      const total = contextReplay.targetPly-contextReplay.from;
      get("coaching-context-status").textContent = contextReplay.done ? "목표 수를 둔 뒤의 장면입니다. 기물 위 평가와 루크의 설명을 살펴보세요." :
        (contextReplay.paused ? "잠깐 멈춤" : "실제 수순 따라가는 중") + " · " + (cursor-contextReplay.from) + "/" + total + "반수";
      for (const row of rows().slice(contextReplay.from, contextReplay.targetPly)) {
        const span = document.createElement("span");
        span.textContent = moveTitle(row); span.className = cursor === row.ply ? "current" : "";
        span.setAttribute("aria-current", cursor === row.ply ? "step" : "false"); fragment.append(span);
      }
    }
    get("coaching-context-moves").replaceChildren(fragment);
    const active = get("coaching-context-moves").querySelector?.(".current");
    if (active) get("coaching-context-moves").scrollLeft = Math.max(0,active.offsetLeft-get("coaching-context-moves").offsetLeft-8);
  }
  function choosePly(ply, follow = true, lesson = false) {
    if (!rows().some((row) => row.ply === ply)) return;
    cancelExercise();
    const from = variation ? selectedPly : cursor, fromVariation=Boolean(variation);
    stopContext(); variation = null; lineCursor = 0; motionStep = false;viewBefore=false;
    if (follow && ply > from && (lesson || criticalKinds.has(rows().find((row) => row.ply === ply).classification))) {
      cursor = from; selectedPly = Math.max(1,Math.min(rows().length, cursor));
      contextReplay = {from,targetPly:ply,paused:false,done:false,lesson}; render(); scheduleContext();
    } else { selectedPly = ply; cursor = ply;motionStep=!fromVariation && Math.abs(ply-from)===1; render(); }
  }
  function seek(index) {
    cancelExercise();
    stopContext();
    motionStep = !variation && Math.abs(index-cursor) === 1;
    cursor = Math.max(0,Math.min(frames().length-1,index));
    selectedPly = Math.max(1,Math.min(rows().length,cursor));
    variation = null; lineCursor = 0;viewBefore=false; render();
  }
  function showBefore() {
    if(exerciseHidden() || !selectedRow())return;
    cancelExercise();stopContext();variation=null;lineCursor=0;cursor=selectedPly-1;viewBefore=true;motionStep=false;render();
  }
  function showLine(kind) {
    if(exerciseHidden())return;
    if (!selectedRow()?.[kind]?.pv_frames || selectedRow()[kind].pv_frames.length < 2) return;
    cancelExercise();stopContext(); motionStep = false;
    variation = kind; lineCursor = 0; render();
  }

  async function loadSource(value) {
    if (!visible || busy || running()) return;
    cancelExercise();stopContext(); window.RegicideMotion?.cancel(get("coaching-board"),true);
    get("coaching-setup").open=true;
    const ticket = ++epoch;
    busy = true; failure = ""; source = null; report = null; ownedJob = null; cursor = 0; selectedPly = 1; variation = null;viewBefore=false; render();
    try {
      let replay;
      if (value === "current") {
        const fresh = await request("/api/state");
        if (fresh.result === "*" || !fresh.moves?.length || fresh.handicap_mode) throw new Error("현재 대국이 끝난 뒤 코칭할 수 있습니다.");
        const pgn = await request("/api/pgn", undefined, true);
        replay = await request("/api/pgn-preview", {pgn});
        replay.summary.human_color = fresh.human_color;
        const saved = libraryGames.find((game) => game.game_id === fresh.game_id);
        if (saved) for (const field of ["started_at","ended_at","started_at_local","ended_at_local"])
          replay.summary[field] = saved[field];
      } else if (value) replay = await request("/api/games/" + encodeURIComponent(value));
      if (ticket !== epoch || !visible) return;
      source = replay || null;
      get("coaching-side").value = source?.summary?.human_color || "white";
      blackView = get("coaching-side").value === "black";
    } catch (error) { if (ticket === epoch) failure = error.message; }
    finally { if (ticket === epoch) { busy = false; render(); } }
  }

  async function loadFile(file) {
    if (!visible || busy || running() || !file) return;
    if (!file.size || file.size > 1048576) { failure = "비어 있지 않은 1MiB 이하의 PGN 파일을 선택하세요."; render(); return; }
    cancelExercise();stopContext(); window.RegicideMotion?.cancel(get("coaching-board"),true);
    get("coaching-setup").open=true;
    const ticket = ++epoch;
    busy = true; failure = ""; render();
    try {
      const pgn = new TextDecoder("utf-8", {fatal:true,ignoreBOM:true}).decode(await file.arrayBuffer());
      const replay = await request("/api/pgn-preview", {pgn});
      if (ticket !== epoch || !visible) return;
      source = replay; report = null; ownedJob = null; cursor = 0; selectedPly = 1; variation = null;viewBefore=false;
      get("coaching-source").value = ""; get("coaching-side").value = "white"; blackView = false;
    } catch (error) { if (ticket === epoch) failure = error instanceof TypeError ? "UTF-8 형식의 PGN 파일을 선택하세요." : error.message; }
    finally { if (ticket === epoch) { busy = false; get("coaching-file").value = ""; render(); } }
  }

  async function enter(asLibrary = false) {
    if (visible || analysisOpen) return;
    cancelExercise();stopContext();get("coaching-setup").open=true;
    libraryMode = asLibrary;
    visible = true; busy = true; failure = ""; source = null; report = null; session = null; ownedJob = null;
    cursor = 0; selectedPly = 1; variation = null;viewBefore=false; returnNotice.hidden = true;
    const ticket = ++epoch;
    get("live-view").hidden = true;
    get("coaching-mode-button").setAttribute("aria-pressed", "true");
    get("live-mode-button").setAttribute("aria-pressed", "false");
    window.dispatchEvent(new CustomEvent("regicide-coaching-mode", {detail:true}));
    render();
    try {
      const [next, live, library] = await Promise.all([request("/api/coaching"),request("/api/state"),request("/api/games")]);
      if (ticket !== epoch || !visible) return;
      accept(next);
      libraryGames = library.games || [];
      const options = document.createDocumentFragment();
      const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "기보를 선택하세요"; options.append(placeholder);
      const recent = document.createElement("option"); recent.value = "current"; recent.textContent = "방금 둔 대국";
      recent.disabled = live.result === "*" || !live.moves?.length || Boolean(live.handicap_mode); options.append(recent);
      for (const game of libraryGames) {
        const option = document.createElement("option"); option.value = game.game_id;
        option.textContent = recordedTime(game,"started") + " · " + game.status + " · " + game.ply_count + "반수 · 내 기물 " + sideName(game.human_color);
        options.append(option);
      }
      get("coaching-source").replaceChildren(options);
      get("coaching-source").value = recent.disabled || asLibrary ? "" : "current";
      if (!recent.disabled && !asLibrary) { busy = false; await loadSource("current"); }
    } catch (error) { if (ticket === epoch) failure = error.message; }
    finally { if (ticket === epoch) { busy = false; render(); } }
  }

  async function action(kind) {
    if (!visible || busy || !session || (kind === "start" && (!source?.pgn || running())) || (kind === "cancel" && !running())) return;
    cancelExercise();stopContext(); window.RegicideMotion?.cancel(get("coaching-board"),true);
    const ticket = ++epoch;
    busy = true; failure = ""; render();
    try {
      const payload = {action:kind, revision:session.revision};
      if (kind === "start") Object.assign(payload, {pgn:source.pgn,human_color:get("coaching-side").value,movetime_ms:Number(get("coaching-time").value)});
      const next = await request("/api/coaching", payload);
      if (ticket !== epoch || !visible) return;
      if (accept(next)) {
        ownedJob = session.job_id;
        report = session;
        if (kind === "start") { cursor = Math.min(1,Math.max(0,frames().length-1)); selectedPly = 1; variation = null;viewBefore=false; blackView = mine() === "black"; get("coaching-setup").open=false; }
      }
    } catch (error) {
      if (ticket === epoch) {
        if (kind === "start") {
          if (error.coaching) accept(error.coaching);
          ownedJob = null; report = null; variation = null;
        } else if (error.coaching && error.coaching.job_id !== ownedJob) acceptForeign(error.coaching);
        else if (error.coaching && accept(error.coaching)) report = session;
        failure = error.message;
      }
    } finally { if (ticket === epoch) { busy = false; render(); } }
  }

  async function poll() {
    if (!visible || busy || polling || !running()) return;
    polling = true;
    const ticket = epoch, revision = session.revision, job = session.job_id;
    try {
      const next = await request("/api/coaching");
      if (ticket === epoch && visible && session?.revision === revision && session?.job_id === job) {
        if (next.job_id !== job) acceptForeign(next);
        else if (accept(next)) { stopContinuation();report = session; failure = session.error || ""; }
        render();
      }
    } catch (error) { if (ticket === epoch && visible) { failure = error.message; render(); } }
    finally { polling = false; }
  }

  async function leave() {
    if (!visible || busy) return;
    cancelExercise();stopContext(); window.RegicideMotion?.cancel(get("coaching-board"),true);
    const ticket = ++epoch;
    busy = true; failure = ""; render();
    try {
      if (running()) accept(await request("/api/coaching", {action:"cancel",revision:session.revision}));
    } catch (error) { returnNotice.textContent = "코칭 중지를 확인하지 못했습니다. 코칭 화면에서 다시 확인해 주세요. " + error.message; returnNotice.hidden = false; }
    finally {
      if (ticket === epoch) {
        visible = false; busy = false; variation = null; ownedJob = null;
        get("live-view").hidden = false;
        get("coaching-mode-button").setAttribute("aria-pressed", "false");
        get("live-mode-button").setAttribute("aria-pressed", "true");
        window.dispatchEvent(new CustomEvent("regicide-coaching-mode", {detail:false})); render();
      }
    }
  }

  get("coaching-mode-button").addEventListener("click", () => { void enter(); });
  get("archive-mode-button").addEventListener("click", () => { void enter(true); });
  get("live-mode-button").addEventListener("click", () => { void leave(); });
  get("coaching-return").addEventListener("click", () => { void leave(); });
  get("coaching-source").addEventListener("change", () => { void loadSource(get("coaching-source").value); });
  get("coaching-file").addEventListener("change", () => { void loadFile(get("coaching-file").files?.[0]); });
  get("coaching-side").addEventListener("change", () => { cancelExercise();stopContext(); blackView = get("coaching-side").value === "black"; render(); });
  get("coaching-time").addEventListener("change", render);
  get("coaching-start").addEventListener("click", () => { void action("start"); });
  get("coaching-cancel").addEventListener("click", () => { void action("cancel"); });
  get("coaching-flip").addEventListener("click", () => { clearRetryPointer();stopSpeech();stopContext(); blackView = !blackView; render(); });
  get("coaching-first").addEventListener("click", () => seek(0));
  get("coaching-prev").addEventListener("click", () => seek(cursor-1));
  get("coaching-next").addEventListener("click", () => seek(cursor+1));
  get("coaching-last").addEventListener("click", () => seek(frames().length-1));
  get("coaching-before").addEventListener("click",showBefore);
  get("coaching-after").addEventListener("click",()=>{if(!exerciseHidden())choosePly(selectedPly,false);});
  get("coaching-prev-critical").addEventListener("click", () => { const row = criticalRows().filter((item) => item.ply < selectedPly).at(-1); if (row) choosePly(row.ply); });
  get("coaching-next-critical").addEventListener("click", () => { const row = criticalRows().find((item) => item.ply > selectedPly); if (row) choosePly(row.ply); });
  get("coaching-filter").addEventListener("change", () => { cancelExercise();stopContext(); render(); });
  get("coaching-moves").addEventListener("click", (event) => { const button = event.target.closest("button[data-coaching-ply]"); if (button) choosePly(Number(button.dataset.coachingPly)); });
  for (const kind of ["best","played"]) get("coaching-"+kind+"-line").addEventListener("click", () => showLine(kind));
  get("coaching-line-prev").addEventListener("click", () => stepLine(-1));
  get("coaching-line-next").addEventListener("click", () => stepLine(1));
  get("coaching-line-play").addEventListener("click",pauseContinuation);
  get("coach-continuation").addEventListener("click",startContinuation);
  for(const id of ["idea","plan","counterplay"])get("coach-strategy-"+id).addEventListener("click",()=>showStrategy(id));
  get("coach-strategy-return").addEventListener("click",leaveStrategy);
  for(const id of ["white","black","balance"])get("coach-opening-"+id).addEventListener("click",()=>showOpening(id));
  get("coach-opening-return").addEventListener("click",leaveOpening);
  get("coach-opening-jump").addEventListener("click",jumpOpening);
  get("coaching-mainline").addEventListener("click", () => { stopSpeech();stopContext(); motionStep=false; variation = null;cursor=selectedPly;viewBefore=false; render(); });
  get("coaching-context-pause").addEventListener("click", () => {
    if (!contextReplay || contextReplay.done) return;
    if (contextReplay.paused) { contextReplay.paused=false; scheduleContext(); }
    else { contextReplay.paused=true; if(contextTimer!==null)clearTimeout(contextTimer); contextTimer=null; }
    render();
  });
  get("coaching-context-skip").addEventListener("click", () => {
    if (!contextReplay) return;
    const target=contextReplay.targetPly; choosePly(target,false);
  });
  get("coach-retry").addEventListener("click",startRetry);
  get("coach-retry-again").addEventListener("click",()=>{if(!retry?.submitting)startRetry();});
  get("coach-retry-cancel").addEventListener("click",()=>{cancelExercise();motionStep=false;render();});
  get("coach-retry-reveal").addEventListener("click",()=>{
    if(!retry || retry.submitting)return;
    stopSpeech();clearRetryPointer();retry.revealed=true;retry.selected=null;retry.promotion=[];motionStep=false;
    window.RegicideMotion?.cancel(get("coaching-board"),true);render();
  });
  get("coach-retry-candidate-line").addEventListener("click",()=>showRetryLine("retry_candidate"));
  get("coach-retry-best-line").addEventListener("click",()=>showRetryLine("retry_best"));
  get("coach-lesson-prev").addEventListener("click",()=>{const ply=lessons().filter((item)=>item<selectedPly).at(-1);if(ply)choosePly(ply,true,true);});
  get("coach-lesson-next").addEventListener("click",()=>{const ply=lessons().find((item)=>item>selectedPly);if(ply)choosePly(ply,true,true);});
  get("coach-lessons").addEventListener("click",(event)=>{const button=event.target.closest("button[data-lesson-ply]");if(button)choosePly(Number(button.dataset.lessonPly),true,true);});
  get("coach-evidence").addEventListener("click",(event)=>{const button=event.target.closest("button[data-evidence-index]");if(button)showEvidence(Number(button.dataset.evidenceIndex));});
  get("coach-focus").addEventListener("click",()=>{
    if(exerciseHidden() || (contextReplay && !contextReplay.done) || !selectedRow()?.coach?.focus_squares?.length)return;
    stopSpeech();stopContext();variation=null;cursor=selectedPly-1;viewBefore=true;motionStep=false;
    evidenceFocus={ply:selectedPly,line:null,squares:selectedRow().coach.focus_squares};render();
  });
  get("coach-speak").addEventListener("click",toggleSpeech);
  get("coach-speech-stop").addEventListener("click",stopCurrentSpeech);
  get("coach-overview-box").addEventListener("toggle",()=>{
    const opened=Boolean(get("coach-overview-box").open);
    speechOverview=opened;
    if(speechOverview){stopOpening();stopStrategy();stopContinuation();}
    render();
    // Leaving a deep explanation changes the scene key during render. The
    // explicit overview choice still owns the narration after that reset.
    speechOverview=opened;scheduleSpeech();
  });
  window.speechSynthesis?.addEventListener?.("voiceschanged",()=>{renderSpeech();scheduleSpeech();});
  get("coaching-board").addEventListener("click",(event)=>{
    if(Date.now()<suppressRetryClickUntil)return;
    const cell=event.target.closest("[data-coaching-square]");if(cell)retrySquare(cell.dataset.coachingSquare);
  });
  for(const kind of ["pointer","mouse"]) {
    get("coaching-board").addEventListener(kind+"down",(event)=>beginRetryPointer(event,kind));
    window.addEventListener(kind+"move",(event)=>moveRetryPointer(event,kind));
    window.addEventListener(kind+"up",(event)=>endRetryPointer(event,kind));
  }
  window.addEventListener("pointercancel",clearRetryPointer);
  window.addEventListener("blur",clearRetryPointer);
  get("coaching-board").addEventListener("dragstart",(event)=>event.preventDefault());
  window.addEventListener("regicide-analysis-mode", (event) => { analysisOpen = event.detail === true; get("coaching-mode-button").disabled = analysisOpen; get("archive-mode-button").disabled = analysisOpen || visible; });
  window.addEventListener("keydown", (event) => {
    if (!visible || busy || event.altKey || event.ctrlKey || event.metaKey || ["INPUT","SELECT","TEXTAREA"].includes(event.target?.tagName)) return;
    if(event.key==="Escape" && exerciseHidden() && !retry.submitting){retry.selected=null;retry.promotion=[];clearRetryPointer();render();event.preventDefault();}
    else if ((event.key==="Enter" || event.key===" ") && event.target?.dataset?.coachingSquare && exerciseHidden()) {
      retrySquare(event.target.dataset.coachingSquare);event.preventDefault();
    } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const delta = event.key === "ArrowLeft" ? -1 : 1;
      if (variation) stepLine(delta);
      else seek(cursor+delta);
      event.preventDefault();
    }
  });
  const timer = setInterval(() => { void poll(); }, 400);
  window.addEventListener("pagehide", () => { cancelExercise();stopContext(); window.RegicideMotion?.cancel(get("coaching-board"),true); ++epoch; visible = false; clearInterval(timer); });
  document.addEventListener?.("visibilitychange", () => {
    if (document.hidden) { cancelExercise();stopContext(); window.RegicideMotion?.cancel(get("coaching-board"),true); render(); }
    else scheduleSpeech();
  });
  render();
})();
