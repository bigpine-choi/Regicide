"""Independent, read-only replay of controlled-match JSON and PGN evidence."""
import collections
import hashlib
import io
import json
from pathlib import Path
import statistics
import subprocess
import sys

import chess
import chess.pgn

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "CMakeLists.txt").is_file() and (parent / "src/board.cpp").is_file())


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(actual, expected, context):
    assert actual == expected, (context, actual, expected)


def resources(moves):
    return {"searched_moves": len(moves),
            "reported_nodes": sum(m["nodes"] for m in moves),
            "moves_without_node_report": 0,
            "elapsed_seconds": round(sum(m["elapsed_seconds"] for m in moves), 6)}


def stats(moves, requested_ms, effective_ms):
    return {"searched_moves": len(moves),
            "mean_depth": statistics.mean(m["depth"] for m in moves),
            "mean_nodes": statistics.mean(m["nodes"] for m in moves),
            "mean_seldepth": statistics.mean(m["seldepth"] for m in moves),
            "mean_engine_time_ms": statistics.mean(m["engine_time_seconds"] * 1000 for m in moves),
            "max_engine_time_ms": max(m["engine_time_seconds"] * 1000 for m in moves),
            "mean_wall_time_ms": statistics.mean(m["elapsed_seconds"] * 1000 for m in moves),
            "max_wall_time_ms": max(m["elapsed_seconds"] * 1000 for m in moves),
            "wall_over_requested_budget_moves": sum(m["elapsed_seconds"] * 1000 > requested_ms + 1e-9 for m in moves),
            "engine_over_effective_allowance_moves": sum(m["engine_time_seconds"] * 1000 > effective_ms + 1e-9 for m in moves),
            "total_nodes": sum(m["nodes"] for m in moves),
            "total_wall_seconds": round(sum(m["elapsed_seconds"] for m in moves), 6)}


