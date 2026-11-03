"""Optional local Stockfish referee; Regicide still supplies every candidate.

The live reference has its own source/channel and never impersonates Regicide's
evaluation. Candidate screening compares a completed MultiPV round from one
Stockfish search, then verifies the chosen alternative from the child position.
All failures preserve the legal Regicide baseline. These bounded checks are not
proofs of safety, an Elo calibration, or a guarantee of a close game.
"""

import copy
import hashlib
import math
import os
from pathlib import Path
import subprocess
import threading
import time

import chess
import chess.engine


POLICY = "regicide-stockfish-referee-v3"
SUPPORTED_POLICIES = ("regicide-stockfish-referee-v1", "regicide-stockfish-referee-v2", POLICY)
SOURCE = "local_stockfish_referee"
MAX_LIVE_MS = 1500
LIVE_STARTUP_SECONDS = 1.5
MAX_SCREEN_MS = 2000
MAX_CANDIDATES = 4
MIN_SCREEN_DEPTH = 6
MIN_POST_DEPTH = 4
POSTMOVE_MAX_MS = 400
MAX_POST_DELTA_CP = 80
MAX_PACKET_DEPTH = 256
MAX_PV_PLIES = 128
MAX_BINARY_BYTES = 256 * 1024 * 1024


class RefereeUnavailable(RuntimeError):
    pass


def _board_context(game_id, revision, board):
    if (not isinstance(game_id, str) or not 0 < len(game_id) <= 200
            or type(revision) is not int or revision < 0
            or not isinstance(board, chess.Board) or board.chess960 or not board.is_valid()):
        raise ValueError("별도 심판에 전달할 대국 위치를 확인해 주세요.")
    board = board.copy(stack=True)
    return {"game_id": game_id, "revision": revision,
            "fen": board.fen(en_passant="fen"), "turn": "white" if board.turn else "black",
            "history": tuple(move.uci() for move in board.move_stack), "board": board}


def _same_context(left, right):
    return left is not None and right is not None and all(
        left[key] == right[key] for key in ("game_id", "revision", "fen", "history"))


def _budget(value, maximum):
    if type(value) is not int or value < 1:
        raise ValueError("별도 심판의 분석 시간은 양의 정수 밀리초로 지정해 주세요.")
    return min(value, maximum)


def _move(value):
    if isinstance(value, chess.Move):
        return value
    if isinstance(value, str) and len(value) in (4, 5):
        return chess.Move.from_uci(value)
    raise ValueError("합법적인 Regicide 후보 수를 전달해 주세요.")


def _packet(board, info, *, allowed=None, multipv=None):
    """Read one complete score/depth/PV packet; never merge partial messages."""
    if (not isinstance(info, dict) or type(info.get("depth")) is not int
            or not 1 <= info["depth"] <= MAX_PACKET_DEPTH
            or info.get("lowerbound") or info.get("upperbound")):
        return None
    rank = info.get("multipv", 1)
    if type(rank) is not int or rank < 1 or multipv is not None and rank > multipv:
        return None
    score, pv = info.get("score"), info.get("pv")
    if (not isinstance(score, chess.engine.PovScore) or score.turn != board.turn
            or not isinstance(pv, (list, tuple)) or not 1 <= len(pv) <= MAX_PV_PLIES
            or any(not isinstance(move, chess.Move) for move in pv)):
        return None
    if allowed is not None and pv[0] not in allowed:
        return None
    after = board.copy(stack=True)
    for move in pv:
        if move not in after.legal_moves:
            return None
        after.push(move)
    white_score = score.white()
    cp, mate = white_score.score(), white_score.mate()
    if cp is not None:
        if type(cp) is not int or abs(cp) > 100000 or mate is not None or after.is_checkmate():
            return None
    elif type(mate) is not int or abs(mate) > 10000 or mate == 0:
        return None
    elif after.is_checkmate():
        winner = chess.WHITE if mate > 0 else chess.BLACK
        if after.turn == winner:
            return None
    nodes = info.get("nodes")
    nodes = nodes if type(nodes) is int and 0 <= nodes <= 10 ** 15 else None
    wdl_white, share = None, None
    wdl = info.get("wdl")
    if isinstance(wdl, chess.engine.PovWdl) and wdl.turn == board.turn:
        values = wdl.white()
        counts = [values.wins, values.draws, values.losses]
        if all(type(value) is int and 0 <= value <= 1000 for value in counts) and sum(counts) == 1000:
            wdl_white = {"wins": counts[0], "draws": counts[1], "losses": counts[2]}
            share = (counts[0] + counts[1] / 2) / 1000
    return {"uci": pv[0].uci(), "cp": cp, "mate": mate, "cp_white": cp, "mate_white": mate,
            "depth": info["depth"], "nodes": nodes, "bound": "exact", "multipv": rank,
            "pv_uci": [move.uci() for move in pv], "white_share": share, "wdl_white": wdl_white}


