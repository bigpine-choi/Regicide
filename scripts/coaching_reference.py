"""Read-only Stockfish position reference, isolated from play and coaching scores.

The full path is part of the cache key (repetition changes evaluation). No moves
or PV leave this service; hiding a retry clears its public score immediately.
"""
from collections import OrderedDict
import copy
import threading
import uuid

import chess

from sparring_referee import SparringReferee


class CoachingReference:
    def __init__(self, binary=None, *, referee=None):
        self._referee = referee if referee is not None else SparringReferee(binary, live_claim_draw=False)
        self._lock = threading.RLock()
        self._session = "coaching-reference-" + uuid.uuid4().hex
        self._generation = 0
        self._context = None
        self._cache = OrderedDict()
        self._closed = False
        self._value = self._blank("idle")

    @staticmethod
    def _blank(status):
        return {"source": "stockfish_coaching_reference", "status": status,
                "request_id": None, "fen": None, "cp": None, "mate": None,
                "depth": None, "bound": None, "winner": None}

    @staticmethod
    def _position(data):
        if set(data) - {"request_id", "root_fen", "history_uci", "fen", "hidden"}:
            raise ValueError("코칭 형세에는 위치와 기보만 전달해 주세요.")
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 160:
            raise ValueError("코칭 위치 요청을 확인해 주세요.")
        if type(data.get("hidden", False)) is not bool:
            raise ValueError("형세 숨김 상태가 올바르지 않습니다.")
        if data.get("hidden"):
            return request_id, None, None
        root, history, fen = data.get("root_fen"), data.get("history_uci"), data.get("fen")
        if (not isinstance(root, str) or len(root) > 150 or not isinstance(fen, str)
                or len(fen) > 150 or not isinstance(history, list) or len(history) > 1024):
            raise ValueError("코칭 위치와 전체 기보를 확인해 주세요.")
        board = chess.Board(root)
        if not board.is_valid():
            raise ValueError("코칭 시작 위치가 올바르지 않습니다.")
        canonical_root = board.fen(en_passant="fen")
        for uci in history:
            if not isinstance(uci, str) or len(uci) not in (4, 5) or board.is_game_over(claim_draw=False):
                raise ValueError("코칭 기보가 올바르지 않습니다.")
            board.push_uci(uci)
        if board.fen(en_passant="fen") != fen:
            raise ValueError("코칭 위치와 기보가 일치하지 않습니다.")
        return request_id, board, (canonical_root, tuple(history))

    def select(self, data):
        request_id, board, key = self._position(data)
        with self._lock:
            if self._closed:
                return self._blank("unavailable")
            if board is None:
                # A late hide request must not cancel a newer selected frame.
                if self._value.get("request_id") == request_id:
                    self._referee.cancel()
                    self._context = None
                    self._value = {**self._blank("hidden"), "request_id": request_id}
                return self.snapshot()
            if self._context is not None and self._context["key"] == key:
                self.snapshot()
                retry = (self._value.get("status") in ("error", "unavailable")
                         and self._value.get("request_id") != request_id)
                if not retry:
                    self._value["request_id"] = request_id
                    return self.snapshot()
            self._generation += 1
            self._context = {"key": key, "revision": self._generation,
                             "fen": board.fen(en_passant="fen")}
            self._value = {**self._blank("loading"), "request_id": request_id,
                           "fen": self._context["fen"], "turn": "white" if board.turn else "black"}
            self._referee.cancel()
            # Match coaching PGN replay: a claimable draw is not an actual result.
            outcome = board.outcome(claim_draw=False)
            if outcome is not None:
                self._value.update(status="terminal", source="chess_rules", result=outcome.result(),
                                   winner=None if outcome.winner is None else "white" if outcome.winner else "black")
            elif key in self._cache:
                self._value.update(copy.deepcopy(self._cache[key]), request_id=request_id, cached=True)
                self._cache.move_to_end(key)
            else:
                self._referee.update_position(game_id=self._session, revision=self._generation,
                                              board=board, budget_ms=1000)
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            if self._context is not None and self._value["status"] == "loading":
                value = self._referee.snapshot()
                if (value.get("game_id") == self._session
                        and value.get("revision") == self._context["revision"]
                        and value.get("fen") == self._context["fen"]):
                    # Whitelist: never expose PV / best move, even accidentally.
                    for field in ("status", "cp", "mate", "depth", "bound", "white_share",
                                  "wdl_white", "engine_name", "binary_sha256", "available"):
                        self._value[field] = copy.deepcopy(value.get(field))
                    mate = self._value.get("mate")
                    self._value["winner"] = ("white" if mate > 0 else "black") if mate is not None else None
                    if self._value["status"] == "ready":
                        self._cache[self._context["key"]] = copy.deepcopy(self._value)
                        while len(self._cache) > 32:
                            self._cache.popitem(last=False)
            return copy.deepcopy(self._value)

    def close(self):
        with self._lock:
            self._closed = True
            self._context = None
            self._cache.clear()
            self._value = self._blank("unavailable")
            self._referee.close()
