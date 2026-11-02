"""Local practice host. Regicide chooses moves except explicit user handicaps.

python-chess supplies UCI transport, legality, adjudication and PGN formatting.
Bind only to loopback; JSON mutations require a per-process browser token.
"""

import argparse
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
from opening_presentation import OpeningGuideCache
import opening_variety
import dynamic_play
import boss_play
import sparring_play
import sparring_rating
import evaluation_stability
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
                 opening_variety_enabled=True, dynamic_play_enabled=False):
        if type(opening_variety_enabled) is not bool:
            raise ValueError("Opening variety must be enabled or disabled explicitly.")
        self.opening_variety_enabled = opening_variety_enabled
        if type(dynamic_play_enabled) is not bool:
            raise ValueError("Dynamic play must be enabled or disabled explicitly.")
        self.dynamic_play_enabled = dynamic_play_enabled
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
        self._active_movetime_ms = None
        self._active_extra_budget_ms = None
        self._engine = None
        self._hints = None
        self._live_evaluation = None
        self.analysis = None
        self.coaching = None
        self.revision = 0
        try:
            self._ensure_engine()
            self._hints = MoveHints(self.binary, hint_engine_factory)
            self._live_evaluation = MoveHints(self.binary, evaluation_engine_factory,
                                              stream_evaluation=True)
            self.analysis = AnalysisSession(self.binary, self.engine_name, hint_engine_factory)
            self.coaching = CoachingSession(self.binary, self.engine_name, self.save_dir,
                                            hint_engine_factory,
                                            evaluation_id=self.neural_sha256 or self.classical_evaluation_mode)
            self._reset("white", 1000)
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

    def _reset(self, color, movetime_ms, boss_mode=False, sparring_mode=False):
        if getattr(self, "_game_open", False):
            self._game_open = False
            self._save()
        self.board = chess.Board()
        self.human_color = chess.WHITE if color == "white" else chess.BLACK
        self.movetime_ms = movetime_ms
        self._settings_revision = 0
        self._time_settings_events = []
        self._engine_movetimes = {}
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
        self.revision += 1
        self._sync_evaluation()
        self._save()

    def _check_revision(self, revision):
        if self._closing or not self._game_open:
            raise ValueError("종료된 대국입니다. 대국 프로그램을 다시 실행해 주세요.")
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

    def _outcome(self):
        if self._override:
            return self._override
        outcome = self.board.outcome(claim_draw=True)
        return (outcome.result(), outcome.termination.name.lower()) if outcome else ("*", None)

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
        evaluation_budget_ms = (max(LIVE_EVALUATION_MIN_MS, self.movetime_ms) if evaluating else
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
            # Stream shallow results immediately, then refine with a budget of
            # at least 3s; this never extends the opponent's move limit.
            self._live_evaluation.update_position(
                game_id=self.game_id, revision=self.revision, board=self.board,
                human_color=self.human_color, movetime_ms=evaluation_budget_ms,
                engine_name=self.engine_name, active=evaluating)
            if not self._closing:
                self._live_evaluation.set_enabled(evaluating)
        self._hints.update_position(
            game_id=self.game_id, revision=self.revision, board=self.board,
            human_color=self.human_color, movetime_ms=self.movetime_ms,
            engine_name=self.engine_name, active=active,
            handicap_mode=self._handicap_backup is not None)

    def _publish_evaluation(self, info):
        """Publish completed-depth opponent-search scores in White's point of view."""
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
            self._evaluation_packets[depth] = dict(info)
            self._evaluation_packets = dict(sorted(self._evaluation_packets.items())[-6:])
            self._evaluation.update(cp=cp, mate=mate, depth=depth,
                                    bound="lower" if lower else "upper" if upper else "exact",
                                    white_share=share, winner=winner, status="searching",
                                    source="search", source_fen=self.board.fen(en_passant="fen"))
            self._evaluation.update(evaluation_stability.assess(self.board, self._evaluation_packets))

    def state(self):
        with self._lock:
            result, termination = self._outcome()
            handicap_mode = self._handicap_backup is not None
            active = self._game_open and result == "*" and not self.error
            human_turn = active and not handicap_mode and self.board.turn == self.human_color
            engine_turn = active and not handicap_mode and self.board.turn != self.human_color
            names = {"checkmate": "체크메이트", "stalemate": "스테일메이트",
                     "insufficient_material": "메이트 불가능한 기물 구성",
                     "threefold_repetition": "3회 반복 청구", "fivefold_repetition": "5회 반복",
                     "fifty_moves": "50수 청구", "seventyfive_moves": "75수 규칙",
                     "resignation": "기권"}
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
            return {"revision": self.revision, "game_id": self.game_id,
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
                               "opening_variety_enabled": self.opening_variety_enabled and not self.dynamic_play_enabled and not self.boss_mode and not self.sparring_mode,
                               "dynamic_play_enabled": self.dynamic_play_enabled and not self.boss_mode and not self.sparring_mode,
                               "boss_mode": self.boss_mode,
                               "sparring_mode": self.sparring_mode,
                               "sparring": (sparring_play.derive_profile(self.board, list(self._sparring_decisions.values()))
                                            if self.sparring_mode else None),
                               "sparring_rating": self._rating_state() if self.sparring_mode else None,
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
                                                    if boss_play.phase(self.board, self._boss_decisions, bool(self._forced_plies), not self.human_color, self.movetime_ms) in ("opening", "trap_hunt") else 0)
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

    def new_game(self, color, movetime_ms, revision, boss_mode=False, sparring_mode=False):
        with self._lock:
            self._check_revision(revision)
            self._require_idle()
            self._require_no_handicap()
            if color not in ("white", "black") or type(movetime_ms) is not int or not 100 <= movetime_ms <= 5000:
                raise ValueError("백 또는 흑, 100~5000ms의 생각 시간을 선택해 주세요.")
            if type(boss_mode) is not bool:
                raise ValueError("보스 모드 여부는 true 또는 false로 지정해 주세요.")
            if type(sparring_mode) is not bool:
                raise ValueError("스파링 모드 여부는 true 또는 false로 지정해 주세요.")
            if boss_mode and sparring_mode:
                raise ValueError("보스 모드와 스파링 모드 중 하나를 선택해 주세요.")
            self._ensure_engine()
            self._reset(color, movetime_ms, boss_mode, sparring_mode)
            return self.state()

    def _rating_metadata(self):
        return sparring_rating.metadata(self.board, self._sparring_decisions,
                                        self.human_color, self._rating_calibration)

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

    def label_sparring_rating(self, rating, game_id, revision):
        """Save private supervision only after the current game has ended."""
        with self._lock:
            self._check_revision(revision)
            self._require_idle()
            self._require_no_handicap()
            if not isinstance(game_id, str) or game_id != self.game_id:
                raise RevisionError("다른 대국의 레이팅 요청입니다. 현재 대국에서 다시 입력해 주세요.")
            if not self.sparring_mode or self._record_status() != "completed":
                raise ValueError("완료된 스파링 대국에서 실제 래피드 레이팅을 입력해 주세요.")
            result, termination = self._outcome()
            sample = {"completed": True, "fingerprint": sparring_rating.fingerprint(self.board, result, termination),
                      "features": self._rating_metadata(), "result": result, "termination": termination,
                      "handicap": bool(self._handicap_events or self._forced_plies),
                      "runtime": {"source": self.source, "binary_sha256": self.binary_sha256,
                          "evaluation": self.neural_evaluation_mode if self.neural_model else self.classical_evaluation_mode,
                          "model_sha256": self.neural_sha256, "policy": sparring_play.POLICY,
                          "engine_movetimes_ms": self._engine_movetimes,
                          "time_settings_events": self._time_settings_events}}
            # The game's calibration remains frozen: its own label cannot
            # retrospectively improve its estimate. Subsequent games reload it.
            self._rating_label = self._rating_store.upsert(self.game_id, rating, sample)
            self.revision += 1
            self._sync_evaluation()
            self._save()
            return self.state()

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
                    self.revision += 1
                    self._sync_evaluation()
                    self._save()
                    return self.state()
                board = self.board.copy(stack=True)
                movetime_ms = self.movetime_ms
                forced_game = bool(self._forced_plies)
                boss_decisions = copy.deepcopy(list(self._boss_decisions.values()))
                sparring_decisions = copy.deepcopy(list(self._sparring_decisions.values()))
                move_policy = (None if self.sparring_mode else boss_play if self.boss_mode else dynamic_play if self.dynamic_play_enabled else
                               opening_variety if self.opening_variety_enabled else None)
                search_plan = (move_policy.plan(board, movetime_ms, self._opening_seed,
                                               forced=bool(self._forced_plies),
                                               **({"decisions": self._boss_decisions} if self.boss_mode else {})) if move_policy else None)
                policy_memo = (self._boss_memo if self.boss_mode else
                               self._dynamic_memo if self.dynamic_play_enabled else self._opening_memo)
                memo_key = (move_policy.memo_key(board, movetime_ms, self.binary_sha256,
                                                     self.neural_sha256 or self.classical_evaluation_mode,
                                                     seed=self._opening_seed,
                                                     catalog_id=([search_plan.get("catalog_commit"),
                                                                  boss_play.boss_opponent.derive_profile(board, boss_decisions)]
                                                                 if self.boss_mode else search_plan.get("catalog_commit")))
                            if search_plan and search_plan["eligible"] else None)
                remembered = copy.deepcopy(policy_memo.get(memo_key)) if memo_key else None
                token, game_id = self._game_token, self.game_id
                self._engine_searching = True
                self._active_movetime_ms = movetime_ms
                self._active_extra_budget_ms = (self._sparring_extra_budget(movetime_ms, board)
                                                if self.sparring_mode else search_plan["extra_budget_ms"] if search_plan else 0)
                self._sync_evaluation(status="searching")
            reply, failure = None, None
            decision = None
            discard_engine = False
            watchdog = None
            timed_out = threading.Event()
            try:
                engine = self._engine
                if callable(getattr(engine, "analysis", None)):
                    def expire_search():
                        timed_out.set()
                        engine.close()
                    # Baseline retains the existing full-budget watchdog. The
                    # optional verification phase gets its own much tighter one.
                    watchdog = threading.Timer(movetime_ms / 1000 + float(getattr(engine, "timeout", 10) or 10),
                                               expire_search)
                    watchdog.daemon = True
                    watchdog.start()
                def search(budget, *, root_move=None, root_moves=None, publish=True, fresh=False, depth=None, position=None):
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
                    else:
                        result = engine.play(search_board.copy(stack=True), limit, **options)
                        latest = result.info
                        if publish:
                            self._publish_evaluation(latest)
                        if type(latest.get("depth")) is int and "score" in latest:
                            packets[latest["depth"]] = latest
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
                    if self.sparring_mode:
                        decision, discard_engine = sparring_play.choose(
                            board, reply.move, baseline_info, _packets, search, engine.close,
                            decisions=sparring_decisions, seed=self._opening_seed,
                            movetime_ms=movetime_ms, forced=forced_game)
                        decision["baseline_elapsed_ms"] = round(baseline_elapsed_ms, 3)
                        reply = chess.engine.BestMove(chess.Move.from_uci(decision["selected_uci"]), None)
                    if self.boss_mode:
                        boss_extra_started = time.monotonic()
                        total_extra = search_plan["extra_budget_ms"]
                        quality_budget = min(boss_play.QUALITY_BUDGET_MS, total_extra)
                        observation, discard_engine = boss_play.observe_human_move(
                            board, search, engine.close, budget_ms=quality_budget, forced=forced_game)
                        observed_decisions = [*boss_decisions, {"opponent_observation": observation}]
                        frequency = boss_play.boss_opponent.gate(board, observed_decisions, self._opening_seed)
                        frequency["baseline_matches_repertoire"] = reply.move.uci() == search_plan["candidate_uci"]
                        remaining_extra = max(0, total_extra - (time.monotonic() - boss_extra_started) * 1000)
                        search_plan = {**search_plan, "extra_budget_ms": remaining_extra,
                                       "candidate_budget_ms": remaining_extra}
                        conservative_setup = (not frequency["attempt_trap"] and search_plan["eligible"]
                                              and search_plan["mode"] == "opening" and not search_plan["line_complete"])
                        search_plan["conservative_setup"] = conservative_setup
                        if discard_engine or (search_plan["eligible"] and not frequency["attempt_trap"]
                                              and not frequency["baseline_matches_repertoire"] and not conservative_setup):
                            search_plan.update(eligible=False, extra_budget_ms=0, candidate_budget_ms=0,
                                               reason="quality_probe_failed" if discard_engine else "opponent_frequency_hold")
                        compare = bool(search_plan["eligible"])
                    if self.boss_mode and search_plan and search_plan["eligible"] and search_plan.get("mode") == "trap_hunt":
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
                if reply.move is None or reply.move not in board.legal_moves:
                    raise RuntimeError("엔진이 합법 수를 반환하지 않았습니다.")
            except BaseException as exc:
                failure = f"{type(exc).__name__}: {exc}"
            finally:
                if watchdog is not None:
                    watchdog.cancel()
            with self._lock:
                self._engine_searching = False
                self._active_movetime_ms = None
                self._active_extra_budget_ms = None
                if self.game_id != game_id or self._game_token is not token or self.revision != revision:
                    failure = "RuntimeError: 검색 중 대국 상태가 바뀌어 이전 응답을 적용하지 않았습니다."
                if failure is None:
                    self.board.push(reply.move)
                    self._engine_movetimes[len(self.board.move_stack)] = movetime_ms
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
            self._opening_decisions = {ply: row for ply, row in self._opening_decisions.items()
                                       if ply <= len(self.board.move_stack)}
            self._dynamic_decisions = {ply: row for ply, row in self._dynamic_decisions.items()
                                       if ply <= len(self.board.move_stack)}
            self._boss_decisions = {ply: row for ply, row in self._boss_decisions.items()
                                    if ply <= len(self.board.move_stack)}
            self._sparring_decisions = {ply: row for ply, row in self._sparring_decisions.items()
                                        if ply <= len(self.board.move_stack)}
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
            self._handicap_backup = {"board": self.board.copy(stack=True),
                                     "forced_plies": self._forced_plies.copy(),
                                     "engine_movetimes": self._engine_movetimes.copy(),
                                     "opening_decisions": copy.deepcopy(self._opening_decisions),
                                     "dynamic_decisions": copy.deepcopy(self._dynamic_decisions),
                                     "boss_decisions": copy.deepcopy(self._boss_decisions),
                                     "sparring_decisions": copy.deepcopy(self._sparring_decisions),
                                     "override": self._override, "error": self.error,
                                     "outcome": self._outcome(), "ply": ply,
                                     "updated_at": self.updated_at, "ended_at": self.ended_at}
            self.board = preview
            self._forced_plies.intersection_update(range(1, ply))
            self._engine_movetimes = {number: ms for number, ms in self._engine_movetimes.items()
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
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
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
            self._forced_plies = backup["forced_plies"]
            self._engine_movetimes = backup["engine_movetimes"]
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

    def pgn(self):
        with self._lock:
            backup = self._handicap_backup
            board = backup["board"] if backup else self.board
            forced_plies = backup["forced_plies"] if backup else self._forced_plies
            movetimes = backup["engine_movetimes"] if backup else self._engine_movetimes
            error = backup["error"] if backup else self.error
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
            game.headers.update(opening_variety.pgn_headers(self._opening_metadata()))
            game.headers.update(dynamic_play.pgn_headers(self._dynamic_metadata()))
            game.headers.update(boss_play.pgn_headers(self._boss_metadata()))
            game.headers.update(sparring_play.pgn_headers(self._sparring_metadata()))
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
                "risk_profile": boss_play.RISK_PROFILE, "trap_max_plies": boss_play.TRAP_MAX_PLIES,
                "opponent_profile": boss_play.boss_opponent.derive_profile(self.board, list(self._boss_decisions.values())),
                "decisions": [copy.deepcopy(row) for _ply, row in sorted(self._boss_decisions.items())],
                "memo": [{"key": key, "decision": copy.deepcopy(row)}
                         for key, row in sorted(self._boss_memo.items())]}

    def _sparring_metadata(self):
        return {"enabled": self.sparring_mode, "policy": sparring_play.POLICY, "seed": self._opening_seed,
                "profile": sparring_play.derive_profile(self.board, list(self._sparring_decisions.values())),
                "decisions": [copy.deepcopy(row) for _ply, row in sorted(self._sparring_decisions.items())]}

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
        if state["engine"].get("sparring_rating") is not None:
            state["engine"]["sparring_rating"]["label"] = None
        record = {"game_id": self.game_id, "started_at": self.started_at,
                  "updated_at": self.updated_at, "ended_at": self.ended_at,
                  "record_status": self._record_status(), "schema_version": 2,
                  "source": self.source, "binary": str(self.binary),
                  "initial_fen": self.board.root().fen(en_passant="fen"),
                  "final_fen": self.board.fen(en_passant="fen"),
                  "uci_moves": [move.uci() for move in self.board.move_stack],
                  "state": state, "draw_policy": "always claim available threefold/50-move draws",
                  "handicap_events": self._handicap_events,
                  "engine_movetimes_ms": self._engine_movetimes,
                  "time_settings_events": self._time_settings_events,
                  "opening_variety": self._opening_metadata(),
                  "dynamic_play": self._dynamic_metadata(),
                  "boss_play": self._boss_metadata(),
                  "sparring_play": self._sparring_metadata(),
                  "sparring_rating": self._rating_metadata() if self.sparring_mode else None,
                  "purpose": "Human practice; undo and user-forced handicaps permitted, not a strength measurement"}
        for extension, contents in (("pgn", self.pgn()),
                                    ("json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")):
            target = self.save_dir / f"{self.game_id}.{extension}"
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(contents, encoding="utf-8", newline="\n")
            temporary.replace(target)

    def close(self):
        with self._evaluation_lock:
            self._closing = True
        if self._live_evaluation is not None:
            self._live_evaluation.close()
        if self.coaching is not None:
            self.coaching.close()
        if self.analysis is not None:
            self.analysis.close()
        if self._hints is not None:
            self._hints.close()
        with self._search_lock, self._lock:
            try:
                if getattr(self, "_game_open", False):
                    self._game_open = False
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

    def __init__(self, game, port=0):
        super().__init__(("127.0.0.1", port), HumanHandler)
        self.game = game
        self.archive = GameArchive(game.save_dir)
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"


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
            self._send(200, self.server.game.evaluation())
        elif self.path == "/api/hint":
            self._send(200, self.server.game.hint())
        elif self.path == "/api/analysis":
            self._send(200, self.server.game.analysis.snapshot())
        elif self.path == "/api/coaching":
            self._send(200, self.server.game.coaching.snapshot())
        elif self.path == "/api/pgn":
            self._send(200, self.server.game.pgn(), "application/x-chess-pgn; charset=utf-8")
        elif self.path == "/api/games" or self.path.startswith("/api/games/"):
            self._archive_get()
        elif self.path in ("/audio/move.wav", "/audio/capture.wav"):
            self._send(200, (ASSETS / self.path[1:]).read_bytes(), "audio/wav")
        elif self.path in ("/", "/index.html", "/style.css", "/app.js", "/analysis.js", "/coaching.js", "/board_motion.js", "/move_sound.js"):
            filename = "index.html" if self.path == "/" else self.path[1:]
            mime = {"index.html": "text/html", "style.css": "text/css", "app.js": "text/javascript",
                    "analysis.js": "text/javascript", "coaching.js": "text/javascript", "board_motion.js": "text/javascript",
                    "move_sound.js": "text/javascript"}[filename]
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
            maximum = MAX_PGN_REQUEST_BYTES if self.path in ("/api/pgn-preview", "/api/coaching") else 4096
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
                                                        data.get("boss_mode", False), data.get("sparring_mode", False)),
                       "/api/think-time": lambda: game.set_think_time(data.get("movetime_ms"),
                           data.get("game_id"), data.get("settings_revision")),
                       "/api/move": lambda: game.human_move(data.get("move"), revision),
                       "/api/engine": lambda: game.engine_move(revision),
                       "/api/handicap": lambda: game.begin_handicap(data.get("ply"), revision),
                       "/api/forced-move": lambda: game.forced_move(data.get("move"), revision),
                       "/api/handicap-cancel": lambda: game.cancel_handicap(revision),
                       "/api/undo": lambda: game.undo(revision),
                       "/api/resign": lambda: game.resign(revision)}
            actions["/api/sparring/rating-label"] = lambda: game.label_sparring_rating(
                data.get("rating"), data.get("game_id"), revision)
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
            elif self.path == "/api/analysis":
                error["analysis"] = self.server.game.analysis.snapshot()
            elif self.path != "/api/pgn-preview":
                error["state"] = self.server.game.state()
            self._send(400, error)
        except Exception as exc:
            print(f"request failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            error = {"error": "대국 요청을 처리하지 못했습니다. 저장 위치와 실행 기록을 확인해 주세요."}
            if self.path == "/api/coaching":
                error["coaching"] = self.server.game.coaching.snapshot()
            elif self.path == "/api/analysis":
                error["analysis"] = self.server.game.analysis.snapshot()
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
    game = HumanGame(args.binary, save_dir, neural_model=selected["model"],
                     neural_adopted_generation=selected["generation"], dynamic_play_enabled=True)
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
