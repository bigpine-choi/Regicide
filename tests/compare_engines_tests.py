"""Cheap controlled-match regression checks, plus one real paired-color replay.

Usage: python tests/compare_engines_tests.py path/to/regicide
The real games intentionally stop at a six-ply cap. Completion and scoring are
tested separately with deterministic scripted legal mates, avoiding a slow match.
"""

import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile

import chess
import chess.engine
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("compare_engines", ROOT / "scripts/compare_engines.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class ScriptedEngine:
    def __init__(self, moves):
        self.moves = moves
        self.calls = []

    def play(self, board, limit, **kwargs):
        self.calls.append((list(move.uci() for move in board.move_stack), limit, kwargs))
        move = chess.Move.from_uci(self.moves[board.ply()])
        return chess.engine.PlayResult(move, None, info={
            "nodes": 7, "depth": 1, "score": chess.engine.PovScore(chess.engine.Cp(12), board.turn)})


def fake_game(opening, moves, candidate_white=True, cap=500):
    args = SimpleNamespace(nodes=50, depth=None, movetime=None, max_plies=cap, draw_policy="claim")
    record = RUNNER.new_record(1, 1, opening, candidate_white)
    engines = {role: ScriptedEngine(moves) for role in ("baseline", "candidate")}
    RUNNER.play_one(args, engines, record)
    return record, engines


def opening(name, moves):
    board = chess.Board()
    for text in moves:
        board.push_uci(text)
    return {"name": name, "moves": moves, "fen_after_opening": board.fen(en_passant="fen")}


def check_replay(path, code):
    data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["exit_code"] == code, data
    source = io.StringIO(path.read_text(encoding="utf-8"))
    for record in data["games"]:
        game = chess.pgn.read_game(source)
        assert game is not None and not game.errors, game
        board = game.board()
        assert board.fen(en_passant="fen") == record["initial_fen"], record
        moves = []
        for move in game.mainline_moves():
            assert move in board.legal_moves, (record, move)
            moves.append(move.uci())
            board.push(move)
        assert moves == record["uci_moves"], record
        assert moves[:len(record["opening_uci_moves"])] == record["opening_uci_moves"], record
        assert board.fen(en_passant="fen") == record["final_fen"], record
        assert game.headers["Result"] == record["result"], record
        assert game.headers["TerminationReason"] == record["termination"], record
        if record["completed"]:
            outcome = board.outcome(claim_draw=data["draw_policy"] == "claim")
            assert outcome is not None and outcome.result() == record["result"], record
        else:
            assert record["result"] == "*" and game.headers["Termination"] == "unterminated", record
        offset = len(record["opening_uci_moves"])
        assert len(record["search_moves"]) == record["plies"] - offset, record
        for index, telemetry in enumerate(record["search_moves"], offset):
            assert telemetry["ply"] == index + 1, telemetry
            assert telemetry["move"] == record["uci_moves"][index], telemetry
            assert telemetry["nodes"] is not None, telemetry
            assert telemetry["elapsed_seconds"] >= 0, telemetry
    assert chess.pgn.read_game(source) is None
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve()
    subprocess.run([sys.executable, str(ROOT / "tests/external_baseline_tests.py"), str(binary)],
                   check=True, timeout=90)
    passed = 0
    _, declared = RUNNER.load_openings(ROOT / "tests/data/comparison_openings.json")
    assert len(declared) == 8 and declared[0]["moves"] == [], declared
    assert len({item["name"] for item in declared}) == 8
    assert len(RUNNER.load_openings(ROOT / "tests/data/comparison_openings.json", 4)[1]) == 4
    print("PASS eight predeclared legal opening lines and prefix selection", flush=True)
    passed += 1

    # Full opening history reaches engine.play, preserving repetition information.
    first = opening("Scripted Fool's Mate", ["f2f3", "e7e5"])
    sequence = ["f2f3", "e7e5", "g2g4", "d8h4"]
    white_record, white_engines = fake_game(first, sequence)
    black_record, _ = fake_game(first, sequence, candidate_white=False)
    black_record.update(game=2)
    assert white_engines["candidate"].calls[0][0] == first["moves"]
    assert white_record["result"] == black_record["result"] == "0-1"
    assert white_record["candidate_points"] == 0 and black_record["candidate_points"] == 1
    assert white_record["termination"] == "checkmate" and white_record["completed"]
    assert white_record["resources"]["candidate"]["reported_nodes"] == 7
    assert white_record["search_moves"][0]["score"]["pov"] == "mover"
    assert white_record["search_moves"][0]["score"]["cp"] == 12
    summary = RUNNER.summarize([white_record, black_record], [first])
    assert (summary["candidate_wins"], summary["candidate_draws"], summary["candidate_losses"]) == (1, 0, 1)
    assert summary["candidate_points"] == 1 and summary["candidate_score"] == 0.5
    assert summary["pairs"][0]["completed"] and summary["pairs"][0]["candidate_points"] == 1
    assert summary["wilson95_descriptive"][0] < 0.5 < summary["wilson95_descriptive"][1]
    print("PASS preserved opening history, legal checkmate, paired colors, scoring and per-move resources", flush=True)
    passed += 1

    # Moving a knight out and back on its very next turn triggers two opportunities.
    reversals = ["g1f3", "g8f6", "f3g1", "f6g8"]
    reversed_record, _ = fake_game(declared[0], reversals, cap=4)
    assert reversed_record["backtracks"] == {
        "baseline": {"count": 1, "opportunities": 1},
        "candidate": {"count": 1, "opportunities": 1}}, reversed_record
    assert reversed_record["result"] == "*" and not reversed_record["completed"]
    capped_summary = RUNNER.summarize([reversed_record], [declared[0]])
    assert capped_summary["candidate_draws"] == 0 and capped_summary["candidate_score"] is None
    assert capped_summary["incomplete_games"] == 1 and not capped_summary["pairs"][0]["completed"]
    assert reversed_record["termination"] == "ply_cap"
    print("PASS precisely defined backtrack count and unfinished-game exclusion", flush=True)
    passed += 1

    # Pawn moves are excluded; reversed book moves are excluded from engine metrics.
    book = opening("Book knight", ["g1f3", "g8f6"])
    book_record, _ = fake_game(book, reversals, cap=4)
    assert all(item["opportunities"] == 0 for item in book_record["backtracks"].values()), book_record
    pawn_record, _ = fake_game(declared[0], ["e2e4", "e7e5", "g1f3", "g8f6"], cap=4)
    assert all(item["opportunities"] == 0 for item in pawn_record["backtracks"].values()), pawn_record
    print("PASS opening moves and pawn moves excluded from backtrack opportunities", flush=True)
    passed += 1

    # The runner explicitly claims repetitions, including announcing a next move.
    repetition, _ = fake_game(declared[0], reversals * 2)
    assert repetition["result"] == "1/2-1/2" and repetition["completed"], repetition
    assert repetition["termination"] == "threefold_repetition" and repetition["plies"] == 7
    repetition_summary = RUNNER.summarize([repetition], [declared[0]])
    assert repetition_summary["candidate_draws"] == 1 and repetition_summary["candidate_score"] == 0.5
    assert repetition_summary["unplayed_games"] == 1
    print("PASS independently claimed repetition is distinguished from a cap", flush=True)
    passed += 1

    # An illegal move must leave the exact last legal position and explicit error evidence.
    broken = {role: ScriptedEngine(["e2e5"]) for role in ("baseline", "candidate")}
    broken_record = RUNNER.new_record(1, 1, declared[0], True)
    try:
        RUNNER.play_one(SimpleNamespace(nodes=50, depth=None, movetime=None,
                                       max_plies=4, draw_policy="claim"), broken, broken_record)
        raise AssertionError("illegal move accepted")
    except RuntimeError:
        pass
    assert broken_record["error"] and broken_record["termination"] == "engine_error"
    assert broken_record["uci_moves"] == [] and broken_record["final_fen"] == chess.STARTING_FEN
    print("PASS illegal engine move preserves legal prefix and error evidence", flush=True)
    passed += 1

    # Pair bootstrap has a fixed seed and resamples whole color pairs, never single games.
    second_white = dict(white_record, game=3, pair=2, candidate_points=1.0, result="1-0")
    second_black = dict(black_record, game=4, pair=2, candidate_points=1.0, result="0-1")
    pair_summary = RUNNER.summarize([white_record, black_record, second_white, second_black],
                                  [first, dict(first, name="second")])
    bootstrap = pair_summary["paired_opening_bootstrap95_descriptive"]
    assert bootstrap["seed"] == 20261017 and bootstrap["completed_pairs"] == 2
    assert bootstrap["interval"] == [0.5, 1.0], bootstrap
    print("PASS paired bootstrap resamples complete opening pairs deterministically", flush=True)
    passed += 1

    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="regicide-comparison-") as directory:
        output = Path(directory)
        path = output / "paired.pgn"
        command = [sys.executable, str(ROOT / "scripts/compare_engines.py"), str(binary), str(binary),
                   "--baseline-commit", commit, "--candidate-commit", commit,
                   "--pgn", str(path), "--nodes", "50", "--opening-count", "2", "--max-plies", "6"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
        data = check_replay(path, 2)
        assert len(data["games"]) == 4 and data["games_requested"] == 4
        assert [item["white"] for item in data["games"]] == ["candidate", "baseline"] * 2
        assert [item["pair"] for item in data["games"]] == [1, 1, 2, 2]
        assert data["games"][2]["opening_uci_moves"] == declared[1]["moves"]
        assert data["summary"]["completed_games"] == 0 and data["summary"]["incomplete_games"] == 4
        assert data["summary"]["candidate_draws"] == 0 and data["summary"]["candidate_score"] is None
        assert data["limits"] == {"nodes": 50, "depth": None, "movetime_ms": None}
        with binary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        for item in data["engines"].values():
            assert item["sha256"] == digest and item["provided_source_commit"] == commit
            assert item["source_commit_object_verified"] and item["engine_id"]["name"].startswith("Regicide")
            assert item["command"] == [str(binary), "uci"]
        assert "status_porcelain" in data["harness_checkout"]
        print("PASS real UCI match, paired assignments, full legal PGN/JSON replay and separate provenance", flush=True)
        passed += 1

        before = (path.read_bytes(), path.with_suffix(".json").read_bytes())
        refused = subprocess.run(command, capture_output=True, text=True, timeout=15)
        assert refused.returncode == 2 and "already exists" in refused.stderr, refused
        assert before == (path.read_bytes(), path.with_suffix(".json").read_bytes())
        conflict = subprocess.run([*command, "--depth", "2"], capture_output=True, text=True, timeout=15)
        assert conflict.returncode == 2 and "not allowed" in conflict.stderr, conflict
        print("PASS existing evidence protection and exclusive search budgets", flush=True)
        passed += 1

        # Configuration failure after engine startup still emits replayable records.
        error_path = output / "error.pgn"
        error_command = list(command)
        error_command[error_command.index(str(path))] = str(error_path)
        error_command.extend(["--candidate-options", '{"NonexistentOption":true}'])
        failed = subprocess.run(error_command, capture_output=True, text=True, timeout=30)
        assert failed.returncode == 2, failed
        error_data = check_replay(error_path, 2)
        assert error_data["errors"] and error_data["games"][0]["termination"] == "engine_error"
        assert error_data["games"][0]["uci_moves"] == []
        error_summary = error_data["summary"]
        assert error_summary["scheduled_games"] == 4 and error_summary["attempted_games"] == 1
        assert error_summary["completed_games"] == 0 and error_summary["incomplete_games"] == 1
        assert error_summary["unplayed_games"] == 3
        assert (error_summary["completed_games"] + error_summary["incomplete_games"]
                + error_summary["unplayed_games"]) == error_summary["scheduled_games"]
        print("PASS startup/configuration errors persist PGN and JSON evidence", flush=True)
        passed += 1

    print(f"PASS comparison runner: {passed} cases; chess={chess.__version__}")


if __name__ == "__main__":
    main()