"""Post-game coaching from independent, bounded Regicide searches.

The original PGN is legally replayed with its full history. Every comparison
starts at the *same* position: unrestricted search versus the played move
restricted at the root. No other engine, language model or invented chess
explanation participates. Scores are estimates, not objective move grades.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

import chess
import chess.engine

from pgn_preview import preview_pgn
from coach_explanations import describe_move, describe_report


SCHEMA = 2
PROFILES = (500, 1500, 5000)
MAX_PV_PLIES = 12
MAX_CACHE_BYTES = 32 * 1024 * 1024
NOTICE = ("Regicide의 제한 시간 탐색 기준입니다. 점수는 백 기준이며 +는 백, −는 흑에게 유리합니다. "
          "1.00은 폰 1개 정도의 평가 단위이며 승률이 아닙니다. 더 긴 분석에서 추천과 분류가 달라질 수 있습니다.")
LABELS = {"pending": "분석 대기", "best": "추천 수", "good": "좋은 수",
          "inaccuracy": "아쉬운 수", "mistake": "실수", "blunder": "큰 실수",
          "missed_mate": "메이트 기회 놓침", "allows_mate": "메이트 허용",
          "uncertain": "판단 보류"}
SEVERITY = {"allows_mate": 6, "missed_mate": 5, "blunder": 4,
            "mistake": 3, "inaccuracy": 2}
# Chess.com Game Review notation; the classification remains Regicide's own.
# Great is !; !! denotes Brilliant (a sound sacrifice), which we do not grade.
ANNOTATION_SYMBOLS = {"best": "★", "good": "👍", "inaccuracy": "?!", "mistake": "?",
                      "blunder": "??", "missed_mate": "×", "allows_mate": "??"}


class CoachingRevisionError(ValueError):
    """A client tried to replace or cancel a different coaching job."""


def _frame(board, ply, human_color):
    # Imported games may continue after an optional draw claim. Only automatic
    # board outcomes are terminal positions in the review and its PV frames.
    outcome = board.outcome(claim_draw=False)
    return {"ply": ply, "fen": board.fen(en_passant="fen"),
            "board": {chess.square_name(s): p.symbol() for s, p in board.piece_map().items()},
            "turn": "white" if board.turn else "black", "human_color": human_color,
            "last_move": board.peek().uci() if board.move_stack else None,
            "in_check": board.is_check(), "result": outcome.result() if outcome else "*",
            "status": "코칭 복기", "legal_moves": [], "human_to_move": False,
            "engine_to_move": False, "can_undo": False, "can_handicap": False,
            "handicap_mode": False, "error": None}


def _score(info):
    value = info.get("score")
    if not isinstance(value, chess.engine.PovScore):
        return None
    white = value.white()
    cp, mate = white.score(), white.mate()
    if cp is None and mate is None:
        return None
    winner = None
    if mate is not None:
        # MateGiven and Mate(0) both expose mate()==0 but have opposite winners.
        winner = "white" if white.score(mate_score=100000) > 0 else "black"
    bound = "lower" if info.get("lowerbound") else "upper" if info.get("upperbound") else "exact"
    if not value.turn and bound != "exact":
        bound = "upper" if bound == "lower" else "lower"
    return {"score_cp_white": cp, "mate_white": mate, "winner": winner,
            "bound": bound, "depth": info.get("depth"), "nodes": info.get("nodes")}


def _line(board, info, human_color, required=None):
    score = _score(info)
    raw = info.get("pv")
    if score is None or not isinstance(raw, (list, tuple)) or not raw:
        return None
    if required is not None and raw[0] != required:
        return None
    replay = board.copy(stack=True)
    pv_uci, pv_san, frames = [], [], [_frame(replay, len(board.move_stack), human_color)]
    for move in raw[:MAX_PV_PLIES]:
        if not isinstance(move, chess.Move) or move not in replay.legal_moves:
            # Never show a malformed/truncated engine line as reliable evidence.
            return None
        pv_san.append(replay.san(move))
        pv_uci.append(move.uci())
        replay.push(move)
        frames.append(_frame(replay, len(replay.move_stack), human_color))
    return {**score, "uci": pv_uci[0], "san": pv_san[0], "pv_uci": pv_uci,
            "pv_san": pv_san, "pv_frames": frames, "pv_truncated": len(raw) > MAX_PV_PLIES}


def _mover_mate(line, color):
    if line["mate_white"] is None:
        return 0
    return 1 if line["winner"] == color else -1


def _classify(best, played, color, same):
    if best is None or played is None or best["bound"] != "exact" or played["bound"] != "exact":
        return "uncertain", None
    if same:
        return "best", 0
    best_mate, played_mate = _mover_mate(best, color), _mover_mate(played, color)
    if best_mate == 1 and played_mate != 1:
        return "missed_mate", None
    if played_mate == -1 and best_mate != -1:
        return "allows_mate", None
    if best_mate or played_mate:
        # Different mate distances are not pawn losses. If restricted search
        # finds a better outcome than unrestricted, explicitly withhold a grade.
        return ("uncertain", None) if played_mate > best_mate else ("good", None)
    direction = 1 if color == "white" else -1
    loss = direction * (best["score_cp_white"] - played["score_cp_white"])
    if loss < 0:
        return "uncertain", None
    if loss >= 200:
        return "blunder", loss
    if loss >= 100:
        return "mistake", loss
    if loss >= 50:
        return "inaccuracy", loss
    return "good", loss


def _can_compare_alternatives(board, best, actual):
    """Spend the second search only on a reliable nonlosing actual best move."""
    if (best is None or best["uci"] != actual or best["bound"] != "exact"
            or board.legal_moves.count() < 2):
        return False
    color = "white" if board.turn else "black"
    if best["mate_white"] is not None:
        return best["winner"] == color
    return (1 if board.turn else -1) * best["score_cp_white"] >= 0


def _annotation(board, row):
    kind = row["classification"]
    if kind not in ANNOTATION_SYMBOLS:
        return None
    symbol, label = ANNOTATION_SYMBOLS[kind], LABELS[kind]
    reason = "Regicide의 제한 시간 분석에 따른 수 분류이며, 더 긴 탐색에서 달라질 수 있습니다."
    alternative = row.get("alternative")
    if (kind == "best" and _can_compare_alternatives(board, row["best"], row["uci"])
            and alternative is not None and alternative["bound"] == "exact"
            and alternative["uci"] != row["uci"]):
        color = "white" if board.turn else "black"
        losing_alternative = (alternative["winner"] != color if alternative["mate_white"] is not None
                              else (1 if board.turn else -1) * alternative["score_cp_white"] <= -200)
        if losing_alternative:
            kind, symbol, label = "great", "!", "탁월 후보"
            reason = ("실제 수를 제외한 모든 합법수 중 최선 대안도 같은 제한 시간으로 탐색했습니다. "
                      "이번 탐색에서는 실제 수가 착수한 쪽 기준 0.00 이상 또는 승리 메이트로, "
                      "다른 최선 대안이 −2.00 이하 또는 패배 메이트로 평가되어 탁월 후보로 표시합니다. "
                      "객관적인 유일수나 희생 묘수를 증명한 것은 아니며 더 긴 탐색에서 달라질 수 있습니다.")
    return {"symbol": symbol, "kind": kind, "label": label, "square": row["uci"][2:4],
            "scope": "actual_move", "provisional": True, "ply": row["ply"], "uci": row["uci"],
            "fen_after": row["fen_after"], "reason": reason}


def _formatted_score(line):
    if not line:
        return "평가 없음"
    if line["mate_white"] is not None:
        side = "백" if line["winner"] == "white" else "흑"
        return f"{side} 메이트 {abs(line['mate_white'])}"
    return f"{line['score_cp_white'] / 100:+.2f}"


def _explain(row):
    kind, best, played = row["classification"], row["best"], row["played"]
    if kind == "uncertain":
        return "탐색 결과를 확실하게 비교할 수 없어 분류를 보류했습니다. 더 긴 시간으로 다시 분석해 보세요."
    if kind == "best":
        lead = f"{row['san']}은 이 시간 안에 Regicide가 찾은 추천 수와 같습니다."
    elif kind == "missed_mate":
        lead = f"{best['san']}에서는 메이트 수순을 찾았지만, 실제 수 {row['san']}에서는 그 기회를 확인하지 못했습니다."
    elif kind == "allows_mate":
        lead = f"{row['san']} 뒤 상대의 메이트 수순이 발견됐습니다. 추천 수 {best['san']}에서는 같은 결과가 발견되지 않았습니다."
    elif row["loss_cp"] is not None:
        lead = (f"추천 수 {best['san']}({ _formatted_score(best) })과 비교해 실제 수 {row['san']}"
                f"({ _formatted_score(played) })에서 둔 쪽의 평가가 {row['loss_cp'] / 100:.2f} 낮아졌습니다.")
    else:
        lead = "두 수의 탐색 결과가 같은 메이트 방향입니다. 메이트까지의 길이는 실수 점수로 환산하지 않았습니다."
    if played and len(played["pv_san"]) > 1:
        reply = played["pv_san"][1]
        if reply.endswith("#"):
            lead += f" 상대는 {reply}로 체크메이트할 수 있습니다."
        elif "x" in reply:
            lead += f" 상대의 예상 응수 {reply}는 기물을 잡는 수입니다. 이후 수순까지 비교해 보세요."
        elif reply.endswith("+"):
            lead += f" 상대의 예상 응수 {reply}는 체크입니다."
        else:
            lead += f" 상대의 예상 응수는 {reply}입니다."
    return lead


def _summary(rows, human_color):
    ready = [row for row in rows if row["classification"] != "pending"]
    critical = sorted((row for row in ready if row["classification"] in SEVERITY),
                      key=lambda row: (-SEVERITY[row["classification"]], -(row["loss_cp"] or 0), row["ply"]))
    by_color = {}
    for color in ("white", "black"):
        side_rows = [row for row in ready if row["color"] == color]
        side_critical = [row["ply"] for row in critical if row["color"] == color]
        by_color[color] = {"analyzed": len(side_rows),
                           "counts": {kind: sum(row["classification"] == kind for row in side_rows)
                                      for kind in LABELS if kind != "pending"},
                           "critical_count": len(side_critical), "critical_plies": side_critical}
    human = by_color[human_color]
    opponent = by_color["black" if human_color == "white" else "white"]
    all_counts = {kind: by_color["white"]["counts"][kind] + by_color["black"]["counts"][kind]
                  for kind in LABELS if kind != "pending"}
    message = (f"내 수 {human['analyzed']}개 · 상대 수 {opponent['analyzed']}개 분석 · "
               f"다시 볼 국면: 내 {human['critical_count']}개, 상대 {opponent['critical_count']}개")
    return {"counts": human["counts"],  # Legacy clients interpret this field as the human side.
            "all_counts": all_counts, "by_color": by_color,
            "human_counts": human["counts"], "opponent_counts": opponent["counts"],
            "total_analyzed": len(ready), "human_analyzed": human["analyzed"],
            "opponent_analyzed": opponent["analyzed"], "human_critical_count": human["critical_count"],
            "opponent_critical_count": opponent["critical_count"],
            "critical_count": len(critical), "message": message}, [row["ply"] for row in critical]


def _cached_line(value, board, human_color, required=None):
    """Rebuild all cached chess/presentation data from typed scores and legal PV."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid cached line")
    cp, mate = value["score_cp_white"], value["mate_white"]
    winner = value["winner"]
    if cp is not None:
        if type(cp) is not int or abs(cp) > 1_000_000 or mate is not None or winner is not None:
            raise ValueError("invalid cached cp")
        score = chess.engine.Cp(cp)
    else:
        if type(mate) is not int or abs(mate) > 10_000 or winner not in ("white", "black"):
            raise ValueError("invalid cached mate")
        if mate and winner != ("white" if mate > 0 else "black"):
            raise ValueError("inconsistent cached mate")
        score = chess.engine.MateGiven if mate == 0 and winner == "white" else chess.engine.Mate(mate)
    pv = value["pv_uci"]
    if not isinstance(pv, list) or not 1 <= len(pv) <= MAX_PV_PLIES:
        raise ValueError("invalid cached pv")
    if value["bound"] not in ("exact", "lower", "upper"):
        raise ValueError("invalid cached bound")
    for field in ("depth", "nodes"):
        if value[field] is not None and (type(value[field]) is not int or value[field] < 0):
            raise ValueError("invalid cached search metadata")
    info = {"score": chess.engine.PovScore(score, chess.WHITE),
            "pv": [chess.Move.from_uci(uci) for uci in pv],
            "depth": value["depth"], "nodes": value["nodes"],
            "lowerbound": value["bound"] == "lower", "upperbound": value["bound"] == "upper"}
    result = _line(board, info, human_color, required)
    if result is None or value["uci"] != result["uci"]:
        raise ValueError("illegal cached line")
    result["pv_truncated"] = value.get("pv_truncated") is True
    return result


