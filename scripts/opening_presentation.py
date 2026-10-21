"""Optional read-only opening information shared by live play and archive replay."""

from collections import OrderedDict
from copy import deepcopy
import threading

from opening_guide import get_opening_guide


class OpeningGuideCache:
    """Bounded per-view cache; callers receive independent presentation objects."""

    def __init__(self, capacity=128):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("opening guide cache capacity must be positive")
        self.capacity = capacity
        self._entries = OrderedDict()
        self._lock = threading.RLock()

    def get(self, board):
        key = (board.root().fen(en_passant="fen"), board.fen(en_passant="fen"),
               tuple(move.uci() for move in board.move_stack))
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return deepcopy(self._entries[key])
            try:
                guide = get_opening_guide(board)
            except (OSError, ValueError, RuntimeError):
                # Optional study data must never prevent a move, save or replay.
                # Do not expose local filesystem paths in a user-facing error.
                return {
                    "status": "unavailable", "history_scope": "unknown",
                    "current_matches": [], "matched_opening": None,
                    "continuations": [], "continuations_total": 0,
                    "continuations_truncated": False,
                    "source": {"repository_url": "https://github.com/lichess-org/chess-openings",
                               "attribution": "Lichess 오프닝 목록", "license": "CC0-1.0"},
                    "note": "오프닝 자료를 읽지 못했습니다. 대국은 계속할 수 있습니다.",
                    "disclaimer": "등록 수순은 최선수나 승률을 뜻하지 않습니다.",
                }
            if guide.get("status") == "unavailable":
                return guide
            self._entries[key] = deepcopy(guide)
            if len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
            return guide