class _Control:
    """One module-owned operation. Host callbacks always run outside its lock."""
    def __init__(self, deadline, cancelled=None):
        self.deadline = deadline
        self.cancelled = cancelled
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.engine = None
        self.reason = None
        self.monitor = threading.Thread(target=self._watch, name="regicide-referee-guard", daemon=True)

    def start(self):
        self.monitor.start()

    def stopped(self):
        if self.cancel.is_set():
            return True
        if self.cancelled is not None:
            try:
                if self.cancelled():
                    self.stop("cancelled")
            except Exception:
                self.stop("cancelled")
        if time.monotonic() >= self.deadline:
            self.stop("timeout")
        return self.cancel.is_set()

    def attach(self, engine):
        with self.lock:
            self.engine = engine
            stopped = self.cancel.is_set()
        if stopped:
            _close_engine(engine)

    def stop(self, reason="cancelled"):
        with self.lock:
            if self.reason is None:
                self.reason = reason
            self.cancel.set()
            engine = self.engine
        if engine is not None:
            _close_engine(engine)

    def _watch(self):
        while not self.done.wait(.01):
            if self.stopped():
                return

    def finish(self):
        self.done.set()
        with self.lock:
            engine = self.engine
            self.engine = None
        if engine is not None:
            _close_engine(engine)
        if self.monitor.ident is not None and self.monitor is not threading.current_thread():
            self.monitor.join(timeout=.1)


def _close_engine(engine):
    try:
        engine.close()
    except Exception:
        pass


