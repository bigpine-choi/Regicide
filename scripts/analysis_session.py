"""A disposable position-analysis board, independent of personal games.

python-chess validates positions and moves; Regicide's UCI search alone chooses
recommendations through the existing cancellable MoveHints boundary.
"""

import re
import threading
import uuid

import chess

from move_hints import MoveHints


class AnalysisRevisionError(ValueError):
    """A client is attempting to edit an older analysis position."""


def validated_position(fen):
    """Validate before replacing any live analysis state or launching UCI."""
    if not isinstance(fen, str) or not 0 < len(fen) <= 200:
        raise ValueError("기물 배치(FEN)는 200자 이내로 입력해 주세요.")
    fields = fen.split()
    if len(fields) != 6:
        raise ValueError("FEN의 기물 배치·차례·캐슬링·앙파상·반수·수 번호 6개 항목이 필요합니다.")
    placement, turn, castling, ep, halfmove, fullmove = fields
    if (not re.fullmatch(r"[PNBRQKpnbrqk1-8/]+", placement)
            or turn not in ("w", "b")
            or (castling != "-" and not re.fullmatch(r"K?Q?k?q?", castling))
            or not castling
            or (ep != "-" and not re.fullmatch(r"[a-h][36]", ep))):
        raise ValueError("표준 체스 FEN 형식과 백(w)·흑(b) 차례를 확인해 주세요.")
    if (not re.fullmatch(r"[0-9]+", halfmove) or not re.fullmatch(r"[0-9]+", fullmove)
            or not 0 <= int(halfmove) <= 1_000_000_000
            or not 1 <= int(fullmove) <= 1_000_000_000):
        raise ValueError("반수는 0~1000000000, 수 번호는 1~1000000000으로 입력해 주세요.")
    try:
        board = chess.Board(fen)
    except ValueError as exc:
        raise ValueError("기물 배치(FEN)가 올바르지 않습니다. 각 줄이 8칸인지 확인해 주세요.") from exc
    status = board.status()
    errors = []
    if status & (chess.STATUS_NO_WHITE_KING | chess.STATUS_NO_BLACK_KING | chess.STATUS_TOO_MANY_KINGS):
        errors.append("백 왕과 흑 왕을 정확히 하나씩 배치해 주세요.")
    if status & chess.STATUS_PAWNS_ON_BACKRANK:
        errors.append("폰은 1행과 8행에 놓을 수 없습니다.")
    if status & (chess.STATUS_TOO_MANY_WHITE_PAWNS | chess.STATUS_TOO_MANY_BLACK_PAWNS):
        errors.append("폰은 색마다 최대 8개입니다.")
    if status & (chess.STATUS_TOO_MANY_WHITE_PIECES | chess.STATUS_TOO_MANY_BLACK_PIECES):
        errors.append("기물은 왕을 포함해 색마다 최대 16개입니다.")
    if status & chess.STATUS_BAD_CASTLING_RIGHTS:
        errors.append("캐슬링 권리가 있는 왕과 룩은 원래 자리에 있어야 합니다.")
    if status & chess.STATUS_INVALID_EP_SQUARE or (board.ep_square is not None and board.halfmove_clock != 0):
        errors.append("앙파상 칸과 직전 폰 이동 상태가 맞지 않습니다.")
    if status & chess.STATUS_OPPOSITE_CHECK:
        errors.append("차례가 아닌 쪽의 왕이 체크 상태입니다. 차례나 기물 배치를 바꿔 주세요.")
    if status & (chess.STATUS_TOO_MANY_CHECKERS | chess.STATUS_IMPOSSIBLE_CHECK):
        errors.append("현재 체크 형태는 표준 체스에서 가능한 배치가 아닙니다.")
    if status != chess.STATUS_VALID or errors:
        raise ValueError(" ".join(errors) or "표준 체스에서 분석할 수 없는 기물 배치입니다.")
    return board


