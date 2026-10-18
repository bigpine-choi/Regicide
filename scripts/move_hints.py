"""Optional human-move advice, isolated from the live game and opponent search.

Only immutable position copies cross this boundary. Preference and snapshot
reads use this service's lock, never the live game's search lock.
"""

import threading

import chess
import chess.engine


class MoveHints:
    def __init__(self, binary, engine_factory=None, timeout_grace=2.0):
        self.binary = binary
        self._factory = engine_factory or (
            lambda binary: chess.engine.SimpleEngine.popen_uci(
                [str(binary), "uci"], timeout=3))
        self._timeout_grace = timeout_grace
        self._condition = threading.Condition()
        self._enabled = False
        self._closed = False
        self._generation = 0
        self._context = None
        self._snapshot = {}
        self._pending = None
        self._cancel = None
        self._worker = None

    def snapshot(self):
        with self._condition:
            return dict(self._snapshot)

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("추천수 표시 여부는 true 또는 false로 지정해 주세요.")
        with self._condition:
            if self._closed:
                raise ValueError("대국 화면이 종료됐습니다.")
            if self._enabled != enabled:
                self._enabled = enabled
                self._schedule_locked()
            return dict(self._snapshot)

    def update_position(self, *, game_id, revision, board, human_color,
                        movetime_ms, engine_name, active, handicap_mode=False):
        """Called under the game lock; its copy retains repetition/EP history."""
        fen = board.fen(en_passant="fen")
        eligible = active and not handicap_mode and board.turn == human_color
        context = {"game_id": game_id, "revision": revision, "fen": fen,
                   "movetime_ms": movetime_ms, "engine_name": engine_name,
                   "active": active, "handicap_mode": handicap_mode,
                   "eligible": eligible}
        with self._condition:
            if self._closed:
                return
            identity = {key: value for key, value in context.items()}
            previous = ({key: value for key, value in self._context.items() if key != "board"}
                        if self._context else None)
            if previous == identity:
                return
            # Even while OFF keep one copy of the current human position, so
            # ON can launch immediately without waiting for the game lock.
            context["board"] = board.copy(stack=True) if eligible else None
            self._context = context
            self._schedule_locked()

    def _schedule_locked(self):
        self._generation += 1
        if self._cancel is not None:
            self._cancel.set()
        self._pending = None
        context = self._context
        if context is None:
            return
        enabled = self._enabled and not self._closed
        status = "disabled"
        message = "추천수 표시가 꺼져 있습니다."
        if enabled:
            if not context["active"]:
                status, message = "unavailable", "현재 대국에서는 추천할 수 없습니다."
            elif context["handicap_mode"]:
                status, message = "waiting", "Regicide의 수 바꾸기를 마치면 추천합니다."
            elif not context["eligible"]:
                status, message = "waiting", "당신 차례가 되면 추천합니다."
            else:
                status, message = "searching", "Regicide가 추천수를 찾고 있습니다."
                self._pending = (self._generation, context)
        self._snapshot = {key: context[key] for key in
                          ("game_id", "revision", "fen", "movetime_ms", "engine_name")}
        self._snapshot.update(enabled=enabled, status=status, move=None, san=None,
                              **{"from": None, "to": None}, promotion=None,
                              depth=None, message=message)
        if self._pending is not None and self._worker is None:
            self._worker = threading.Thread(target=self._run, name="regicide-move-hints", daemon=True)
            self._worker.start()
        self._condition.notify_all()

    @staticmethod
    def _close_engine(engine):
        # close() terminates the UCI transport, including a wedged search;
        # quit() could itself wait for an unresponsive helper.
        try:
            engine.close()
        except Exception:
            pass

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                generation, context = self._pending
                self._pending = None
                cancel = threading.Event()
                self._cancel = cancel
            engine = None
            guard = None
            finished = threading.Event()
            timed_out = threading.Event()
            result = None
            failure = None
            try:
                # The default UCI factory has a bounded initialization timeout.
                # An OFF during startup is checked before issuing any search.
                if cancel.is_set():
                    continue
                engine = self._factory(self.binary)
                if cancel.is_set():
                    continue
                seconds = context["movetime_ms"] / 1000

                def guard_search(peer, cancellation, completion, expired, duration):
                    cancelled = cancellation.wait(duration + self._timeout_grace)
                    if not completion.is_set():
                        if not cancelled:
                            expired.set()
                        self._close_engine(peer)

                guard = threading.Thread(target=guard_search,
                                         args=(engine, cancel, finished, timed_out, seconds),
                                         name="regicide-hint-watchdog", daemon=True)
                guard.start()
                board = context["board"]
                reply = engine.play(board.copy(stack=True), chess.engine.Limit(time=seconds),
                                    game=object(), info=chess.engine.INFO_ALL)
                if timed_out.is_set():
                    raise TimeoutError("추천수 탐색 시간이 초과됐습니다.")
                move = reply.move
                if move is None or move not in board.legal_moves:
                    raise ValueError("추천 엔진이 합법 수를 반환하지 않았습니다.")
                depth = reply.info.get("depth")
                result = {"move": move.uci(), "san": board.san(move),
                          "from": chess.square_name(move.from_square),
                          "to": chess.square_name(move.to_square),
                          "promotion": chess.piece_symbol(move.promotion) if move.promotion else None,
                          "depth": depth if type(depth) is int and depth >= 0 else None,
                          "status": "ready", "message": "추천수입니다. 직접 선택해서 두세요."}
            except Exception:
                failure = ("추천수 탐색 시간이 초과됐습니다. 껐다 켜면 다시 시도합니다."
                           if timed_out.is_set() else
                           "추천수를 찾지 못했습니다. 껐다 켜면 다시 시도합니다.")
            finally:
                finished.set()
                cancel.set()
                if engine is not None:
                    self._close_engine(engine)
                    if isinstance(engine, chess.engine.SimpleEngine):
                        try:
                            # Reap the short-lived process before starting the
                            # next request; preference writes remain immediate.
                            engine.returncode.result(timeout=1)
                        except Exception:
                            pass
                if guard is not None:
                    guard.join(timeout=1)
                with self._condition:
                    if self._cancel is cancel:
                        self._cancel = None
                    # Invalidation wins even if a cancelled helper returns a
                    # seemingly valid move at the exact cancellation boundary.
                    if not self._closed and generation == self._generation and self._enabled:
                        if result is not None:
                            self._snapshot.update(result)
                        elif failure is not None:
                            self._snapshot.update(status="unavailable", message=failure)

    def close(self):
        with self._condition:
            self._closed = True
            self._enabled = False
            self._schedule_locked()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            # Default startup is bounded at 3s; running searches are closed by
            # their cancellation guard. Never wait on the live game's lock.
            worker.join(timeout=4)