class SparringReferee:
    def __init__(self, binary=None, engine_factory=None, *, hash_mb=32, timeout_grace=.35):
        if hash_mb not in (32, 64):
            raise ValueError("별도 심판 Hash는 32 또는 64MiB로 지정해 주세요.")
        self._condition = threading.Condition()
        self._screen_lock = threading.Lock()
        self._factory = engine_factory
        self._hash_mb = hash_mb
        self._grace = max(0.05, min(float(timeout_grace), .5))
        self._binary, self._sha256, self._binary_error = None, None, None
        self._binary_stamp = None
        self._engine_name = "Stockfish · 별도 심판"
        self._invalid_engine = False
        try:
            if binary is None:
                raise OSError()
            path = Path(binary).expanduser().resolve(strict=True)
            initial_stat = path.stat()
            if not path.is_file() or not 0 < initial_stat.st_size <= MAX_BINARY_BYTES:
                raise OSError()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            final_stat = path.stat()
            if (initial_stat.st_size, initial_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
                raise OSError()
            self._binary, self._sha256 = path, digest.hexdigest()
            self._binary_stamp = final_stat.st_size, final_stat.st_mtime_ns
        except (OSError, ValueError):
            self._binary_error = "Stockfish 실행 파일을 사용할 수 없어 자체 Regicide를 유지합니다."
        self._closed, self._generation = False, 0
        self._context, self._pending, self._worker = None, None, None
        self._live_control, self._screen_control = None, None
        self._screen_active = False
        self._live_done = threading.Event()
        self._live_done.set()
        self._snapshot = self._blank()

    def _metadata(self):
        return {"source": SOURCE, "policy": POLICY, "engine_name": self._engine_name,
                "binary": str(self._binary) if self._binary else None,
                "binary_sha256": self._sha256,
                "available": self._binary is not None and self._binary_error is None and not self._invalid_engine,
                "threads": 1, "hash_mb": self._hash_mb,
                "skill_level": 20, "limit_strength": False}

    def _blank(self, context=None):
        value = {**self._metadata(), "status": "idle", "game_id": None, "revision": None,
                 "fen": None, "turn": None, "cp": None, "mate": None, "depth": None,
                 "bound": None, "white_share": None, "wdl_white": None, "pv_uci": [],
                 "nodes": None, "budget_ms": None, "elapsed_ms": 0,
                 "startup_ms": None, "analysis_elapsed_ms": 0,
                 "message": "별도 Stockfish 참고 평가입니다. 자체 평가와 구분합니다."}
        if context is not None:
            value.update({key: context[key] for key in ("game_id", "revision", "fen", "turn")})
        if self._binary_error or self._invalid_engine:
            value.update(status="unavailable", message=self._binary_error or "Stockfish 엔진 확인에 실패해 자체 Regicide를 유지합니다.")
        return value

    def snapshot(self):
        with self._condition:
            value = copy.deepcopy(self._snapshot)
            value.update(self._metadata())
            return value

    def update_position(self, *, game_id, revision, board, active=True, budget_ms=500):
        context = _board_context(game_id, revision, board)
        if type(active) is not bool:
            raise ValueError("별도 심판 활성 여부는 true 또는 false로 지정해 주세요.")
        budget_ms = _budget(budget_ms, MAX_LIVE_MS)
        context.update(active=active, budget_ms=budget_ms)
        controls = []
        with self._condition:
            if self._closed:
                return self.snapshot()
            if (_same_context(self._context, context) and self._context["active"] == active
                    and self._context["budget_ms"] == budget_ms):
                return self.snapshot()
            self._generation += 1
            self._context = context
            self._pending = None
            controls = [control for control in (self._live_control, self._screen_control) if control is not None]
            self._snapshot = self._blank(context)
            self._snapshot["budget_ms"] = budget_ms
            if active and self._snapshot["available"] and not board.is_game_over(claim_draw=True):
                self._snapshot.update(status="loading", message="별도 Stockfish가 현재 위치를 분석합니다.")
                self._pending = (self._generation, context)
                if self._worker is None:
                    self._worker = threading.Thread(target=self._run_live, name="regicide-stockfish-reference", daemon=True)
                    self._worker.start()
            elif board.is_game_over(claim_draw=True):
                self._snapshot.update(status="idle", message="체스 규칙에 따른 종료 판정을 우선합니다.")
            self._condition.notify_all()
        for control in controls:
            control.stop("position_changed")
        return self.snapshot()

    def cancel(self):
        with self._condition:
            self._generation += 1
            self._pending = None
            controls = [control for control in (self._live_control, self._screen_control) if control is not None]
            self._snapshot = self._blank(self._context)
            self._snapshot["message"] = "별도 심판 조회를 중단했습니다. 자체 Regicide는 유지합니다."
            self._condition.notify_all()
        for control in controls:
            control.stop("cancelled")

    def close(self):
        with self._condition:
            self._closed = True
        self.cancel()

    def _start_engine(self, control):
        if control.stopped():
            raise TimeoutError()
        try:
            current = self._binary.stat()
            if (current.st_size, current.st_mtime_ns) != self._binary_stamp:
                raise OSError()
        except (OSError, AttributeError):
            with self._condition:
                self._binary_error = "Stockfish 실행 파일이 바뀌었거나 없어져 자체 Regicide를 유지합니다. 다시 실행해 주세요."
            raise RefereeUnavailable() from None
        remaining = max(.05, control.deadline - time.monotonic())
        if self._factory is None:
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            engine = chess.engine.SimpleEngine.popen_uci([str(self._binary)], timeout=min(1.0, remaining), **options)
        else:
            engine = self._factory(self._binary)
        control.attach(engine)
        if control.stopped():
            raise TimeoutError()
        name = getattr(engine, "id", {}).get("name", "")
        if not isinstance(name, str) or "stockfish" not in name.lower() or len(name) > 200:
            with self._condition:
                self._invalid_engine = True
            raise RefereeUnavailable()
        supported = getattr(engine, "options", {}) or {}
        required = ("Threads", "Hash", "Skill Level", "UCI_LimitStrength")
        if any(key not in supported for key in required):
            raise RefereeUnavailable()
        settings = {"Threads": 1, "Hash": self._hash_mb, "Skill Level": 20, "UCI_LimitStrength": False}
        if "UCI_ShowWDL" in supported:
            settings["UCI_ShowWDL"] = True
        engine.configure(settings)
        with self._condition:
            self._engine_name = name
        if control.stopped():
            raise TimeoutError()
        return engine

    @staticmethod
    def _analyse(engine, board, control, seconds, *, allowed=None, multipv=1, publish=None):
        packets, invalid_depths = {}, set()
        options = {"game": object(), "info": chess.engine.INFO_ALL}
        if allowed is not None:
            options["root_moves"] = list(allowed)
        if multipv > 1:
            options["multipv"] = multipv
        with engine.analysis(board.copy(stack=True), chess.engine.Limit(time=max(.001, seconds)), **options) as analysis:
            for info in analysis:
                if control.stopped():
                    raise TimeoutError()
                packet = _packet(board, info, allowed=allowed, multipv=multipv)
                if packet is None:
                    if (isinstance(info, dict) and type(info.get("depth")) is int
                            and "score" in info and 1 <= info["depth"] <= MAX_PACKET_DEPTH):
                        invalid_depths.add(info["depth"])
                    continue
                packets.setdefault(packet["depth"], {})[packet["uci"]] = packet
                # Only a bounded number of recent complete-iteration packets
                # survives, independent of arbitrary engine output messages.
                for old in sorted(packets)[:-8]:
                    del packets[old]
                invalid_depths = {depth for depth in invalid_depths if depth >= max(packets) - 8}
                if publish is not None and packet["multipv"] == 1:
                    publish(packet)
            best = analysis.wait()
        if control.stopped():
            raise TimeoutError()
        if not isinstance(best.move, chess.Move) or best.move not in board.legal_moves:
            raise ValueError("invalid referee root")
        if allowed is not None and best.move not in allowed:
            raise ValueError("foreign referee root")
        return best.move, packets, invalid_depths

    def _run_live(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None and not self._screen_active)
                if self._closed:
                    return
                generation, context = self._pending
                self._pending = None
                self._live_done.clear()
                started = time.monotonic()
                # A cold embedded-network Stockfish may spend most of 500ms
                # on UCI/Hash initialization. Bound that separately rather
                # than consuming the live reference's entire search allowance.
                control = _Control(started + LIVE_STARTUP_SECONDS)
                self._live_control = control
            control.start()
            failure, final_packet, search_started, startup_ms = None, None, None, None
            def publish(packet):
                with self._condition:
                    if (not self._closed and generation == self._generation and not self._screen_active
                            and _same_context(self._context, context) and not control.cancel.is_set()):
                        self._snapshot.update(packet, status="loading", source_fen=context["fen"],
                                              elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                                              analysis_elapsed_ms=round((time.monotonic() - search_started) * 1000, 3))
            try:
                engine = self._start_engine(control)
                search_started = time.monotonic()
                startup_ms = round((search_started - started) * 1000, 3)
                control.deadline = search_started + context["budget_ms"] / 1000 + self._grace
                with self._condition:
                    if generation == self._generation:
                        self._snapshot["startup_ms"] = startup_ms
                seconds = context["budget_ms"] / 1000
                returned, packets, invalid = self._analyse(engine, context["board"], control, seconds, publish=publish)
                eligible = [rows[returned.uci()] for depth, rows in packets.items()
                            if depth not in invalid and returned.uci() in rows]
                if not eligible:
                    raise ValueError("no completed reference")
                final_packet = max(eligible, key=lambda packet: packet["depth"])
            except RefereeUnavailable:
                failure = "Stockfish 엔진 확인에 실패해 자체 Regicide 평가를 유지합니다."
            except Exception:
                failure = "별도 심판 분석을 마치지 못해 자체 Regicide 평가를 유지합니다."
            finally:
                control.finish()
                with self._condition:
                    if self._live_control is control:
                        self._live_control = None
                    if (not self._closed and generation == self._generation and not self._screen_active
                            and _same_context(self._context, context) and not control.cancel.is_set()):
                        if failure is not None:
                            self._snapshot = self._blank(context)
                            self._snapshot.update(status="error", message=failure, budget_ms=context["budget_ms"], startup_ms=startup_ms,
                                                  elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                        else:
                            self._snapshot.update(final_packet, status="ready", source_fen=context["fen"],
                                                  elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                                                  startup_ms=startup_ms,
                                                  analysis_elapsed_ms=round((time.monotonic() - search_started) * 1000, 3),
                                                  message="별도 Stockfish 참고 평가입니다. 실제 수는 Regicide가 생성합니다.")
                    elif (not self._closed and generation == self._generation and control.reason == "timeout"):
                        self._snapshot = self._blank(context)
                        self._snapshot.update(status="error", budget_ms=context["budget_ms"], startup_ms=startup_ms,
                                              elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                                              message="별도 심판 제한 시간을 넘어 자체 평가를 유지합니다.")
                    self._live_done.set()
                    self._condition.notify_all()

    def screen_candidates(self, *, game_id, revision, board, baseline_move, candidates,
                          budget_ms=1500, cancelled=None):
        context = _board_context(game_id, revision, board)
        baseline = _move(baseline_move)
        if baseline not in board.legal_moves:
            raise ValueError("Regicide 기준수는 현재 위치의 합법 수여야 합니다.")
        budget_ms = _budget(budget_ms, MAX_SCREEN_MS)
        started = time.monotonic()
        deadline = started + budget_ms / 1000
        result = {**self._metadata(), **{key: context[key] for key in ("game_id", "revision", "fen", "turn")},
                  "status": "fallback", "baseline_uci": baseline.uci(), "selected_uci": baseline.uci(),
                  "accepted": False, "reason": "baseline_preserved", "message": "자체 Regicide 기준수를 유지합니다.",
                  "cp": None, "mate": None, "cp_white": None, "mate_white": None,
                  "depth": None, "candidates": [], "postmove": None, "budget_ms": budget_ms,
                  "elapsed_ms": 0, "band": None}
        def finish(reason, *, status="fallback"):
            if not result["accepted"]:
                # The selected move remains the baseline after a rejection.
                # Keep the rejected proposal's score in candidates/postmove,
                # never in fields that describe the move actually selected.
                selected = next((row for row in result["candidates"] if row["uci"] == baseline.uci()), None)
                result.update(cp=selected["cp"] if selected else None,
                              mate=selected["mate"] if selected else None,
                              cp_white=selected["cp"] if selected else None,
                              mate_white=selected["mate"] if selected else None)
            result.update(self._metadata())
            result.update(reason=reason, status=status, elapsed_ms=round((time.monotonic() - started) * 1000, 3))
            if status == "fallback":
                explanations = {"referee_unavailable": "별도 Stockfish를 사용할 수 없어",
                                "engine_identity_or_options_failed": "별도 Stockfish 확인에 실패해",
                                "incomplete_candidate_round": "후보들의 같은 깊이 평가가 완성되지 않아",
                                "timeout": "별도 심판 제한 시간을 넘어",
                                "cancelled": "별도 심판 검사가 취소되어",
                                "position_changed": "대국 위치가 바뀌어",
                                "stale_position": "검사한 대국 위치가 현재와 달라",
                                "referee_search_failed": "별도 심판 분석을 마치지 못해"}
                result["message"] = explanations.get(reason, "별도 심판의 후보 검증 조건을 충족하지 못해") + " 자체 Regicide 기준수를 유지합니다."
            return copy.deepcopy(result)
        if len(board.move_stack) >= 98:
            return finish("full_strength_regicide_baseline", status="skipped")
        if board.is_game_over(claim_draw=True):
            return finish("terminal_position", status="skipped")
        if not result["available"]:
            return finish("referee_unavailable")
        if not isinstance(candidates, (list, tuple)) or len(candidates) > MAX_CANDIDATES:
            return finish("candidate_limit")
        allowed = [baseline]
        try:
            for value in candidates:
                candidate = _move(value)
                if candidate not in board.legal_moves:
                    return finish("illegal_candidate")
                if candidate not in allowed:
                    allowed.append(candidate)
        except ValueError:
            return finish("invalid_candidate")
        if len(allowed) > MAX_CANDIDATES:
            return finish("candidate_limit")
        if len(allowed) == 1:
            return finish("no_alternative", status="skipped")
        control = _Control(deadline, cancelled)
        if control.stopped():
            return finish(control.reason or "cancelled")
        if not self._screen_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return finish("referee_busy")
        live = None
        generation = None
        try:
            with self._condition:
                if self._closed or self._context is not None and not _same_context(self._context, context):
                    return finish("stale_position")
                if self._context is None:
                    self._context = {**context, "active": False, "budget_ms": 500}
                generation = self._generation
                self._screen_active = True
                self._screen_control = control
                live = self._live_control
            if live is not None:
                live.stop("screen_priority")
            control.start()
            while not self._live_done.wait(.01):
                if control.stopped():
                    return finish(control.reason or "cancelled")
            if control.stopped():
                return finish(control.reason or "cancelled")
            engine = self._start_engine(control)
            left = deadline - time.monotonic()
            reserve = min(POSTMOVE_MAX_MS / 1000, max(.10, left * .30))
            if left - reserve < .05:
                return finish("insufficient_budget")
            returned, packets, invalid = self._analyse(engine, board, control, left - reserve - .02,
                                                      allowed=allowed, multipv=len(allowed))
            keys = {move.uci() for move in allowed}
            rounds = [(depth, rows) for depth, rows in packets.items()
                      if depth >= MIN_SCREEN_DEPTH and depth not in invalid and keys <= rows.keys()
                      and {rows[key]["multipv"] for key in keys} == set(range(1, len(allowed) + 1))]
            if not rounds:
                return finish("incomplete_candidate_round")
            depth, rows = max(rounds, key=lambda row: row[0])
            baseline_packet = rows[baseline.uci()]
            sign = 1 if board.turn else -1
            baseline_cp = baseline_packet["cp"]
            if baseline_cp is None:
                return finish("mate_baseline_not_comparable")
            baseline_own = baseline_cp * sign
            ramp = max(0.0, min(1.0, (len(board.move_stack) - 40) / 58))
            target = 20 + ramp * (baseline_own - 20)
            lower = -80 + ramp * min(0, baseline_own + 80)
            upper = 80 + ramp * max(0, baseline_own - 80)
            result["band"] = {"lower_cp_own": lower, "upper_cp_own": upper, "target_cp_own": target,
                              "ramp": ramp, "baseline_cp_own": baseline_own,
                              "safety_floor_cp_own": min(lower, baseline_own),
                              "selection": ("distance_to_target_then_band" if ramp > 0 else
                                            "distance_to_band_then_target") + "; baseline wins ties"}
            result["depth"] = depth
            result["candidates"] = [{**copy.deepcopy(rows[move.uci()]),
                                      "cp_own": rows[move.uci()]["cp"] * sign if rows[move.uci()]["cp"] is not None else None}
                                     for move in allowed]
            floor = min(lower, baseline_own)
            def distance(cp):
                band_distance = max(lower - cp, cp - upper, 0)
                return ((abs(cp - target), band_distance) if ramp > 0 else
                        (band_distance, abs(cp - target)))
            eligible = [row for row in result["candidates"]
                        if row["cp_own"] is not None and row["cp_own"] >= floor]
            if not eligible:
                return finish("no_candidate_in_reference_band", status="ready")
            # Repair large imbalances in verified steps without crossing the
            # safety floor or worsening an already losing baseline.
            chosen = min(eligible, key=lambda row: (*distance(row["cp_own"]),
                         row["uci"] != baseline.uci(), row["uci"]))
            result.update(cp=chosen["cp"], mate=chosen["mate"], cp_white=chosen["cp"], mate_white=chosen["mate"])
            if chosen["uci"] == baseline.uci():
                return finish("reference_prefers_regicide_baseline", status="ready")
            remaining = min(POSTMOVE_MAX_MS / 1000, deadline - time.monotonic() - .01)
            if remaining < .05:
                return finish("postmove_budget_missing")
            child = board.copy(stack=True)
            child.push_uci(chosen["uci"])
            if child.is_game_over(claim_draw=True):
                return finish("postmove_terminal")
            child_move, child_packets, child_invalid = self._analyse(engine, child, control, remaining)
            child_eligible = [rows[child_move.uci()] for child_depth, rows in child_packets.items()
                              if child_depth not in child_invalid and child_move.uci() in rows
                              and child_depth >= MIN_POST_DEPTH]
            if not child_eligible:
                return finish("postmove_incomplete")
            packet = max(child_eligible, key=lambda row: row["depth"])
            own_cp = packet["cp"] * sign if packet["cp"] is not None else None
            delta = abs(own_cp - chosen["cp_own"]) if own_cp is not None else None
            result["postmove"] = {**packet, "fen": child.fen(en_passant="fen"),
                                  "cp_own": own_cp, "disagreement_cp": delta,
                                  "max_disagreement_cp": MAX_POST_DELTA_CP,
                                  "baseline_distance": list(distance(baseline_own)),
                                  "selected_distance": list(distance(own_cp)) if own_cp is not None else None,
                                  "scope": "unrestricted opponent replies; same local Stockfish"}
            if own_cp is None or own_cp < floor:
                return finish("postmove_outside_reference_band")
            if delta > MAX_POST_DELTA_CP:
                return finish("postmove_score_disagreement")
            if distance(own_cp) >= distance(baseline_own):
                return finish("postmove_no_reference_progress")
            if control.stopped():
                return finish(control.reason or "cancelled")
            with self._condition:
                if self._closed or generation != self._generation or not _same_context(self._context, context):
                    return finish("stale_position")
            result.update(selected_uci=chosen["uci"], accepted=True,
                          message="Regicide가 만든 후보를 별도 Stockfish가 비교한 혼합 스파링 선택입니다.")
            partial = not (lower <= chosen["cp_own"] <= upper and lower <= own_cp <= upper)
            return finish("accepted_reference_partial_recovery" if partial else "accepted_reference_screen", status="ready")
        except RefereeUnavailable:
            return finish("engine_identity_or_options_failed")
        except Exception:
            return finish(control.reason or "referee_search_failed")
        finally:
            control.finish()
            with self._condition:
                if self._screen_control is control:
                    self._screen_control = None
                self._screen_active = False
                if (not self._closed and generation == self._generation
                        and _same_context(self._context, context) and self._context.get("active")
                        and self._pending is None and self._metadata()["available"]):
                    self._pending = (self._generation, self._context)
                self._condition.notify_all()
            self._screen_lock.release()
