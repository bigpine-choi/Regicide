"""A freely playable study board isolated from the live game and its engine.

Only checkmate/stalemate prevent further moves. Automatic repetition, move-count
and insufficient-material draws are reported, but a study may continue through
them while legal moves remain. Analysis positions are never auto-saved as games.
"""

import threading
import uuid

import chess
import chess.pgn


class AnalysisRevisionError(ValueError):
    """An action targets a position that has already changed."""


class AnalysisSession:
    MAX_PLIES = 2048

    def __init__(self):
        self._lock = threading.RLock()
        self.revision = 0
        self.game_id = ""
        self.board = chess.Board()
        self._redo = []
        self.start()

    @staticmethod
    def _board_from_fen(fen):
        if fen is None:
            return chess.Board()
        if not isinstance(fen, str) or not 1 <= len(fen) <= 200:
            raise ValueError("올바른 FEN 포지션을 입력해 주세요.")
        try:
            board = chess.Board(fen)
            if not board.is_valid():
                raise ValueError("invalid position")
            return board
        except (ValueError, TypeError, IndexError):
            raise ValueError("올바른 FEN 포지션을 입력해 주세요.") from None

    def _check_revision(self, revision):
        if type(revision) is not int or revision != self.revision:
            raise AnalysisRevisionError("분석 위치가 바뀌었습니다. 현재 체스판에서 다시 선택해 주세요.")

    def start(self, fen=None):
        # Validate before replacing a previously usable study position.
        board = self._board_from_fen(fen)
        with self._lock:
            self.board = board
            self._redo = []
            self.game_id = "analysis-" + uuid.uuid4().hex
            self.revision += 1
            return self.state()

    def state(self):
        with self._lock:
            replay = self.board.root()
            moves = []
            for ply, move in enumerate(self.board.move_stack, 1):
                moves.append({"ply": ply, "san": replay.san(move), "uci": move.uci(),
                              "actor": "analysis"})
                replay.push(move)
            legal = [move.uci() for move in self.board.legal_moves]
            outcome = self.board.outcome(claim_draw=False)
            result = outcome.result() if outcome else "*"
            termination = outcome.termination.name.lower() if outcome else None
            turn = "백" if self.board.turn else "흑"
            if self.board.is_checkmate():
                status = "분석 · 체크메이트"
            elif self.board.is_stalemate():
                status = "분석 · 스테일메이트"
            else:
                status = "분석 · " + turn + " 차례"
                if self.board.is_check():
                    status += " · 체크"
            return {"analysis_mode": True, "game_id": self.game_id, "revision": self.revision,
                    "fen": self.board.fen(en_passant="fen"),
                    "root_fen": self.board.root().fen(en_passant="fen"),
                    "board": {chess.square_name(square): piece.symbol()
                              for square, piece in self.board.piece_map().items()},
                    "turn": "white" if self.board.turn else "black", "human_color": "white",
                    "human_to_move": bool(legal), "engine_to_move": False,
                    "legal_moves": legal, "moves": moves, "last_move": self.board.peek().uci()
                    if self.board.move_stack else None,
                    "can_undo": bool(self.board.move_stack), "can_redo": bool(self._redo),
                    "status": status, "result": result, "termination": termination,
                    "in_check": self.board.is_check(), "error": None,
                    "handicap_mode": False, "can_handicap": False,
                    "engine": {"name": "Regicide", "movetime_ms": 1000}}

    def move(self, uci, revision):
        with self._lock:
            self._check_revision(revision)
            if not isinstance(uci, str) or len(uci) not in (4, 5):
                raise ValueError("올바른 수를 선택해 주세요.")
            if len(self.board.move_stack) >= self.MAX_PLIES:
                raise ValueError("분석 수순이 너무 깁니다. 새 분석을 시작해 주세요.")
            try:
                move = self.board.parse_uci(uci)
            except ValueError:
                raise ValueError("그 칸으로 이동할 수 없습니다.") from None
            if move not in self.board.legal_moves:
                raise ValueError("그 칸으로 이동할 수 없습니다.")
            self.board.push(move)
            self._redo.clear()
            self.revision += 1
            return self.state()

    def undo(self, revision):
        with self._lock:
            self._check_revision(revision)
            if not self.board.move_stack:
                raise ValueError("되돌릴 분석 수가 없습니다.")
            self._redo.append(self.board.pop())
            self.revision += 1
            return self.state()

    def redo(self, revision):
        with self._lock:
            self._check_revision(revision)
            if not self._redo:
                raise ValueError("다시 둘 분석 수가 없습니다.")
            self.board.push(self._redo.pop())
            self.revision += 1
            return self.state()

    def reset(self, revision):
        with self._lock:
            self._check_revision(revision)
            return self.start()

    def pgn(self):
        with self._lock:
            game = chess.pgn.Game()
            game.setup(self.board.root())
            game.headers["Event"] = "Regicide Analysis"
            game.headers["White"] = "Analysis"
            game.headers["Black"] = "Analysis"
            # A study is not a completed game and must not enter win statistics.
            game.headers["Result"] = "*"
            node = game
            for move in self.board.move_stack:
                node = node.add_variation(move)
            return game.accept(chess.pgn.StringExporter(headers=True, variations=False,
                                                        comments=False)) + "\n"