class AnalysisSession:
    def __init__(self, binary, engine_name="Regicide", engine_factory=None):
        self._lock = threading.RLock()
        self._hints = MoveHints(binary, engine_factory, stream_evaluation=True)
        self.engine_name = engine_name
        self.board = chess.Board()
        self.game_id = "analysis-" + uuid.uuid4().hex
        self.revision = 0
        self.movetime_ms = 1000
        self.active = False
        self._closed = False
        self._sync()

    def _check_revision(self, revision):
        if self._closed:
            raise ValueError("분석 화면이 종료됐습니다. 프로그램을 다시 실행해 주세요.")
        if type(revision) is not int or revision != self.revision:
            raise AnalysisRevisionError("분석 위치가 먼저 바뀌었습니다. 현재 체스판에서 다시 선택해 주세요.")

    def _position_status(self):
        if not self.active:
            return "editing", "기물을 배치한 뒤 분석 시작을 누르세요."
        outcome = self.board.outcome(claim_draw=True)
        if outcome is not None:
            if outcome.termination == chess.Termination.CHECKMATE:
                return "checkmate", "체크메이트입니다. 추천할 수가 없습니다."
            if outcome.termination == chess.Termination.STALEMATE:
                return "stalemate", "스테일메이트입니다. 합법 수가 없습니다."
            return "draw", "무승부 위치입니다. 배치를 편집하거나 한 수 무르기로 계속하세요."
        side = "백" if self.board.turn else "흑"
        return "active", side + " 차례 · 양쪽 수를 직접 두며 분석할 수 있습니다." + (" 체크입니다." if self.board.is_check() else "")

    def _sync(self):
        status, _ = self._position_status()
        eligible = self.active and status == "active" and not self._closed
        self._hints.update_position(game_id=self.game_id, revision=self.revision,
                                    board=self.board, human_color=self.board.turn,
                                    movetime_ms=self.movetime_ms, engine_name=self.engine_name,
                                    active=eligible)
        self._hints.set_enabled(eligible)

    def _castling_diagnostics(self, status, legal_moves):
        """Explain rejected castles without changing the board or its rights.

        The canonical move list is the sole availability authority. Public
        python-chess attack queries explain a rejection, with the king removed
        for transit attacks and the rook relocated for the landing square.
        """
        color = self.board.turn
        rank = "1" if color else "8"
        root = self.board.root()
        king_from = chess.parse_square("e" + rank)
        diagnostics = {}
        for side, target, rook_file, rook_target, path in (
                ("king", "g", "h", "f", "fg"),
                ("queen", "c", "a", "d", "bcd")):
            move = "e" + rank + target + rank
            value = {"move": move, "available": False}
            has_right = (self.board.has_kingside_castling_rights(color) if side == "king"
                         else self.board.has_queenside_castling_rights(color))
            root_right = (root.has_kingside_castling_rights(color) if side == "king"
                          else root.has_queenside_castling_rights(color))
            if move in legal_moves:
                value.update(available=True, reason_code="available",
                             reason=("킹사이드" if side == "king" else "퀸사이드") + " 캐슬링을 둘 수 있습니다.")
            elif not self.active:
                value.update(reason_code="editing", reason="분석을 시작한 뒤 캐슬링할 수 있습니다.")
            elif status != "active":
                value.update(reason_code="terminal", reason="종료된 분석 위치에서는 캐슬링할 수 없습니다.")
            elif not has_right:
                value.update(reason_code="rights_lost" if root_right else "no_rights",
                             reason="이 분석에서 왕이 움직였거나 해당 룩이 움직이거나 잡혀 캐슬링 권리가 사라졌습니다."
                             if root_right else "시작 배치에 이쪽 캐슬링 권리가 없습니다. 배치 편집에서 권리 설정을 확인하세요.")
            elif self.board.is_check():
                value.update(reason_code="in_check", reason="왕이 체크 상태이므로 캐슬링할 수 없습니다.")
            else:
                occupied = [file + rank for file in path if self.board.piece_at(chess.parse_square(file + rank))]
                if occupied:
                    value.update(reason_code="occupied_path", reason=", ".join(occupied) + " 칸에 기물이 있어 캐슬링 길이 막혀 있습니다.")
                else:
                    transit = chess.parse_square(rook_target + rank)
                    landing = chess.parse_square(target + rank)
                    without_king = self.board.occupied & ~chess.BB_SQUARES[king_from]
                    rook_from = chess.parse_square(rook_file + rank)
                    after_castle = (without_king & ~chess.BB_SQUARES[rook_from]) | chess.BB_SQUARES[transit]
                    if self.board.is_attacked_by(not color, transit, occupied=without_king):
                        value.update(reason_code="attacked_path", reason="왕이 지나갈 " + rook_target + rank + " 칸이 공격받고 있습니다.")
                    elif self.board.is_attacked_by(not color, landing, occupied=after_castle):
                        value.update(reason_code="attacked_path", reason="왕이 도착할 " + target + rank + " 칸이 공격받고 있습니다.")
                    else:
                        value.update(reason_code="illegal", reason="현재 위치에서 합법적인 캐슬링 수가 아닙니다.")
            diagnostics[side] = value
        return diagnostics

    def snapshot(self):
        with self._lock:
            status, message = self._position_status()
            hint, evaluation = self._hints.snapshot_with_evaluation()
            if not self.active:
                evaluation.update(status="waiting", cp=None, mate=None, depth=None, bound=None,
                                  white_share=None, winner=None, result="*", source_fen=None,
                                  game_id=self.game_id, revision=self.revision, fen=self.board.fen(en_passant="fen"),
                                  turn="white" if self.board.turn else "black")
            elif status != "active":
                outcome = self.board.outcome(claim_draw=True)
                winner = "white" if outcome.winner == chess.WHITE else "black" if outcome.winner == chess.BLACK else None
                evaluation.update(status="terminal", cp=None, mate=0 if status == "checkmate" else None,
                                  depth=None, bound=None, white_share=1.0 if winner == "white" else
                                  0.0 if winner == "black" else 0.5, winner=winner,
                                  result=outcome.result(), source="result", source_fen=self.board.fen(en_passant="fen"))
            replay = self.board.root()
            initial_fen = replay.fen(en_passant="fen")
            legal_moves = ([move.uci() for move in self.board.legal_moves]
                           if self.active and status == "active" else [])
            moves = []
            for ply, move in enumerate(self.board.move_stack, 1):
                moves.append({"ply": ply, "uci": move.uci(), "san": replay.san(move),
                              "color": "white" if replay.turn else "black"})
                replay.push(move)
            return {"revision": self.revision, "game_id": self.game_id,
                    "active": self.active, "fen": self.board.fen(en_passant="fen"),
                    "initial_fen": initial_fen,
                    "board": {chess.square_name(square): piece.symbol()
                              for square, piece in self.board.piece_map().items()},
                    "turn": "white" if self.board.turn else "black",
                    "legal_moves": legal_moves,
                    "castling": self._castling_diagnostics(status, legal_moves),
                    "can_undo": self.active and bool(self.board.move_stack),
                    "status": status, "message": message, "movetime_ms": self.movetime_ms,
                    "last_move": self.board.peek().uci() if self.board.move_stack else None,
                    "in_check": self.board.is_check(), "moves": moves,
                    "hint": hint, "evaluation": evaluation}

    def start(self, fen, movetime_ms, revision):
        with self._lock:
            self._check_revision(revision)
            if type(movetime_ms) is not int or not 100 <= movetime_ms <= 5000:
                raise ValueError("분석 시간은 100~5000ms로 선택해 주세요.")
            board = validated_position(fen)
            self.board = board
            self.movetime_ms = movetime_ms
            self.game_id = "analysis-" + uuid.uuid4().hex
            self.active = True
            self.revision += 1
            self._sync()
            return self.snapshot()

    def move(self, move, revision):
        with self._lock:
            self._check_revision(revision)
            if not self.active or self._position_status()[0] != "active":
                raise ValueError("분석을 시작한 뒤 합법 수를 선택해 주세요.")
            if not isinstance(move, str) or not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", move):
                raise ValueError("올바른 수를 선택해 주세요. 예: e2e4, a7a8q")
            selected = chess.Move.from_uci(move)
            if selected not in self.board.legal_moves:
                raise ValueError("현재 분석 위치에서 둘 수 없는 수입니다.")
            self.board.push(selected)
            self.revision += 1
            self._sync()
            return self.snapshot()

    def undo(self, revision):
        with self._lock:
            self._check_revision(revision)
            if not self.active or not self.board.move_stack:
                raise ValueError("분석에서 무를 수가 없습니다.")
            self.board.pop()
            self.revision += 1
            self._sync()
            return self.snapshot()

    def stop(self, revision):
        with self._lock:
            self._check_revision(revision)
            self.active = False
            self.revision += 1
            self._sync()
            return self.snapshot()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self.active = False
            self._closed = True
            self.revision += 1
        self._hints.close()
