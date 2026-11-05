"""Shared, auditable native analysis for offline opening research.

An exact UCI CP is a bounded search result, not a theorem about a sacrifice.
Every query uses the full history and a fresh UCI game. Research never changes
the active engine, its options, its opening policy, or the user's games.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

import chess

from neural_search_data.engine import EngineTeacher, SearchInterrupted

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "regicide.gambit-research-query.v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def replay(history_uci):
    if not isinstance(history_uci, (list, tuple)) or len(history_uci) > 256:
        raise ValueError("Expected a bounded complete standard-start UCI history")
    board = chess.Board()
    for uci in history_uci:
        if not isinstance(uci, str):
            raise ValueError("Move history must contain UCI strings")
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves or board.is_game_over(claim_draw=False):
            raise ValueError("Illegal or post-terminal research history: " + uci)
        board.push(move)
    return board


def source_hashes():
    paths = sorted({*ROOT.glob("src/*.cpp"), *ROOT.glob("include/regicide/*.hpp"),
                    *ROOT.glob("scripts/*gambit*.py"),
                    ROOT / "scripts/neural_search_data/engine.py",
                    ROOT / "data/openings/manifest.json"})
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


class _RuntimeTeacher(EngineTeacher):
    """Keep the existing strict timeout/raw log, restore the current app profile."""

    def __enter__(self):
        super().__enter__()
        try:
            if not self.identity.get("name", "").startswith("Regicide"):
                raise ValueError("Opening research requires the project's own Regicide engine")
            defaults = {name: self._engine.options[name].default for name in self.options}
            if "UsePieceActivity" not in self._engine.options:
                raise ValueError("Regicide must advertise UsePieceActivity")
            defaults["UsePieceActivity"] = True
            self._engine.configure(defaults)
            self.options = defaults
            self.runtime_defaults = {name: option.default for name, option in self._engine.options.items()}
            return self
        except BaseException:
            self.close()
            raise


class NativeAnalyzer:
    """Own-engine fixed-node queries with exact-score and history provenance."""

    def __init__(self, binary, output_dir, *, nodes=25000, wall_seconds=300,
                 minimum_depth=4, search_timeout=10):
        if type(nodes) is not int or nodes < 1:
            raise ValueError("nodes must be a positive integer")
        if type(minimum_depth) is not int or minimum_depth < 1:
            raise ValueError("minimum_depth must be positive")
        if (type(wall_seconds) not in (int, float) or not math.isfinite(wall_seconds)
                or wall_seconds <= 0):
            raise ValueError("wall_seconds must be finite and positive")
        self.binary = Path(binary).resolve()
        self.output_dir = Path(output_dir)
        self.nodes = nodes
        self.minimum_depth = minimum_depth
        self.wall_seconds = wall_seconds
        self.search_timeout = search_timeout
        self.records = []
        self.identity = {}
        self.options = {}
        self.teacher = None
        self._entered = False
        self._sources = source_hashes()

    def __enter__(self):
        if self._entered:
            raise RuntimeError("NativeAnalyzer may only be entered once")
        self._entered = True
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.teacher = _RuntimeTeacher(self.binary, self.output_dir / "native-uci.jsonl", self.nodes,
                                       time.monotonic() + self.wall_seconds, self.search_timeout)
        try:
            self.teacher.__enter__()
            self.options = self.teacher.options.copy()
            self.identity = {"engine": self.teacher.identity.copy(),
                             "binary_sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                             "options": self.options, "source_sha256": self._sources,
                             "minimum_depth": self.minimum_depth}
            write_json(self.output_dir / "analysis_manifest.json", {
                "schema": "regicide.gambit-research-analysis.v1",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "identity": self.identity, "default_nodes": self.nodes,
                "wall_seconds": self.wall_seconds, "search_timeout": self.search_timeout,
                "full_history": True, "fresh_uci_game_per_query": True,
                "meaning": "bounded native search evidence; no proof or Elo claim"})
        except BaseException:
            self.teacher.close()
            raise
        return self

    def analyze(self, board, *, nodes=None, root_moves=None):
        if root_moves is not None:
            raise ValueError("Pass the actual child board; restricted-root CP is not synthesized")
        amount = self.nodes if nodes is None else nodes
        if type(amount) is not int or amount < 1:
            raise ValueError("nodes must be a positive integer")
        if self.teacher is None:
            raise RuntimeError("NativeAnalyzer must be entered first")
        if not isinstance(board, chess.Board) or board.chess960 or not board.is_valid():
            raise ValueError("Expected a legal standard chess board")
        snapshot = board.copy(stack=True)
        history = [move.uci() for move in snapshot.move_stack]
        if (snapshot.root().fen(en_passant="fen") != chess.STARTING_FEN
                or replay(history).fen(en_passant="fen") != snapshot.fen(en_passant="fen")):
            raise ValueError("Native opening research requires the complete standard-start history")
        query_id = "q%06d" % (len(self.records) + 1)
        row = {"schema": SCHEMA, "query_id": query_id, "requested_nodes": amount,
               "root_fen": snapshot.root().fen(en_passant="fen"), "history_uci": history,
               "fen": snapshot.fen(en_passant="fen"),
               "turn": "white" if snapshot.turn else "black", "status": "unknown",
               "cp": None, "mate": None, "depth": None, "nodes": None,
               "pv_uci": [], "bestmove_uci": None}
        outcome = snapshot.outcome(claim_draw=False)
        if outcome is not None:
            row.update(status="terminal", cp=0 if outcome.winner is None else None,
                       mate=0 if snapshot.is_checkmate() else None,
                       terminal_result=outcome.result(), reason=outcome.termination.name)
        elif self.teacher.closed:
            row["reason"] = "analyzer_closed_or_budget_exhausted"
        else:
            previous = self.teacher.nodes
            try:
                self.teacher.nodes = amount
                raw = self.teacher.query(snapshot, query_id)
                if not isinstance(raw, dict) or not isinstance(raw.get("score"), dict):
                    raise ValueError("Malformed native query record")
                row.update(native=raw, depth=raw.get("depth"), nodes=raw.get("nodes"),
                           pv_uci=raw.get("pv_uci", []), bestmove_uci=raw.get("bestmove_uci"))
                score = raw.get("score", {})
                if score.get("kind") == "mate":
                    row["mate"] = score.get("stm")
                if score.get("kind") == "cp":
                    row["cp"] = score.get("stm")
                line = snapshot.copy(stack=True)
                valid = bool(row["pv_uci"]) and row["bestmove_uci"] == row["pv_uci"][0]
                for uci in row["pv_uci"]:
                    move = chess.Move.from_uci(uci)
                    if move not in line.legal_moves:
                        valid = False
                        break
                    line.push(move)
                if (raw.get("completed") is True and not raw.get("lowerbound")
                        and not raw.get("upperbound") and valid
                        and type(row["depth"]) is int and row["depth"] >= self.minimum_depth
                        and (type(row["cp"]) is int or type(row["mate"]) is int)):
                    row.update(status="complete", reason="exact_completed_native_search")
                else:
                    row["reason"] = "insufficient_exact_depth_or_legal_pv"
            except SearchInterrupted as error:
                row.update(reason=error.reason, native=error.record)
            except (ValueError, TypeError, chess.engine.EngineError, OSError) as error:
                row.update(status="unknown", reason="invalid_native_response",
                           error=type(error).__name__)
            finally:
                self.teacher.nodes = previous
        self.records.append(row)
        # Checkpoint on every query: interruptions never turn partial evidence
        # into a missing report or overwrite a previous experiment.
        with (self.output_dir / "queries.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        return row

    def __exit__(self, exc_type, exc, traceback):
        if self.teacher is not None:
            self.teacher.__exit__(exc_type, exc, traceback)
            # The in-memory ledger is authoritative at shutdown. Persist one
            # atomic full snapshot as well as the incremental checkpoint log;
            # readers must never mistake a partial live journal for a complete
            # experiment. Preserve the checkpoint file separately for audit.
            write_json(self.output_dir / "queries.json", self.records)
            journal = self.output_dir / "queries.jsonl"
            if journal.exists():
                journal.replace(self.output_dir / "queries.incremental.jsonl")
            temporary = self.output_dir / "queries.final.tmp"
            temporary.write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                                         for row in self.records), encoding="utf-8", newline="\n")
            temporary.replace(journal)
            write_json(self.output_dir / "analysis_summary.json", {
                "identity": self.identity, "queries": len(self.records),
                "statuses": dict(Counter(row["status"] for row in self.records)),
                "requested_nodes": sum(row["requested_nodes"] for row in self.records),
                "actual_nodes": sum(row.get("nodes") or 0 for row in self.records),
                "final_query_ledger_sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
                "final_query_ledger_rows": len(self.records),
                "cleanup": self.teacher.cleanup_status, "closed": self.teacher.closed,
                "sources_unchanged": source_hashes() == self._sources,
                "binary_unchanged": hashlib.sha256(self.binary.read_bytes()).hexdigest()
                                    == self.identity.get("binary_sha256")})
