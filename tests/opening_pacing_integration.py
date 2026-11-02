"""Real HTTP opening turns on authored positions, with a pinned v5 comparison.

Fixture positions and zero-cost prefix clock histories are injected into an
isolated temporary game. The next move, search, wait and clock debit are real.
The v5 comparison reuses v6's observed search; it does not measure v5 latency,
prove equal playing strength, or use personal games or human timing data.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
import types
from unittest.mock import patch
import urllib.request

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import engine_pacing
import game_clock
import play_human
from game_archive import GameArchive
from runtime_engine import resolve_runtime_engine
from check_sparring_evaluation import json_packet

BASELINE = "895cf07894574665f5f536d4633660cacb374643"
SOURCES = ("scripts/engine_pacing.py", "scripts/play_human.py", "scripts/game_clock.py",
           "scripts/game_archive.py", "scripts/decision_support.py", "scripts/sparring_play.py",
           "scripts/sparring_pacing.py", "scripts/sparring_evaluation.py",
           "scripts/evaluation_stability.py", "tests/opening_pacing_integration.py")
POSITIONS = (
    ("offbook_start", ""),
    ("after_nc3", "Nc3"),
    ("reti", "Nf3 d5 g3 Nf6"),
    ("italian_contact", "e4 e5 Nf3 Nc6 Bc4 Nf6"),
    ("closed_sicilian", "e4 c5 Nc3 Nc6 g3 g6 Bg2 Bg7"),
    ("london_contact", "d4 d5 Bf4 Nf6 e3 e6 Nf3 c5"),
)


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def clock(board, left=600000, elapsed=0):
    return {"enabled": True, "status": "running", "running_color": "white" if board.turn else "black",
            "white_ms": left, "black_ms": left, "turn_elapsed_ms": elapsed,
            "increment_ms": 0, "control_id": "10+0", "initial_ms": 600000}


def position(line):
    board = chess.Board()
    for san in line.split():
        board.push_san(san)
    assert board.is_valid()
    return board


def run(binary, output):
    if output.exists():
        raise ValueError("Choose a new output directory; previous evidence is immutable")
    output.mkdir(parents=True)
    sources = hashes()
    raw = subprocess.check_output(["git", "show", BASELINE + ":scripts/engine_pacing.py"], cwd=ROOT)
    legacy = types.ModuleType("pinned_v5_pacing")
    exec(compile(raw, "<pinned-v5-pacing>", "exec"), legacy.__dict__)
    assert legacy.POLICY == "regicide-engine-pacing-v5"
    assert engine_pacing.POLICY == "regicide-engine-pacing-v6"
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def factory(path):
        return chess.engine.SimpleEngine.popen_uci([str(path), "uci"], timeout=10, **options)

    original_refine = engine_pacing.refine
    observed, records = [], []

    def capture_refinement(board, plan, current_clock, **kwargs):
        # Observe and delegate unchanged: no fabricated moves, search scores,
        # timing decisions or sleeps enter the production host.
        observed.append((board.copy(stack=True), copy.deepcopy(plan), copy.deepcopy(current_clock), copy.deepcopy(kwargs)))
        return original_refine(board, plan, current_clock, **kwargs)

    with tempfile.TemporaryDirectory(prefix="regicide-opening-native-") as folder:
        game = play_human.HumanGame(binary, Path(folder) / "games", engine_factory=factory,
                                   hint_engine_factory=factory, evaluation_engine_factory=factory,
                                   opening_variety_enabled=False)
        server = play_human.HumanServer(game)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def request(path, body=None):
            req = urllib.request.Request(server.origin + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"X-Regicide-Token": server.token, "Origin": server.origin,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read())

        try:
            for name, line in POSITIONS:
                board = position(line)
                own = "white" if board.turn else "black"
                human = "black" if board.turn else "white"
                seed = "opening-native-" + name
                state = request("/api/state")
                state = request("/api/new", {"revision": state["revision"], "color": human,
                                             "sparring_mode": True, "time_control": "10+0"})
                with game._lock:
                    game.board = board.copy(stack=True)
                    game._opening_seed = seed
                    current_clock = game_clock.GameClock("10+0", timeout_policy="engine_only", soft_timeout_color=human)
                    # Preserve a coherent prefix for clock/PGN validation.
                    # These setup moves have zero injected cost, not measured cost.
                    for ply in range(1, len(board.move_stack) + 1):
                        current_clock.ply = ply
                        current_clock.history.append(current_clock._entry("white" if ply % 2 else "black", 0))
                    current_clock._running = own
                    current_clock._turn_color = own
                    game._clock = current_clock
                    game._sync_evaluation()
                    game._clock._anchor = time.monotonic()
                started = time.monotonic()
                with patch.object(engine_pacing, "refine", side_effect=capture_refinement):
                    result = request("/api/engine", {"revision": state["revision"]})
                http_ms = round((time.monotonic() - started) * 1000, 3)
                assert result["error"] is None, result["error"]
                assert len(result["moves"]) == len(board.move_stack) + 1
                selected = chess.Move.from_uci(result["moves"][-1]["uci"])
                assert selected in board.legal_moves
                saved = json.loads((game.save_dir / (game.game_id + ".json")).read_text(encoding="utf-8"))
                receipt = saved["engine_pacing"]["decisions"][0]
                actual_ms = saved["clock"]["history"][-1]["elapsed_ms"]
                assert receipt["opening_budget"] is not None, name
                assert receipt["target_total_ms"] <= 2000, receipt
                assert receipt["allocation"]["automatic"] is True
                assert receipt["allocation"]["total_search_ms"] <= 2000
                assert actual_ms >= receipt["target_total_ms"] - 2
                # Desktop scheduling may add overhead; report its actual cost.
                assert actual_ms < 3000, (name, actual_ms)
                assert "refinement_error" not in receipt
                assert receipt["refinement"]["kind"] != "long_deliberation"
                assert len(observed) == len(records) + 1
                captured_board, captured_plan, captured_clock, kwargs = observed[-1]
                old_plan = legacy.plan(captured_board, clock(captured_board), seed=seed)
                new_plan = engine_pacing.plan(captured_board, clock(captured_board), seed=seed)
                old_refined = legacy.refine(captured_board, old_plan, captured_clock, **kwargs)
                new_refined = original_refine(captured_board, new_plan, captured_clock, **kwargs)
                assert new_refined["target_total_ms"] <= old_refined["target_total_ms"]
                archive = GameArchive(game.save_dir).get_game(game.game_id)
                parsed = chess.pgn.read_game(io.StringIO(archive["pgn"]))
                assert not parsed.errors
                assert parsed.headers["EnginePacingPolicy"] == engine_pacing.POLICY
                assert list(parsed.mainline_moves()) == [*board.move_stack, selected]
                pressure = []
                for left in (240000, 180000, 60000):
                    imagined = clock(board, left)
                    plan = engine_pacing.plan(board, imagined, seed=seed)
                    refined = original_refine(board, plan, imagined, **kwargs)
                    pressure.append({"remaining_ms": left, "target_total_ms": refined["target_total_ms"], "simulated": True})
                assert [r["target_total_ms"] for r in pressure] == sorted((r["target_total_ms"] for r in pressure), reverse=True)
                row = {"id": name, "root_fen": board.root().fen(en_passant="fen"),
                       "moves_san": line, "moves_uci": [m.uci() for m in board.move_stack],
                       "fen": board.fen(en_passant="fen"), "seed": seed, "selected_uci": selected.uci(),
                       "legal": True, "pgn_policy_verified": True, "receipt": receipt,
                       "actual_clock_charge_ms": actual_ms, "http_request_ms": http_ms,
                       "same_observed_search_comparison": {
                           "v5_initial_ms": old_plan["target_total_ms"], "v6_initial_ms": new_plan["target_total_ms"],
                           "v5_refined_ms": old_refined["target_total_ms"], "v6_refined_ms": new_refined["target_total_ms"],
                           "v5_kind": old_refined.get("refinement", {}).get("kind")},
                       "pressure_grid_reusing_search": pressure,
                       "observed_search_evidence": json_packet(kwargs), "observed_search_end_clock": captured_clock}
                records.append(row)
                (output / (name + ".json")).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"id": name, "target_ms": receipt["target_total_ms"], "actual_ms": actual_ms,
                                  "v5_model_target_ms": old_refined["target_total_ms"], "selected": selected.uci()}), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            game.close()
            assert not worker.is_alive() and game._engine is None
    # Model-only guards: do not shorten real tactical danger or a later phase.
    guards = []
    dangerous = position("e4 e5 Nf3 Nc6 Bc4 d6 Nc3 Bg4 Nxe5")
    late = position("d4 d5 Nf3 Nf6 Bf4 e6 e3 Bd6")
    late.fullmove_number = 20
    for name, board in (("poisoned_queen_tactic", dangerous), ("later_phase_counter", late)):
        seed = "opening-guard-" + name
        old = legacy.plan(board, clock(board), seed=seed)
        new = engine_pacing.plan(board, clock(board), seed=seed)
        assert new["opening_budget"] is None
        assert new["target_total_ms"] == old["target_total_ms"]
        guards.append({"id": name, "fen": board.fen(en_passant="fen"), "seed": seed,
                       "model_only": True, "opening_budget": None, "target_ms": new["target_total_ms"]})
    after = hashes()
    assert sources == after, "Source changed during diagnostic"
    report = {"schema": "regicide.opening-pacing-native.v1", "passed": True,
              "created_utc": datetime.now(timezone.utc).isoformat(), "policy": engine_pacing.POLICY,
              "source_sha256": sources, "source_sha256_after": after,
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "baseline_commit": BASELINE, "baseline_pacing_sha256": hashlib.sha256(raw).hexdigest(),
              "python": platform.python_version(), "platform": platform.platform(),
              "records": records, "model_only_guards": guards, "closed_test_processes": True,
              "scope": __doc__}
    (output / "native-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "output": str(output), "source_unchanged": True}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run((args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True), args.output.resolve())
