"""Two native host decisions at move 50, using fixed legal history prefixes.

This replays a controlled fixture and searches only the next move for each
color. It is not a 50-move match, strength benchmark, or rating calibration.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import play_human
from sparring_state_tests import late_game_board, LATE_GAME_HISTORY_UCI

SOURCES = ("scripts/sparring_play.py", "scripts/sparring_pacing.py", "scripts/play_human.py",
           "scripts/sparring_rating.py", "tests/sparring_state_tests.py",
           "tests/sparring_rating_state_tests.py", "tests/sparring_fifty_integration.py")


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


class RecordedAnalysis:
    def __init__(self, native, record):
        self.native, self.record = native, record

    def __enter__(self):
        self.native.__enter__()
        return self

    def __exit__(self, *args):
        return self.native.__exit__(*args)

    def __iter__(self):
        return iter(self.native)

    def __getattr__(self, name):
        return getattr(self.native, name)

    def wait(self):
        result = self.native.wait()
        self.record["native_bestmove"] = result.move.uci() if result.move else None
        return result


class RecordedEngine:
    """Transparent tracing of the real native reply, independent of receipts."""
    def __init__(self, binary, searches):
        self.native = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        self.searches = searches
        self.native.configure({name:value for name, value in {"Threads":1, "Hash":16}.items()
                               if name in self.native.options})

    def __getattr__(self, name):
        return getattr(self.native, name)

    def analysis(self, board, limit, **kwargs):
        record = {"ply":len(board.move_stack), "fen":board.fen(en_passant="fen"),
                  "requested_ms":None if limit.time is None else round(limit.time * 1000),
                  "root_moves":None if kwargs.get("root_moves") is None else
                      [move.uci() for move in kwargs["root_moves"]]}
        self.searches.append(record)
        return RecordedAnalysis(self.native.analysis(board, limit, **kwargs), record)


def run(output):
    binary = Path(subprocess.check_output([sys.executable, str(ROOT / "scripts/runtime_engine.py"),
        "--resolve"], cwd=ROOT, text=True).strip()).resolve(strict=True)
    before, cases, searches = hashes(), [], []
    with tempfile.TemporaryDirectory(prefix="regicide-fifty-native-") as folder:
        game = play_human.HumanGame(binary, Path(folder) / "games",
            engine_factory=lambda executable: RecordedEngine(executable, searches),
            opening_variety_enabled=False)
        try:
            for ply in (98, 99):
                position = late_game_board(ply)
                assert position.fullmove_number == 50 and not position.is_game_over(claim_draw=True)
                game.new_game("black" if position.turn else "white", 1000, game.revision,
                              sparring_mode=True)
                game.board = position.copy(stack=True)
                game._sync_evaluation()
                game._save()
                start = game.state()
                assert start["engine_to_move"] and start["engine"]["sparring"]["phase"] == "finish"
                assert start["engine"]["sparring_analysis_only"]
                assert start["engine"]["extra_budget_ms"] == 500
                offset, started = len(searches), time.monotonic()
                state = game.engine_move(game.revision)
                elapsed = round((time.monotonic() - started) * 1000, 3)
                assert state["error"] is None and len(game.board.move_stack) == ply + 1, state["error"]
                receipt = game._sparring_decisions[ply + 1]
                trace = searches[offset:]
                baseline = trace[0]["native_bestmove"]
                assert trace[0]["ply"] == ply and trace[0]["requested_ms"] == 1000
                assert trace[0]["root_moves"] is None
                assert state["last_move"] == baseline == receipt["baseline_uci"] == receipt["selected_uci"]
                assert chess.Move.from_uci(baseline) in position.legal_moves
                assert receipt["reason"] == "finish_full_strength" and receipt["phase"] == "finish"
                assert receipt["analysis_only"] and receipt["candidate_budget_ms"] == 0
                assert receipt["probes"] == receipt["discovery"] == []
                assert receipt["quality_budget_ms"] == receipt["extra_budget_ms"] == 500
                assert not receipt["band_control"]["active"]
                # Extra native calls assess the previous human turn only.
                assert len(trace) > 1 and all(call["ply"] == ply - 1 for call in trace[1:])
                observation = receipt["opponent_observation"]
                rating = state["engine"]["sparring_rating"]
                if observation["valid"] and not observation.get("forced"):
                    assert rating["observations"] == 1 and rating["updated_human_ply"] == ply
                else:
                    assert rating["observations"] == 0
                cases.append({"engine_color":"white" if position.turn else "black", "move_number":50,
                    "fixture_prefix_plies":ply, "fixture_fen":position.fen(en_passant="fen"),
                    "native_baseline_uci":baseline, "selected_uci":state["last_move"],
                    "baseline_preserved":True, "concession_candidate_searches":0,
                    "native_search_trace":trace, "elapsed_ms":elapsed,
                    "rating_observation":observation, "rating_state":rating, "decision":receipt})
        finally:
            game.close()
            assert game._engine is None
    after = hashes()
    assert before == after, "Sources changed during native verification"
    result = {"schema":"regicide.sparring-fifty-native.v1", "passed":True,
        "created_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "binary":str(binary), "binary_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),
        "source_commit":subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256":before, "source_sha256_after":after,
        "fixture_history_uci":LATE_GAME_HISTORY_UCI, "cases":cases, "engine_closed":True,
        "scope":"Two real native host turns from fixed legal standard-start histories at 98/99 plies; earlier moves are fixture replay, not newly played games. Descriptive rating analysis continues, with validity checked rather than invented. No strength or rating accuracy claim."}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed":True, "cases":len(cases), "rating_observations":[
        case["rating_state"]["observations"] for case in cases], "output":str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args().output.resolve())