def audit(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    check(data["exit_code"], 0, "successful exit")
    check(data["completed"], True, "all games completed")
    check(data["errors"], [], "no errors")
    check(data["harness_checkout"]["status_porcelain"], "", "clean checkout recorded at start")
    check(data["limits"]["nodes"], None, "no node cap")
    check(data["limits"]["depth"], None, "no depth cap")
    assert data["limits"]["movetime_ms"] in [40, 100]
    check(data["draw_policy"], "claim", "claim policy")
    check(len(data["selected_openings"]), 8, "eight fixed openings")
    check(data["games_requested"], 16, "sixteen scheduled games")
    check(len(data["games"]), 16, "sixteen recorded games")
    check(digest(Path(data["opening_source"])), data["opening_file_sha256"], "opening file SHA256")
    source_checks = {}
    for role, engine in data["engines"].items():
        check(engine["requested_options"], {}, role + " uses advertised defaults")
        for option in ["UsePositionalEvaluation", "UseQuiescence"]:
            check(engine["advertised_options"][option]["default"], True, role + " " + option)
        check(engine["advertised_options"]["Move Overhead"]["default"], 20, role + " 20ms guard")
        if role == "candidate":
            for option in ["UseTranspositionTable", "UseFastMoves"]:
                check(engine["advertised_options"][option]["default"], True, "candidate " + option)
            check(engine["advertised_options"]["Hash"]["default"], 16, "candidate 16MiB table")
        check(digest(Path(engine["command"][0])), engine["sha256"], role + " current executable SHA256")
        commit = engine["provided_source_commit"]
        check(git("rev-parse", "--verify", commit + "^{commit}").decode().strip(), commit, role + " commit object")
        source_root = Path(engine["command"][0]).parents[1]
        files = git("ls-tree", "-r", "--name-only", commit, "include", "src", "CMakeLists.txt", "scripts/build.ps1").decode().splitlines()
        for relative in files:
            expected = git("show", commit + ":" + relative).replace(b"\r\n", b"\n")
            actual = (source_root / relative).read_bytes().replace(b"\r\n", b"\n")
            check(actual, expected, role + " source " + relative)
        source_checks[role] = {"source_commit": commit, "binary_sha256": engine["sha256"],
                               "source_files_compared_to_commit": len(files),
                               "source_root": str(source_root),
                               "limit": "Source files and recorded binary hashes verified; this audit does not independently rebuild the executable."}
    pgn_stream = io.StringIO(path.with_suffix(".pgn").read_text(encoding="utf-8"))
    all_moves = []
    total_plies = pv_plies = 0
    terminations = collections.Counter()
    points = []
    claims = []
    backtracks = {role: {"count": 0, "opportunities": 0} for role in ["baseline", "candidate"]}
    for number, record in enumerate(data["games"], 1):
        game = chess.pgn.read_game(pgn_stream)
        assert game is not None and not game.errors
        check(record["game"], number, "game order")
        check(record["pair"], (number + 1) // 2, "pair order")
        opening = data["selected_openings"][(number - 1) // 2]
        check(record["opening"], opening["name"], "opening name")
        check(record["opening_uci_moves"], opening["moves"], "opening prefix")
        check(record["white"], "candidate" if number % 2 else "baseline", "balanced colors")
        check(record["black"], "baseline" if number % 2 else "candidate", "balanced colors")
        check(game.headers["Pair"], str(record["pair"]), "PGN pair")
        check(game.headers["RequestedMoveTimeMS"], str(data["limits"]["movetime_ms"]), "PGN time")
        check(game.headers["RequestedNodes"], "-", "PGN no node cap")
        check(game.headers["RequestedDepth"], "-", "PGN no depth cap")
        check(game.headers["Result"], record["result"], "PGN result")
        check(game.headers["TerminationReason"], record["termination"], "PGN termination")
        check(game.headers["Termination"], "normal", "PGN completed ending")
        check(game.headers["OpeningPlies"], str(len(opening["moves"])), "PGN opening count")
        check(game.headers["Opening"], opening["name"], "PGN opening")
        check(game.headers["DrawPolicy"], "claim", "PGN draw policy")
        for color in ["White", "Black"]:
            engine = data["engines"][record[color.lower()]]
            check(game.headers[color + "SHA256"], engine["sha256"], "PGN binary hash")
            check(game.headers[color + "SourceCommit"], engine["provided_source_commit"], "PGN source commit")
        pgn_moves = [m.uci() for m in game.mainline_moves()]
        check(pgn_moves, record["uci_moves"], "exact PGN/JSON moves")
        check(len(pgn_moves), record["plies"], "ply count")
        check(pgn_moves[:len(opening["moves"])], opening["moves"], "actual opening prefix")
        board = game.board()
        check(board.fen(en_passant="fen"), record["initial_fen"], "initial FEN")
        previous = {True: None, False: None}
        selected = 0
        game_backtracks = {role: {"count": 0, "opportunities": 0} for role in backtracks}
        for ply, text in enumerate(pgn_moves, 1):
            assert board.outcome(claim_draw=True) is None, (number, ply, "move after claimable ending")
            move = chess.Move.from_uci(text)
            assert move in board.legal_moves, (number, ply, text)
            if ply > len(opening["moves"]):
                item = record["search_moves"][selected]
                selected += 1
                role = record["white" if board.turn else "black"]
                check(item["ply"], ply, "search ply")
                check(item["role"], role, "search role")
                check(item["color"], "white" if board.turn else "black", "search color")
                check(item["move"], text, "selected move")
                check(item["fen_before"], board.fen(en_passant="fen"), "search FEN")
                assert isinstance(item["nodes"], int) and item["nodes"] > 0
                assert isinstance(item["depth"], int) and item["depth"] >= 0
                assert item["elapsed_seconds"] >= 0 and item["engine_time_seconds"] >= 0
                variation = board.copy()
                if item["pv"]:
                    check(item["pv"][0], text, "PV starts with selected move")
                for pv_text in item["pv"]:
                    pv_move = chess.Move.from_uci(pv_text)
                    assert pv_move in variation.legal_moves, (number, ply, item["pv"], pv_text)
                    variation.push(pv_move)
                    pv_plies += 1
                piece = board.piece_type_at(move.from_square)
                eligible = piece != chess.PAWN and not board.is_capture(move) and not board.is_check()
                prior = previous[board.turn]
                opportunity = bool(eligible and prior is not None and prior[0])
                reversal = bool(opportunity and piece == prior[1] and move.from_square == prior[3] and move.to_square == prior[2])
                check(item["quiet_nonpawn_not_in_check"], eligible, "backtrack eligibility")
                check(item["backtrack_opportunity"], opportunity, "backtrack opportunity")
                check(item["immediate_backtrack"], reversal, "backtrack reversal")
                game_backtracks[role]["opportunities"] += opportunity
                game_backtracks[role]["count"] += reversal
                previous[board.turn] = (eligible, piece, move.from_square, move.to_square)
                all_moves.append(item)
            board.push(move)
            if ply == len(opening["moves"]):
                check(board.fen(en_passant="fen"), opening["fen_after_opening"], "opening FEN")
        check(selected, len(record["search_moves"]), "searched move count")
        check(board.fen(en_passant="fen"), record["final_fen"], "final FEN")
        outcome = board.outcome(claim_draw=True)
        assert outcome is not None
        check(outcome.result(), record["result"], "independent result")
        check(outcome.termination.name.lower(), record["termination"], "independent termination")
        assert record["completed"] and record["error"] is None and record["plies"] < data["max_plies"]
        value = 0.5 if outcome.winner is None else float((outcome.winner == chess.WHITE) == (record["white"] == "candidate"))
        check(value, record["candidate_points"], "candidate points")
        points.append(value)
        if outcome.termination == chess.Termination.THREEFOLD_REPETITION:
            intended = []
            if not board.is_repetition(3):
                for move in list(board.legal_moves):
                    board.push(move)
                    if board.is_repetition(3):
                        intended.append(move.uci())
                    board.pop()
                assert intended
            claims.append({"game": number, "current_threefold": board.is_repetition(3), "intended_moves": intended})
        check(game_backtracks, record["backtracks"], "game backtrack totals")
        for role in backtracks:
            for key in backtracks[role]:
                backtracks[role][key] += game_backtracks[role][key]
            check(resources([m for m in record["search_moves"] if m["role"] == role]), record["resources"][role], "game resources")
        total_plies += len(pgn_moves)
        terminations[record["termination"]] += 1
    assert chess.pgn.read_game(pgn_stream) is None
    wdl = [points.count(1), points.count(0.5), points.count(0)]
    summary = data["summary"]
    check(wdl, [summary["candidate_wins"], summary["candidate_draws"], summary["candidate_losses"]], "reported WDL")
    check(sum(points), summary["candidate_points"], "total points")
    check(sum(points) / 16, summary["candidate_score"], "score rate")
    role_stats = {}
    for role in backtracks:
        selected = [m for m in all_moves if m["role"] == role]
        check(resources(selected), summary["resources"][role], "summary resources")
        for key, value in backtracks[role].items():
            check(value, summary["backtracks"][role][key], "summary backtracks")
        role_stats[role] = stats(selected, data["limits"]["movetime_ms"], data["limits"]["movetime_ms"] - 20)
    return {"file": str(path), "status": "PASS", "games": 16, "paired_openings": 8,
            "limits": data["limits"], "total_pgn_plies": total_plies,
            "engine_selected_plies": len(all_moves), "pv_plies": pv_plies,
            "candidate_wdl": wdl, "candidate_score": sum(points) / 16,
            "options_verified": {"both": {"Move Overhead": 20, "UsePositionalEvaluation": True, "UseQuiescence": True},
                                 "candidate": {"UseTranspositionTable": True, "Hash": 16, "UseFastMoves": True},
                                 "requested_options": {}},
            "terminations": dict(terminations), "claims": claims,
            "statistics": role_stats, "source_checks": source_checks,
            "recorded_harness_checkout": data["harness_checkout"],
            "audit_current_checkout": {"commit": git("rev-parse", "HEAD").decode().strip(),
                                       "status_porcelain": git("status", "--porcelain").decode().strip()},
            "time_note": "Requested movetime is equal. Both engines default to Move Overhead=20ms; effective search allowance is requested time minus 20ms. Reported and wall times are measured separately."}


if __name__ == "__main__":
    reports = []
    for argument in sys.argv[1:]:
        source = Path(argument).resolve(strict=True)
        report = audit(source)
        output = source.with_suffix(".independent-audit.json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if len(reports) > 1:
        combined = {"status": "PASS", "games": sum(r["games"] for r in reports),
                    "total_pgn_plies": sum(r["total_pgn_plies"] for r in reports),
                    "engine_selected_plies": sum(r["engine_selected_plies"] for r in reports),
                    "pv_plies": sum(r["pv_plies"] for r in reports),
                    "conditions": reports}
        for report in reports[1:]:
            check(report["source_checks"], reports[0]["source_checks"], "same binaries and source commits across time conditions")
            check(report["options_verified"], reports[0]["options_verified"], "same options across time conditions")
        (Path(__file__).parent / "independent-audit-summary.json").write_text(
            json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("COMBINED", json.dumps({key: value for key, value in combined.items() if key != "conditions"}))
