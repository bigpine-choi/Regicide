"""Bounded fixed-node queries to Regicide's own classical UCI evaluator.

Every query starts a fresh UCI game, so its transposition table does not depend
on earlier labels.  The complete python-chess move stack is passed to UCI.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from typing import Any

import chess
import chess.engine


CLASSICAL_OPTIONS = {
    "Hash": 16,
    "Move Overhead": 0,
    "UseNeuralEvaluation": False,
    "UsePositionalEvaluation": True,
    "UseQuiescence": True,
    "UseTranspositionTable": True,
    "UseFastMoves": True,
    "UseQuietMoveOrdering": True,
    "UseQuiescenceChecks": True,
    "PVS": True,
    "UseSEE": True,
}

# These options did not exist in the original research engine. When an integrated
# runtime advertises them, disable them explicitly to preserve the old teacher
# profile. Keep CLASSICAL_OPTIONS unchanged so historical manifests remain honest.
LEGACY_EXTENSION_OPTIONS = {
    "UseNNUEEvaluation": False,
    "UsePawnShield": False,
    "UsePawnStructure": False,
    "UseLMR": False,
}


def legacy_options(advertised):
    return {name: value for name, value in LEGACY_EXTENSION_OPTIONS.items()
            if name in advertised}


def normalize_root_moves(board: chess.Board, root_moves: list[chess.Move] | None) -> list[chess.Move] | None:
    """Snapshot a legal root set in deterministic order, or retain free search."""
    if root_moves is None:
        return None
    if not isinstance(root_moves, list) or not root_moves:
        raise ValueError("root_moves must be a nonempty list of legal chess.Move objects")
    try:
        invalid = any(not isinstance(move, chess.Move) or move not in board.legal_moves for move in root_moves)
    except (IndexError, TypeError, ValueError) as error:
        raise ValueError("root_moves contains an invalid or illegal move") from error
    if invalid:
        raise ValueError("root_moves contains an invalid or illegal move")
    # UCI spelling is stable for promotions/castling too. New Move instances
    # isolate the request from later caller mutation of either list or moves.
    return [chess.Move.from_uci(uci) for uci in sorted({move.uci() for move in root_moves})]


class SearchInterrupted(RuntimeError):
    """A query was interrupted and must never become a training label."""

    def __init__(self, reason: str, record: dict[str, Any]):
        self.reason = reason
        self.record = record
        super().__init__(reason)


class _RawLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("x", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def append(self, direction: str, line: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._seq += 1
            self._stream.write(json.dumps({
                "seq": self._seq,
                "utc": datetime.now(timezone.utc).isoformat(),
                "monotonic": time.monotonic(),
                "direction": direction,
                "line": line,
            }, sort_keys=True) + "\n")
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._stream.close()
                self._closed = True


def _logging_protocol(log: _RawLog) -> type[chess.engine.UciProtocol]:
    class LoggingProtocol(chess.engine.UciProtocol):
        def send_line(self, line: str) -> None:
            log.append("send", line)
            super().send_line(line)

        def _line_received(self, line: str) -> None:
            log.append("recv", line)
            super()._line_received(line)

        def error_line_received(self, line: str) -> None:
            log.append("stderr", line)
            super().error_line_received(line)

    return LoggingProtocol


class EngineTeacher:
    """Context manager for one engine process with a strict wall-clock guard.

    python-chess 1.11.2 does not apply SimpleEngine.timeout to a nodes-only
    SimpleEngine.play call.  We therefore wrap the same protocol.play coroutine
    in asyncio.wait_for and bound its cross-thread Future as a second guard.
    Cancellation sends UCI stop; closing the transport terminates an engine
    that ignores it.  No delayed timer can interrupt a later query.
    """

    def __init__(self, engine_path: Path, raw_path: Path, nodes: int,
                 deadline: float, search_timeout: float = 15):
        if type(nodes) is not int or nodes < 1:
            raise ValueError("nodes must be a positive integer")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise ValueError("deadline must be finite monotonic time")
        if isinstance(search_timeout, bool) or not isinstance(search_timeout, (int, float)) or not math.isfinite(search_timeout) or search_timeout <= 0:
            raise ValueError("search_timeout must be finite and positive")
        self.engine_path = Path(engine_path).resolve()
        self.raw_path = Path(raw_path)
        self.nodes = nodes
        self.deadline = float(deadline)
        self.search_timeout = float(search_timeout)
        self.options: dict[str, Any] = {}
        self.identity: dict[str, str] = {}
        self.closed = True
        self.cleanup_status: dict[str, Any] = {
            "quit_requested": False, "transport_closed": False,
            "returncode": None, "returncode_observed": False,
        }
        self._engine: chess.engine.SimpleEngine | None = None
        self._log: _RawLog | None = None
        self._entered = False
        self._abort = False

    def __enter__(self) -> EngineTeacher:
        if self._entered:
            raise RuntimeError("EngineTeacher may only be entered once")
        self._entered = True
        if not self.engine_path.is_file():
            raise ValueError(f"engine executable does not exist: {self.engine_path}")
        if time.monotonic() >= self.deadline:
            raise SearchInterrupted("wall_budget", self._empty_record(None))
        self._log = _RawLog(self.raw_path)
        self.closed = False
        try:
            boot_timeout = min(15.0, self.deadline - time.monotonic())
            if boot_timeout <= 0:
                raise SearchInterrupted("wall_budget", self._empty_record(None))
            self._engine = chess.engine.SimpleEngine.popen(
                _logging_protocol(self._log), [str(self.engine_path), "uci"],
                timeout=boot_timeout,
            )
            missing = set(CLASSICAL_OPTIONS) - set(self._engine.options)
            if missing:
                raise ValueError("engine lacks required Regicide options: " + ", ".join(sorted(missing)))
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise SearchInterrupted("wall_budget", self._empty_record(None))
            self._engine.timeout = min(15.0, remaining)
            self.options = {**legacy_options(self._engine.options), **CLASSICAL_OPTIONS}
            self._engine.configure(self.options)
            self.identity = dict(self._engine.id)
            if time.monotonic() >= self.deadline:
                raise SearchInterrupted("wall_budget", self._empty_record(None))
            return self
        except BaseException:
            self._abort = True
            self.close()
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is not None:
            self._abort = True
        self.close()

    def _empty_record(self, query_id: Any) -> dict[str, Any]:
        seq = self._log.seq if self._log is not None else 0
        return {
            "query_id": query_id, "requested_nodes": self.nodes,
            "nodes": None, "depth": None, "seldepth": None,
            "engine_time_ms": None, "wall_seconds": 0.0,
            "bestmove_uci": None, "pv_uci": [],
            "score": {"kind": "missing", "stm": None},
            "lowerbound": False, "upperbound": False,
            "completed": False, "uci_begin_seq": seq + 1,
            "uci_end_seq": seq,
        }

    def query(self, board: chess.Board, query_id: Any, *, root_moves: list[chess.Move] | None = None) -> dict[str, Any]:
        if self.closed or self._engine is None or self._log is None:
            raise RuntimeError("EngineTeacher must be entered before querying")
        # Snapshot both the full move history and the optional root restriction.
        snapshot = board.copy(stack=True)
        roots = normalize_root_moves(snapshot, root_moves)
        record = self._empty_record(query_id)
        if roots is not None:
            record["root_moves_uci"] = [move.uci() for move in roots]
        started = time.monotonic()
        remaining = self.deadline - started
        if remaining <= 0:
            self._abort = True
            self.close()
            raise SearchInterrupted("wall_budget", record)
        duration = min(self.search_timeout, remaining)
        reason = "wall_budget" if remaining <= self.search_timeout else "search_timeout"
        root_options = {} if roots is None else {"root_moves": roots}
        coroutine = asyncio.wait_for(
            self._engine.protocol.play(
                snapshot, chess.engine.Limit(nodes=self.nodes), game=object(),
                info=chess.engine.INFO_ALL, **root_options,
            ), duration,
        )
        future = asyncio.run_coroutine_threadsafe(coroutine, self._engine.protocol.loop)
        try:
            result = future.result(timeout=duration + 0.05)
        except (TimeoutError, concurrent.futures.TimeoutError):
            future.cancel()
            record["wall_seconds"] = time.monotonic() - started
            record["uci_end_seq"] = self._log.seq
            self._abort = True
            self.close()
            raise SearchInterrupted(reason, record) from None
        except BaseException:
            future.cancel()
            self._abort = True
            self.close()
            raise
        elapsed = time.monotonic() - started
        record.update({
            "wall_seconds": elapsed,
            "uci_end_seq": self._log.seq,
            "nodes": result.info.get("nodes"),
            "depth": result.info.get("depth"),
            "seldepth": result.info.get("seldepth"),
            "engine_time_ms": None if "time" not in result.info else result.info["time"] * 1000.0,
            "bestmove_uci": result.move.uci() if result.move is not None else None,
            "pv_uci": [move.uci() for move in result.info.get("pv", [])],
            "lowerbound": bool(result.info.get("lowerbound", False)),
            "upperbound": bool(result.info.get("upperbound", False)),
        })
        score = result.info.get("score")
        if score is not None:
            stm_score = score.pov(snapshot.turn)
            if stm_score.is_mate():
                record["score"] = {"kind": "mate", "stm": stm_score.mate()}
            elif stm_score.score() is not None:
                record["score"] = {"kind": "cp", "stm": stm_score.score()}
        # A response racing the guard remains an interrupted query.
        if elapsed >= duration or time.monotonic() >= self.deadline:
            self._abort = True
            self.close()
            raise SearchInterrupted(reason, record)
        record["completed"] = True
        return record

    def close(self) -> None:
        if self.closed:
            return
        engine = self._engine
        if engine is not None:
            if not self._abort and time.monotonic() < self.deadline:
                engine.timeout = min(0.25, max(0.001, self.deadline - time.monotonic()))
                self.cleanup_status["quit_requested"] = True
                try:
                    engine.quit()
                except (TimeoutError, chess.engine.EngineError):
                    pass
            engine.close()
            self.cleanup_status["transport_closed"] = True
            try:
                self.cleanup_status["returncode"] = engine.returncode.result(timeout=0.5)
                self.cleanup_status["returncode_observed"] = True
            except (TimeoutError, concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
                pass
        if self._log is not None:
            self._log.close()
        self.closed = True
