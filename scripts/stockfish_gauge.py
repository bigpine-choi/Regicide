"""Display-only Stockfish scores, with ownership separate from move selection.

The referee's existing bounded UCI worker supplies the scores. This wrapper
never exposes its moves and never borrows Regicide's own evaluation on failure.
Each owner has a private generation, including when undo returns to the same FEN.
"""
import copy
import threading
import uuid

import chess

from sparring_referee import SparringReferee, MAX_LIVE_MS
from game_outcome import automatic_outcome


SOURCE = "stockfish_gauge"
POLICY = "regicide-stockfish-gauge-v1"
DEFAULT_BUDGET_MS = 500


class StockfishGauge:
    def __init__(self, binary=None, *, referee=None, live_claim_draw=True):
        self._lock = threading.RLock()
        self._referee = (referee if referee is not None else
                         SparringReferee(binary, live_claim_draw=live_claim_draw))
        self._configured = referee is not None or binary is not None
        self._claim_draw = live_claim_draw
        self._session = "stockfish-gauge-" + uuid.uuid4().hex
        self._generation = 0
        self._context = None
        self._closed = False
        self._value = self._blank("idle")

    @staticmethod
    def _blank(status, context=None):
        value = {"source": SOURCE, "policy": POLICY, "status": status,
                 "game_id": None, "revision": None, "fen": None, "turn": None,
                 "request_id": None, "source_fen": None, "cp": None, "mate": None,
                 "depth": None, "bound": None, "white_share": None, "wdl_white": None,
                 "winner": None, "result": "*", "available": False,
                 "message": "Stockfish 형세를 아직 확인하지 않았습니다."}
        if context is not None:
            value.update({key: context[key] for key in
                          ("game_id", "revision", "fen", "turn", "request_id")})
        return value

    def _read(self):
        try:
            value = self._referee.snapshot()
            return value if isinstance(value, dict) else {"available": False, "status": "error"}
        except Exception:
            return {"available": False, "status": "error"}

    def metadata(self):
        with self._lock:
            raw = self._read()
            value = self.snapshot()
            return {"enabled": True, "source": SOURCE, "policy": POLICY,
                    "configured": self._configured,
                    "available": not self._closed and raw.get("available") is True,
                    "name": raw.get("engine_name") or "Stockfish",
                    "binary_sha256": raw.get("binary_sha256"),
                    "status": value["status"], "message": value["message"]}

    def update_position(self, *, game_id, revision, board, active=True,
                        budget_ms=DEFAULT_BUDGET_MS):
        if (not isinstance(game_id, str) or not 0 < len(game_id) <= 200
                or type(revision) is not int or revision < 0
                or not isinstance(board, chess.Board) or board.chess960 or not board.is_valid()
                or type(active) is not bool or type(budget_ms) is not int or budget_ms < 1):
            raise ValueError("Stockfish 형세에 전달할 위치와 요청을 확인해 주세요.")
        board = board.copy(stack=True)
        fen = board.fen(en_passant="fen")
        key = (game_id, revision, fen, board.root().fen(en_passant="fen"),
               tuple(move.uci() for move in board.move_stack), active, min(budget_ms, MAX_LIVE_MS))
        with self._lock:
            if self._closed:
                return self.snapshot()
            if self._context is not None and self._context["key"] == key:
                return self.snapshot()
            self._generation += 1
            context = {"key": key, "game_id": game_id, "revision": revision, "fen": fen,
                       "turn": "white" if board.turn else "black", "generation": self._generation,
                       "request_id": self._session + ":" + str(self._generation)}
            self._context = context
            self._value = self._blank("loading" if active else "idle", context)
            self._value["budget_ms"] = min(budget_ms, MAX_LIVE_MS)
            try:
                # This object owns its referee. Cancelling it cannot touch the
                # live opponent, sparring candidate checks, or coaching review.
                self._referee.cancel()
                outcome = automatic_outcome(board) if self._claim_draw else board.outcome(claim_draw=False)
                if outcome is not None:
                    winner = None if outcome.winner is None else "white" if outcome.winner else "black"
                    self._value.update(status="terminal", source="chess_rules", result=outcome.result(),
                                       winner=winner, white_share=1.0 if winner == "white" else
                                       0.0 if winner == "black" else .5,
                                       message="체스 규칙에 따른 종료 결과입니다.")
                elif active:
                    self._referee.update_position(game_id=self._session, revision=self._generation,
                                                  board=board, active=True, budget_ms=min(budget_ms, MAX_LIVE_MS))
                    self._value["message"] = "Stockfish가 현재 위치의 형세를 계산하고 있습니다."
                else:
                    self._value["message"] = "Stockfish 형세 계산을 중단했습니다."
            except Exception:
                self._value.update(status="error", message="Stockfish 형세를 계산하지 못했습니다.")
            return self.snapshot()

    @staticmethod
    def _valid_score(value):
        cp, mate, depth = value.get("cp"), value.get("mate"), value.get("depth")
        return (value.get("available") is True and value.get("bound") == "exact"
                and type(depth) is int and 1 <= depth <= 256
                and ((type(cp) is int and abs(cp) <= 100000 and mate is None)
                     or (cp is None and type(mate) is int and 0 < abs(mate) <= 10000)))

    def snapshot(self):
        with self._lock:
            if self._closed or self._context is None or self._value["status"] in ("terminal", "idle", "error"):
                return copy.deepcopy(self._value)
            raw, context = self._read(), self._context
            matches = (raw.get("game_id") == self._session
                       and raw.get("revision") == context["generation"]
                       and raw.get("fen") == context["fen"]
                       and raw.get("turn") == context["turn"])
            if matches:
                status = raw.get("status")
                if status == "ready" and self._valid_score(raw):
                    for field in ("cp", "mate", "depth", "bound", "white_share", "wdl_white",
                                  "engine_name", "binary_sha256", "budget_ms", "elapsed_ms",
                                  "startup_ms", "analysis_elapsed_ms"):
                        self._value[field] = copy.deepcopy(raw.get(field))
                    mate = self._value["mate"]
                    self._value.update(status="ready", available=True, source_fen=context["fen"],
                                       winner=("white" if mate > 0 else "black") if mate is not None else None,
                                       message="Stockfish의 현재 위치 평가입니다. 실제 수는 Regicide가 선택합니다.")
                elif status in ("error", "unavailable") or status == "ready":
                    self._value = {**self._blank("unavailable" if status == "unavailable" else "error", context),
                                   "message": "Stockfish 형세를 확인할 수 없습니다."}
            elif raw.get("available") is False:
                self._value = {**self._blank("error" if raw.get("status") == "error" else "unavailable", context),
                               "message": "Stockfish를 사용할 수 없어 형세를 표시하지 않습니다."}
            return copy.deepcopy(self._value)

    def cancel(self):
        with self._lock:
            self._generation += 1
            self._value = self._blank("idle", self._context)
            self._context = None
            self._referee.cancel()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            self._context = None
            self._value = self._blank("unavailable")
            self._value["message"] = "Stockfish 형세 계산이 종료됐습니다."
            self._referee.close()
