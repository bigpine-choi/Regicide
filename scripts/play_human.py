"""Local practice host. Regicide chooses moves except explicit user handicaps.

python-chess supplies UCI transport, legality, adjudication and PGN formatting.
Bind only to loopback; JSON mutations require a per-process browser token.
"""

import argparse
import contextlib
import copy
import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser

import chess
import chess.engine
import chess.pgn

from play_game import source_version
from game_archive import GameArchive, default_game_directory
from move_hints import MoveHints
from analysis_session import AnalysisSession, AnalysisRevisionError
from coaching_session import CoachingSession, CoachingRevisionError
from coaching_reference import CoachingReference
from stockfish_gauge import StockfishGauge
from game_outcome import automatic_outcome, DRAW_POLICY
from opening_presentation import OpeningGuideCache
from opening_explorer import OpeningExplorer
from sparring_referee import SparringReferee, POLICY as REFEREE_POLICY, SOURCE as REFEREE_SOURCE
from sparring_referee_pool import build_pool as referee_candidate_pool, extend_pool as extend_referee_pool
from sparring_referee_discovery import discover as discover_referee_candidates
import opening_variety
import dynamic_play
import boss_play
import sparring_play
import sparring_rating
import evaluation_stability
import sparring_evaluation
import sparring_safety
import sparring_quality
import game_clock
import decision_support
import engine_pacing
from pgn_preview import MAX_PGN_REQUEST_BYTES, preview_pgn

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web/human-play"
LIVE_EVALUATION_MIN_MS = 3000
CLASSICAL_ACTIVITY_ID = "classical_activity_v1"


def runtime_evaluation(binary, explicit_model=None, learning_state=None, classical=False):
    """Resolve one immutable adopted model at session start; corrupt state fails closed."""
    if explicit_model is not None:
        if classical:
            raise ValueError("--classical cannot be combined with --neural-model")
        return {"model": Path(explicit_model), "generation": None, "warning": None}
    if classical:
        return {"model": None, "generation": None, "warning": None}
    state = Path(learning_state) if learning_state is not None else ROOT / "build/learning-cycle"
    if not (state / "champion.json").exists():
        return {"model": None, "generation": None, "warning": None}
    try:
        from learning_registry import load_champion
        champion = load_champion(state, binary)
        return {"model": Path(champion["model_path"]), "generation": champion["generation"], "warning": None}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return {"model": None, "generation": None,
                "warning": f"채택 모델을 확인하지 못해 기존 평가를 사용합니다: {error}"}


class RevisionError(ValueError):
    """A stale browser must refresh instead of applying its old action."""


