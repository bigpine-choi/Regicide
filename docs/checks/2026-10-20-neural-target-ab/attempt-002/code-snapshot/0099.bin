"""End-to-end self-play checks; both players are the supplied Regicide binary."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import chess
import chess.pgn


def check_outputs(path, expected_code, expected_results=None, expected_termination=None):
    data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["exit_code"] == expected_code, data
    assert data["engine_ids"]["white"]["name"].startswith("Regicide"), data["engine_ids"]
    assert data["engine_ids"]["black"]["name"].startswith("Regicide"), data["engine_ids"]
    assert data["chess_version"] == chess.__version__, data
    assert len(data["binary_sha256"]) == 64, data
    source = io.StringIO(path.read_text(encoding="utf-8"))
    for index, record in enumerate(data["games"]):
        game = chess.pgn.read_game(source)
        assert game is not None and not game.errors, (path, index, game)
        board = game.board()
        assert board.fen(en_passant="fen") == record["initial_fen"], record
        sequence = []
        for move in game.mainline_moves():
            assert board.outcome(claim_draw=data["draw_policy"] == "claim") is None, record
            assert move in board.legal_moves, (record, move)
            sequence.append(move.uci())
            board.push(move)
        assert sequence == record["uci_moves"], record
        assert len(sequence) == record["plies"], record
        assert board.fen(en_passant="fen") == record["final_fen"], record
        assert game.headers["Result"] == record["result"], record
        assert game.headers["TerminationReason"] == record["termination"], record
        outcome = board.outcome(claim_draw=data["draw_policy"] == "claim")
        if record["completed"]:
            assert outcome is not None, record
            assert outcome.result() == record["result"], record
            assert outcome.termination.name.lower() == record["termination"], record
        else:
            assert record["result"] == "*", record
            assert game.headers["Termination"] == "unterminated", record
        if expected_results is not None:
            assert record["result"] == expected_results[index], record
        if expected_termination is not None:
            assert record["termination"] == expected_termination, record
    assert chess.pgn.read_game(source) is None, path
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve()
    runner = Path(__file__).resolve().parents[1] / "scripts" / "play_game.py"
    passed = 0

    with tempfile.TemporaryDirectory(prefix="regicide-games-") as folder:
        output = Path(folder)

        def run(name, *options, code=0, results=None, termination=None):
            nonlocal passed
            path = output / name / "game.pgn"
            command = [sys.executable, str(runner), str(binary), "--pgn", str(path), *options]
            result = subprocess.run(command, capture_output=True, text=True, timeout=240)
            assert result.returncode == code, (command, result.returncode, result.stdout, result.stderr)
            data = check_outputs(path, code, results, termination)
            assert data["engine_command"] == [str(binary), "uci"], data
            with binary.open("rb") as stream:
                assert data["binary_sha256"] == hashlib.file_digest(stream, "sha256").hexdigest(), data
            assert data["completed"] == all(game["completed"] for game in data["games"]), data
            print(f"PASS {name}: " + ", ".join(
                f"{g['result']} {g['termination']} ({g['plies']} plies)" for g in data["games"]), flush=True)
            passed += 1
            return path, data, command

        # Startpos must genuinely reach a rules-based ending, not a move-cap adjudication.
        path, data, command = run("startpos", "--depth", "2", "--games", "2")
        assert data["completed"] and data["limits"]["depth"] == 2, data
        assert all(game["plies"] > 0 for game in data["games"]), data
        assert data["games"][0]["uci_moves"] == data["games"][1]["uci_moves"], data

        # Refusal must leave BOTH existing files byte-for-byte unchanged.
        before = (path.read_bytes(), path.with_suffix(".json").read_bytes())
        refused = subprocess.run(command, capture_output=True, text=True, timeout=15)
        assert refused.returncode != 0 and "already exists" in refused.stderr, refused
        assert before == (path.read_bytes(), path.with_suffix(".json").read_bytes()), path
        passed += 1
        print("PASS refuse overwrite", flush=True)

        run("white-mate", "--fen", "7k/5K2/6Q1/8/8/8/8/8 w - - 0 1",
            results=["1-0"], termination="checkmate")
        run("black-mate", "--fen", "8/8/8/8/8/6q1/5k2/7K b - - 0 1",
            results=["0-1"], termination="checkmate")
        _, draw, _ = run("forced-draw", "--fen", "8/8/8/8/8/8/5k1r/7K w - - 0 1",
                         results=["1/2-1/2"], termination="insufficient_material")
        assert draw["games"][0]["uci_moves"] == ["h1h2"], draw
        _, auto, _ = run("automatic-75", "--draw-policy", "automatic", "--fen",
                         "7k/8/8/8/8/8/R7/4K3 w - - 149 1",
                         results=["1/2-1/2"], termination="seventyfive_moves")
        assert auto["games"][0]["plies"] == 1, auto
        run("claim-50", "--fen", "7k/8/8/8/8/8/R7/4K3 w - - 100 1",
            results=["1/2-1/2"], termination="fifty_moves")
        _, capped, _ = run("cap", "--max-plies", "1", code=2, results=["*"], termination="ply_cap")
        assert not capped["completed"] and capped["games"][0]["plies"] == 1, capped
        run("terminal-stalemate", "--fen", "7k/5K2/6Q1/8/8/8/8/8 b - - 0 1",
            results=["1/2-1/2"], termination="stalemate")
        run("terminal-mate", "--fen", "7k/5KQ1/8/8/8/8/8/8 b - - 0 1",
            results=["1-0"], termination="checkmate")

        # A JSON-only collision also protects the companion output and creates no PGN.
        collision = output / "collision.pgn"
        collision.with_suffix(".json").write_text("preserve", encoding="utf-8")
        refused = subprocess.run([sys.executable, str(runner), str(binary), "--pgn", str(collision)],
                                 capture_output=True, text=True, timeout=15)
        assert refused.returncode != 0 and not collision.exists(), refused
        assert collision.with_suffix(".json").read_text(encoding="utf-8") == "preserve", collision
        passed += 1
        print("PASS refuse companion collision", flush=True)

    print(f"PASS game runner: {passed} cases; complete PGN/JSON replay verified; chess={chess.__version__}")


if __name__ == "__main__":
    main()
