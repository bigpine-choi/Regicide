"""Native HTTP timing contrasts on isolated, authored positions.

Positions and starting clock balances are injected into a temporary game.
Move selection, search, refinement, real waits, clock debit, and HTTP response
use production code. Clock grids reuse one observed search and are NOT
additional latency measurements or a fit to human behavior.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
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
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import engine_pacing
import game_clock
import play_human
from runtime_engine import resolve_runtime_engine
from check_sparring_evaluation import json_packet

SOURCES = ("scripts/engine_pacing.py", "scripts/play_human.py", "scripts/game_clock.py",
           "scripts/decision_support.py", "scripts/sparring_play.py",
           "scripts/sparring_pacing.py", "scripts/sparring_evaluation.py",
           "scripts/evaluation_stability.py", "tests/contrast_pacing_integration.py")
COMPLEX_ROOT = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 12"
COMPLEX_MOVES = "g2g3 e8d8 e1g1 d7d6 f3h1 a8b8 d2h6 b6a4".split()
POSITIONS = (
    ("single_reply", "8/8/8/8/8/k7/8/Kr6 w - - 0 1", 600000),
    ("clear_queen_gain", "6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30", 600000),
    # Legal continuation of the repository Kiwipete fixture:
    # g2g3 e8d8 e1g1 d7d6 f3h1 a8b8 d2h6 b6a4.
    # Authored stress position, not a representative game or human dataset.
    ("complex_choices", "1r1k3r/p1p1qpb1/b2ppnpB/3PN3/np2P3/2N3Pp/PPP1BP1P/R4RKQ w - - 4 16", 300000),
)


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def clock(left):
    return {"enabled": True, "status": "running", "running_color": "white",
            "white_ms": left, "black_ms": 600000, "turn_elapsed_ms": 0,
            "increment_ms": 0, "control_id": "10+0", "initial_ms": 600000}


def seed_for(board, kind, left):
    for index in range(1000):
        seed = f"contrast-pacing-{kind}-{index}"
        plan = engine_pacing.plan(board, clock(left), seed=seed)
        if kind == "complex_choices":
            if engine_pacing._unit(seed, "long-think", board.fen(en_passant="fen")) < .10:
                assert plan["complexity_score"] >= 70
                return seed
        elif plan["target_total_ms"] == 100:
            return seed
    raise AssertionError("No deterministic timing fixture seed")


def run(binary, output):
    if output.exists():
        raise ValueError("Choose a new output directory; previous evidence is immutable")
    output.mkdir(parents=True)
    sources = hashes()
    records = []
    native_options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def native_factory(path):
        return chess.engine.SimpleEngine.popen_uci([str(path), "uci"], timeout=10, **native_options)

    original_refine = engine_pacing.refine
    observed = []

    def capture_refinement(board, plan, current_clock, **kwargs):
        # Observe arguments and delegate unchanged. No replacement scores,
        # selected moves, clock, timing policy, or sleep behavior are supplied.
        observed.append((board.copy(stack=True), copy.deepcopy(plan), copy.deepcopy(current_clock), copy.deepcopy(kwargs)))
        return original_refine(board, plan, current_clock, **kwargs)

    with tempfile.TemporaryDirectory(prefix="regicide-contrast-native-") as folder:
        game = play_human.HumanGame(binary, Path(folder) / "games", engine_factory=native_factory,
                                   hint_engine_factory=native_factory, evaluation_engine_factory=native_factory,
                                   opening_variety_enabled=False)
        server = play_human.HumanServer(game)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def request(path, body=None):
            req = urllib.request.Request(server.origin + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"X-Regicide-Token": server.token, "Origin": server.origin,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=75) as response:
                return json.loads(response.read())

        try:
            for kind, fen, left in POSITIONS:
                board = chess.Board(fen)
                assert board.is_valid() and not board.is_game_over(claim_draw=True)
                provenance = {"kind": "repository-authored fixture", "root_fen": fen, "moves_uci": []}
                if kind == "complex_choices":
                    replay = chess.Board(COMPLEX_ROOT)
                    for uci in COMPLEX_MOVES:
                        move = chess.Move.from_uci(uci)
                        assert move in replay.legal_moves
                        replay.push(move)
                    assert replay.fen() == fen
                    provenance = {"kind": "authored legal continuation of repository Kiwipete fixture",
                                  "root_fen": COMPLEX_ROOT, "moves_uci": COMPLEX_MOVES}
                seed = seed_for(board, kind, left)
                state = request("/api/state")
                state = request("/api/new", {"revision": state["revision"], "color": "black",
                                              "sparring_mode": True, "time_control": "10+0"})
                with game._lock:
                    game.board = board.copy(stack=True)
                    game._opening_seed = seed
                    game._clock = game_clock.GameClock("10+0", timeout_policy="engine_only", soft_timeout_color="black")
                    game._clock._balances["white"] = float(left)
                    game._clock.history = [game._clock._entry(None, 0)]
                    game._sync_evaluation()
                    game._clock._anchor = time.monotonic()
                started = time.monotonic()
                with patch.object(engine_pacing, "refine", side_effect=capture_refinement):
                    result = request("/api/engine", {"revision": state["revision"]})
                request_ms = (time.monotonic() - started) * 1000
                assert result["error"] is None, result["error"]
                assert len(result["moves"]) == 1
                selected = chess.Move.from_uci(result["moves"][0]["uci"])
                assert selected in board.legal_moves
                saved = json.loads((game.save_dir / (game.game_id + ".json")).read_text(encoding="utf-8"))
                receipt = saved["engine_pacing"]["decisions"][0]
                actual_ms = saved["clock"]["history"][-1]["elapsed_ms"]
                assert "refinement_error" not in receipt
                assert receipt["allocation"]["automatic"] is True
                assert receipt["completion_reason"] in ("target_reached", "wall_deadline")
                assert actual_ms >= receipt["target_total_ms"] - 2
                assert len(observed) == len(records) + 1
                captured_board, captured_plan, captured_clock, kwargs = observed[-1]
                if kind == "complex_choices":
                    assert receipt["refinement"]["kind"] == "long_deliberation", receipt["refinement"]
                    assert actual_ms >= 15000, actual_ms
                else:
                    assert receipt["target_total_ms"] == 100, receipt
                    # Actual time is reported separately. A broad functional
                    # ceiling catches unintended multi-second waiting without
                    # asserting desktop scheduling is exactly 100 milliseconds.
                    assert actual_ms < 2000, actual_ms
                    assert receipt["allocation"]["total_search_ms"] <= 90
                    if kind == "clear_queen_gain":
                        assert board.piece_type_at(selected.to_square) == chess.QUEEN
                grids = []
                for remaining in (240000, 180000, 60000):
                    hypothetical = clock(remaining)
                    plan = engine_pacing.plan(board, hypothetical, seed=seed)
                    refined = original_refine(captured_board, plan, hypothetical, **kwargs)
                    grids.append({"remaining_ms": remaining, "target_total_ms": refined["target_total_ms"],
                                  "refinement": refined.get("refinement"), "simulated": True})
                targets = [row["target_total_ms"] for row in grids]
                assert targets == sorted(targets, reverse=True)
                record = {"id": kind, "fen": fen, "seed": seed, "starting_remaining_ms": left,
                          "position_provenance": provenance,
                          "sparring_mode": True, "selected_uci": selected.uci(), "legal": True,
                          "actual_clock_charge_ms": actual_ms, "http_request_ms": round(request_ms, 3),
                          "receipt": receipt, "pressure_grid_reusing_actual_search": grids,
                          "observed_search_evidence": json_packet(kwargs),
                          "observed_search_end_clock": captured_clock}
                records.append(record)
                (output / (kind + ".json")).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps({"id": kind, "target_ms": receipt["target_total_ms"], "actual_ms": actual_ms,
                                  "http_ms": round(request_ms, 3), "selected": selected.uci(), "passed": True}), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            game.close()
            assert not worker.is_alive() and game._engine is None
    after = hashes()
    assert sources == after, "Source changed during the diagnostic; rerun after edits settle"
    report = {"schema": "regicide.contrast-pacing-native.v1", "passed": True,
              "created_utc": datetime.now(timezone.utc).isoformat(), "policy": engine_pacing.POLICY,
              "source_sha256": sources, "source_sha256_after": after,
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "python": platform.python_version(), "platform": platform.platform(),
              "records": records, "closed_test_processes": True,
              "scope": __doc__, "limitations": ["Three authored positions; no whole-game average or human fit.",
                "FEN and starting balance injected for isolated fixture setup; all waits used the real clock.",
                "HTTP time includes serialization; clock debit records the actual engine turn.",
                "Near-instant search can provide less analysis evidence; this is not a strength claim."]}
    (output / "native-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "output": str(output), "source_unchanged": True}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run((args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True), args.output.resolve())