class HumanGame:
    def __init__(self, binary, save_dir, engine_factory=None, hint_engine_factory=None, neural_model=None,
                 neural_adopted_generation=None, *, evaluation_engine_factory=None,
                 opening_variety_enabled=True, dynamic_play_enabled=False, monotonic=None, pacing_wait=None,
                 referee=None, referee_binary=None, referee_info=None, initial_time_control=None, gauge=None):
        if type(opening_variety_enabled) is not bool:
            raise ValueError("Opening variety must be enabled or disabled explicitly.")
        self.opening_variety_enabled = opening_variety_enabled
        if type(dynamic_play_enabled) is not bool:
            raise ValueError("Dynamic play must be enabled or disabled explicitly.")
        self.dynamic_play_enabled = dynamic_play_enabled
        self._clock_now = monotonic or time.monotonic
        self._pacing_wait = pacing_wait or (lambda seconds, cancel_event: cancel_event.wait(seconds))
        self.binary = Path(binary).resolve(strict=True)
        self.save_dir = Path(save_dir).resolve()
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._rating_store = sparring_rating.RatingStore(self.save_dir)
        self.binary_sha256 = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.source = source_version()
        self._factory = engine_factory or (
            lambda binary: chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10))
        evaluation_engine_factory = evaluation_engine_factory or hint_engine_factory
        self.neural_model = Path(neural_model).resolve(strict=True) if neural_model else None
        if neural_adopted_generation is not None and (
                type(neural_adopted_generation) is not int or neural_adopted_generation < 1 or not self.neural_model):
            raise ValueError("An adopted generation requires a valid model and a positive integer.")
        self.neural_adopted_generation = neural_adopted_generation
        self.neural_evaluation_mode = "neural_champion" if neural_adopted_generation else "neural_experimental"
        self.neural_sha256 = None
        self.classical_evaluation_mode = "classical"
        self._model_snapshot = None
        if self.neural_model:
            # One immutable session copy is shared by opponent and advice
            # engines, even if the original candidate is replaced later.
            self._model_snapshot = tempfile.TemporaryDirectory(prefix="regicide-model-")
            session_model = Path(self._model_snapshot.name) / "session.nn"
            try:
                with self.neural_model.open("rb") as model_stream:
                    payload = model_stream.read(4 * 1024 * 1024)
                if len(payload) >= 4 * 1024 * 1024:
                    raise ValueError("학습 모델 파일은 4MiB보다 작아야 합니다.")
                session_model.write_bytes(payload)
                self.neural_sha256 = hashlib.sha256(payload).hexdigest()
                checked = subprocess.run([str(self.binary), "eval", "--eval-file", str(session_model)],
                                         capture_output=True, text=True, timeout=15, check=False)
                if checked.returncode != 0:
                    raise ValueError("학습 모델을 읽을 수 없습니다: " + checked.stderr.strip())
            except Exception:
                self._model_snapshot.cleanup()
                self._model_snapshot = None
                raise
            def configured(factory):
                def start(binary):
                    engine = factory(binary)
                    try:
                        engine.configure({"EvalFile": str(session_model), "UseNeuralEvaluation": True})
                    except Exception:
                        engine.close()
                        raise
                    return engine
                return start
            self._factory = configured(self._factory)
            hint_engine_factory = configured(hint_engine_factory or (
                lambda binary: chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=3)))
            evaluation_engine_factory = configured(evaluation_engine_factory or (
                lambda binary: chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=3)))
        else:
            def configured_classical(factory):
                def start(binary):
                    engine = factory(binary)
                    # Application policy only: native/research defaults remain
                    # unchanged, and older binaries keep their existing path.
                    if "UsePieceActivity" in (getattr(engine, "options", None) or {}):
                        try:
                            engine.configure({"UsePieceActivity": True})
                        except Exception:
                            engine.close()
                            raise
                    return engine
                return start
            self._factory = configured_classical(self._factory)
            hint_engine_factory = configured_classical(hint_engine_factory or (
                lambda binary: chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=3)))
            evaluation_engine_factory = configured_classical(evaluation_engine_factory or (
                lambda binary: chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=3)))
        self._lock = threading.RLock()
        self._opening_guide = OpeningGuideCache()
        # Evaluation polling must not wait for the game lock held by a search.
        self._evaluation_lock = threading.Lock()
        self._evaluation = {}
        self._evaluation_uses_helper = False
        self._closing = False
        self._search_lock = threading.Lock()
        self._engine_searching = False
        self._active_pacing = None
        self._active_timing_evidence = None
        self._pacing_event = threading.Event()
        self._skip_wait_for = None
        self._active_movetime_ms = None
        self._active_extra_budget_ms = None
        self._engine = None
        self._hints = None
        self._live_evaluation = None
        self._referee = referee
        self._referee_info = copy.deepcopy(referee_info) if isinstance(referee_info, dict) else {}
        if self._referee is None and referee_binary is not None:
            try:
                self._referee = SparringReferee(referee_binary)
            except Exception:
                self._referee_info.update(available=False, reason="심판 엔진을 준비하지 못했습니다.")
        self._referee_error = None
        initial_referee = self._referee_snapshot()
        self._referee_sha256 = self._referee_info.get("sha256") or initial_referee.get("binary_sha256")
        self._referee_name = self._referee_info.get("name") or initial_referee.get("engine_name") or "Stockfish"
        self._gauge = gauge if gauge is not None else StockfishGauge(referee_binary)
        self.analysis = None
        self.coaching = None
        # Separate ownership: browsing coaching must never cancel the live-game referee.
        self.coaching_reference = CoachingReference(referee_binary)
        self.revision = 0
        try:
            self._ensure_engine()
            self._hints = MoveHints(self.binary, hint_engine_factory)
            self._live_evaluation = MoveHints(self.binary, evaluation_engine_factory,
                                              stream_evaluation=True)
            self.analysis = AnalysisSession(self.binary, self.engine_name, hint_engine_factory,
                                            reference_binary=referee_binary)
            self.coaching = CoachingSession(self.binary, self.engine_name, self.save_dir,
                                            hint_engine_factory,
                                            evaluation_id=self.neural_sha256 or self.classical_evaluation_mode)
            self._reset("white", 1000, time_control=initial_time_control)
        except Exception:
            self.close()
            raise

    def _ensure_engine(self):
        if self._engine is None:
            self._engine = self._factory(self.binary)
        self.engine_name = self._engine.id.get("name", "Regicide")
        if not self.neural_model:
            self.classical_evaluation_mode = (CLASSICAL_ACTIVITY_ID if
                "UsePieceActivity" in (getattr(self._engine, "options", None) or {}) else "classical")
        if self.neural_model:
            self.engine_name += " · 검증 통과 학습 평가" if self.neural_adopted_generation else " · 학습 평가 실험"

    def _reset(self, color, movetime_ms, boss_mode=False, sparring_mode=False, time_control=None):
        if getattr(self, "_game_open", False):
            self._adjudicate_clock()
            self._clock.finish()
            self._game_open = False
            self._save()
        self.board = chess.Board()
        self.human_color = chess.WHITE if color == "white" else chess.BLACK
        self.movetime_ms = movetime_ms
        self._settings_revision = 0
        self._time_settings_events = []
        self._engine_movetimes = {}
        self._engine_pacing_decisions = {}
        self._active_pacing = None
        self._active_timing_evidence = None
        self._skip_wait_for = None
        self._opening_seed = secrets.token_hex(16)
        self._opening_decisions = {}
        self._opening_memo = {}
        self._dynamic_decisions = {}
        self._dynamic_memo = {}
        self.boss_mode = boss_mode
        self._boss_decisions = {}
        self._boss_memo = {}
        self.sparring_mode = sparring_mode
        self._sparring_decisions = {}
        evaluation_key = "|".join((self.binary_sha256, self.neural_sha256 or self.classical_evaluation_mode))
        self._rating_calibration = (self._rating_store.calibration(evaluation_key) if sparring_mode
                                    else sparring_rating.prior(evaluation_key))
        self._rating_label = None
        self.error = None
        self._override = None
        self._forced_plies = set()
        self._handicap_backup = None
        self._handicap_events = []
        self._game_token = object()
        self._game_open = True
        self.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.updated_at = self.started_at
        self.ended_at = None
        self.game_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12]
        self._clock = game_clock.GameClock(time_control, monotonic=self._clock_now,
                                           timeout_policy="engine_only" if time_control else "both",
                                           soft_timeout_color=color if time_control else None)
        self.revision += 1
        self._sync_evaluation()
        self._save()

    def _check_revision(self, revision):
        if self._closing or not self._game_open:
            raise ValueError("종료된 대국입니다. 대국 프로그램을 다시 실행해 주세요.")
        self._adjudicate_clock()
        if type(revision) is not int or revision != self.revision:
            raise RevisionError("다른 작업이 먼저 반영됐습니다. 현재 체스판에서 다시 선택해 주세요.")

    def _require_idle(self):
        if self._engine_searching:
            raise ValueError("Regicide가 생각하고 있습니다. 현재 수가 끝나면 다시 선택해 주세요.")

    @staticmethod
    def _validate_movetime(movetime_ms):
        if type(movetime_ms) is not int or not 100 <= movetime_ms <= 5000:
            raise ValueError("100~5000ms의 생각 시간을 선택해 주세요.")

    def set_think_time(self, movetime_ms, game_id, settings_revision):
        """Persist the next search budget without changing the board revision.

        Settings have their own revision so a time change cannot invalidate an
        already submitted engine request. The game ID prevents an old window
        from applying its setting to a newly started game.
        """
        with self._lock:
            if self._closing or not self._game_open or game_id != self.game_id:
                raise RevisionError("대국이 바뀌었습니다. 현재 대국에서 생각 시간을 다시 선택해 주세요.")
            if type(settings_revision) is not int or settings_revision != self._settings_revision:
                raise RevisionError("생각 시간 설정이 먼저 바뀌었습니다. 다시 선택해 주세요.")
            self._require_no_handicap()
            if self._clock.enabled:
                raise ValueError("시간제 대국은 모든 모드에서 남은 시간과 형세에 따라 생각시간을 자동으로 조절합니다.")
            self._validate_movetime(movetime_ms)
            if movetime_ms != self.movetime_ms:
                self._time_settings_events.append({
                    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "after_ply": len(self.board.move_stack), "from_ms": self.movetime_ms,
                    "to_ms": movetime_ms, "active_search_ms": self._active_movetime_ms})
                self.movetime_ms = movetime_ms
                self._settings_revision += 1
                self._save()
            return self.state()

    def adjust_clock(self, side, delta_ms, game_id, clock_revision):
        """Apply a relative clock edit without invalidating an in-flight move."""
        with self._lock:
            if self._closing or not self._game_open or game_id != self.game_id:
                raise RevisionError("대국이 바뀌었습니다. 현재 대국에서 다시 조절해 주세요.")
            if type(clock_revision) is not int or clock_revision != self._clock.edit_revision:
                raise RevisionError("남은 시간이 먼저 변경됐습니다. 다시 조절해 주세요.")
            self._require_no_handicap()
            self._adjudicate_clock()
            if not self._clock.enabled:
                raise ValueError("시간 제한이 있는 대국에서 사용할 수 있습니다.")
            if self._outcome()[0] != "*" or self.error:
                raise ValueError("종료된 대국의 시간은 변경할 수 없습니다.")
            if side not in ("human", "engine"):
                raise ValueError("나 또는 Regicide의 시계를 선택해 주세요.")
            if type(delta_ms) is not int or delta_ms not in (-60000, -10000, 10000, 60000):
                raise ValueError("10초 또는 1분 버튼으로 시간을 조절해 주세요.")
            color = self.human_color if side == "human" else not self.human_color
            self._clock.adjust("white" if color else "black", delta_ms)
            self._adjudicate_clock()
            if side == "engine" and self._engine_searching and self._outcome()[0] == "*":
                self._refresh_active_pacing()
            self._save()
            return self.state()

    def _refresh_active_pacing(self):
        """Reconsider this turn's target; an issued native search is not restarted."""
        previous = self._active_pacing
        if previous is None:
            return
        clock = self._clock.snapshot()
        try:
            history = copy.deepcopy(self._engine_pacing_decisions)
            pacing = engine_pacing.plan(self.board, clock, evaluation=self.evaluation(),
                                        seed=self._opening_seed, history=history,
                                        pressure_start_ms=engine_pacing.PRESSURE_START_MS)
            reserve = previous.get("analysis_reserve") or {}
            if reserve:
                pacing = engine_pacing.reserve_analysis(
                    pacing, clock,
                    minimum_baseline_ms=reserve.get("requested_minimum_baseline_ms", 0),
                    optional_ms=reserve.get("requested_optional_ms", 0))
            for key in ("allocation", "rating_only_budget_ms", "auxiliary_reason"):
                if key in previous:
                    pacing[key] = previous[key]
            if self._active_timing_evidence is not None:
                pacing = engine_pacing.refine(self.board, pacing, clock,
                    **self._active_timing_evidence, history=history, seed=self._opening_seed)
            if previous.get("balance_recovery", {}).get("active"):
                pacing["balance_recovery"] = copy.deepcopy(previous["balance_recovery"])
                cap = engine_pacing.recovery_clock_limit(
                    clock, pacing["color"], pacing["balance_recovery"]["direction"],
                    pressure_start_ms=pacing["pressure_start_ms"])
                pacing["target_total_ms"] = min(previous["target_total_ms"], clock["turn_elapsed_ms"] + cap)
        except Exception as exc:
            # A cosmetic timing failure must never undo an accepted clock edit.
            pacing = {**previous, "refinement_error": type(exc).__name__}
        self._active_pacing = {**pacing, "status": previous["status"],
                              "clock_adjusted": True, "clock_edit_revision": self._clock.edit_revision}

    def _outcome(self):
        if self._override:
            return self._override
        outcome = automatic_outcome(self.board)
        return (outcome.result(), outcome.termination.name.lower()) if outcome else ("*", None)

    def _adjudicate_clock(self):
        """Called under the game lock; a timeout is a single durable result."""
        if not hasattr(self, "_clock") or not self._clock.enabled:
            return False
        if self._outcome()[0] != "*":
            self._clock.finish()
            return False
        flagged = self._clock.flagged_color or self._clock.expire()
        if not flagged:
            return False
        loser = chess.WHITE if flagged == "white" else chess.BLACK
        # python-chess safely recognizes insufficient mating material. It
        # does not prove every position-dependent dead position/fortress.
        if self._clock.timeout_policy == "both" and self.board.has_insufficient_material(not loser):
            self._override = ("1/2-1/2", "time_forfeit_insufficient_material")
        else:
            self._override = ("0-1" if loser else "1-0", "time_forfeit")
        self.error = None
        self.revision += 1
        self._sync_evaluation()
        self._save()
        return True

    def evaluation(self):
        """Read an immutable score snapshot without touching the game/search lock."""
        with self._evaluation_lock:
            snapshot = dict(self._evaluation)
            if not self._evaluation_uses_helper:
                return snapshot
            # Hold only the short snapshot lock, never the live game/search
            # lock. A late worker cannot attach a score to a newer position.
            _, current = self._live_evaluation.snapshot_with_evaluation()
            if all(current.get(key) == snapshot.get(key) for key in ("game_id", "revision", "fen")):
                # The helper's limit is independent from the opponent's move
                # time. Keep the captured budget attached to this generation.
                current["budget_ms"] = snapshot["budget_ms"]
                return current
            return snapshot

    def _referee_snapshot(self):
        if self._referee is None:
            return {"available": False, "status": "unavailable"}
        if getattr(self, "_referee_error", None):
            return {"available": False, "status": "error", "message": self._referee_error}
        try:
            sample = self._referee.snapshot()
            if isinstance(sample, dict):
                return sample
        except Exception:
            pass
        return {"available": False, "status": "error", "message": "심판 상태를 읽지 못해 자체 평가를 사용합니다."}

    def _referee_metadata(self):
        sample = self._referee_snapshot()
        available = bool(self._referee is not None and sample.get("available", True))
        name = sample.get("engine_name")
        if (isinstance(name, str) and "stockfish" in name.casefold() and len(name) <= 200
                and sample.get("binary_sha256") == self._referee_sha256):
            self._referee_name = name
        return {"enabled": bool(self.sparring_mode), "available": available,
                "configured": self._referee is not None,
                "assisted": bool(self.sparring_mode and self._referee is not None),
                "name": self._referee_name,
                "binary_sha256": self._referee_sha256,
                "policy": REFEREE_POLICY,
                "status": sample.get("status", "unavailable"),
                "message": sample.get("message") or self._referee_info.get("reason") or
                           ("로컬 Stockfish 심판을 사용합니다." if available else
                            "Stockfish 심판을 사용할 수 없어 Regicide 자체 판단을 사용합니다.")}

    def evaluation_with_reference(self):
        """UI-only envelope. Internal pacing/rating keeps evaluation()'s own score."""
        own = self.evaluation()
        return {**own, "reference_evaluation": self._gauge.snapshot(),
                "stockfish_reference": self._gauge.metadata(),
                "sparring_referee": self._referee_metadata()}

    def _apply_sparring_referee(self, board, decision, baseline_info, baseline_packets, search,
                                game_id, token, revision, *, discovery_search=None, close_engine=None):
        """Stockfish screens the bounded pool of native Regicide candidates.

        The native engine remains untouched. A referee failure rejects a
        concession and retains the legal Regicide baseline, never an SF move.
        """
        if self._referee is None or decision is None:
            return decision
        baseline = chess.Move.from_uci(decision["baseline_uci"])
        proposal = chess.Move.from_uci(decision["selected_uci"])
        if baseline not in board.legal_moves or proposal not in board.legal_moves:
            raise ValueError("Regicide의 스파링 후보가 현재 위치와 맞지 않습니다.")
        original = copy.deepcopy(decision)
        original.pop("referee", None)
        original.pop("regicide_proposal", None)
        native_pool = referee_candidate_pool(board, original)

        def cancelled():
            with self._lock:
                if (self._closing or not self._game_open or self.game_id != game_id
                        or self._game_token is not token or self.revision != revision
                        or self._pacing_event.is_set()):
                    return True
                clock = self._clock.snapshot()
                return bool(clock["enabled"] and
                            clock["white_ms" if board.turn else "black_ms"] <= 150)

        with self._lock:
            clock = self._clock.snapshot()
        budget = 1500
        if clock["enabled"]:
            remaining = clock["white_ms" if board.turn else "black_ms"]
            budget = min(budget, max(0, remaining - 500))
            if remaining < 10_000:
                budget = 0
            elif remaining < 60_000:
                budget = min(budget, 350)
            elif remaining < 180_000:
                budget = min(budget, 800)
        discovery = None
        can_discover = (discovery_search is not None and close_engine is not None
            and 8 <= len(board.move_stack) < 40 and 0 < len(native_pool) < 4
            and not self._forced_plies and not self._sparring_analysis_only()
            and not cancelled() and self._referee_metadata()["available"]
            and (not clock["enabled"] or clock["white_ms" if board.turn else "black_ms"] >= 180_000))
        if can_discover:
            owned_engine = self._engine
            try:
                discovery = discover_referee_candidates(board, discovery_search, close_engine,
                    excluded_uci=[row["uci"] for row in native_pool], budget_ms=600,
                    cancelled=cancelled, max_candidates=4 - len(native_pool))
            except Exception as exc:
                discovery = {"reason": "native_discovery_failed", "error": type(exc).__name__,
                             "native_candidates": [], "discard_engine": False}
            context = discovery.get("context", {})
            if (not isinstance(context, dict) or context.get("before_fen") != board.fen(en_passant="fen")
                    or context.get("history_uci") != [move.uci() for move in board.move_stack]
                    or context.get("pov") != ("white" if board.turn else "black")):
                discovery["native_candidates"] = []
            native_pool = extend_referee_pool(board, original, native_pool,
                discovery.get("native_candidates", []), origin="shallow_discovery")
            if discovery.get("discard_engine"):
                with self._lock:
                    if self._engine is owned_engine:
                        self._engine = None
        pool_by_uci = {row["uci"]: row for row in native_pool}
        allowed = (len(native_pool) > 1 and not self._sparring_analysis_only()
                   and sparring_play.phase(board) != "finish" and budget >= 250 and not cancelled())
        if allowed:
            try:
                receipt = self._referee.screen_candidates(
                    game_id=game_id, revision=revision, board=board.copy(stack=True),
                    baseline_move=baseline, candidates=[chess.Move.from_uci(row["uci"]) for row in native_pool],
                    budget_ms=int(budget),
                    cancelled=cancelled)
            except Exception as exc:
                receipt = {"status": "error", "reason": "referee_exception", "error": type(exc).__name__}
        else:
            receipt = {"status": "skipped", "reason": ("full_strength_phase" if sparring_play.phase(board) == "finish"
                       else "baseline_only" if len(native_pool) <= 1 else "insufficient_clock_or_analysis_only"),
                       "selected_uci": baseline.uci()}
        if not isinstance(receipt, dict):
            receipt = {"status": "error", "reason": "invalid_referee_receipt"}
        receipt = copy.deepcopy(receipt)
        identity = (isinstance(self._referee_sha256, str) and len(self._referee_sha256) == 64
                    and all(receipt.get(key) == value for key, value in (
                        ("game_id", game_id), ("revision", revision), ("fen", board.fen(en_passant="fen")),
                        ("source", REFEREE_SOURCE), ("policy", REFEREE_POLICY),
                        ("binary_sha256", self._referee_sha256))))
        selected = receipt.get("selected_uci")
        verified = (allowed and identity and receipt.get("status") == "ready"
                    and selected in pool_by_uci
                    and (selected == baseline.uci() or receipt.get("accepted") is True)
                    and not cancelled())
        # Skips at move 50 preserve the native baseline. A missing judge cannot
        # silently turn a rejected/incomplete concession back into a played one.
        selected = selected if verified else baseline.uci()
        if selected == baseline.uci() and proposal != baseline:
            replacement, _discarded = sparring_play.choose(
                board, baseline, baseline_info, baseline_packets, search, lambda: None,
                decisions=list(self._sparring_decisions.values()), seed=self._opening_seed,
                movetime_ms=0, forced=bool(self._forced_plies))
            for key in ("opponent_observation", "profile", "balance_recovery", "baseline_budget_ms",
                        "baseline_elapsed_ms", "extra_budget_ms", "extra_elapsed_ms", "rating_only"):
                if key in original:
                    replacement[key] = copy.deepcopy(original[key])
            decision = replacement
        elif verified and selected not in (baseline.uci(), proposal.uci()):
            # Scores below remain native Regicide evidence. The new child
            # check belongs only to the nested external referee receipt.
            evidence = pool_by_uci[selected]["native_evidence"]
            comparable = "candidate_compared" in evidence
            common = evidence["candidate_compared"] if comparable else evidence["line"]
            final = evidence["candidate_final"] if comparable else evidence.get("completed_final_line", evidence["line"])
            decision = copy.deepcopy(original)
            decision.update(reason="referee_selected_verified_native_pool_candidate",
                candidate_compared=copy.deepcopy(common), candidate_final=copy.deepcopy(final),
                naturalness=copy.deepcopy(evidence["naturalness"]),
                loss_cp=max(0, evidence["baseline"]["cp"] - common["cp"]) if comparable else None,
                native_comparison_available=comparable, native_candidate_depth=common["depth"],
                native_final_depth=final["depth"],
                selected_cp_own=common["cp"], selected_cp_white=common["cp_white"],
                verification_policy=REFEREE_POLICY,
                postmove_verification={"valid": False, "candidate_uci": selected,
                    "reason": "native_child_not_researched; see separate Stockfish referee.postmove"})
            control = copy.deepcopy(decision.get("band_control") or {})
            inside = all(-sparring_play.BAND_CP <= value <= sparring_play.BAND_CP
                         for value in (common["cp"], final["cp"]))
            control.update(selected_cp=common["cp"], selected_final_cp=final["cp"],
                selected_postmove_cp=None, basis="native_pool_then_separate_stockfish_postmove",
                status="externally_screened", strict_band_status="met" if inside else "unmet",
                strict_deviation_cp=max(max(-sparring_play.BAND_CP - value,
                    value - sparring_play.BAND_CP, 0) for value in (common["cp"], final["cp"])),
                pre20_inside=inside, pre30_inside=inside, pre40_inside=inside, pre50_inside=inside,
                failure_reason=None, recovered=False, pacing_status="external_selection")
            control["deviation_cp"] = None
            decision["band_control"] = control
            decision.pop("pressure", None)
            decision.pop("candidate_final_raw", None)
            decision["postmove_attempts"] = []
            decision["postmove_attempts_scope"] = "original attempts retained in regicide_proposal; selected child in referee.postmove"
            if not comparable:
                decision.update(comparison_basis="individual_native_depth; separate Stockfish common round",
                                compared_depth=None, verification_depth=common["depth"])
        decision["selected_uci"] = selected
        decision["regicide_proposal"] = original
        receipt.update(game_id=game_id, revision=revision, fen=board.fen(en_passant="fen"),
                       baseline_uci=baseline.uci(), selected_uci=selected,
                       identity_verified=bool(identity), applied=bool(verified),
                       native_pool=copy.deepcopy(native_pool),
                       native_discovery=copy.deepcopy(discovery),
                       assistant_scope="external-assisted practice; not standalone Regicide strength")
        if not verified and allowed:
            receipt["reason"] = receipt.get("reason") or "unverified_reference_baseline_retained"
        decision["referee"] = receipt
        return decision

    def _check_sparring_quality(self, board, move, search, close_engine, game_id, token, revision):
        """Audit the final native move, independently of concession selection."""
        def cancelled():
            with self._lock:
                clock = self._clock.snapshot()
                return (self._closing or not self._game_open or self.game_id != game_id
                        or self._game_token is not token or self.revision != revision
                        or self._pacing_event.is_set() or clock["enabled"] and
                        clock["white_ms" if board.turn else "black_ms"] <= engine_pacing.LOW_CLOCK_MS)

        with self._lock:
            clock = self._clock.snapshot()
            budget = sparring_quality.MAX_BUDGET_MS
            if clock["enabled"]:
                remaining = clock["white_ms" if board.turn else "black_ms"]
                pressure = min(1.0, remaining / engine_pacing.SPARRING_PRESSURE_START_MS)
                budget = int(max(0, min(max(sparring_quality.MIN_BUDGET_MS, budget * pressure ** 2),
                    remaining - engine_pacing.LOW_CLOCK_MS - 150)))
        assessor = getattr(self._referee, "assess_move_quality", None)
        # Finish/resource protection disables concessions, not safety audits.
        skip_reason = "referee_unavailable" if not callable(assessor) else None
        if skip_reason:
            _replacement, receipt, _discarded = sparring_quality.verify(
                board, move, search, close_engine, None, budget_ms=0)
            receipt.update(reason=skip_reason)
            return None, receipt, False

        def assess(candidate, amount, interrupted):
            data = assessor(game_id=game_id, revision=revision, board=board.copy(stack=True),
                            selected_move=candidate, budget_ms=int(amount), cancelled=interrupted)
            identity = (isinstance(data, dict) and isinstance(self._referee_sha256, str)
                and len(self._referee_sha256) == 64 and all(data.get(key) == value for key, value in (
                    ("game_id", game_id), ("revision", revision), ("fen", board.fen(en_passant="fen")),
                    ("history_uci", [item.uci() for item in board.move_stack]),
                    ("turn", "white" if board.turn else "black"), ("selected_uci", candidate.uci()),
                    ("source", REFEREE_SOURCE), ("policy", REFEREE_POLICY),
                    ("binary_sha256", self._referee_sha256))))
            if not identity or cancelled():
                return {"status": "unverified", "reason": "invalid_or_stale_quality_reference"}
            return data

        def native_search(amount, **options):
            if cancelled():
                raise InterruptedError("Quality check cancelled")
            with self._lock:
                current = self._clock.snapshot()
                if current["enabled"]:
                    amount = min(amount, current["white_ms" if board.turn else "black_ms"]
                                 - engine_pacing.LOW_CLOCK_MS - 150)
                if self._active_pacing is not None:
                    self._active_pacing["quality_check"] = {"active": True, "budget_ms": budget}
            return search(amount, **options, referee_discovery=True)

        session = getattr(self._referee, "quality_session", None)
        try:
            with session() if callable(session) else contextlib.nullcontext():
                return sparring_quality.verify(board, move, native_search, close_engine, assess,
                                               budget_ms=budget, cancelled=cancelled)
        except Exception as exc:
            _replacement, receipt, _discarded = sparring_quality.verify(
                board, move, search, close_engine, None, budget_ms=0)
            receipt.update(status="unverified", reason="quality_session_unavailable", error=type(exc).__name__)
            return None, receipt, False

    def hint(self):
        """Advice preference and polling never wait for opponent search."""
        return self._hints.snapshot()

    def set_hint_enabled(self, enabled):
        return self._hints.set_enabled(enabled)

    def _sync_evaluation(self, status="waiting"):
        """Called under the game lock after a position or revision changes."""
        result, termination = self._outcome()
        active = result == "*" and not self.error and self._game_open and not self._closing
        evaluating = active and self._handicap_backup is None and self.board.turn == self.human_color
        evaluation_budget_ms = ((sparring_evaluation.MAX_BUDGET_MS if self.sparring_mode else
                                 max(LIVE_EVALUATION_MIN_MS, self.movetime_ms)) if evaluating else
                                self._active_movetime_ms or self.movetime_ms)
        snapshot = {"game_id": self.game_id, "revision": self.revision,
                    "fen": self.board.fen(en_passant="fen"),
                    "turn": "white" if self.board.turn else "black",
                    "status": "unavailable" if self.error else "searching" if evaluating else status,
                    "cp": None, "mate": None, "depth": None, "bound": None,
                    "white_share": None, "result": result, "winner": None,
                    "source": "search", "source_fen": None,
                    "budget_ms": evaluation_budget_ms if active and self._handicap_backup is None else None}
        snapshot.update(evaluation_stability.assess(self.board, {}))
        if result != "*":
            winner = "white" if result == "1-0" else "black" if result == "0-1" else None
            snapshot.update(status="terminal", source="result", winner=winner,
                            white_share=1.0 if winner == "white" else 0.0 if winner == "black" else 0.5)
            if termination == "checkmate":
                snapshot["mate"] = 0
        with self._evaluation_lock:
            self._evaluation = snapshot
            self._evaluation_packets = {}
            self._evaluation_uses_helper = evaluating
            # The helper evaluates the current human position independently of
            # advice. On the engine's turn its move search already evaluates
            # the same position, so cancel the helper instead of duplicating it.
            # Sparring refines an unsettled position for up to 10s, stopping
            # after 3s when deeper iterations agree. This independent helper
            # is cancelled on a move and never extends the opponent's limit.
            self._live_evaluation.update_position(
                game_id=self.game_id, revision=self.revision, board=self.board,
                human_color=self.human_color, movetime_ms=evaluation_budget_ms,
                engine_name=self.engine_name, active=evaluating,
                precision=self.sparring_mode)
            if not self._closing:
                self._live_evaluation.set_enabled(evaluating)
        self._hints.update_position(
            game_id=self.game_id, revision=self.revision, board=self.board,
            human_color=self.human_color, movetime_ms=self.movetime_ms,
            engine_name=self.engine_name, active=active,
            handicap_mode=self._handicap_backup is not None)
        self._gauge.update_position(game_id=self.game_id, revision=self.revision,
                                   board=self.board, active=active)
        if self._referee is not None:
            try:
                self._referee.update_position(game_id=self.game_id, revision=self.revision,
                    board=self.board.copy(stack=True), active=bool(active and self.sparring_mode
                                                                 and self._handicap_backup is None),
                    budget_ms=500)
                self._referee_error = None
            except Exception:
                self._referee_error = "심판 평가를 시작하지 못해 Regicide 자체 평가를 사용합니다."

    def _publish_evaluation(self, info):
        """Publish completed-depth opponent-search scores in White's point of view."""
        # A weak/short sparring move search must not lend an incomplete or
        # malformed score to the gauge. Child/candidate searches never enter
        # this path, and no mate is converted to an arbitrary centipawn score.
        if self.sparring_mode and sparring_evaluation.validate_packet(self.board, info) is None:
            with self._evaluation_lock:
                depth = info.get("depth") if isinstance(info, dict) else None
                if (not self._closing and self._evaluation.get("status") != "terminal"
                        and type(depth) is int and depth >= 1 and "score" in info
                        and depth >= max(self._evaluation_packets, default=0)):
                    # Keep the last exact score but invalidate its old stable
                    # label when a newer completed iteration is unreliable.
                    self._evaluation_packets[depth] = dict(info)
                    self._evaluation_packets = dict(sorted(self._evaluation_packets.items())[-6:])
                    self._evaluation.update(reliability="provisional", recent_cp_range_white=None,
                        score_swing_cp=None, stable_depths=0,
                        retained_previous_score=self._evaluation.get("depth") is not None)
            return
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
            winner = "white" if white_score.score(mate_score=30000) > 0 else "black"
            share = 1.0 if winner == "white" else 0.0
        else:
            share = 0.5 + 0.5 * cp / (abs(cp) + 400.0)
        with self._evaluation_lock:
            if self._closing or self._evaluation.get("status") == "terminal":
                return
            if self.sparring_mode and depth < max(self._evaluation_packets, default=0):
                return
            if self.sparring_mode:
                self._evaluation["retained_previous_score"] = False
            self._evaluation_packets[depth] = dict(info)
            self._evaluation_packets = dict(sorted(self._evaluation_packets.items())[-6:])
            self._evaluation.update(cp=cp, mate=mate, depth=depth,
                                    bound="lower" if lower else "upper" if upper else "exact",
                                    white_share=share, winner=winner, status="searching",
                                    source="search", source_fen=self.board.fen(en_passant="fen"))
            self._evaluation.update(evaluation_stability.assess(self.board, self._evaluation_packets))

    def state(self):
        with self._lock:
            self._adjudicate_clock()
            result, termination = self._outcome()
            if result != "*" or not self._game_open:
                self._clock.finish()
            handicap_mode = self._handicap_backup is not None
            active = self._game_open and not self._closing and result == "*" and not self.error
            human_turn = active and not handicap_mode and self.board.turn == self.human_color
            engine_turn = active and not handicap_mode and self.board.turn != self.human_color
            names = {"checkmate": "체크메이트", "stalemate": "스테일메이트",
                     "insufficient_material": "메이트 불가능한 기물 구성",
                     "threefold_repetition": "3회 반복", "fivefold_repetition": "5회 반복",
                     "fifty_moves": "50수 규칙", "seventyfive_moves": "75수 규칙",
                     "resignation": "기권", "time_forfeit": "시간 초과",
                     "time_forfeit_insufficient_material": "시간 초과 · 상대 메이트 기물 부족"}
            if handicap_mode:
                status = "핸디캡 · Regicide 대신 두세요"
            elif not self._game_open and result == "*":
                status = "종료된 대국입니다"
            elif self.error:
                status = "엔진 응답에 문제가 생겼습니다. 기보는 보존됐습니다. 새 대국으로 다시 시작해 주세요."
            elif result == "1/2-1/2":
                status = "무승부 · " + names.get(termination, termination or "대국 종료")
            elif result != "*":
                human_won = (result == "1-0") == self.human_color
                status = ("당신이 이겼습니다" if human_won else "Regicide가 이겼습니다")
                status += " · " + names.get(termination, termination or "대국 종료")
            else:
                status = "당신 차례입니다" if human_turn else "Regicide 차례입니다"
                if self.board.is_check():
                    status += " · 체크"
            replay = self.board.root()
            moves = []
            for ply, move in enumerate(self.board.move_stack, 1):
                entry = {"ply": ply, "san": replay.san(move), "uci": move.uci(),
                         "actor": "human" if replay.turn == self.human_color else "engine"}
                if ply in self._forced_plies:
                    entry["forced"] = True
                moves.append(entry)
                replay.push(move)
            # Include a human move that itself ended the game (there may be no
            # engine reply). The initial engine opening is still retained.
            can_undo = (self._game_open and not handicap_mode and bool(self.board.move_stack) and
                        (self.board.turn != self.human_color or len(self.board.move_stack) >= 2))
            clock_snapshot = self._clock.snapshot()
            clock_snapshot["editable"] = bool(active and self._clock.enabled
                                               and not handicap_mode and clock_snapshot["status"] == "running")
            support = (decision_support.assess(self.board, self.human_color, clock_snapshot,
                                              evaluation=self.evaluation(),
                                              rating=self._rating_state() if self.sparring_mode else None)
                       if human_turn else None)
            return {"revision": self.revision, "game_id": self.game_id,
                    "clock": clock_snapshot, "decision_support": support,
                    "fen": self.board.fen(en_passant="fen"),
                    "started_at": self.started_at, "updated_at": self.updated_at,
                    "ended_at": self.ended_at, "record_status": self._record_status(),
                    "storage_path": str(self.save_dir),
                    "board": {chess.square_name(square): piece.symbol()
                              for square, piece in self.board.piece_map().items()},
                    "turn": "white" if self.board.turn else "black",
                    "human_color": "white" if self.human_color else "black",
                    "human_to_move": bool(human_turn), "engine_to_move": bool(engine_turn),
                    "hint_enabled": self.hint()["enabled"],
                    "legal_moves": [move.uci() for move in self.board.legal_moves]
                    if human_turn or handicap_mode else [],
                    "hint_legal_moves": [move.uci() for move in self.board.legal_moves]
                    if active and not handicap_mode else [],
                    "moves": moves, "status": status, "result": result, "termination": termination,
                    "opening_guide": self._opening_guide.get(self.board),
                    "in_check": self.board.is_check(),
                    "last_move": self.board.peek().uci() if self.board.move_stack else None,
                    "can_undo": can_undo, "error": self.error,
                    "engine_thinking": self._engine_searching,
                    "handicap_mode": handicap_mode,
                    "can_handicap": not handicap_mode and any(move["actor"] == "engine" for move in moves),
                    "engine": {"name": self.engine_name, "movetime_ms": self.movetime_ms,
                               "automatic_think_time": self._clock.enabled,
                               "time_management": ("automatic" if self._clock.enabled
                                                   else "manual_maximum" if self._clock.enabled else "fixed_legacy"),
                               "pacing": self._pacing_state(clock_snapshot),
                               "opening_variety_enabled": self.opening_variety_enabled and not self.dynamic_play_enabled and not self.boss_mode and not self.sparring_mode,
                               "dynamic_play_enabled": self.dynamic_play_enabled and not self.boss_mode and not self.sparring_mode,
                               "boss_mode": self.boss_mode,
                               "sparring_mode": self.sparring_mode,
                               "sparring_referee": self._referee_metadata(),
                               "stockfish_reference": self._gauge.metadata(),
                               "sparring": (sparring_play.derive_profile(self.board, list(self._sparring_decisions.values()))
                                            if self.sparring_mode else None),
                               "rating": self._rating_state(),
                               "sparring_rating": self._rating_state(),
                               "sparring_analysis_only": self.sparring_mode and self._sparring_analysis_only(),
                               "extra_budget_purpose": ("rating_observation" if self._sparring_analysis_only()
                                                         else "sparring_control_and_rating") if self.sparring_mode else None,
                               "boss_opponent": (boss_play.boss_opponent.derive_profile(self.board, list(self._boss_decisions.values()))
                                                 if self.boss_mode else None),
                               "boss_phase": boss_play.phase(self.board, self._boss_decisions, bool(self._forced_plies), not self.human_color, self.movetime_ms),
                               "settings_revision": self._settings_revision,
                               "active_movetime_ms": self._active_movetime_ms,
                               "extra_budget_ms": (self._sparring_extra_budget(self.movetime_ms)
                                                   if self.sparring_mode else (boss_play.extra_budget(self.movetime_ms, ply=len(self.board.move_stack))
                                                    if boss_play.phase(self.board, self._boss_decisions, bool(self._forced_plies), not self.human_color, self.movetime_ms) in ("opening", "initiative", "trap_hunt") else 0)
                                                   if self.boss_mode else dynamic_play.extra_budget(self.movetime_ms, ply=len(self.board.move_stack))
                                                   if self.dynamic_play_enabled else
                                                   opening_variety.extra_budget(self.movetime_ms) if self.opening_variety_enabled else 0),
                               "extra_budget_schedule": ([
                                   {"until_ply": opening_variety.MAX_PLIES,
                                    "budget_ms": dynamic_play.extra_budget(self.movetime_ms, ply=0)},
                                   {"until_ply": dynamic_play.MAX_PLIES,
                                    "budget_ms": dynamic_play.extra_budget(self.movetime_ms, ply=opening_variety.MAX_PLIES)}]
                                   if self.dynamic_play_enabled and not self.boss_mode and not self.sparring_mode else None),
                               "active_extra_budget_ms": self._active_extra_budget_ms,
                               "source_commit": self.source["commit"],
                               "binary_sha256": self.binary_sha256,
                               "evaluation": self.classical_evaluation_mode,
                               **({"evaluation": self.neural_evaluation_mode, "model_sha256": self.neural_sha256,
                                   "model_file": self.neural_model.name,
                                   **({"model_generation": self.neural_adopted_generation}
                                      if self.neural_adopted_generation else {})} if self.neural_model else {})}}

    def new_game(self, color, movetime_ms, revision, boss_mode=False, sparring_mode=False, time_control=None):
        with self._lock:
            self._check_revision(revision)
            self._require_idle()
            self._require_no_handicap()
            if time_control is not None and movetime_ms is None:
                movetime_ms = 1000  # Compatibility reference; every timed mode derives its actual budget.
            if color not in ("white", "black") or type(movetime_ms) is not int or not 100 <= movetime_ms <= 5000:
                raise ValueError("백 또는 흑, 100~5000ms의 생각 시간을 선택해 주세요.")
            if type(boss_mode) is not bool:
                raise ValueError("갬빗 모드 여부는 true 또는 false로 지정해 주세요.")
            if type(sparring_mode) is not bool:
                raise ValueError("스파링 모드 여부는 true 또는 false로 지정해 주세요.")
            if boss_mode and sparring_mode:
                raise ValueError("갬빗 모드와 스파링 모드 중 하나를 선택해 주세요.")
            game_clock.validate_control(time_control)
            self._ensure_engine()
            self._reset(color, movetime_ms, boss_mode, sparring_mode, time_control)
            return self.state()

    def _rating_metadata(self):
        return sparring_rating.metadata(self.board, self._sparring_decisions,
                                        self.human_color, self._rating_calibration)

    def _pacing_state(self, clock_snapshot):
        if not self._engine_searching or self._active_pacing is None:
            return None
        return {**self._active_pacing,
                "remaining_wait_ms": round(engine_pacing.remaining_wait_ms(self._active_pacing, clock_snapshot), 3)}

    def skip_engine_wait(self, revision):
        """A closing client can release this turn's hold without cancelling search."""
        with self._lock:
            self._check_revision(revision)
            self._require_no_handicap()
            if self.board.turn == self.human_color or self._outcome()[0] != "*" or self.error:
                return {"released": False, "state": self.state()}
            self._skip_wait_for = (self.game_id, self.revision)
            self._pacing_event.set()
            return {"released": True, "state": self.state()}

    def _wait_for_engine_turn(self, pacing, game_id, token, revision, cancel_event):
        """Wait with no game lock held; polling and close remain responsive."""
        with self._lock:
            start_clock = self._clock.snapshot()
            wait_start = self._clock_now()
            initially_left = engine_pacing.remaining_wait_ms(pacing, start_clock)
        # A real-time deadline also bounds a misconfigured test clock that
        # never advances. Tests normally inject a wait hook advancing it.
        wall_deadline = time.monotonic() + initially_left / 1000
        reason = pacing["reason"]
        while True:
            with self._lock:
                if self._closing or not self._game_open:
                    reason = "closing"
                    break
                if self.game_id != game_id or self._game_token is not token or self.revision != revision:
                    reason = "position_changed"
                    break
                if cancel_event.is_set():
                    reason = "client_closing"
                    break
                clock = self._clock.snapshot()
                if (self._active_pacing is not None and self._active_pacing.get("clock_adjusted")
                        and self._active_pacing.get("clock_edit_revision") != pacing.get("clock_edit_revision")):
                    pacing = copy.deepcopy(self._active_pacing)
                    wall_deadline = time.monotonic() + engine_pacing.remaining_wait_ms(pacing, clock) / 1000
                remaining = engine_pacing.remaining_wait_ms(pacing, clock)
                if remaining <= 0:
                    reason = ("low_clock" if clock.get(pacing["color"] + "_ms") is not None
                              and clock[pacing["color"] + "_ms"] <= engine_pacing.LOW_CLOCK_MS
                              else "target_reached" if pacing["enabled"] else pacing["reason"])
                    break
                remaining = min(remaining, max(0, (wall_deadline - time.monotonic()) * 1000))
                if remaining <= 0:
                    reason = "wall_deadline"
                    break
                self._active_pacing = {**pacing, "status": "waiting"}
            self._pacing_wait(min(engine_pacing.WAIT_SLICE_SECONDS, remaining / 1000), cancel_event)
        return {**pacing, "wait_target_total_ms": engine_pacing.wait_target_ms(pacing),
                "spent_before_wait_ms": start_clock["turn_elapsed_ms"],
                "waited_ms": round(max(0, (self._clock_now() - wait_start) * 1000), 3),
                "completion_reason": reason}

    def _sparring_analysis_only(self, board=None):
        position = self.board if board is None else board
        return (sparring_play.phase(position) == "finish"
                or sparring_play.sparring_pacing.resource_guard(position).get("active") is True)

    def _sparring_extra_budget(self, movetime_ms, board=None):
        position = self.board if board is None else board
        return sparring_play.extra_budget(movetime_ms, ply=len(position.move_stack),
                                          analysis_only=self._sparring_analysis_only(position))

    def _rating_state(self):
        value = sparring_rating.snapshot(self.board, self._sparring_decisions,
                                         self.human_color, self._rating_calibration)
        if self._handicap_backup is None and self._rating_label is not None:
            value["label"] = copy.deepcopy(self._rating_label)
        return value

    def _invalidate_rating_label(self):
        if self._rating_label is not None:
            self._rating_store.invalidate(self.game_id)
            self._rating_label = None

    def label_opponent_rating(self, rating, game_id, revision):
        """Save private supervision only after the current game has ended."""
        with self._lock:
            self._check_revision(revision)
            self._require_idle()
            self._require_no_handicap()
            if not isinstance(game_id, str) or game_id != self.game_id:
                raise RevisionError("다른 대국의 레이팅 요청입니다. 현재 대국에서 다시 입력해 주세요.")
            if self._record_status() != "completed":
                raise ValueError("완료된 대국에서 상대의 실제 래피드 레이팅을 입력해 주세요.")
            result, termination = self._outcome()
            sample = {"completed": True, "fingerprint": sparring_rating.fingerprint(self.board, result, termination),
                      "features": self._rating_metadata(), "result": result, "termination": termination,
                      "handicap": bool(self._handicap_events or self._forced_plies),
                      "runtime": {"source": self.source, "binary_sha256": self.binary_sha256,
                          "evaluation": self.neural_evaluation_mode if self.neural_model else self.classical_evaluation_mode,
                          "model_sha256": self.neural_sha256,
                          "mode": "sparring" if self.sparring_mode else "gambit" if self.boss_mode else "basic",
                          "policy": (sparring_play.POLICY if self.sparring_mode else boss_play.POLICY
                                     if self.boss_mode else dynamic_play.POLICY if self.dynamic_play_enabled
                                     else opening_variety.POLICY if self.opening_variety_enabled else "regicide-basic"),
                          "engine_movetimes_ms": self._engine_movetimes,
                          "time_settings_events": self._time_settings_events}}
            # The game's calibration remains frozen: its own label cannot
            # retrospectively improve its estimate. Subsequent games reload it.
            self._rating_label = self._rating_store.upsert(self.game_id, rating, sample)
            self.revision += 1
            self._sync_evaluation()
            self._save()
            return self.state()

    def label_sparring_rating(self, rating, game_id, revision):
        """Compatibility entry point for older clients."""
        return self.label_opponent_rating(rating, game_id, revision)

    def human_move(self, uci, revision):
        with self._lock:
            self._check_revision(revision)
            self._require_idle()
            if not self.state()["human_to_move"]:
                raise ValueError("지금은 당신 차례가 아닙니다.")
            if not isinstance(uci, str) or len(uci) not in (4, 5):
                raise ValueError("올바른 수를 선택해 주세요.")
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                raise ValueError("올바른 수를 선택해 주세요.") from None
            if move not in self.board.legal_moves:
                raise ValueError("그 칸으로 이동할 수 없습니다.")
            if not self._clock.complete_move("white" if self.board.turn else "black", len(self.board.move_stack) + 1):
                self._adjudicate_clock()
                return self.state()
            self.board.push(move)
            self.revision += 1
            self._sync_evaluation()
            self._save()
            return self.state()

    def engine_move(self, revision):
        # Reuse the opponent search for the engine-turn evaluation. The human-
        # turn helper is cancelled before this search. Each keeps its own budget.
        with self._search_lock:
            with self._lock:
                self._check_revision(revision)
                if not self.state()["engine_to_move"]:
                    raise ValueError("지금은 Regicide 차례가 아닙니다.")
                try:
                    self._ensure_engine()
                except Exception as exc:
                    self.error = f"{type(exc).__name__}: {exc}"
                    self._clock.pause("engine_error")
                    self.revision += 1
                    self._sync_evaluation()
                    self._save()
                    return self.state()
                board = self.board.copy(stack=True)
                configured_ms = self.movetime_ms
                movetime_ms = configured_ms
                clock_state = self._clock.snapshot()
                clock_remaining_ms = (clock_state["white_ms" if board.turn else "black_ms"]
                                      if clock_state["enabled"] else None)
                pacing_history = copy.deepcopy(self._engine_pacing_decisions)
                pacing_plan = engine_pacing.plan(board, clock_state, evaluation=self.evaluation(),
                                                seed=self._opening_seed, history=pacing_history,
                                                pressure_start_ms=engine_pacing.PRESSURE_START_MS)
                forced_game = bool(self._forced_plies)
                boss_decisions = copy.deepcopy(list(self._boss_decisions.values()))
                mode_policy = (boss_play if self.boss_mode else dynamic_play if self.dynamic_play_enabled else
                               opening_variety if self.opening_variety_enabled else None)
                policy_plan = (mode_policy.plan(board, configured_ms, self._opening_seed,
                                               forced=forced_game,
                                               **({"decisions": boss_decisions, "engine_identity": {
                                                   "binary_sha256": self.binary_sha256,
                                                   "evaluation_id": self.neural_sha256 or self.classical_evaluation_mode}}
                                                  if self.boss_mode else {}))
                               if mode_policy is not None and not self.sparring_mode else None)
                if clock_state["enabled"] and policy_plan and policy_plan["eligible"]:
                    pacing_plan = engine_pacing.reserve_analysis(
                        pacing_plan, clock_state, minimum_baseline_ms=mode_policy.MIN_TOTAL_MS,
                        optional_ms=policy_plan["extra_budget_ms"])
                allocation = engine_pacing.search_budget(
                    pacing_plan, clock_state, configured_ms,
                    automatic=clock_state["enabled"])
                movetime_ms = allocation["baseline_ms"]
                clock_limited = False
                if clock_remaining_ms is not None:
                    optional_ms = (self._sparring_extra_budget(movetime_ms, board) if self.sparring_mode else
                                   boss_play.extra_budget(movetime_ms, ply=len(board.move_stack)) if self.boss_mode else
                                   dynamic_play.extra_budget(movetime_ms, ply=len(board.move_stack)) if self.dynamic_play_enabled else
                                   opening_variety.extra_budget(movetime_ms) if self.opening_variety_enabled else 0)
                    clock_limited = (clock_remaining_ms <= engine_pacing.LOW_CLOCK_MS or
                                     self.sparring_mode and movetime_ms + optional_ms > allocation["total_search_ms"])
                # A short baseline need not discard a human observation when
                # the original whole-turn allocation still has room. This
                # path only describes the last human move; it cannot concede
                # or replace the engine's selected move.
                rating_only_cap = 0
                if (self.sparring_mode and clock_remaining_ms is not None
                        and clock_remaining_ms > engine_pacing.LOW_CLOCK_MS
                        and board.move_stack and not forced_game
                        and (clock_limited or movetime_ms < sparring_play.MIN_TOTAL_MS)):
                    rating_only_cap = min(sparring_play.QUALITY_BUDGET_MS,
                                          max(0, allocation["total_search_ms"] - movetime_ms - 25))
                    if rating_only_cap < 100:
                        rating_only_cap = 0
                pacing_plan.update(allocation=allocation, clock_edit_revision=self._clock.edit_revision,
                                   rating_only_budget_ms=rating_only_cap,
                                   auxiliary_reason=("within_budget_rating_only" if rating_only_cap else
                                                     "clock_budget_baseline_only" if clock_limited else
                                                     "below_sparring_observation_minimum" if self.sparring_mode and movetime_ms < sparring_play.MIN_TOTAL_MS
                                                     else "within_budget"))
                sparring_decisions = copy.deepcopy(list(self._sparring_decisions.values()))
                move_policy = None if self.sparring_mode or clock_limited else mode_policy
                search_plan = copy.deepcopy(policy_plan) if move_policy is not None else None
                if search_plan is not None:
                    search_plan.update(baseline_budget_ms=movetime_ms, configured_budget_ms=configured_ms)
                    if clock_state["enabled"]:
                        available_extra = max(0, allocation["total_search_ms"] - movetime_ms)
                        if allocation.get("optional_ms") is not None:
                            available_extra = min(available_extra, allocation["optional_ms"])
                        allowed_extra = min(search_plan["extra_budget_ms"], available_extra)
                        search_plan.update(extra_budget_ms=allowed_extra, candidate_budget_ms=allowed_extra)
                        if (search_plan.get("mode") in ("initiative", "trap_hunt")
                                and allowed_extra < move_policy.MIN_PROBE_MS):
                            search_plan.update(eligible=False, reason="insufficient_optional_budget")
                policy_memo = (self._boss_memo if self.boss_mode else
                               self._dynamic_memo if self.dynamic_play_enabled else self._opening_memo)
                memo_key = (move_policy.memo_key(board, movetime_ms, self.binary_sha256,
                                                     self.neural_sha256 or self.classical_evaluation_mode,
                                                     seed=self._opening_seed,
                                                     catalog_id=([search_plan.get("catalog_commit"),
                                                                  boss_play.boss_opponent.derive_profile(board, boss_decisions),
                                                                  search_plan.get("knowledge_ranking", {}).get("model_id"),
                                                                  search_plan.get("practice_memory")]
                                                                 if self.boss_mode else search_plan.get("catalog_commit")))
                            if search_plan and search_plan["eligible"] else None)
                remembered = copy.deepcopy(policy_memo.get(memo_key)) if memo_key else None
                token, game_id = self._game_token, self.game_id
                self._pacing_event = threading.Event()
                if self._skip_wait_for == (game_id, revision):
                    self._pacing_event.set()
                pacing_event = self._pacing_event
                self._engine_searching = True
                self._active_timing_evidence = None
                self._active_pacing = {**pacing_plan, "status": "searching"}
                self._active_movetime_ms = movetime_ms
                self._active_extra_budget_ms = (self._sparring_extra_budget(movetime_ms, board)
                                                if self.sparring_mode and not clock_limited else search_plan["extra_budget_ms"] if search_plan else 0)
                if rating_only_cap:
                    self._active_extra_budget_ms = rating_only_cap
                self._sync_evaluation(status="searching")
            reply, failure = None, None
            decision = None
            discard_engine = False
            watchdog = None
            clock_watchdog = None
            clock_monitor_done = threading.Event()
            active_analysis = [None]
            referee_search_active = threading.Event()
            pacing_receipt = None
            baseline_packets, timing_infos = {}, {}
            recovery_deadline = None
            timed_out = threading.Event()
            try:
                engine = self._engine
                def expire_search():
                    timed_out.set()
                    engine.close()
                if clock_remaining_ms is not None:
                    def monitor_clock():
                        while not clock_monitor_done.wait(.02):
                            with self._lock:
                                if (self._closing or self.game_id != game_id or self._game_token is not token
                                        or recovery_deadline is not None and
                                           (self.revision != revision or pacing_event.is_set())):
                                    if recovery_deadline is not None and active_analysis[0] is not None:
                                        stop = getattr(active_analysis[0], "stop", None)
                                        if callable(stop):
                                            stop()
                                    return
                                current = self._clock.snapshot()
                                expired = (self._clock.flagged_color is not None or
                                           current["white_ms" if board.turn else "black_ms"] <= 0)
                                pacing = self._active_pacing
                                if not expired and pacing and not referee_search_active.is_set() and (pacing.get("clock_adjusted")
                                        or pacing.get("balance_recovery", {}).get("active")):
                                    left = max(0, pacing.get("target_total_ms", 0) - current["turn_elapsed_ms"])
                                    available = current["white_ms" if board.turn else "black_ms"]
                                    if min(left, available) <= 100 and active_analysis[0] is not None:
                                        stop = getattr(active_analysis[0], "stop", None)
                                        if callable(stop):
                                            stop()
                            if expired:
                                expire_search()
                                return
                    clock_watchdog = threading.Thread(target=monitor_clock, daemon=True)
                    clock_watchdog.daemon = True
                    clock_watchdog.start()
                if callable(getattr(engine, "analysis", None)):
                    # Baseline retains the existing full-budget watchdog. The
                    # optional verification phase gets its own much tighter one.
                    watchdog = threading.Timer(movetime_ms / 1000 + float(getattr(engine, "timeout", 10) or 10),
                                               expire_search)
                    watchdog.daemon = True
                    watchdog.start()
                def search(budget, *, root_move=None, root_moves=None, publish=True, fresh=False, depth=None, position=None,
                           referee_discovery=False):
                    with self._lock:
                        pacing = self._active_pacing
                        if referee_discovery:
                            if (self._closing or not self._game_open or self.game_id != game_id
                                    or self._game_token is not token or self.revision != revision or pacing_event.is_set()):
                                raise InterruptedError("Native referee discovery cancelled")
                            current = self._clock.snapshot()
                            if current["enabled"]:
                                budget = min(budget, current["white_ms" if board.turn else "black_ms"] - 500)
                        elif recovery_deadline is not None:
                            if (self._closing or not self._game_open or self.game_id != game_id
                                    or self._game_token is not token or self.revision != revision
                                    or pacing_event.is_set()):
                                raise InterruptedError("Balance recovery cancelled")
                            budget = min(budget, (recovery_deadline - time.monotonic()) * 1000)
                        if not referee_discovery and pacing and (pacing.get("clock_adjusted") or recovery_deadline is not None):
                            current = self._clock.snapshot()
                            left = pacing.get("target_total_ms", 0) - current["turn_elapsed_ms"] - 100
                            available = current["white_ms" if board.turn else "black_ms"] - 100
                            if recovery_deadline is None:
                                left, available = max(1, left), max(1, available)
                            budget = min(budget, left, available)
                        if budget < 1:
                            raise TimeoutError("No remaining search budget")
                    search_board = board if position is None else position
                    if position is not None or root_move is not None or root_moves is not None:
                        publish = False
                    limit = chess.engine.Limit(time=budget / 1000, depth=depth)
                    options = {"game": object() if fresh else token, "info": chess.engine.INFO_ALL}
                    if root_move is not None:
                        options["root_moves"] = [root_move]
                    if root_moves is not None:
                        if root_move is not None:
                            raise ValueError("Provide either one root move or an allowed root set.")
                        options["root_moves"] = list(root_moves)
                    packets, latest = {}, {}
                    if callable(getattr(engine, "analysis", None)):
                        with engine.analysis(search_board.copy(stack=True), limit, **options) as analysis:
                            with self._lock:
                                active_analysis[0] = analysis
                            waited = False
                            try:
                                for info in analysis:
                                    if publish:
                                        self._publish_evaluation(info)
                                    # Do not attach a partial depth-only record to
                                    # a score from a different completed iteration.
                                    if type(info.get("depth")) is int and "score" in info:
                                        latest = dict(info)
                                        packets[info["depth"]] = latest
                                waited = True
                                result = analysis.wait()
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
                            finally:
                                with self._lock:
                                    active_analysis[0] = None
                    else:
                        result = engine.play(search_board.copy(stack=True), limit, **options)
                        latest = result.info
                        if publish:
                            self._publish_evaluation(latest)
                        if type(latest.get("depth")) is int and "score" in latest:
                            packets[latest["depth"]] = latest
                    # Retain a completed packet for the actually returned root
                    # move. Child verification evaluates a different position
                    # and must never become the current turn's expected line.
                    if (position is None and callable(getattr(engine, "analysis", None))
                            and result.move is not None):
                        for completed_depth in sorted(packets, reverse=True):
                            packet = packets[completed_depth]
                            sample = sparring_evaluation.validate_packet(board, packet)
                            if sample is not None and sample["root"] == result.move.uci():
                                if sample["depth"] >= timing_infos.get(sample["root"], {}).get("depth", 0):
                                    timing_infos[sample["root"]] = copy.deepcopy(packet)
                                break
                    return result, latest, packets

                remembered_move = (chess.Move.from_uci(remembered["selected_uci"])
                                   if remembered is not None else None)
                if remembered_move is not None and remembered_move in board.legal_moves:
                    decision = remembered
                    decision["reused"] = True
                    reply = chess.engine.BestMove(remembered_move, None)
                else:
                    first_budget = movetime_ms
                    compare = bool(search_plan and search_plan["eligible"])
                    baseline_started = time.monotonic()
                    reply, baseline_info, _packets = search(first_budget)
                    baseline_packets = _packets
                    baseline_elapsed_ms = (time.monotonic() - baseline_started) * 1000
                    if watchdog is not None:
                        watchdog.cancel()
                        watchdog.join(timeout=.2)
                        watchdog = None
                    if timed_out.is_set():
                        raise TimeoutError("엔진 탐색 응답 시간이 초과됐습니다.")
                    if reply.move is None or reply.move not in board.legal_moves:
                        raise RuntimeError("엔진이 합법 수를 반환하지 않았습니다.")
                    boss_extra_started = None
                    recovery = None
                    if self.sparring_mode:
                        with self._lock:
                            recovery_clock = self._clock.snapshot()
                            current_pacing = copy.deepcopy(self._active_pacing or pacing_plan)
                            recovery_requested = engine_pacing.recovery_plan(
                                board, current_pacing, recovery_clock,
                                baseline_info=baseline_info, baseline_move=reply.move)["balance_recovery"]["direction"] is not None
                            if recovery_requested:
                                try:
                                    current_pacing = engine_pacing.refine(
                                        board, current_pacing, recovery_clock, selected_move=reply.move,
                                        baseline_packets=baseline_packets, selected_info=timing_infos.get(reply.move.uci()),
                                        history=pacing_history, seed=self._opening_seed)
                                except Exception as timing_error:
                                    current_pacing["refinement_error"] = type(timing_error).__name__
                            current_pacing = engine_pacing.recovery_plan(
                                board, current_pacing, recovery_clock,
                                baseline_info=baseline_info, baseline_move=reply.move)
                            recovery = current_pacing["balance_recovery"]
                            if recovery["active"]:
                                pacing_plan = current_pacing
                                self._active_pacing = {**current_pacing, "status": "searching"}
                                self._active_extra_budget_ms = recovery["budget_ms"]
                                self._active_timing_evidence = {
                                    "selected_move": reply.move, "baseline_packets": baseline_packets,
                                    "selected_info": timing_infos.get(reply.move.uci())}
                    if recovery and recovery["active"]:
                        recovery_started = time.monotonic()
                        recovery_deadline = recovery_started + recovery["budget_ms"] / 1000
                        recovery.update(outcome="baseline_retained", forced_game=forced_game)

                        def rescue(budget):
                            # Optional deeper search must never destroy the legal
                            # baseline when the engine stops or returns bad evidence.
                            done, expired = threading.Event(), threading.Event()
                            def expire_rescue():
                                if not done.is_set():
                                    expired.set()
                                    engine.close()
                            guard = threading.Timer(budget / 1000 + .1, expire_rescue)
                            guard.daemon = True
                            guard.start()
                            started = time.monotonic()
                            try:
                                candidate, info, packets = search(budget)
                                sample = sparring_evaluation.validate_packet(board, info)
                                previous = sparring_evaluation.validate_packet(board, baseline_info)
                                if (expired.is_set() or (time.monotonic() - started) * 1000 > budget + 100):
                                    raise TimeoutError("Recovery search exceeded allowance")
                                if (candidate.move not in board.legal_moves or sample is None
                                        or sample["root"] != candidate.move.uci()
                                        or previous is not None and sample["depth"] < previous["depth"]):
                                    recovery["outcome"] = "unverified_rescue_baseline_retained"
                                    return None, False
                                recovery.update(outcome="deeper_search_completed", recovered_depth=sample["depth"],
                                                recovered_cp_own=(sample["cp"] * (1 if board.turn else -1)
                                                                  if sample["cp"] is not None else None))
                                return (candidate, info, packets), False
                            except Exception as exc:
                                recovery.update(outcome="rescue_failed_baseline_retained", error=type(exc).__name__)
                                engine.close()
                                return None, True
                            finally:
                                done.set()
                                guard.cancel()
                                guard.join(timeout=.2)

                        # Short opening searches may detect imbalance at depth1.
                        # Confirm it before attempting any verified concession.
                        if recovery["direction"] == "ahead" and recovery["baseline_depth"] < sparring_play.MIN_DEPTH:
                            confirmed, discard_engine = rescue(min(1000, recovery["budget_ms"] / 4))
                            if confirmed is not None:
                                reply, baseline_info, _packets = confirmed
                                baseline_packets = _packets
                                confirmed_sample = sparring_evaluation.validate_packet(board, baseline_info)
                                cp_own = (confirmed_sample["cp"] * (1 if board.turn else -1)
                                          if confirmed_sample["cp"] is not None else None)
                                if (cp_own is not None and cp_own < -100
                                        or cp_own is None and confirmed_sample["winner"] != ("white" if board.turn else "black")):
                                    recovery["direction"] = "behind"
                        remaining = max(0, (recovery_deadline - time.monotonic()) * 1000)
                        if recovery["direction"] == "behind":
                            observation = None
                            if remaining >= 800 and not forced_game:
                                observation, discard_engine = boss_play.observe_human_move(
                                    board, search, engine.close, budget_ms=min(500, remaining / 5), forced=forced_game)
                            remaining = max(0, (recovery_deadline - time.monotonic()) * 1000 - 25)
                            if remaining >= 100 and not discard_engine:
                                rescued, discard_engine = rescue(remaining)
                                if rescued is not None:
                                    reply, baseline_info, _packets = rescued
                                    baseline_packets = _packets
                            decision, _ = sparring_play.choose(
                                board, reply.move, baseline_info, _packets, search, engine.close,
                                decisions=sparring_decisions, seed=self._opening_seed, movetime_ms=0, forced=forced_game)
                            decision["reason"] = "balance_recovery_strongest"
                            if observation is not None:
                                decision["opponent_observation"] = observation
                                decision["profile"] = sparring_play.derive_profile(
                                    board, [*sparring_decisions, {"opponent_observation": observation}])
                        elif not discard_engine and remaining >= 100:
                            decision, discard_engine = sparring_play.choose(
                                board, reply.move, baseline_info, _packets, search, engine.close,
                                decisions=sparring_decisions, seed=self._opening_seed,
                                movetime_ms=movetime_ms, forced=forced_game, recovery_budget_ms=int(remaining))
                            reply = chess.engine.BestMove(chess.Move.from_uci(decision["selected_uci"]), None)
                            recovery["outcome"] = decision["reason"]
                            if (recovery.get("recovered_depth", 0) >= sparring_play.MIN_DEPTH
                                    and recovery.get("recovered_cp_own") is not None
                                    and abs(recovery["recovered_cp_own"]) <= 100
                                    and reply.move.uci() == decision.get("baseline_uci")):
                                recovery["outcome"] = "baseline_rechecked_inside_band"
                        if decision is None:
                            decision, _ = sparring_play.choose(
                                board, reply.move, baseline_info, _packets, search, engine.close,
                                decisions=sparring_decisions, seed=self._opening_seed, movetime_ms=0, forced=forced_game)
                        recovery["elapsed_ms"] = round((time.monotonic() - recovery_started) * 1000, 3)
                        decision.update(balance_recovery=copy.deepcopy(recovery),
                                        baseline_budget_ms=movetime_ms,
                                        extra_budget_ms=recovery["budget_ms"], extra_elapsed_ms=recovery["elapsed_ms"],
                                        baseline_elapsed_ms=round(baseline_elapsed_ms, 3))
                        with self._lock:
                            if self._active_pacing is not None:
                                self._active_pacing["balance_recovery"] = copy.deepcopy(recovery)
                    elif self.sparring_mode and rating_only_cap:
                        with self._lock:
                            observation_clock = self._clock.snapshot()
                            current_remaining = observation_clock["white_ms" if board.turn else "black_ms"]
                            observation_allowed = (not self._closing and self._game_open and self.game_id == game_id
                                                   and self._game_token is token and self.revision == revision)
                        used = max(baseline_elapsed_ms,
                                   observation_clock["turn_elapsed_ms"] - clock_state["turn_elapsed_ms"])
                        observation_budget = min(rating_only_cap,
                                                 allocation["total_search_ms"] - used - 25,
                                                 current_remaining - engine_pacing.LOW_CLOCK_MS)
                        if observation_allowed and observation_budget >= 100:
                            decision, _ = sparring_play.choose(
                                board, reply.move, baseline_info, _packets, search, engine.close,
                                decisions=sparring_decisions, seed=self._opening_seed, movetime_ms=0,
                                forced=forced_game)
                            observation, discard_engine = boss_play.observe_human_move(
                                board, search, engine.close, budget_ms=observation_budget, forced=forced_game)
                            decision.update(opponent_observation=observation, rating_only=True,
                                            reason="rating_observation_only", baseline_budget_ms=movetime_ms,
                                            baseline_elapsed_ms=round(baseline_elapsed_ms, 3),
                                            quality_budget_ms=round(observation_budget, 3),
                                            extra_budget_ms=rating_only_cap,
                                            extra_elapsed_ms=observation.get("elapsed_ms", 0))
                            decision["profile"] = sparring_play.derive_profile(
                                board, [*sparring_decisions, {"opponent_observation": observation}])
                    elif self.sparring_mode and not clock_limited:
                        decision, discard_engine = sparring_play.choose(
                            board, reply.move, baseline_info, _packets, search, engine.close,
                            decisions=sparring_decisions, seed=self._opening_seed,
                            movetime_ms=movetime_ms, forced=forced_game)
                        decision["baseline_elapsed_ms"] = round(baseline_elapsed_ms, 3)
                        reply = chess.engine.BestMove(chess.Move.from_uci(decision["selected_uci"]), None)
                    if self.boss_mode and not clock_limited:
                        boss_extra_started = time.monotonic()
                        total_extra = search_plan["extra_budget_ms"]
                        quality_budget = min(boss_play.QUALITY_BUDGET_MS, total_extra)
                        observation, discard_engine = boss_play.observe_human_move(
                            board, search, engine.close, budget_ms=quality_budget, forced=forced_game)
                        observed_decisions = [*boss_decisions, {"opponent_observation": observation}]
                        frequency = boss_play.boss_opponent.gate(board, observed_decisions, self._opening_seed)
                        remaining_extra = max(0, total_extra - (time.monotonic() - boss_extra_started) * 1000)
                        search_plan = {**search_plan, "extra_budget_ms": remaining_extra,
                                       "candidate_budget_ms": remaining_extra}
                        search_plan, frequency = boss_play.apply_frequency(
                            search_plan, frequency, reply.move, probe_failed=discard_engine)
                        compare = bool(search_plan["eligible"])
                    if self.boss_mode and search_plan and search_plan["eligible"] and search_plan.get("mode") == "initiative":
                        decision, discard_engine = boss_play.explore_initiative(
                            board, search_plan, reply.move, baseline_info, _packets, self._opening_seed,
                            search, engine.close, baseline_elapsed_ms=baseline_elapsed_ms)
                        reply = chess.engine.BestMove(chess.Move.from_uci(decision["selected_uci"]), None)
                    elif self.boss_mode and search_plan and search_plan["eligible"] and search_plan.get("mode") == "trap_hunt":
                        decision, discard_engine = boss_play.explore_traps(
                            board, search_plan, reply.move, baseline_info, _packets, self._opening_seed,
                            search, engine.close, baseline_elapsed_ms=baseline_elapsed_ms)
                        reply = chess.engine.BestMove(chess.Move.from_uci(decision["selected_uci"]), None)
                    elif search_plan and (self.boss_mode or len(board.move_stack) < move_policy.MAX_PLIES):
                        probes, extra_elapsed_ms = [], 0
                        baseline = move_policy.score_line(board, baseline_info, reply.move)
                        baseline_guard = getattr(move_policy, "baseline_guard", None)
                        if (compare and baseline is not None and baseline["depth"] >= move_policy.MIN_DEPTH and
                                (baseline_guard is None or baseline_guard(board, reply.move, baseline_info) is None)):
                            phase_started = time.monotonic()
                            extra_ms = search_plan["extra_budget_ms"]
                            deadline = phase_started + extra_ms / 1000
                            phase_done, phase_expired = threading.Event(), threading.Event()
                            def expire_verification():
                                if not phase_done.is_set():
                                    phase_expired.set()
                                    engine.close()
                            verification_guard = threading.Timer(extra_ms / 1000 + .1, expire_verification)
                            verification_guard.daemon = True
                            verification_guard.start()
                            try:
                                for uci in move_policy.proposals(search_plan, reply.move):
                                    remaining_ms = (deadline - time.monotonic()) * 1000
                                    budget_ms = min(remaining_ms, extra_ms / getattr(move_policy, "PROBE_DIVISOR", 3))
                                    if budget_ms < move_policy.MIN_PROBE_MS or phase_expired.is_set():
                                        break
                                    candidate_move, candidate_final, candidate_packets, candidate_error = None, None, {}, None
                                    probe_done, probe_expired = threading.Event(), threading.Event()
                                    def expire_probe(done=probe_done, expired=probe_expired):
                                        if not done.is_set():
                                            expired.set()
                                            engine.close()
                                    probe_guard = threading.Timer(budget_ms / 1000 + .1, expire_probe)
                                    probe_guard.daemon = True
                                    probe_started = time.monotonic()
                                    probe_guard.start()
                                    try:
                                        candidate, candidate_final, candidate_packets = search(
                                            budget_ms, root_move=chess.Move.from_uci(uci), publish=False,
                                            fresh=True, depth=baseline["depth"])
                                        candidate_move = candidate.move
                                    except Exception as exc:
                                        candidate_error = type(exc).__name__
                                    finally:
                                        probe_done.set()
                                        probe_guard.cancel()
                                        probe_guard.join(timeout=.2)
                                    if probe_expired.is_set() or phase_expired.is_set():
                                        candidate_error = "TimeoutError"
                                    elapsed_ms = (time.monotonic() - probe_started) * 1000
                                    probe = move_policy.assess(board, reply.move, baseline_info, uci,
                                                                  candidate_packets, candidate_final, candidate_move,
                                                                  candidate_error, budget_ms=round(budget_ms, 3), elapsed_ms=elapsed_ms)
                                    if (self.boss_mode and search_plan.get("conservative_setup") and probe["admitted"]
                                            and (probe["loss_cp"] > boss_play.SETUP_MAX_LOSS_CP
                                                 or baseline["cp"] - probe["candidate_final"]["cp"] > boss_play.SETUP_MAX_LOSS_CP)):
                                        probe.update(admitted=False, reason="conservative_setup_cost")
                                    probes.append(probe)
                                    if candidate_error:
                                        discard_engine = True
                                        break
                                    if probe["reason"] == "candidate_score_conflict":
                                        break
                            finally:
                                phase_done.set()
                                verification_guard.cancel()
                                verification_guard.join(timeout=.2)
                            if phase_expired.is_set():
                                discard_engine = True
                                if probes:
                                    probes[-1].update(admitted=False, reason="candidate_failed", error="TimeoutError")
                            extra_elapsed_ms = (time.monotonic() - phase_started) * 1000
                        decision = move_policy.receipt(board, search_plan, reply.move, baseline_info, probes,
                                                           self._opening_seed, baseline_elapsed_ms=baseline_elapsed_ms,
                                                           extra_elapsed_ms=extra_elapsed_ms)
                        reply = chess.engine.BestMove(chess.Move.from_uci(decision["selected_uci"]), None)
                    if self.boss_mode and decision is not None:
                        decision.update(opponent_observation=observation, frequency=frequency,
                                        extra_budget_ms=total_extra, tactical_budget_ms=search_plan["extra_budget_ms"],
                                        extra_elapsed_ms=round((time.monotonic() - boss_extra_started) * 1000, 3))
                if self.sparring_mode and decision is not None and self._referee is not None:
                    def discovery_search(*args, **kwargs):
                        referee_search_active.set()
                        try:
                            return search(*args, **kwargs, referee_discovery=True)
                        finally:
                            referee_search_active.clear()
                    decision = self._apply_sparring_referee(
                        board, decision, baseline_info, baseline_packets, search, game_id, token, revision,
                        discovery_search=discovery_search, close_engine=engine.close)
                    reply = chess.engine.BestMove(chess.Move.from_uci(decision["selected_uci"]), None)
                # Final gate covers both the native baseline and concessions,
                # after the referee can no longer restore an unchecked move.
                if self.sparring_mode and not discard_engine:
                    risk = sparring_safety.inspect(board, reply.move)
                    if risk["risky"]:
                        def safety_cancelled():
                            with self._lock:
                                current = self._clock.snapshot()
                                return (self._closing or not self._game_open or self.game_id != game_id
                                        or self._game_token is not token or self.revision != revision
                                        or pacing_event.is_set() or current["enabled"] and
                                        current["white_ms" if board.turn else "black_ms"] <= engine_pacing.LOW_CLOCK_MS)

                        with self._lock:
                            current = self._clock.snapshot()
                            safety_budget = sparring_safety.MAX_BUDGET_MS
                            if current["enabled"]:
                                remaining = current["white_ms" if board.turn else "black_ms"]
                                pressure = min(1.0, remaining / engine_pacing.SPARRING_PRESSURE_START_MS)
                                safety_budget = int(max(0, min(max(sparring_safety.MIN_BUDGET_MS, safety_budget * pressure ** 2),
                                    remaining - engine_pacing.LOW_CLOCK_MS - 150)))
                            if self._active_pacing is not None and safety_budget >= sparring_safety.MIN_BUDGET_MS:
                                self._active_pacing.update(
                                    safety_check={"active": True, "budget_ms": safety_budget},
                                    target_total_ms=int(current["turn_elapsed_ms"] + safety_budget + 150))
                                self._active_extra_budget_ms = safety_budget

                        def safety_search(budget, **options):
                            if safety_cancelled():
                                raise InterruptedError("Tactical safety cancelled")
                            with self._lock:
                                current = self._clock.snapshot()
                                if current["enabled"]:
                                    budget = min(budget, current["white_ms" if board.turn else "black_ms"]
                                                 - engine_pacing.LOW_CLOCK_MS - 150)
                            return search(budget, **options, referee_discovery=True)

                        referee_search_active.set()  # Old pacing targets must not truncate this allowance.
                        try:
                            replacement, safety, discarded = sparring_safety.verify(
                                board, reply.move, safety_search, engine.close,
                                budget_ms=safety_budget, cancelled=safety_cancelled)
                        finally:
                            referee_search_active.clear()
                        discard_engine = discard_engine or discarded
                        previous_decision = copy.deepcopy(decision)
                        if replacement is not None:
                            reply, baseline_info, baseline_packets = replacement
                            decision = None
                        if decision is None:
                            decision, _ = sparring_play.choose(
                                board, reply.move, baseline_info, baseline_packets, search, engine.close,
                                decisions=sparring_decisions, seed=self._opening_seed,
                                movetime_ms=0, forced=forced_game)
                        if replacement is not None:
                            decision.update(reason="tactical_safety_replacement", pre_safety_selection=previous_decision)
                            if previous_decision is not None:
                                for field in ("opponent_observation", "profile"):
                                    if field in previous_decision:
                                        decision[field] = copy.deepcopy(previous_decision[field])
                        checked_child = safety.get("alternative_child" if replacement is not None else "original_child")
                        if checked_child is not None:
                            # A failed replacement must not leave a disproven
                            # original +0.17 labelled as a verified close game.
                            child_cp = checked_child["cp_own"]
                            control = decision["band_control"]
                            control["selected_postmove_cp"] = child_cp
                            values = [control.get("selected_cp"), child_cp]
                            inside = all(type(value) in (int, float) and abs(value) <= 100 for value in values)
                            deviation = max(max(abs(value) - 100, 0) for value in values) if all(
                                type(value) in (int, float) for value in values) else None
                            control.update(status="met" if inside else "unmet", strict_band_status="met" if inside else "unmet",
                                           strict_deviation_cp=deviation, deviation_cp=deviation,
                                           recovered=inside and replacement is not None,
                                           failure_reason=None if inside else "tactical_safety_outside_band")
                            if not control.get("active"):
                                control.update(status="inactive", failure_reason="finish_full_strength", deviation_cp=None)
                            for field in ("pre20_inside", "pre30_inside", "pre40_inside", "pre50_inside"):
                                control[field] = inside
                        decision["tactical_safety"] = safety
                        with self._lock:
                            if self._active_pacing is not None and "safety_check" in self._active_pacing:
                                self._active_pacing["safety_check"]["reason"] = safety["reason"]
                # Audit the actual final selection, including quiet baseline
                # errors and move 50+, after the native tactical gate.
                if self.sparring_mode and not discard_engine:
                    referee_search_active.set()
                    try:
                        replacement, quality, discarded = self._check_sparring_quality(
                            board, reply.move, search, engine.close, game_id, token, revision)
                    finally:
                        referee_search_active.clear()
                    discard_engine = discard_engine or discarded
                    previous_decision = copy.deepcopy(decision)
                    if replacement is not None:
                        reply, baseline_info, baseline_packets = replacement
                        decision = None
                    if decision is None:
                        decision, _ = sparring_play.choose(
                            board, reply.move, baseline_info, baseline_packets, search, engine.close,
                            decisions=sparring_decisions, seed=self._opening_seed,
                            movetime_ms=0, forced=forced_game)
                    if replacement is not None:
                        decision.update(reason=("quality_guard_damage_limitation" if quality["status"] == "mitigated"
                                                else "quality_guard_replacement"),
                                        pre_quality_selection=previous_decision)
                        if previous_decision is not None:
                            for field in ("opponent_observation", "profile"):
                                if field in previous_decision:
                                    decision[field] = copy.deepcopy(previous_decision[field])
                    # Keep external position evidence separate from native
                    # scores; unverified fallbacks must remain explicit.
                    decision["quality_guard"] = quality
                    with self._lock:
                        if self._active_pacing is not None and "quality_check" in self._active_pacing:
                            self._active_pacing["quality_check"]["reason"] = quality["reason"]
                if reply.move is None or reply.move not in board.legal_moves:
                    raise RuntimeError("엔진이 합법 수를 반환하지 않았습니다.")
            except BaseException as exc:
                failure = f"{type(exc).__name__}: {exc}"
            finally:
                if watchdog is not None:
                    watchdog.cancel()
                clock_monitor_done.set()
                if clock_watchdog is not None:
                    clock_watchdog.join(timeout=.2)
            if failure is None:
                # Search allocation is already spent/frozen. Refine only the
                # whole-turn timing target using the selected move's evidence.
                with self._lock:
                    same_turn = (not self._closing and self._game_open and self.game_id == game_id
                                 and self._game_token is token and self.revision == revision)
                    if same_turn:
                        refinement_clock = self._clock.snapshot()
                        if self._active_pacing is not None:
                            pacing_plan = copy.deepcopy(self._active_pacing)
                        self._active_timing_evidence = {
                            "selected_move": reply.move, "baseline_packets": baseline_packets,
                            "selected_info": timing_infos.get(reply.move.uci())}
                        try:
                            pacing_plan = engine_pacing.refine(board, pacing_plan, refinement_clock,
                                **self._active_timing_evidence, history=pacing_history, seed=self._opening_seed)
                        except Exception as timing_error:
                            pacing_plan = {**pacing_plan, "refinement_error": type(timing_error).__name__}
                        self._active_pacing = {**pacing_plan, "status": "searching"}
                pacing_receipt = self._wait_for_engine_turn(pacing_plan, game_id, token, revision, pacing_event)
            with self._lock:
                self._engine_searching = False
                self._active_pacing = None
                self._active_timing_evidence = None
                self._active_movetime_ms = None
                self._active_extra_budget_ms = None
                self._adjudicate_clock()
                # A polling request may already have adjudicated this turn.
                # Never replace the timeout result with an engine error, nor
                # apply the move that arrived after the clock reached zero.
                if self._clock.flagged_color is not None:
                    if self._engine is not None:
                        self._engine.close()
                        self._engine = None
                    self._save(update_timestamp=False)
                    return self.state()
                if self.game_id != game_id or self._game_token is not token or self.revision != revision:
                    return self.state()
                if self._closing or not self._game_open:
                    return self.state()
                if failure is None:
                    if not self._clock.complete_move("white" if self.board.turn else "black", len(self.board.move_stack) + 1):
                        self._adjudicate_clock()
                        return self.state()
                    self.board.push(reply.move)
                    self._engine_movetimes[len(self.board.move_stack)] = movetime_ms
                    if pacing_receipt is not None:
                        self._engine_pacing_decisions[len(self.board.move_stack)] = pacing_receipt
                    if decision is not None:
                        decisions = (self._sparring_decisions if self.sparring_mode else self._boss_decisions if self.boss_mode else
                                     self._dynamic_decisions if self.dynamic_play_enabled else self._opening_decisions)
                        decisions[len(self.board.move_stack)] = copy.deepcopy(decision)
                        if memo_key:
                            policy_memo[memo_key] = copy.deepcopy(decision)
                    if discard_engine and self._engine is not None:
                        self._engine.close()
                        self._engine = None
                else:
                    self.error = failure
                    self._clock.pause("engine_error")
                    if self._engine is not None:
                        self._engine.close()
                        self._engine = None
                self.revision += 1
                self._sync_evaluation()
                self._save()
                return self.state()

    def undo(self, revision):
        with self._lock:
            self._check_revision(revision)
            self._require_idle()
            self._require_no_handicap()
            if not self.state()["can_undo"]:
                raise ValueError("되돌릴 당신의 수가 없습니다.")
            self._invalidate_rating_label()
            if self.board.turn != self.human_color:
                self.board.pop()  # Human move submitted before the engine reply.
            else:
                self.board.pop()
                self.board.pop()
            self.error = None
            self._override = None
            self._forced_plies.intersection_update(range(1, len(self.board.move_stack) + 1))
            self._engine_movetimes = {ply: ms for ply, ms in self._engine_movetimes.items()
                                     if ply <= len(self.board.move_stack)}
            self._engine_pacing_decisions = {ply: row for ply, row in self._engine_pacing_decisions.items()
                                            if ply <= len(self.board.move_stack)}
            self._opening_decisions = {ply: row for ply, row in self._opening_decisions.items()
                                       if ply <= len(self.board.move_stack)}
            self._dynamic_decisions = {ply: row for ply, row in self._dynamic_decisions.items()
                                       if ply <= len(self.board.move_stack)}
            self._boss_decisions = {ply: row for ply, row in self._boss_decisions.items()
                                    if ply <= len(self.board.move_stack)}
            self._sparring_decisions = {ply: row for ply, row in self._sparring_decisions.items()
                                        if ply <= len(self.board.move_stack)}
            self._clock.restore_ply(len(self.board.move_stack), "white" if self.board.turn else "black")
            self._game_token = object()
            if self._engine is None:
                self._ensure_engine()
            self.revision += 1
            self._sync_evaluation()
            self._save()
            return self.state()

    def resign(self, revision):
        with self._lock:
            self._check_revision(revision)
            self._require_idle()
            self._require_no_handicap()
            if self._outcome()[0] != "*" or self.error:
                raise ValueError("이미 종료된 대국입니다.")
            self._override = ("0-1" if self.human_color else "1-0", "resignation")
            self.revision += 1
            self._sync_evaluation()
            self._save()
            return self.state()

    def _require_no_handicap(self):
        if self._handicap_backup is not None:
            raise ValueError("Regicide의 수 바꾸기를 완료하거나 취소해 주세요.")

    def begin_handicap(self, ply, revision):
        """Preview a replacement without overwriting the durable original line."""
        with self._lock:
            self._check_revision(revision)
            self._require_idle()
            self._require_no_handicap()
            if type(ply) is not int or not 1 <= ply <= len(self.board.move_stack):
                raise ValueError("바꿀 Regicide의 수를 선택해 주세요.")
            preview = self.board.copy(stack=True)
            while len(preview.move_stack) >= ply:
                preview.pop()
            if preview.turn == self.human_color:
                raise ValueError("당신의 수가 아닌 Regicide의 수를 선택해 주세요.")
            self._clock.pause("handicap_preview")
            self._adjudicate_clock()
            self._handicap_backup = {"board": self.board.copy(stack=True),
                                     "clock": self._clock,
                                     "forced_plies": self._forced_plies.copy(),
                                     "engine_movetimes": self._engine_movetimes.copy(),
                                     "engine_pacing_decisions": copy.deepcopy(self._engine_pacing_decisions),
                                     "opening_decisions": copy.deepcopy(self._opening_decisions),
                                     "dynamic_decisions": copy.deepcopy(self._dynamic_decisions),
                                     "boss_decisions": copy.deepcopy(self._boss_decisions),
                                     "sparring_decisions": copy.deepcopy(self._sparring_decisions),
                                     "override": self._override, "error": self.error,
                                     "outcome": self._outcome(), "ply": ply,
                                     "updated_at": self.updated_at, "ended_at": self.ended_at}
            self.board = preview
            # The preview has no ticking timer. The original paused object is
            # retained intact for cancellation and durable PGN export.
            original_clock = self._clock
            self._clock = game_clock.GameClock(original_clock.control_id, monotonic=self._clock_now,
                                               timeout_policy=original_clock.timeout_policy,
                                               soft_timeout_color=original_clock.soft_timeout_color)
            self._clock.history = copy.deepcopy(original_clock.history)
            self._clock.adjustments = copy.deepcopy(original_clock.adjustments)
            self._clock.edit_revision = original_clock.edit_revision
            self._clock.restore_ply(len(preview.move_stack), "white" if preview.turn else "black")
            self._clock.pause("handicap_preview")
            self._forced_plies.intersection_update(range(1, ply))
            self._engine_movetimes = {number: ms for number, ms in self._engine_movetimes.items()
                                     if number < ply}
            self._engine_pacing_decisions = {number: row for number, row in self._engine_pacing_decisions.items()
                                            if number < ply}
            self._opening_decisions = {number: row for number, row in self._opening_decisions.items()
                                       if number < ply}
            self._dynamic_decisions = {number: row for number, row in self._dynamic_decisions.items()
                                       if number < ply}
            self._boss_decisions = {number: row for number, row in self._boss_decisions.items()
                                    if number < ply}
            self._sparring_decisions = {number: row for number, row in self._sparring_decisions.items()
                                        if number < ply}
            self.error = None
            self._override = None
            self.revision += 1
            self._sync_evaluation()
            return self.state()

    def forced_move(self, uci, revision):
        with self._lock:
            self._check_revision(revision)
            self._require_idle()
            if self._handicap_backup is None or self.board.turn == self.human_color:
                raise ValueError("먼저 바꿀 Regicide의 수를 선택해 주세요.")
            if not isinstance(uci, str) or len(uci) not in (4, 5):
                raise ValueError("올바른 수를 선택해 주세요.")
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                raise ValueError("올바른 수를 선택해 주세요.") from None
            if move not in self.board.legal_moves:
                raise ValueError("그 칸으로 이동할 수 없습니다. 합법 수 중에서 선택해 주세요.")
            backup = self._handicap_backup
            ply = backup["ply"]
            self._invalidate_rating_label()
            self.board.push(move)
            self._forced_plies.add(ply)
            self._handicap_events.append({
                "ply": ply, "replacement": move.uci(),
                "removed_suffix": [item.uci() for item in backup["board"].move_stack[ply - 1:]],
                "original_forced_plies": sorted(backup["forced_plies"]),
                "original_result": backup["outcome"][0],
                "original_termination": backup["outcome"][1],
                "original_error": backup["error"],
                "original_clock": backup["clock"].metadata(),
                "clock_restore_policy": "after_replaced_move_no_extra_increment",
                "clock_restore_ply": ply,
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
            # Replace the move, not the time control. Retain the original
            # move's elapsed time and its one increment, exactly as recorded.
            # Preview time and the discarded continuation must not be charged
            # to the new branch, nor may replacing a move earn an increment.
            original_clock = backup["clock"]
            restored_clock = game_clock.GameClock(original_clock.control_id, monotonic=self._clock_now,
                timeout_policy=original_clock.timeout_policy, soft_timeout_color=original_clock.soft_timeout_color)
            restored_clock.history = copy.deepcopy(original_clock.history)
            restored_clock.adjustments = copy.deepcopy(original_clock.adjustments)
            restored_clock.edit_revision = max(original_clock.edit_revision, self._clock.edit_revision)
            restored_clock.restore_ply(ply, "white" if self.board.turn else "black")
            self._clock = restored_clock
            self._handicap_backup = None
            # python-chess sends ucinewgame and the complete new branch before
            # the next search, including all repetition/castling/EP history.
            self._game_token = object()
            self.revision += 1
            self._sync_evaluation()
            self._save()
            return self.state()

    def cancel_handicap(self, revision):
        with self._lock:
            self._check_revision(revision)
            self._require_idle()
            if self._handicap_backup is None:
                raise ValueError("현재 바꾸고 있는 Regicide의 수가 없습니다.")
            backup = self._handicap_backup
            self.board = backup["board"]
            self._clock = backup["clock"]
            self._clock.resume("white" if self.board.turn else "black")
            self._forced_plies = backup["forced_plies"]
            self._engine_movetimes = backup["engine_movetimes"]
            self._engine_pacing_decisions = backup["engine_pacing_decisions"]
            self._opening_decisions = backup["opening_decisions"]
            self._dynamic_decisions = backup["dynamic_decisions"]
            self._boss_decisions = backup["boss_decisions"]
            self._sparring_decisions = backup["sparring_decisions"]
            self.error = backup["error"]
            self._override = backup["override"]
            self.updated_at = backup["updated_at"]
            self.ended_at = backup["ended_at"]
            self._handicap_backup = None
            self.revision += 1
            self._sync_evaluation()
            self._save(update_timestamp=False)
            return self.state()

    def pgn(self, *, adjudicate=True):
        with self._lock:
            if adjudicate and self._handicap_backup is None:
                self._adjudicate_clock()
            backup = self._handicap_backup
            board = backup["board"] if backup else self.board
            forced_plies = backup["forced_plies"] if backup else self._forced_plies
            movetimes = backup["engine_movetimes"] if backup else self._engine_movetimes
            error = backup["error"] if backup else self.error
            clock = backup["clock"] if backup else self._clock
            game = chess.pgn.Game.from_board(board)
            result, termination = backup["outcome"] if backup else self._outcome()
            game.headers.update({"Event": "Regicide local human practice", "Site": "local",
                                 "Date": datetime.datetime.fromisoformat(self.started_at).astimezone().strftime("%Y.%m.%d"),
                                 "White": "Human" if self.human_color else self.engine_name,
                                 "Black": self.engine_name if self.human_color else "Human",
                                 "Result": result, "EngineMoveTimeMS": str(self.movetime_ms),
                                 "EngineSHA256": self.binary_sha256,
                                 "Evaluation": self.classical_evaluation_mode,
                                 "InterfaceSourceCommit": self.source["commit"] or "unknown",
                                 "DrawPolicy": "always claim available threefold/50-move draws",
                                 "Termination": "unterminated" if result == "*" else "normal",
                                 "TerminationReason": termination or ("engine_error" if error else "in_progress"),
                                 "HandicapMoves": str(len(forced_plies))})
            if clock.enabled:
                game.headers["TimeControl"] = f"{clock.config['initial_ms'] // 1000}+{clock.config['increment_ms'] // 1000}"
                if clock.adjustments:
                    game.headers["ClockAdjustments"] = str(len(clock.adjustments))
                    game.headers["ClockAdjustmentPolicy"] = "User-edited practice clocks; completed-move clocks preserved"
                game.headers["ClockUndoPolicy"] = "restore retained-ply clock balances"
                game.headers["TimeForfeitPolicy"] = clock.timeout_policy
                game.headers["EnginePacingPolicy"] = engine_pacing.POLICY
                game.headers["EngineTimeManagement"] = "automatic" if self.sparring_mode else "manual_maximum"
            if termination in ("time_forfeit", "time_forfeit_insufficient_material"):
                game.headers["Termination"] = "time forfeit"
            game.headers.update(opening_variety.pgn_headers(self._opening_metadata()))
            game.headers.update(dynamic_play.pgn_headers(self._dynamic_metadata()))
            game.headers.update(boss_play.pgn_headers(self._boss_metadata()))
            game.headers.update(sparring_play.pgn_headers(self._sparring_metadata()))
            referee = self._referee_metadata()
            if referee["assisted"]:
                game.headers.update({"SparringReferee": str(referee["name"]),
                    "RefereeBinarySHA256": str(referee.get("binary_sha256") or "unknown"),
                    "RefereePolicy": referee["policy"], "AssistedSparring": "true"})
            if self.neural_model:
                game.headers.update({"Evaluation": self.neural_evaluation_mode,
                                     "ModelSHA256": self.neural_sha256,
                                     "ModelFile": self.neural_model.name})
                if self.neural_adopted_generation:
                    game.headers["ModelGeneration"] = str(self.neural_adopted_generation)
            if movetimes or self._time_settings_events:
                game.headers["EngineMoveTimePolicy"] = "Header is latest setting; move comments record each search budget"
            for ply, node in enumerate(game.mainline(), 1):
                if ply in forced_plies:
                    node.comment = "User-forced Regicide move (handicap)"
                elif ply in movetimes:
                    node.comment = f"Regicide think time: {movetimes[ply]} ms"
                if clock.enabled and ply < len(clock.history):
                    row = clock.history[ply]
                    node.set_clock(row[row["mover"] + "_ms"] / 1000)
            return str(game) + "\n\n"

    def _record_status(self):
        if self.error:
            return "error"
        if self._outcome()[0] != "*":
            return "completed"
        return "in_progress" if self._game_open else "unfinished"

    def _opening_metadata(self):
        return {"enabled": self.opening_variety_enabled and not self.dynamic_play_enabled and not self.boss_mode and not self.sparring_mode, "policy": opening_variety.POLICY,
                "seed": self._opening_seed, "max_plies": opening_variety.MAX_PLIES,
                "early_plies": opening_variety.EARLY_PLIES,
                "max_early_probes": opening_variety.MAX_EARLY_PROBES,
                "early_development_weight": opening_variety.EARLY_DEVELOPMENT_WEIGHT,
                "tolerance_cp": opening_variety.TOLERANCE_CP, "min_total_ms": opening_variety.MIN_TOTAL_MS,
                "decisions": [copy.deepcopy(row) for _ply, row in sorted(self._opening_decisions.items())],
                "memo": [{"key": key, "decision": copy.deepcopy(row)}
                         for key, row in sorted(self._opening_memo.items())]}

    def _dynamic_metadata(self):
        return {"enabled": self.dynamic_play_enabled and not self.boss_mode and not self.sparring_mode, "policy": dynamic_play.POLICY,
                "seed": self._opening_seed, "max_plies": dynamic_play.MAX_PLIES,
                "decisions": [copy.deepcopy(row) for _ply, row in sorted(self._dynamic_decisions.items())],
                "memo": [{"key": key, "decision": copy.deepcopy(row)}
                         for key, row in sorted(self._dynamic_memo.items())]}

    def _boss_metadata(self):
        return {"enabled": self.boss_mode, "policy": boss_play.POLICY, "seed": self._opening_seed,
                "knowledge": boss_play.gambit_knowledge.metadata(),
                "learning": boss_play.gambit_learning.model_identity(),
                "operations": boss_play.gambit_operations.metadata(),
                "practice_memory": boss_play.gambit_practice_memory.metadata(),
                "risk_profile": boss_play.RISK_PROFILE, "trap_max_plies": boss_play.TRAP_MAX_PLIES,
                "opponent_profile": boss_play.boss_opponent.derive_profile(self.board, list(self._boss_decisions.values())),
                "decisions": [copy.deepcopy(row) for _ply, row in sorted(self._boss_decisions.items())],
                "memo": [{"key": key, "decision": copy.deepcopy(row)}
                         for key, row in sorted(self._boss_memo.items())]}

    def _sparring_metadata(self):
        return {"enabled": self.sparring_mode, "policy": sparring_play.POLICY, "seed": self._opening_seed,
                "evaluation_policy": sparring_evaluation.POLICY if self.sparring_mode else None,
                "verification_policy": sparring_play.POSTMOVE_POLICY if self.sparring_mode else None,
                "profile": sparring_play.derive_profile(self.board, list(self._sparring_decisions.values())),
                "decisions": [copy.deepcopy(row) for _ply, row in sorted(self._sparring_decisions.items())]}

    def _referee_record(self, metadata=None):
        metadata = copy.deepcopy(metadata) if metadata is not None else self._referee_metadata()
        return {**metadata, "identity": {
                    "name": metadata["name"], "binary_sha256": metadata["binary_sha256"],
                    "source_url": self._referee_info.get("source_url"),
                    "license": self._referee_info.get("license", "GPL-3.0")},
                "decisions": [copy.deepcopy(row["referee"]) for _ply, row in
                              sorted(self._sparring_decisions.items()) if isinstance(row.get("referee"), dict)]}

    def _save(self, update_timestamp=True):
        if update_timestamp:
            self.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if self._record_status() == "in_progress":
            self.ended_at = None
        elif self.ended_at is None:
            self.ended_at = self.updated_at
        state = self.state()
        # Actual reported ratings live only in the ignored private database.
        # Public/exportable game receipts retain the experimental estimate.
        for key in ("rating", "sparring_rating"):
            if state["engine"].get(key) is not None:
                state["engine"][key]["label"] = None
        record = {"game_id": self.game_id, "started_at": self.started_at,
                  "updated_at": self.updated_at, "ended_at": self.ended_at,
                  "record_status": self._record_status(), "schema_version": 2,
                  "source": self.source, "binary": str(self.binary),
                  "initial_fen": self.board.root().fen(en_passant="fen"),
                  "final_fen": self.board.fen(en_passant="fen"),
                  "uci_moves": [move.uci() for move in self.board.move_stack],
                  "state": state, "draw_policy": DRAW_POLICY,
                  "handicap_events": self._handicap_events,
                  "engine_movetimes_ms": self._engine_movetimes,
                  "engine_pacing": {"policy": engine_pacing.POLICY, "enabled": state["clock"]["enabled"],
                                    "recommendation_method": decision_support.METHOD,
                                    "decisions": [{"ply": ply, **copy.deepcopy(row)}
                                                  for ply, row in sorted(self._engine_pacing_decisions.items())]},
                  "time_settings_events": self._time_settings_events,
                  "clock": self._clock.metadata(),
                  "opening_variety": self._opening_metadata(),
                  "dynamic_play": self._dynamic_metadata(),
                  "boss_play": self._boss_metadata(),
                  "sparring_play": self._sparring_metadata(),
                  "sparring_referee": self._referee_record(state["engine"]["sparring_referee"]),
                  "opponent_rating": self._rating_metadata(),
                  "sparring_rating": self._rating_metadata() if self.sparring_mode else None,
                  "purpose": "Human practice; undo and user-forced handicaps permitted, not a strength measurement"}
        # state() above already adjudicated. Keep the PGN and JSON on that
        # same result if a clock reaches zero while serializing the files.
        for extension, contents in (("pgn", self.pgn(adjudicate=False)),
                                    ("json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")):
            target = self.save_dir / f"{self.game_id}.{extension}"
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(contents, encoding="utf-8", newline="\n")
            temporary.replace(target)

    def close(self):
        with self._evaluation_lock:
            self._closing = True
        self._pacing_event.set()
        if self._live_evaluation is not None:
            self._live_evaluation.close()
        if getattr(self, "_gauge", None) is not None:
            self._gauge.close()
        if self._referee is not None:
            try:
                self._referee.close()
            except Exception:
                self._referee_error = "별도 심판을 정리하는 중 오류가 발생했습니다."
        if self.coaching is not None:
            self.coaching.close()
        if self.coaching_reference is not None:
            self.coaching_reference.close()
        if self.analysis is not None:
            self.analysis.close()
        if self._hints is not None:
            self._hints.close()
        with self._search_lock, self._lock:
            try:
                if getattr(self, "_game_open", False):
                    if self._handicap_backup is None:
                        self._adjudicate_clock()
                    self._clock.finish()
                    self._game_open = False
                    self._sync_evaluation(status="unavailable")
                    # A handicap preview must leave the original durable files
                    # byte-for-byte intact. A later session labels its still-
                    # open record unfinished when it is no longer the live ID.
                    if self._handicap_backup is None:
                        self._save()
            finally:
                if self._engine is not None:
                    try:
                        self._engine.quit()
                    except Exception:
                        self._engine.close()
                    self._engine = None
                if self._model_snapshot is not None:
                    self._model_snapshot.cleanup()
                    self._model_snapshot = None


class HumanServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, game, port=0, *, explorer=None):
        super().__init__(("127.0.0.1", port), HumanHandler)
        self.game = game
        self.archive = GameArchive(game.save_dir)
        self.explorer = explorer if explorer is not None else OpeningExplorer()
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"

    def server_close(self):
        try:
            self.explorer.close()
        finally:
            super().server_close()


class HumanHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Local request bodies and tokens do not belong in logs.

    def _send(self, code, content, mime="application/json; charset=utf-8"):
        data = json.dumps(content, ensure_ascii=False).encode("utf-8") if isinstance(content, dict) else content
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if mime.startswith("application/x-chess-pgn"):
            self.send_header("Content-Disposition", 'attachment; filename="Regicide-game.pgn"')
        self.end_headers()
        self.wfile.write(data)

    def _forbidden(self, message):
        # Closing a Windows TCP socket with an unread POST body can reset the
        # connection before the client receives its 403. Discard only a small
        # declared body, with a bounded wait; rejected data is never processed.
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if 0 < length <= 4096:
                timeout = self.connection.gettimeout()
                try:
                    deadline = time.monotonic() + 0.1
                    remaining = length
                    while remaining > 0:
                        wait = deadline - time.monotonic()
                        if wait <= 0:
                            break
                        self.connection.settimeout(wait)
                        chunk = self.rfile.read1(remaining)
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except (TimeoutError, OSError):
                    pass
                finally:
                    self.connection.settimeout(timeout)
        self.close_connection = True
        self._send(403, {"error": message})

    def _allowed(self, api=False):
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            self._forbidden("허용되지 않은 주소입니다.")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self._forbidden("같은 대국 화면에서만 요청할 수 있습니다.")
            return False
        if api and not secrets.compare_digest(self.headers.get("X-Regicide-Token", ""), self.server.token):
            self._forbidden("대국 화면을 새로 열어 주세요.")
            return False
        return True

    def do_GET(self):
        if not self._allowed(self.path.startswith("/api/")):
            return
        if self.path == "/api/state":
            self._send(200, self.server.game.state())
        elif self.path == "/api/evaluation":
            self._send(200, self.server.game.evaluation_with_reference())
        elif self.path == "/api/hint":
            self._send(200, self.server.game.hint())
        elif self.path == "/api/analysis":
            self._send(200, self.server.game.analysis.snapshot())
        elif self.path == "/api/opening-explorer":
            self._send(200, self.server.explorer.snapshot())
        elif self.path == "/api/coaching":
            self._send(200, self.server.game.coaching.snapshot())
        elif self.path == "/api/coaching-reference":
            self._send(200, self.server.game.coaching_reference.snapshot())
        elif self.path == "/api/pgn":
            self._send(200, self.server.game.pgn(), "application/x-chess-pgn; charset=utf-8")
        elif self.path == "/api/games" or self.path.startswith("/api/games/"):
            self._archive_get()
        elif self.path in ("/audio/move.wav", "/audio/capture.wav"):
            self._send(200, (ASSETS / self.path[1:]).read_bytes(), "audio/wav")
        elif self.path in ("/", "/index.html", "/style.css", "/app.js", "/analysis.js", "/coaching.js", "/board_motion.js", "/move_sound.js", "/explorer.js", "/sidebar_tabs.js"):
            filename = "index.html" if self.path == "/" else self.path[1:]
            mime = {"index.html": "text/html", "style.css": "text/css", "app.js": "text/javascript",
                    "analysis.js": "text/javascript", "coaching.js": "text/javascript", "board_motion.js": "text/javascript",
                    "move_sound.js": "text/javascript", "explorer.js": "text/javascript",
                    "sidebar_tabs.js": "text/javascript"}[filename]
            contents = (ASSETS / filename).read_text(encoding="utf-8")
            if filename == "index.html":
                contents = contents.replace("__REGICIDE_TOKEN__", self.server.token)
            self._send(200, contents, mime + "; charset=utf-8")
        else:
            self._send(404, {"error": "페이지를 찾을 수 없습니다."})

    def _archive_get(self):
        archive = self.server.archive
        game = self.server.game
        current_id = game.game_id if game._game_open else None
        try:
            if self.path == "/api/games":
                data = archive.list_games(current_game_id=current_id)
                data["storage_path"] = str(game.save_dir)
                self._send(200, data)
                return
            parts = self.path.removeprefix("/api/games/").split("/")
            if len(parts) == 1:
                self._send(200, archive.get_game(parts[0], current_game_id=current_id))
            elif len(parts) == 2 and parts[1] == "pgn":
                self._send(200, archive.get_pgn(parts[0]), "application/x-chess-pgn; charset=utf-8")
            else:
                self._send(404, {"error": "기보를 찾을 수 없습니다."})
        except FileNotFoundError:
            self._send(404, {"error": "기보를 찾을 수 없습니다."})
        except ValueError:
            self._send(400, {"error": "기보가 올바르지 않아 읽을 수 없습니다. 원본은 보존했습니다."})
        except OSError:
            self._send(500, {"error": "기보 보관함을 읽지 못했습니다. 저장 폴더를 확인해 주세요."})

    def do_POST(self):
        if not self._allowed(True):
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._send(415, {"error": "JSON 요청이 필요합니다."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            maximum = MAX_PGN_REQUEST_BYTES if self.path in ("/api/pgn-preview", "/api/coaching", "/api/coaching-reference") else 4096
            if not 0 < length <= maximum:
                raise ValueError("요청 크기가 올바르지 않습니다.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("올바른 요청이 필요합니다.")
            if self.path == "/api/pgn-preview":
                if set(data) != {"pgn"}:
                    raise ValueError("PGN 본문만 전송해 주세요.")
                self._send(200, preview_pgn(data["pgn"]))
                return
            revision = data.get("revision")
            game = self.server.game
            if self.path == "/api/coaching-reference":
                self._send(200, game.coaching_reference.select(data))
                return
            if self.path == "/api/opening-explorer":
                action = data.get("action")
                if action == "query":
                    if set(data) - {"action", "fen", "source", "ratings", "speeds"}:
                        raise ValueError("오프닝 조회에는 위치와 자료 필터만 전송해 주세요.")
                    result = self.server.explorer.query(data.get("fen"), data.get("source", "masters"),
                                                        data.get("ratings"), data.get("speeds"))
                elif action == "token":
                    if set(data) - {"action", "token", "remember"}:
                        raise ValueError("리체스 연결에는 토큰과 저장 여부만 전송해 주세요.")
                    result = self.server.explorer.set_token(data.get("token"), data.get("remember", True))
                else:
                    raise ValueError("오프닝 작업은 조회·리체스 연결 중에서 선택해 주세요.")
                self._send(200, result)
                return
            if self.path == "/api/coaching":
                coaching = game.coaching
                action = data.get("action")
                if action == "start":
                    result = coaching.start(data.get("pgn"), data.get("human_color"),
                                            data.get("movetime_ms", 1500), revision)
                elif action == "cancel":
                    result = coaching.cancel(revision)
                elif action == "retry":
                    result = coaching.retry(data.get("ply"), data.get("uci"), revision, data.get("job_id"))
                else:
                    raise ValueError("코칭 작업은 분석 시작·중단·다시 두기 중에서 선택해 주세요.")
                self._send(200, result)
                return
            if self.path == "/api/analysis":
                analysis = game.analysis
                actions = {"start": lambda: analysis.start(data.get("fen"), data.get("movetime_ms"), revision),
                           "move": lambda: analysis.move(data.get("move"), revision),
                           "undo": lambda: analysis.undo(revision),
                           "stop": lambda: analysis.stop(revision)}
                action = data.get("action")
                if not isinstance(action, str) or action not in actions:
                    raise ValueError("분석 작업을 확인해 주세요. 분석 시작·착수·무르기·종료를 사용할 수 있습니다.")
                self._send(200, actions[action]())
                return
            if self.path == "/api/hint":
                try:
                    self._send(200, game.set_hint_enabled(data.get("enabled")))
                except ValueError as exc:
                    self._send(400, {"error": str(exc), "hint": game.hint()})
                return
            actions = {"/api/new": lambda: game.new_game(data.get("color"), data.get("movetime_ms"), revision,
                                                        data.get("boss_mode", False), data.get("sparring_mode", False),
                                                        data.get("time_control")),
                       "/api/think-time": lambda: game.set_think_time(data.get("movetime_ms"),
                           data.get("game_id"), data.get("settings_revision")),
                       "/api/clock-adjust": lambda: game.adjust_clock(data.get("side"), data.get("delta_ms"),
                           data.get("game_id"), data.get("clock_revision")),
                       "/api/move": lambda: game.human_move(data.get("move"), revision),
                       "/api/engine": lambda: game.engine_move(revision),
                       "/api/engine/skip-wait": lambda: game.skip_engine_wait(revision),
                       "/api/handicap": lambda: game.begin_handicap(data.get("ply"), revision),
                       "/api/forced-move": lambda: game.forced_move(data.get("move"), revision),
                       "/api/handicap-cancel": lambda: game.cancel_handicap(revision),
                       "/api/undo": lambda: game.undo(revision),
                       "/api/resign": lambda: game.resign(revision)}
            actions["/api/rating-label"] = lambda: game.label_opponent_rating(
                data.get("rating"), data.get("game_id"), revision)
            actions["/api/sparring/rating-label"] = actions["/api/rating-label"]
            if self.path == "/api/shutdown":
                with game._lock:
                    game._check_revision(revision)
                    game._require_idle()
                self._send(200, {"closed": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif self.path in actions:
                self._send(200, actions[self.path]())
            else:
                self._send(404, {"error": "지원하지 않는 요청입니다."})
        except CoachingRevisionError as exc:
            self._send(409, {"error": str(exc), "coaching": self.server.game.coaching.snapshot()})
        except AnalysisRevisionError as exc:
            self._send(409, {"error": str(exc), "analysis": self.server.game.analysis.snapshot()})
        except RevisionError as exc:
            self._send(409, {"error": str(exc), "state": self.server.game.state()})
        except (ValueError, UnicodeError) as exc:
            error = {"error": str(exc)}
            if self.path == "/api/coaching":
                error["coaching"] = self.server.game.coaching.snapshot()
            elif self.path == "/api/coaching-reference":
                error["reference"] = self.server.game.coaching_reference.snapshot()
            elif self.path == "/api/analysis":
                error["analysis"] = self.server.game.analysis.snapshot()
            elif self.path == "/api/opening-explorer":
                error["error"] = "입력한 위치·자료 필터 또는 리체스 연결 설정을 확인해 주세요."
                error["explorer"] = self.server.explorer.snapshot()
            elif self.path != "/api/pgn-preview":
                error["state"] = self.server.game.state()
            self._send(400, error)
        except Exception as exc:
            # Explorer configuration can contain an access token. Never log
            # arbitrary exception strings from that request or its transport.
            if self.path != "/api/opening-explorer":
                print(f"request failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            error = {"error": "대국 요청을 처리하지 못했습니다. 저장 위치와 실행 기록을 확인해 주세요."}
            if self.path == "/api/coaching":
                error["coaching"] = self.server.game.coaching.snapshot()
            elif self.path == "/api/coaching-reference":
                error["reference"] = self.server.game.coaching_reference.snapshot()
            elif self.path == "/api/analysis":
                error["analysis"] = self.server.game.analysis.snapshot()
            elif self.path == "/api/opening-explorer":
                error = {"error": "오프닝 통계를 처리하지 못했습니다. 연결 설정을 확인해 주세요.",
                         "explorer": self.server.explorer.snapshot()}
            elif self.path != "/api/pgn-preview":
                error["state"] = self.server.game.state()
            self._send(500, error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=None)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--save-dir", type=Path, help="Override the persistent personal game library")
    parser.add_argument("--neural-model", type=Path, help="Optional experimental learned evaluation model")
    parser.add_argument("--learning-state", type=Path, help="Adopted-model registry; default build/learning-cycle")
    parser.add_argument("--classical", action="store_true", help="Explicitly use the classical evaluation")
    parser.add_argument("--import-games", type=Path, action="append", default=[],
                        help="Import an old local games directory without changing its originals")
    parser.add_argument("--startup-file", type=Path)
    parser.add_argument("--desktop-client", action="store_true",
                        help="Include the local session token in the private startup file, never stdout")
    parser.add_argument("--no-browser", action="store_true")
    referee_options = parser.add_mutually_exclusive_group()
    referee_options.add_argument("--referee-engine", type=Path, help="Local Stockfish executable for assisted sparring")
    referee_options.add_argument("--no-referee", action="store_true", help="Use standalone Regicide sparring without an external referee")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be in 0..65535")
    if args.desktop_client and not args.startup_file:
        parser.error("--desktop-client requires --startup-file")
    if args.binary is None:
        # Resolve only an implicit launch: an explicit binary remains usable
        # even when the optional next-launch manifest needs repair.
        from runtime_engine import resolve_runtime_engine
        args.binary = resolve_runtime_engine(ROOT)
    save_dir = args.save_dir if args.save_dir is not None else default_game_directory()
    imports = list(args.import_games)
    if args.save_dir is None:
        imports.append(ROOT / "build/human-games")
    GameArchive(save_dir).import_legacy(imports)
    selected = runtime_evaluation(args.binary, args.neural_model, args.learning_state, args.classical)
    if selected["warning"]:
        print(selected["warning"], file=sys.stderr, flush=True)
    from referee_engine import resolve_referee_engine
    referee_info = ({"available": False, "reason": "Stockfish 심판을 끄고 자체 스파링으로 실행했습니다."}
                    if args.no_referee else resolve_referee_engine(ROOT, args.referee_engine))
    game = HumanGame(args.binary, save_dir, neural_model=selected["model"],
                     neural_adopted_generation=selected["generation"], dynamic_play_enabled=True,
                     referee_binary=referee_info.get("binary") if referee_info["available"] else None,
                     referee_info=referee_info, initial_time_control="10+0")
    try:
        with HumanServer(game, args.port) as server:
            startup = {"url": server.origin, "pid": os.getpid(),
                       "engine_name": game.engine_name, "binary_sha256": game.binary_sha256}
            if args.startup_file:
                args.startup_file.parent.mkdir(parents=True, exist_ok=True)
                bootstrap = dict(startup)
                if args.desktop_client:
                    bootstrap["token"] = server.token
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                                     dir=args.startup_file.parent,
                                                     prefix=".startup-", suffix=".tmp", delete=False) as stream:
                        temporary = Path(stream.name)
                        json.dump(bootstrap, stream)
                    temporary.replace(args.startup_file)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            print(json.dumps(startup), flush=True)
            if not args.no_browser:
                webbrowser.open(server.origin)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        game.close()


if __name__ == "__main__":
    main()