class CoachingSession:
    def __init__(self, binary, engine_name="Regicide", save_dir=None, engine_factory=None, *, evaluation_id="classical"):
        self.binary = Path(binary)
        self.engine_name = engine_name
        self._factory = engine_factory or (lambda path: chess.engine.SimpleEngine.popen_uci([str(path), "uci"], timeout=3))
        self._cache_dir = Path(save_dir) / "coaching" if save_dir is not None else None
        self._engine_identity = {"name": engine_name, "binary_sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                                 "evaluation_id": str(evaluation_id)}
        self._lock = threading.RLock()
        self._closed = False
        self._cancel = threading.Event()
        self._engine = None
        self._worker = None
        self._retry = None
        self.revision = 0
        self._state = {"revision": 0, "job_id": None, "status": "idle", "progress": {"completed": 0, "total": 0},
                       "game": {}, "settings": {}, "frames": [], "moves": [], "summary": {}, "critical_plies": [],
                       "error": None, "engine": self._engine_identity, "cached": False, "notice": NOTICE,
                       "coach": describe_report([], [])}

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def _check(self, revision):
        if self._closed:
            raise ValueError("코칭 화면이 종료됐습니다. 프로그램을 다시 실행해 주세요.")
        if type(revision) is not int or revision != self.revision:
            raise CoachingRevisionError("코칭 대상이 바뀌었습니다. 현재 화면에서 다시 선택해 주세요.")

    @staticmethod
    def _close_engine(engine):
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass

    def _stop_locked(self):
        self._cancel.set()
        engine, self._engine = self._engine, None
        retry, self._retry = self._retry, None
        if retry is not None:
            retry["cancel"].set()
        return [engine, retry.get("engine") if retry else None]

    def start(self, pgn, human_color, movetime_ms, revision):
        with self._lock:
            self._check(revision)
        if human_color not in ("white", "black"):
            raise ValueError("코칭할 색상을 백 또는 흑으로 선택해 주세요.")
        if movetime_ms is None:
            movetime_ms = 1500
        if type(movetime_ms) is not int or movetime_ms not in PROFILES:
            raise ValueError("분석 시간은 500, 1500, 5000ms 중에서 선택해 주세요.")
        # Expensive parsing and all validation happen before replacing a job.
        preview = preview_pgn(pgn)
        root = chess.Board(preview["summary"]["initial_fen"])
        board, frames, rows = root.copy(stack=True), [], []
        for entry in preview["moves"]:
            frames.append(_frame(board, len(rows), human_color))
            if board.is_game_over(claim_draw=False):
                raise ValueError("이미 종료된 체스판 뒤에 착수가 있는 기보는 코칭할 수 없습니다.")
            color = "white" if board.turn else "black"
            row = {"ply": entry["ply"], "color": color, "actor": "human" if color == human_color else "engine",
                   "uci": entry["uci"], "san": entry["san"], "fen_before": board.fen(en_passant="fen"),
                   "classification": "pending", "label": LABELS["pending"], "loss_cp": None,
                   "best": None, "played": None, "alternative": None, "annotation": None,
                   "explanation": "이 수를 분석할 차례를 기다리고 있습니다.",
                   "coach": None, "retry_legal_moves": []}
            if entry.get("forced"):
                row["forced"] = True
            board.push_uci(entry["uci"])
            row["fen_after"] = board.fen(en_passant="fen")
            rows.append(row)
        frames.append(_frame(board, len(rows), human_color))
        summary, critical = _summary(rows, human_color)
        game = {key: preview["summary"][key] for key in ("result", "white", "black", "initial_fen", "ply_count")}
        game["human_color"] = human_color
        game["incomplete"] = game["result"] == "*"
        identity = {"schema": SCHEMA, "pgn_sha256": hashlib.sha256(pgn.encode("utf-8")).hexdigest(),
                    "human_color": human_color, "movetime_ms": movetime_ms, "engine": self._engine_identity}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cached = self._read_cache(key, identity, rows)
        with self._lock:
            self._check(revision)
            old_engines = self._stop_locked()
            self.revision += 1
            cancel = self._cancel = threading.Event()
            job = uuid.uuid4().hex
            self._state = {"revision": self.revision, "job_id": job, "status": "running",
                           "progress": {"completed": 0, "total": len(rows)}, "game": game,
                           "settings": {"movetime_ms": movetime_ms, "estimated_seconds": len(rows) * 2 * movetime_ms / 1000,
                                        "comparison": "same_root_equal_time", "thresholds_cp": {"inaccuracy": 50, "mistake": 100, "blunder": 200},
                                        "annotation_policy": "regicide_sole_nonlosing_candidate_v1"},
                           "frames": frames, "moves": rows, "summary": summary, "critical_plies": critical,
                           "error": None, "engine": self._engine_identity, "cached": False,
                           "coach": describe_report(rows, critical),
                           "notice": NOTICE + (" 중단된 기보는 기록된 수까지만 분석합니다." if game["incomplete"] else "")}
            if cached is not None:
                self._state.update(moves=cached["moves"], summary=cached["summary"], critical_plies=cached["critical_plies"],
                                   status="completed", progress={"completed": len(rows), "total": len(rows)}, cached=True,
                                   coach=describe_report(cached["moves"], cached["critical_plies"]))
            else:
                worker = threading.Thread(target=self._run, args=(job, root, rows, movetime_ms, human_color, cancel, key, identity),
                                          daemon=True, name="regicide-coaching")
                self._worker = worker
            initial = copy.deepcopy(self._state)
        for engine in old_engines:
            self._close_engine(engine)
        if cached is None:
            worker.start()
        return initial

    def cancel(self, revision):
        with self._lock:
            self._check(revision)
            engines = self._stop_locked()
            self.revision += 1
            self._state["revision"] = self.revision
            if self._state["status"] == "running":
                self._state["status"] = "cancelled"
            result = copy.deepcopy(self._state)
        for engine in engines:
            self._close_engine(engine)
        return result

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            engines = self._stop_locked()
        for engine in engines:
            self._close_engine(engine)

    def _check_retry_locked(self, attempt):
        if attempt.get("expired"):
            raise ValueError("다시 두기 분석 시간이 초과됐습니다. 잠시 후 다시 시도해 주세요.")
        if (self._closed or self._retry is not attempt or attempt["cancel"].is_set()
                or self.revision != attempt["revision"] or self._state["job_id"] != attempt["job_id"]):
            raise CoachingRevisionError("코칭 대상이나 상태가 바뀌어 이전 다시 두기 결과를 취소했습니다.")

    def retry(self, ply, uci, revision, job_id):
        """Evaluate a private candidate without replacing report or game state."""
        with self._lock:
            self._check(revision)
            if not isinstance(job_id, str) or not job_id or job_id != self._state["job_id"]:
                raise CoachingRevisionError("다시 두기 대상이 바뀌었습니다. 현재 국면에서 다시 시도해 주세요.")
            rows = self._state["moves"]
            if (self._state["status"] not in ("completed", "cancelled") or type(ply) is not int
                    or not 1 <= ply <= len(rows) or rows[ply - 1]["classification"] == "pending"):
                raise ValueError("전체 분석을 완료하거나 중단한 뒤, 분석이 끝난 수에서 다시 두기를 시작해 주세요.")
            if self._retry is not None:
                raise ValueError("다른 다시 두기 수를 확인하고 있습니다. 결과가 나온 뒤 다시 시도해 주세요.")
            board = chess.Board(self._state["game"]["initial_fen"])
            for row in rows[:ply - 1]:
                board.push_uci(row["uci"])
            if not isinstance(uci, str) or len(uci) not in (4, 5):
                raise ValueError("다시 둘 기물과 도착 칸을 선택해 주세요.")
            try:
                move = chess.Move.from_uci(uci)
            except ValueError as exc:
                raise ValueError("다시 두기 수의 형식이 올바르지 않습니다.") from exc
            if move not in board.legal_moves:
                raise ValueError("이 국면에서 둘 수 없는 수입니다. 가능한 도착 칸을 선택해 주세요.")
            budget = self._state["settings"]["movetime_ms"]
            human_color = self._state["game"]["human_color"]
            attempt = {"revision": revision, "job_id": job_id, "cancel": threading.Event(), "engine": None, "expired": False}
            self._retry = attempt

        def expire():
            with self._lock:
                if self._retry is not attempt:
                    return
                attempt["expired"] = True
                attempt["cancel"].set()
                peer = attempt["engine"]
            self._close_engine(peer)

        timer = threading.Timer(2 * budget / 1000 + 4, expire)
        timer.daemon = True
        timer.start()
        engine = None
        try:
            engine = self._factory(self.binary)
            with self._lock:
                self._check_retry_locked(attempt)
                attempt["engine"] = engine
            best_info = engine.analyse(board.copy(stack=True), chess.engine.Limit(time=budget / 1000),
                                       game=object(), info=chess.engine.INFO_ALL)
            with self._lock:
                self._check_retry_locked(attempt)
            best = _line(board, best_info, human_color)
            same = best is not None and best["uci"] == uci
            if same:
                line = copy.deepcopy(best)
            else:
                info = engine.analyse(board.copy(stack=True), chess.engine.Limit(time=budget / 1000),
                                      root_moves=[move], game=object(), info=chess.engine.INFO_ALL)
                with self._lock:
                    self._check_retry_locked(attempt)
                line = _line(board, info, human_color, required=move)
            kind, loss = _classify(best, line, "white" if board.turn else "black", same)
            verdict = "recommended" if kind == "best" else "good" if kind == "good" else "uncertain" if kind == "uncertain" else "try_again"
            messages = {
                "recommended": "That's the move I found too. What would you expect them to play next?",
                "good": "That works too. Now check their strongest reply.",
                "try_again": "Take another look at their reply. There's a better option here.",
                "uncertain": "I'm not sure about this one yet. Let's analyze it a little longer.",
            }
            result = {"job_id": job_id, "revision": revision, "ply": ply, "uci": uci, "san": board.san(move),
                      "verdict": verdict, "message": messages[verdict], "line": line, "best": best, "loss_cp": loss}
            with self._lock:
                self._check_retry_locked(attempt)
                return result
        except (ValueError, CoachingRevisionError):
            raise
        except Exception as exc:
            with self._lock:
                self._check_retry_locked(attempt)
            raise ValueError("다시 두기 수를 분석하지 못했습니다. 현재 국면에서 다시 시도해 주세요.") from exc
        finally:
            timer.cancel()
            with self._lock:
                if self._retry is attempt:
                    self._retry = None
            self._close_engine(engine)

    def _current(self, job, cancel):
        return not self._closed and not cancel.is_set() and self._state["job_id"] == job

    def _run(self, job, root, rows, movetime_ms, human_color, cancel, key, identity):
        engine = None
        try:
            if cancel.is_set():
                return
            engine = self._factory(self.binary)
            with self._lock:
                if not self._current(job, cancel):
                    return
                self._engine = engine
            board = root.copy(stack=True)
            for source in rows:
                if cancel.is_set():
                    return
                row = copy.deepcopy(source)
                move = chess.Move.from_uci(row["uci"])
                # Different game tokens clear prior search state/TT, so the
                # restricted move does not inherit the unrestricted search.
                best_info = engine.analyse(board.copy(stack=True), chess.engine.Limit(time=movetime_ms / 1000),
                                           game=object(), info=chess.engine.INFO_ALL)
                if cancel.is_set():
                    return
                best = _line(board, best_info, human_color)
                same = best is not None and best["uci"] == row["uci"]
                alternative = None
                if same:
                    played = copy.deepcopy(best)
                    if _can_compare_alternatives(board, best, row["uci"]):
                        other_moves = [legal for legal in board.legal_moves if legal != move]
                        alternative_info = engine.analyse(board.copy(stack=True), chess.engine.Limit(time=movetime_ms / 1000),
                                                          root_moves=other_moves, game=object(), info=chess.engine.INFO_ALL)
                        if cancel.is_set():
                            return
                        alternative = _line(board, alternative_info, human_color)
                        if alternative is not None and alternative["uci"] == row["uci"]:
                            alternative = None
                else:
                    played_info = engine.analyse(board.copy(stack=True), chess.engine.Limit(time=movetime_ms / 1000),
                                                 root_moves=[move], game=object(), info=chess.engine.INFO_ALL)
                    if cancel.is_set():
                        return
                    played = _line(board, played_info, human_color, required=move)
                kind, loss = _classify(best, played, row["color"], same)
                row.update(best=best, played=played, alternative=alternative,
                           classification=kind, label=LABELS[kind], loss_cp=loss)
                row["annotation"] = _annotation(board, row)
                row["explanation"] = _explain(row)
                row["coach"] = describe_move(board, row)
                row["retry_legal_moves"] = [legal.uci() for legal in board.legal_moves]
                if row.get("forced"):
                    row["explanation"] += " 이 수는 대국 당시 사용자가 대신 지정한 Regicide의 수입니다."
                with self._lock:
                    if not self._current(job, cancel):
                        return
                    self._state["moves"][row["ply"] - 1] = row
                    self._state["progress"]["completed"] = row["ply"]
                    self._state["summary"], self._state["critical_plies"] = _summary(self._state["moves"], human_color)
                    self._state["coach"] = describe_report(self._state["moves"], self._state["critical_plies"])
                board.push(move)
            with self._lock:
                if not self._current(job, cancel):
                    return
                report = copy.deepcopy(self._state)
                report["status"] = "completed"
            self._write_cache(key, identity, report)
            with self._lock:
                if self._current(job, cancel):
                    self._state["status"] = "completed"
        except Exception:
            with self._lock:
                if self._current(job, cancel):
                    self._state["status"] = "error"
                    self._state["error"] = "엔진 분석을 마치지 못했습니다. 완료된 수는 살펴볼 수 있으며, 다시 분석하면 처음부터 재시도합니다."
        finally:
            with self._lock:
                if self._engine is engine:
                    self._engine = None
            self._close_engine(engine)

    def _read_cache(self, key, identity, expected):
        if self._cache_dir is None:
            return None
        try:
            path = self._cache_dir / (key + ".json")
            if path.is_symlink() or path.stat().st_size > MAX_CACHE_BYTES:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            report = data["report"]
            rows = report["moves"]
            if data["identity"] != identity or report["status"] != "completed" or len(rows) != len(expected):
                return None
            board = chess.Board(expected[0]["fen_before"])
            sanitized = []
            for row, source in zip(rows, expected):
                if any(row[field] != source[field] for field in ("ply", "uci", "fen_before", "fen_after", "color")):
                    return None
                if row["classification"] not in LABELS or row["classification"] == "pending":
                    return None
                best = _cached_line(row["best"], board, identity["human_color"])
                played = _cached_line(row["played"], board, identity["human_color"], chess.Move.from_uci(source["uci"]))
                # Live analysis copies this one result verbatim when the actual
                # move is best. Contradictory cached copies are not evidence.
                if best is not None and best["uci"] == source["uci"] and played != best:
                    return None
                alternative = _cached_line(row["alternative"], board, identity["human_color"])
                if alternative is not None and (alternative["uci"] == source["uci"]
                                                or not _can_compare_alternatives(board, best, source["uci"])):
                    return None
                kind, loss = _classify(best, played, source["color"], best is not None and best["uci"] == source["uci"])
                clean = {**source, "best": best, "played": played, "alternative": alternative,
                         "classification": kind, "loss_cp": loss, "label": LABELS[kind]}
                clean["annotation"] = _annotation(board, clean)
                clean["explanation"] = _explain(clean)
                clean["coach"] = describe_move(board, clean)
                clean["retry_legal_moves"] = [legal.uci() for legal in board.legal_moves]
                if clean.get("forced"):
                    clean["explanation"] += " 이 수는 대국 당시 사용자가 대신 지정한 Regicide의 수입니다."
                sanitized.append(clean)
                board.push_uci(source["uci"])
            report["moves"] = sanitized
            report["summary"], report["critical_plies"] = _summary(sanitized, identity["human_color"])
            return report
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError):
            return None

    def _write_cache(self, key, identity, report):
        if self._cache_dir is None:
            return
        temporary = None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._cache_dir, prefix=".coaching-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"identity": identity, "report": report}, stream, ensure_ascii=False)
            os.replace(temporary, self._cache_dir / (key + ".json"))
        except (OSError, ValueError):
            # A storage problem must never discard a finished in-memory report.
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
