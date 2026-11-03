"""Real HTTP/native timing across all three modes on authored positions.

Only fixture positions, initial clock balances and seeds are injected. Search,
move choice, policy verification, refinement, waits, clock debit and responses
use the production host. Display-only peers do not run an engine. The clock
grid reuses measured evidence and is not additional latency measurement.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import urllib.request

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import boss_play
import engine_pacing
import game_clock
import play_human
from boss_strength_match import DisplayOnlyPeer, native_engine
from runtime_engine import resolve_runtime_engine
from gambit_character_validation import TraceEngine, safe

MODES = {"basic": {}, "gambit": {"boss_mode": True}, "sparring": {"sparring_mode": True}}
COMPLEX_ROOT = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 12"
COMPLEX_MOVES = "g2g3 e8d8 e1g1 d7d6 f3h1 a8b8 d2h6 b6a4".split()
POSITIONS = (
    ("single_reply", "8/8/8/8/8/k7/8/Kr6 w - - 0 1", 600000),
    ("clear_queen_gain", "6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30", 600000),
    ("complex_choices", None, 300000),
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_paths():
    return sorted(set(p.relative_to(ROOT).as_posix() for p in ROOT.joinpath("scripts").glob("*.py")) | {
        Path(__file__).relative_to(ROOT).as_posix(), "tests/gambit_character_validation.py"})


def hashes():
    return {name: sha(ROOT / name) for name in source_paths()}


def write(path, value):
    path.write_text(json.dumps(safe(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def timing_clock(board, left, elapsed=0):
    color = "white" if board.turn else "black"
    return {"enabled": True, "status": "running", "running_color": color,
            "white_ms": left if board.turn else 600000, "black_ms": 600000 if board.turn else left,
            "turn_elapsed_ms": elapsed, "increment_ms": 0, "control_id": "10+0", "initial_ms": 600000}


def fixture_board(kind, fen):
    if kind == "complex_choices":
        board = chess.Board(COMPLEX_ROOT)
        for uci in COMPLEX_MOVES:
            board.push_uci(uci)
    else:
        board = chess.Board(fen)
    assert board.is_valid() and not board.is_game_over(claim_draw=True)
    return board


def timing_seed(board, kind, left):
    """Choose by the timing policy before observing any engine result."""
    for index in range(1000):
        seed = f"common-human-pacing-{kind}-{index}"
        plan = engine_pacing.plan(board, timing_clock(board, left), seed=seed,
                                  pressure_start_ms=300000)
        if kind == "complex_choices":
            if engine_pacing._unit(seed, "long-think", board.fen(en_passant="fen")) < .10:
                assert plan["complexity_score"] >= 70
                return seed
        elif plan["target_total_ms"] == 100:
            return seed
    raise AssertionError("No deterministic timing fixture seed: " + kind)


class TimedTraceEngine(TraceEngine):
    def __init__(self, *args, origin):
        self.origin = origin
        super().__init__(*args)

    def emit(self, item):
        super().emit({"wall_since_engine_ms": round((time.monotonic() - self.origin) * 1000, 3), **item})


class Session:
    def __init__(self, binary, directory):
        self.directory = directory
        directory.mkdir()
        self.events, self.identities, self.observed = [], [], []
        self.origin = time.monotonic()
        self.trace = (directory / "native-searches.jsonl").open("x", encoding="utf-8")

        def factory(path):
            engine = TimedTraceEngine(native_engine(path, boss=True), self.trace, self.identities, self.events,
                                      origin=self.origin)
            engine.configure({k: v for k, v in (("Threads", 1), ("Hash", 16)) if k in engine.options})
            return engine

        self.host = play_human.HumanGame(binary, directory / "host", engine_factory=factory,
            hint_engine_factory=lambda _: DisplayOnlyPeer(), evaluation_engine_factory=lambda _: DisplayOnlyPeer(),
            neural_model=None, opening_variety_enabled=True, dynamic_play_enabled=True)
        self.server = play_human.HumanServer(self.host)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.closed = False

    def request(self, path, body=None):
        request = urllib.request.Request(self.server.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Regicide-Token": self.server.token, "Origin": self.server.origin,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read())

    def setup(self, board, mode, seed, left):
        state = self.request("/api/state")
        state = self.request("/api/new", {"revision": state["revision"],
            "color": "black" if board.turn else "white", "movetime_ms": 1000,
            "time_control": "10+0", **MODES[mode]})
        with self.host._lock:
            self.host.board = board.copy(stack=True)
            self.host._opening_seed = seed
            own = "white" if board.turn else "black"
            human = "black" if board.turn else "white"
            clock = game_clock.GameClock("10+0", timeout_policy="engine_only", soft_timeout_color=human)
            clock._balances[own] = float(left)
            clock.ply = len(board.move_stack)
            clock.history = [clock._entry(None, 0)]
            clock.restore_ply(len(board.move_stack), own)
            self.host._clock = clock
            self.host._sync_evaluation()
            self.host._clock._anchor = time.monotonic()
        return state

    def engine(self, label):
        board = self.host.board.copy(stack=True)
        state = self.host.state()
        start_clock = copy.deepcopy(state["clock"])
        event_start = len(self.events)
        observed = []
        original = engine_pacing.refine

        def capture(position, plan, clock, **kwargs):
            # Observation only: delegate without altering evidence or timing.
            observed.append((position.copy(stack=True), copy.deepcopy(plan), copy.deepcopy(clock), copy.deepcopy(kwargs)))
            return original(position, plan, clock, **kwargs)

        started = time.monotonic()
        with patch.object(engine_pacing, "refine", side_effect=capture):
            result = self.request("/api/engine", {"revision": state["revision"]})
        wall_ms = (time.monotonic() - started) * 1000
        if result.get("error"):
            raise AssertionError(result["error"])
        assert len(self.host.board.move_stack) == len(board.move_stack) + 1
        move = self.host.board.peek()
        assert move in board.legal_moves
        expected = board.copy(stack=True)
        expected.push(move)
        assert expected.fen(en_passant="fen") == self.host.board.fen(en_passant="fen")
        ply = len(self.host.board.move_stack)
        saved = json.loads((self.host.save_dir / (self.host.game_id + ".json")).read_text(encoding="utf-8"))
        receipt = copy.deepcopy(self.host._engine_pacing_decisions[ply])
        charge = saved["clock"]["history"][-1]["elapsed_ms"]
        searches = [e for e in self.events[event_start:] if e["event"] == "search"]
        returns = [e for e in self.events[event_start:] if e["event"] == "bestmove"]
        search_map = {(e["instance"], e["search"]): e for e in searches}
        selected_return = any(e["move"] == move.uci() and
            search_map[(e["instance"], e["search"])]["fen"] == board.fen(en_passant="fen") for e in returns)
        span = max(0, returns[-1]["wall_since_engine_ms"] - searches[0]["wall_since_engine_ms"]) if searches and returns else 0
        replay = chess.pgn.read_game(io.StringIO(self.host.pgn()))
        pgn_board = replay.end().board() if replay is not None and not replay.errors else None
        checks = {"legal_exact_append": True, "automatic_allocation": receipt.get("allocation", {}).get("automatic") is True,
            "automatic_mode_metadata": state["engine"].get("automatic_think_time") is True,
            "five_minute_pressure": receipt.get("pressure_start_ms") == 300000,
            "refinement_observed_once": len(observed) == 1,
            "no_refinement_error": "refinement_error" not in receipt,
            "selected_returned_by_native": selected_return,
            "all_search_time_in_clock_charge": charge >= span - 2,
            "no_fresh_full_delay_after_search": charge <= max(receipt.get("spent_before_wait_ms", 0), receipt.get("wait_target_total_ms", 0)) + 250,
            "same_saved_pacing": any(r == {"ply": ply, **receipt} for r in saved["engine_pacing"]["decisions"]),
            "pgn_replays_exactly": pgn_board is not None and pgn_board.fen(en_passant="fen") == self.host.board.fen(en_passant="fen")}
        if observed:
            captured_board, plan, captured_clock, kwargs = observed[0]
            checks["allocation_unchanged_after_search"] = plan["allocation"] == receipt["allocation"]
            checks["fresh_clock_includes_searches"] = captured_clock["turn_elapsed_ms"] >= span - 2
            grid = []
            for left in (300001, 300000, 299999, 180000, 60000, 10000):
                clock = timing_clock(captured_board, left)
                hypothetical = engine_pacing.plan(captured_board, clock, seed=self.host._opening_seed,
                                                   pressure_start_ms=300000)
                refined = original(captured_board, hypothetical, clock, **kwargs)
                grid.append({"remaining_ms": left, "target_total_ms": refined["target_total_ms"],
                    "remaining_hold_ms": engine_pacing.remaining_wait_ms(refined, clock), "simulated": True})
            checks["pressure_grid_monotonic"] = all(a["target_total_ms"] >= b["target_total_ms"] for a, b in zip(grid, grid[1:]))
            checks["ten_seconds_has_no_hold"] = grid[-1]["remaining_hold_ms"] == 0
        else:
            grid = []
        record = {"id": label, "before_fen": board.fen(en_passant="fen"),
            "root_fen": board.root().fen(en_passant="fen"), "history_uci": [m.uci() for m in board.move_stack],
            "seed": self.host._opening_seed, "uci": move.uci(), "san": board.san(move),
            "after_fen": self.host.board.fen(en_passant="fen"), "http_wall_ms": round(wall_ms, 3),
            "clock_charge_ms": charge, "search_span_ms": round(span, 3), "start_clock": start_clock,
            "receipt": receipt, "native_query_count": len(searches), "checks": checks,
            "pressure_grid_reusing_actual_search": grid, "boss_decision": copy.deepcopy(self.host._boss_decisions.get(ply)),
            "sparring_decision": copy.deepcopy(self.host._sparring_decisions.get(ply))}
        self.observed.append(record)
        write(self.directory / (label + ".json"), record)
        return record

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.host.close()
        self.trace.close()
        self.closed = not self.worker.is_alive() and self.host._engine is None
        assert self.closed


def run(binary, output):
    output.mkdir(parents=True, exist_ok=False)
    before = hashes()
    manifest = []
    for kind, fen, left in POSITIONS:
        board = fixture_board(kind, fen)
        seed = timing_seed(board, kind, left)
        manifest.extend({"mode": mode, "kind": kind, "fen": board.fen(en_passant="fen"),
                         "seed": seed, "left_ms": left} for mode in MODES)
    gambit_board = chess.Board()
    gambit_board.push_uci("d2d4")
    gambit_seed = "common-human-pacing-authored-englund"
    assert boss_play.plan(gambit_board, 1000, gambit_seed)["candidate_uci"] == "e7e5"
    write(output / "predeclared-fixtures.json", {"matrix": manifest,
        "standard_gambit": {"history_uci": ["d2d4"], "seed": gambit_seed, "signature_uci": "e7e5",
                            "opponent_reply_uci": "d4e5"},
        "seed_selection": "Timing target/long-think policy only, before native search; same seed per position across modes."})
    report = {"schema": "regicide.common-human-pacing-native.v1", "started_utc": datetime.now(timezone.utc).isoformat(),
        "policy": engine_pacing.POLICY, "source_sha256": before, "binary": str(binary), "binary_sha256": sha(binary),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "python": platform.python_version(), "python_chess": chess.__version__, "platform": platform.platform(),
        "records": [], "sessions": [], "scope": __doc__, "errors": [],
        "limitations": ["Authored diagnostics, no human timing fit, Elo or whole-game average.",
            "Custom-root matrix deliberately disables mode-specific move selection to compare common timing.",
            "The standard-root gambit separately exercises mode-specific verification and shared time reserves.",
            "Pressure grids reuse observed search evidence and are simulations, not repeated latency measurements."]}
    for spec in manifest:
        session = None
        try:
            kind, mode = spec["kind"], spec["mode"]
            fen = next(fen for k, fen, _left in POSITIONS if k == kind)
            session = Session(binary, output / (kind + "-" + mode))
            session.setup(fixture_board(kind, fen), mode, spec["seed"], spec["left_ms"])
            row = session.engine(kind)
            row.update(mode=mode, kind=kind)
            if kind in ("single_reply", "clear_queen_gain"):
                row["checks"]["near_instant_target"] = row["receipt"]["target_total_ms"] == 100
                row["checks"]["quick_actual_bounded"] = row["clock_charge_ms"] < 2000
                if kind == "clear_queen_gain":
                    row["checks"]["takes_visible_queen"] = chess.Board(row["before_fen"]).piece_type_at(chess.Move.from_uci(row["uci"]).to_square) == chess.QUEEN
            else:
                row["checks"]["long_deliberation_observed"] = row["receipt"].get("refinement", {}).get("kind") == "long_deliberation"
                row["checks"]["complex_turn_longer_than_quick"] = row["clock_charge_ms"] > 2000
            write(session.directory / (kind + ".json"), row)
            report["records"].append(row)
            print(json.dumps({"mode": mode, "kind": kind, "clock_ms": row["clock_charge_ms"],
                "target_ms": row["receipt"]["target_total_ms"], "checks": row["checks"]}), flush=True)
        except Exception as error:
            report["errors"].append({"fixture": spec, "error": type(error).__name__ + ": " + str(error)})
        finally:
            if session:
                session.close()
                report["sessions"].append({"id": session.directory.name, "closed": session.closed, "engines": session.identities})
            write(output / "native-result.json", report)
    session = None
    try:
        session = Session(binary, output / "standard-gambit")
        session.setup(gambit_board, "gambit", gambit_seed, 600000)
        signature = session.engine("signature")
        signature.update(mode="gambit", kind="standard_signature")
        decision = signature["boss_decision"] or {}
        signature["checks"]["native_signature_selected"] = signature["uci"] == "e7e5" and decision.get("line_complete") is True
        signature["checks"]["policy_baseline_reserve"] = decision.get("baseline_budget_ms", 0) >= boss_play.MIN_TOTAL_MS
        signature["checks"]["policy_extra_reserve"] = decision.get("extra_budget_ms", 0) > 0
        write(session.directory / "signature.json", signature)
        report["records"].append(signature)
        if signature["uci"] == "e7e5":
            state = session.host.state()
            session.request("/api/move", {"revision": state["revision"], "move": "d4e5"})
            continuation = session.engine("initiative")
            continuation.update(mode="gambit", kind="standard_initiative")
            selected = continuation["boss_decision"] or {}
            continuation["checks"]["initiative_policy_not_disabled_by_fast_pacing"] = selected.get("mode") == "initiative"
            write(session.directory / "initiative.json", continuation)
            report["records"].append(continuation)
        print(json.dumps({"kind": "standard_gambit", "moves": [r["uci"] for r in session.observed]}), flush=True)
    except Exception as error:
        report["errors"].append({"fixture": "standard_gambit", "error": type(error).__name__ + ": " + str(error)})
    finally:
        if session:
            session.close()
            report["sessions"].append({"id": session.directory.name, "closed": session.closed, "engines": session.identities})
    for row in report["records"]:
        row["passed"] = all(row["checks"].values())
    report["mode_comparison"] = [{"kind": kind, "rows": [{"mode": row["mode"],
        "initial_target_ms": row["receipt"].get("refinement", {}).get("original_target_total_ms"),
        "baseline_ms": row["receipt"]["allocation"]["baseline_ms"],
        "total_search_ms": row["receipt"]["allocation"]["total_search_ms"],
        "target_ms": row["receipt"]["target_total_ms"], "clock_ms": row["clock_charge_ms"]}
        for row in report["records"] if row["kind"] == kind]} for kind, _fen, _left in POSITIONS]
    report.update(finished_utc=datetime.now(timezone.utc).isoformat(), source_sha256_after=hashes(),
        source_unchanged=before == hashes(), binary_unchanged=sha(binary) == report["binary_sha256"],
        record_count=len(report["records"]), expected_record_count=11,
        passed_records=sum(r["passed"] for r in report["records"]))
    report["passed"] = (not report["errors"] and report["record_count"] == 11
        and report["passed_records"] == 11 and report["source_unchanged"] and report["binary_unchanged"])
    write(output / "native-result.json", report)
    print(json.dumps({"passed": report["passed"], "records": report["record_count"], "errors": report["errors"]}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(run((args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True), args.output_dir.resolve()))
