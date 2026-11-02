"""Optional current-side advice, isolated from the live game and opponent search.

Only immutable position copies cross this boundary. Preference and snapshot
reads use this service's lock, never the live game's search lock.
"""

import copy
import threading
import time

import chess
import chess.engine

from evaluation_stability import assess as assess_evaluation_stability
import sparring_evaluation


class MoveHints:
    def __init__(self, binary, engine_factory=None, timeout_grace=2.0, *, stream_evaluation=False):
        self.binary = binary
        self._factory = engine_factory or (
            lambda binary: chess.engine.SimpleEngine.popen_uci(
                [str(binary), "uci"], timeout=3))
        self._timeout_grace = timeout_grace
        self._stream_evaluation = stream_evaluation
        self._condition = threading.Condition()
        self._enabled = False
        self._closed = False
        self._generation = 0
        self._context = None
        self._snapshot = {"candidates": []}
        self._evaluation = {}
        self._evaluation_packets = {}
        self._precision_packets = {}
        self._pending = None
        self._cancel = None
        self._worker = None

    def snapshot(self):
        with self._condition:
            return copy.deepcopy(self._snapshot)

    def snapshot_with_evaluation(self):
        """Read the advice and score from the same search generation atomically."""
        with self._condition:
            return copy.deepcopy(self._snapshot), copy.deepcopy(self._evaluation)

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("추천수 표시 여부는 true 또는 false로 지정해 주세요.")
        with self._condition:
            if self._closed:
                raise ValueError("대국 화면이 종료됐습니다.")
            if self._enabled != enabled:
                self._enabled = enabled
                self._schedule_locked()
            return copy.deepcopy(self._snapshot)

    def update_position(self, *, game_id, revision, board, human_color,
                        movetime_ms, engine_name, active, handicap_mode=False, precision=False):
        """Called under the game lock; its copy retains repetition/EP history."""
        fen = board.fen(en_passant="fen")
        eligible = active and not handicap_mode and (
            not self._stream_evaluation or board.turn == human_color)
        context = {"game_id": game_id, "revision": revision, "fen": fen,
                   "movetime_ms": movetime_ms, "engine_name": engine_name,
                   "active": active, "handicap_mode": handicap_mode,
                   "precision": bool(precision and self._stream_evaluation),
                   "eligible": eligible, "turn": "white" if board.turn else "black"}
        with self._condition:
            if self._closed:
                return
            identity = {key: value for key, value in context.items()}
            previous = ({key: value for key, value in self._context.items() if key != "board"}
                        if self._context else None)
            if previous == identity:
                return
            # Even while OFF keep one copy of the current position, so
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
                status, message = "waiting", "현재 차례의 추천을 기다리고 있습니다."
            else:
                status, message = "searching", "Regicide가 추천수를 찾고 있습니다."
                self._pending = (self._generation, context)
        self._snapshot = {key: context[key] for key in
                          ("game_id", "revision", "fen", "turn", "movetime_ms", "engine_name")}
        self._snapshot.update(enabled=enabled, status=status, move=None, san=None,
                              **{"from": None, "to": None}, promotion=None,
                              depth=None, candidates=[], message=message,
                              calculating=status == "searching")
        if self._stream_evaluation:
            self._clear_evaluation_locked("searching" if status == "searching" else
                                          "unavailable" if status == "unavailable" else "waiting")
        if self._pending is not None and self._worker is None:
            self._worker = threading.Thread(target=self._run, name="regicide-move-hints", daemon=True)
            self._worker.start()
        self._condition.notify_all()

    def _clear_evaluation_locked(self, status):
        context = self._context
        self._evaluation_packets = {}
        self._precision_packets = {}
        self._evaluation = {key: context[key] for key in ("game_id", "revision", "fen", "turn")}
        self._evaluation.update(status=status, cp=None, mate=None, depth=None, bound=None,
                                white_share=None, winner=None, result="*", source="search", source_fen=None)
        self._evaluation.update(assess_evaluation_stability(context.get("board"), {}))
        if context.get("precision"):
            self._evaluation["refinement"] = {
                "policy": sparring_evaluation.POLICY, "phase": "initial",
                "base_budget_ms": sparring_evaluation.BASE_BUDGET_MS,
                "max_budget_ms": min(context["movetime_ms"], sparring_evaluation.MAX_BUDGET_MS),
                "elapsed_ms": 0, "stop_reason": None, "converged": False,
                "accepted_packets": 0, "rejected_packets": 0,
                "retained_previous_score": False}

    def _publish_evaluation(self, generation, context, cancel, timed_out, info):
        """Only a score packet with its own completed depth may replace a score.

        AnalysisResult.info merges partial packets; a later depth-only packet
        must not attach its depth to an earlier iteration's score.
        """
        if not self._stream_evaluation:
            return
        if context.get("precision"):
            sample = sparring_evaluation.validate_packet(context["board"], info)
            with self._condition:
                if (self._closed or not self._enabled or generation != self._generation
                        or context is not self._context or cancel.is_set() or timed_out.is_set()):
                    return
                depth = info.get("depth") if isinstance(info, dict) else None
                # Ignore informational progress packets. A score iteration
                # must nevertheless remain in convergence evidence when bad.
                if type(depth) is not int or depth < 1 or "score" not in info:
                    return
                highest = max(self._precision_packets, default=0)
                if depth >= highest:
                    self._precision_packets[depth] = copy.deepcopy(info)
                    self._precision_packets = {key: self._precision_packets[key]
                                               for key in sorted(self._precision_packets)[-3:]}
                refinement = self._evaluation["refinement"]
                refinement["converged"] = sparring_evaluation.converged(context["board"], self._precision_packets)
                if refinement["stop_reason"] == "stable" and not refinement["converged"]:
                    # UCI stop can still deliver a newer completed iteration.
                    # Preserve its exact score without calling it settled.
                    refinement["stop_reason"] = "stability_changed"
                if sample is None or depth < highest:
                    refinement["rejected_packets"] += 1
                    refinement["retained_previous_score"] = self._evaluation["depth"] is not None
                    if depth >= highest:
                        self._evaluation.update(reliability="provisional", recent_cp_range_white=None,
                                                score_swing_cp=None, stable_depths=0)
                    return
                refinement["accepted_packets"] += 1
                refinement["retained_previous_score"] = False
        score, depth = info.get("score"), info.get("depth")
        if not isinstance(score, chess.engine.PovScore) or type(depth) is not int or depth < 1:
            return
        white_score = score.pov(chess.WHITE)
        cp, mate = white_score.score(), white_score.mate()
        if cp is None and mate is None:
            return
        lower, upper = bool(info.get("lowerbound")), bool(info.get("upperbound"))
        if score.turn == chess.BLACK:
            lower, upper = upper, lower
        if lower and upper:
            return
        winner = None
        if mate is not None:
            # Mate(0) and MateGiven both expose distance zero; their numeric
            # mate score retains which side has already been mated.
            winner = "white" if white_score.score(mate_score=30000) > 0 else "black"
            share = 1.0 if winner == "white" else 0.0
        else:
            share = 0.5 + 0.5 * cp / (abs(cp) + 400.0)
        with self._condition:
            if (self._closed or not self._enabled or generation != self._generation
                    or context is not self._context
                    or cancel.is_set() or timed_out.is_set()):
                return
            self._evaluation.update(status="searching", cp=cp, mate=mate, depth=depth,
                                    bound="lower" if lower else "upper" if upper else "exact",
                                    white_share=share, winner=winner, source_fen=self._context["fen"])
            # Keep only this generation's raw iterations. A partial packet may
            # not borrow a PV/depth from analysis.info, and a cancelled worker
            # may not add evidence to the replacement position's assessment.
            self._evaluation_packets[depth] = copy.deepcopy(info)
            self._evaluation_packets = {key: self._evaluation_packets[key]
                                        for key in sorted(self._evaluation_packets)[-3:]}
            evidence = self._precision_packets if context.get("precision") else self._evaluation_packets
            self._evaluation.update(assess_evaluation_stability(context["board"],
                {key: packet for key, packet in evidence.items() if key <= depth}))

    def _refinement_progress(self, generation, context, elapsed_ms, *, complete=False, reason=None):
        """Update this generation only; a timer can stop between UCI packets."""
        with self._condition:
            if (self._closed or not self._enabled or generation != self._generation
                    or context is not self._context or not context.get("precision")):
                return None
            refinement = self._evaluation["refinement"]
            refinement["elapsed_ms"] = round(max(0, elapsed_ms), 3)
            refinement["phase"] = ("complete" if complete else "refining"
                                     if elapsed_ms >= sparring_evaluation.BASE_BUDGET_MS else "initial")
            if reason is not None:
                refinement["stop_reason"] = reason
            if refinement["stop_reason"] is not None:
                return refinement["stop_reason"]
            if elapsed_ms >= refinement["max_budget_ms"]:
                refinement["stop_reason"] = "budget"
            elif elapsed_ms >= sparring_evaluation.BASE_BUDGET_MS and refinement["converged"]:
                refinement["stop_reason"] = "stable"
            return refinement["stop_reason"]

    @staticmethod
    def _close_engine(engine):
        # close() terminates the UCI transport, including a wedged search;
        # quit() could itself wait for an unresponsive helper.
        try:
            engine.close()
        except Exception:
            pass

    @staticmethod
    def _rank_packet(board, allowed, depth, info):
        """A rank needs its own exact completed-depth score and legal PV.

        In particular, never inspect AnalysisResult.info: it merges unrelated
        partial packets and can attach a new depth to an older score/PV.
        """
        if (type(info.get("depth")) is not int or info["depth"] != depth
                or info.get("lowerbound") or info.get("upperbound")
                or not isinstance(info.get("score"), chess.engine.PovScore)):
            return None
        pv = info.get("pv")
        if not isinstance(pv, (list, tuple)) or not pv or len(pv) > 128:
            return None
        if not isinstance(pv[0], chess.Move) or pv[0] not in allowed:
            return None
        position = board.copy(stack=True)
        for move in pv:
            if not isinstance(move, chess.Move) or move not in position.legal_moves:
                return None
            position.push(move)
        score = info["score"].pov(board.turn)
        cp, mate = score.score(), score.mate()
        if (cp is None and mate is None) or (cp is not None and type(cp) is not int) or (mate is not None and type(mate) is not int):
            return None
        move = pv[0]
        return {"move": move.uci(), "san": board.san(move),
                "from": chess.square_name(move.from_square), "to": chess.square_name(move.to_square),
                "promotion": chess.piece_symbol(move.promotion) if move.promotion else None,
                "depth": depth, "_score": score}

    def _publish_ranked(self, generation, context, cancel, timed_out, result):
        """Publish only complete same-depth rankings for this exact position."""
        with self._condition:
            if (self._closed or not self._enabled or generation != self._generation
                    or context is not self._context or cancel.is_set() or timed_out.is_set()):
                return
            self._snapshot.update(copy.deepcopy(result), calculating=True)

    def _ranked_search(self, engine, board, seconds, cancel, timed_out, analysis_ready, analysis_handle,
                       publish=None):
        """Retain complete equal-depth rounds within one wall-clock budget.

        Root exclusions find up to three distinct moves. They do not provide a
        full-position evaluation, so this path never publishes their scores.
        """
        deadline = time.monotonic() + seconds
        legal = sorted(board.legal_moves, key=lambda move: move.uci())
        count = min(3, len(legal))
        if not count:
            return None
        completed = None
        for depth in range(1, 65):
            chosen, round_candidates = set(), []
            for rank in range(count):
                remaining = deadline - time.monotonic()
                if remaining <= .001 or cancel.is_set() or timed_out.is_set():
                    return completed
                allowed = [move for move in legal if move.uci() not in chosen]
                kwargs = {"game": object(), "info": chess.engine.INFO_ALL}
                if rank:
                    kwargs["root_moves"] = allowed
                limit = chess.engine.Limit(time=remaining, depth=depth)
                candidate = None
                try:
                    if callable(getattr(engine, "analysis", None)):
                        analysis_ready.clear()
                        analysis_handle[0] = None
                        with engine.analysis(board.copy(stack=True), limit, **kwargs) as analysis:
                            analysis_handle[0] = analysis
                            analysis_ready.set()
                            waited = False
                            try:
                                for info in analysis:
                                    packet = self._rank_packet(board, allowed, depth, info)
                                    if packet is not None:
                                        candidate = packet
                                waited = True
                                reply = analysis.wait()
                            except BaseException:
                                if not waited:
                                    try:
                                        stop = getattr(analysis, "stop", None)
                                        if callable(stop):
                                            stop()
                                        analysis.wait()
                                    except Exception:
                                        pass
                                raise
                    else:
                        # Compatibility with lightweight injected peers. Real
                        # python-chess engines always use the packet path above.
                        reply = engine.play(board.copy(stack=True), limit, **kwargs)
                        candidate = self._rank_packet(board, allowed, depth, reply.info)
                except Exception:
                    if completed is not None:
                        return completed
                    raise
                finally:
                    analysis_handle[0] = None
                    analysis_ready.clear()
                if (cancel.is_set() or timed_out.is_set() or candidate is None
                        or reply.move is None or reply.move.uci() != candidate["move"]):
                    return completed
                chosen.add(candidate["move"])
                round_candidates.append(candidate)
            # Selective root searches can invert the returned order. Compare
            # exact same-depth scores from the mover's perspective, then use a
            # stable tie break. Score's ordering handles mate distances directly.
            ordered = sorted(round_candidates, key=lambda item: (-item["_score"], item["move"]))
            candidates = [{"rank": rank, **{key: value for key, value in item.items() if key != "_score"}}
                          for rank, item in enumerate(ordered, 1)]
            completed = {**{key: value for key, value in candidates[0].items() if key != "rank"},
                         "candidates": candidates, "status": "ready",
                         "message": "현재 차례의 추천 후보입니다."}
            if publish is not None:
                publish(completed)
        return completed

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
            analysis_ready = threading.Event()
            analysis_handle = [None]
            result = None
            failure = None
            search_started = None
            try:
                # The default UCI factory has a bounded initialization timeout.
                # An OFF during startup is checked before issuing any search.
                if cancel.is_set():
                    continue
                engine = self._factory(self.binary)
                if cancel.is_set():
                    continue
                seconds = context["movetime_ms"] / 1000
                if context.get("precision"):
                    seconds = min(seconds, sparring_evaluation.MAX_BUDGET_MS / 1000)
                search_started = time.monotonic()

                def guard_search(peer, cancellation, completion, expired, duration,
                                 search_context=context, generation_id=generation,
                                 started_at=search_started, ready=analysis_ready, handle=analysis_handle):
                    if search_context.get("precision"):
                        # The stream may be silent while a deeper iteration
                        # runs. Check the base boundary independently so a
                        # stable earlier iteration can stop at 3s, not only
                        # when the next packet eventually arrives.
                        while not completion.is_set():
                            if cancellation.wait(.025):
                                break
                            elapsed = (time.monotonic() - started_at) * 1000
                            reason = self._refinement_progress(generation_id, search_context, elapsed)
                            if reason is None:
                                continue
                            ready.wait(.25)
                            stop = getattr(handle[0], "stop", None)
                            if callable(stop):
                                try:
                                    stop()
                                except Exception:
                                    pass
                            if not completion.wait(self._timeout_grace):
                                expired.set()
                                self._close_engine(peer)
                            return
                        cancelled = cancellation.is_set()
                    else:
                        cancelled = cancellation.wait(duration + self._timeout_grace)
                    if not completion.is_set():
                        if not cancelled:
                            expired.set()
                        if self._stream_evaluation or handle[0] is not None:
                            # Prefer a normal UCI stop so both the iterator and
                            # the analysis completion future can be consumed
                            # before closing the transport. Startup and wedged
                            # peers retain a bounded force-close fallback.
                            ready.wait(.25)
                            stream = handle[0]
                            stop = getattr(stream, "stop", None)
                            if callable(stop):
                                try:
                                    stop()
                                except Exception:
                                    pass
                                if completion.wait(.25):
                                    return
                        self._close_engine(peer)

                guard = threading.Thread(target=guard_search,
                                         args=(engine, cancel, finished, timed_out, seconds),
                                         name="regicide-hint-watchdog", daemon=True)
                guard.start()
                board = context["board"]
                limit = chess.engine.Limit(time=seconds)
                if not self._stream_evaluation:
                    result = self._ranked_search(engine, board, seconds, cancel, timed_out,
                                                 analysis_ready, analysis_handle,
                                                 publish=lambda result: self._publish_ranked(
                                                     generation, context, cancel, timed_out, result))
                    if result is None:
                        if timed_out.is_set():
                            raise TimeoutError("추천수 탐색 시간이 초과됐습니다.")
                        raise ValueError("같은 깊이의 추천수 비교를 완료하지 못했습니다.")
                    continue
                if callable(getattr(engine, "analysis", None)):
                    with engine.analysis(board.copy(stack=True), limit,
                                         game=object(), info=chess.engine.INFO_ALL) as analysis:
                        analysis_handle[0] = analysis
                        analysis_ready.set()
                        waited = False
                        try:
                            for info in analysis:
                                self._publish_evaluation(generation, context, cancel, timed_out, info)
                                if context.get("precision"):
                                    reason = self._refinement_progress(
                                        generation, context, (time.monotonic() - search_started) * 1000)
                                    if reason is not None:
                                        stop = getattr(analysis, "stop", None)
                                        if callable(stop):
                                            stop()
                            waited = True
                            reply = analysis.wait()
                            final_info = analysis.info
                        except BaseException:
                            if not waited:
                                # Iterator failure and analysis completion have
                                # separate futures in python-chess. Retrieve the
                                # latter's exception too, preserving this search
                                # failure and the watchdog's total time bound.
                                try:
                                    stop = getattr(analysis, "stop", None)
                                    if callable(stop):
                                        stop()
                                    analysis.wait()
                                except Exception:
                                    pass
                            raise
                else:
                    if context.get("precision"):
                        # A play() aggregate may merge depth/score/PV from
                        # different events. Precision needs the raw stream;
                        # the configured real UCI engine always provides it.
                        raise ValueError("정밀 형세 판단에는 개별 탐색 기록이 필요합니다.")
                    reply = engine.play(board.copy(stack=True), limit,
                                        game=object(), info=chess.engine.INFO_ALL)
                    final_info = reply.info
                    self._publish_evaluation(generation, context, cancel, timed_out, final_info)
                if timed_out.is_set():
                    raise TimeoutError("추천수 탐색 시간이 초과됐습니다.")
                move = reply.move
                if move is None or move not in board.legal_moves:
                    raise ValueError("추천 엔진이 합법 수를 반환하지 않았습니다.")
                depth = final_info.get("depth")
                result = {"move": move.uci(), "san": board.san(move),
                          "from": chess.square_name(move.from_square),
                          "to": chess.square_name(move.to_square),
                          "promotion": chess.piece_symbol(move.promotion) if move.promotion else None,
                          "depth": depth if type(depth) is int and depth >= 0 else None,
                          "status": "ready", "message": "추천수입니다. 직접 선택해서 두세요."}
                result["candidates"] = [{"rank": 1, **{key: result[key] for key in
                                          ("move", "san", "from", "to", "promotion", "depth")}}]
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
                        if context.get("precision"):
                            refinement = self._evaluation["refinement"]
                            elapsed = 0 if search_started is None else (time.monotonic() - search_started) * 1000
                            reason = refinement["stop_reason"] or ("error" if failure else
                                "budget" if elapsed >= refinement["max_budget_ms"] else "engine_completed")
                            self._refinement_progress(generation, context,
                                elapsed, complete=True, reason=reason)
                        self._snapshot["calculating"] = False
                        if result is not None:
                            self._snapshot.update(result)
                            if self._stream_evaluation:
                                available = self._evaluation.get("cp") is not None or self._evaluation.get("mate") is not None
                                if available:
                                    self._snapshot["depth"] = self._evaluation["depth"]
                                    self._snapshot["candidates"][0]["depth"] = self._evaluation["depth"]
                                self._evaluation["status"] = "ready" if available else "unavailable"
                        elif failure is not None:
                            self._snapshot.update(status="unavailable", message=failure,
                                                  move=None, san=None, promotion=None, depth=None,
                                                  candidates=[], **{"from": None, "to": None})
                            if self._stream_evaluation:
                                self._clear_evaluation_locked("unavailable")
                                if context.get("precision"):
                                    self._refinement_progress(generation, context,
                                        0 if search_started is None else (time.monotonic() - search_started) * 1000,
                                        complete=True, reason="error")

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
