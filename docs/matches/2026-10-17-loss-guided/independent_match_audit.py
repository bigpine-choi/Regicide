"""Independent, read-only replay of controlled-match JSON and PGN evidence."""
import argparse
import collections
import datetime
import hashlib
import io
import json
import random
import re
from pathlib import Path
import statistics
import subprocess
import sys

import chess
import chess.pgn

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "CMakeLists.txt").is_file() and (parent / "src/board.cpp").is_file())
EXPECTED_COMMITS = {"baseline": "a31091548033d538e01d2c194f05bada403d5263", "candidate": None}
EXPECTED_VERSIONS = {"baseline": "Regicide 0.4.1", "candidate": "Regicide 0.5.0"}
PLAN_COMMIT = "f4c5e03d2440618aa3e178932ed3e3a708561359"


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
    if not moves:
        return {"searched_moves": 0, "total_nodes": 0, "total_wall_seconds": 0,
                "note": "No returned searches; failed requests are preserved separately."}
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


def audit(path, opening_path, dataset_role, expected_harness, movetime):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["exit_code"] in [0, 2]
    check(data["completed"], len(data["games"]) == 16 and all(game["completed"] for game in data["games"]), "recorded run completion")
    if data["completed"]:
        check(data["exit_code"], 0, "successful exit")
        check(data["errors"], [], "no errors")
    else:
        check(data["exit_code"], 2, "incomplete run exit")
        assert data["errors"] or any(game["termination"] == "ply_cap" for game in data["games"]), "incomplete run must expose an error or cap"
    check(data["harness_checkout"]["status_porcelain"], "", "clean checkout recorded at start")
    check(data["limits"]["nodes"], None, "no node cap")
    check(data["limits"]["depth"], None, "no depth cap")
    check(data["limits"]["movetime_ms"], movetime, "predeclared requested time")
    check(data["max_plies"], 500, "predeclared ply cap")
    check(data["draw_policy"], "claim", "claim policy")
    check(len(data["selected_openings"]), 8, "eight fixed openings")
    check(data["games_requested"], 16, "sixteen scheduled games")
    assert 1 <= len(data["games"]) <= 16, "attempted game count"
    if data["completed"]:
        check(len(data["games"]), 16, "sixteen completed scheduled games")
    check(Path(data["opening_source"]).resolve(), opening_path, "explicit expected dataset")
    check(digest(Path(data["opening_source"])), data["opening_file_sha256"], "opening file SHA256")
    original_openings = json.loads(opening_path.read_text(encoding="utf-8"))["openings"]
    check(len(original_openings), 8, "whole predeclared dataset")
    relative_opening = opening_path.relative_to(ROOT).as_posix()
    check(json.loads(git("show", PLAN_COMMIT + ":" + relative_opening))["openings"], original_openings, "dataset frozen before implementation")
    check(json.loads(git("show", EXPECTED_COMMITS["candidate"] + ":" + relative_opening))["openings"], original_openings, "candidate did not change held-out or development data")
    for supplied, original in zip(data["selected_openings"], original_openings[:8]):
        check(supplied["name"], original["name"], "predeclared opening order")
        check(supplied["moves"], original["moves"], "predeclared opening moves")
        opening_board = chess.Board()
        for move in original["moves"]:
            opening_board.push_uci(move)
        check(supplied["fen_after_opening"], opening_board.fen(en_passant="fen"), "predeclared opening FEN")
    source_checks = {}
    check(data["harness_checkout"]["commit"], expected_harness, "frozen harness commit")
    check(git("show", expected_harness + ":scripts/compare_engines.py").replace(b"\r\n", b"\n"),
          (ROOT / "scripts/compare_engines.py").read_bytes().replace(b"\r\n", b"\n"), "frozen comparison tool")
    for role, engine in data["engines"].items():
        check(engine["provided_source_commit"], EXPECTED_COMMITS[role], role + " frozen source commit")
        check(engine["engine_id"]["name"], EXPECTED_VERSIONS[role], role + " version")
        check(engine["requested_options"], {}, role + " uses advertised defaults")
        for option in ["UsePositionalEvaluation", "UseQuiescence", "UseTranspositionTable", "UseFastMoves"]:
            check(engine["advertised_options"][option]["default"], True, role + " " + option)
        check(engine["advertised_options"]["Move Overhead"]["default"], 20, role + " 20ms guard")
        check(engine["advertised_options"]["Hash"]["default"], 16, role + " 16MiB table")
        check(engine["source_commit_object_verified"], True, role + " source provenance flag")
        if role == "candidate":
            for option in ["UseQuietMoveOrdering", "UseQuiescenceChecks"]:
                check(engine["advertised_options"][option]["type"], "check", option + " type")
                check(engine["advertised_options"][option]["default"], True, option + " candidate default")
            check(engine["advertised_options"]["Clear Hash"]["type"], "button", "candidate Clear Hash button")
            check(engine["advertised_options"]["Clear Hash"]["default"], None, "candidate Clear Hash default")
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
    incomplete_games = []
    backtracks = {role: {"count": 0, "opportunities": 0} for role in ["baseline", "candidate"]}
    for number, record in enumerate(data["games"], 1):
        game = chess.pgn.read_game(pgn_stream)
        assert game is not None and not game.errors
        check(record["game"], number, "game order")
        check(record["pair"], (number + 1) // 2, "pair order")
        opening = data["selected_openings"][(number - 1) // 2]
        check(record["opening"], opening["name"], "opening name")
        check(record["opening_uci_moves"], opening["moves"], "opening prefix")
        check(record["opening_fen"], opening["fen_after_opening"], "recorded opening FEN")
        check(record["white"], "candidate" if number % 2 else "baseline", "balanced colors")
        check(record["black"], "baseline" if number % 2 else "candidate", "balanced colors")
        check(game.headers["Pair"], str(record["pair"]), "PGN pair")
        check(game.headers["RequestedMoveTimeMS"], str(data["limits"]["movetime_ms"]), "PGN time")
        check(game.headers["RequestedNodes"], "-", "PGN no node cap")
        check(game.headers["RequestedDepth"], "-", "PGN no depth cap")
        check(game.headers["Result"], record["result"], "PGN result")
        check(game.headers["TerminationReason"], record["termination"], "PGN termination")
        check(game.headers["Termination"], "normal" if record["completed"] else "unterminated", "PGN completion")
        check(game.headers["OpeningPlies"], str(len(opening["moves"])), "PGN opening count")
        check(game.headers["Opening"], opening["name"], "PGN opening")
        check(game.headers["DrawPolicy"], "claim", "PGN draw policy")
        for color in ["White", "Black"]:
            engine = data["engines"][record[color.lower()]]
            check(game.headers[color], record[color.lower()] + ": " + engine["engine_id"]["name"], "PGN engine identity")
            check(game.headers[color + "SHA256"], engine["sha256"], "PGN binary hash")
            check(game.headers[color + "SourceCommit"], engine["provided_source_commit"], "PGN source commit")
        pgn_moves = [m.uci() for m in game.mainline_moves()]
        check(pgn_moves, record["uci_moves"], "exact PGN/JSON moves")
        check(len(pgn_moves), record["plies"], "ply count")
        check(pgn_moves[:len(opening["moves"])], opening["moves"], "actual opening prefix")
        board = game.board()
        check(board.fen(en_passant="fen"), chess.STARTING_FEN, "game starts from standard initial board")
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
        if record["completed"]:
            assert outcome is not None
            check(outcome.result(), record["result"], "independent result")
            check(outcome.termination.name.lower(), record["termination"], "independent termination")
            assert record["error"] is None and record["plies"] <= data["max_plies"]
            value = 0.5 if outcome.winner is None else float((outcome.winner == chess.WHITE) == (record["white"] == "candidate"))
            check(value, record["candidate_points"], "candidate points")
            points.append(value)
        else:
            assert outcome is None and (record["error"] or record["termination"] == "ply_cap")
            check(record["result"], "*", "unfinished game is not scored")
            check(record["candidate_points"], None, "unfinished game has no points")
            incomplete_games.append({"game": number, "plies": record["plies"], "termination": record["termination"],
                                     "error": record["error"], "fen": board.fen(en_passant="fen"),
                                     "next_role": record["white" if board.turn else "black"],
                                     "elapsed_seconds": record["elapsed_seconds"],
                                     "returned_search_wall_seconds": sum(item["elapsed_seconds"] for item in record["search_moves"]),
                                     "legal_next_moves": [move.uci() for move in board.legal_moves]})
        if outcome is not None and outcome.termination == chess.Termination.THREEFOLD_REPETITION:
            intended = []
            if not board.is_repetition(3):
                for move in list(board.legal_moves):
                    board.push(move)
                    if board.is_repetition(3):
                        intended.append(move.uci())
                    board.pop()
                assert intended
            claims.append({"game": number, "type": "threefold_repetition", "current_threefold": board.is_repetition(3), "intended_moves": intended})
        if outcome is not None and outcome.termination == chess.Termination.FIFTY_MOVES:
            intended = []
            if board.halfmove_clock < 100:
                for move in list(board.legal_moves):
                    board.push(move)
                    if board.is_fifty_moves():
                        intended.append(move.uci())
                    board.pop()
                assert intended
            claims.append({"game": number, "type": "fifty_moves", "halfmove_clock": board.halfmove_clock, "intended_moves": intended})
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
    check(summary["completed_games"], len(points), "summary completed")
    check(summary["incomplete_games"], len(incomplete_games), "summary incomplete")
    check(summary["scheduled_games"], 16, "summary scheduled")
    check(summary["attempted_games"], len(data["games"]), "summary attempted")
    check(summary["unplayed_games"], 16 - len(data["games"]), "summary unplayed")
    check(len(summary["pairs"]), 8, "all eight pair summaries")
    for index, pair in enumerate(summary["pairs"], 1):
        check(pair["opening"], original_openings[index - 1]["name"], "summary pair opening")
        pair_games = data["games"][(index - 1) * 2:index * 2]
        check(pair["pair"], index, "summary pair order")
        check(pair["games"], [game["game"] for game in pair_games], "summary pair games")
        check(pair["results"], [game["result"] for game in pair_games], "summary pair results")
        complete_pair = len(pair_games) == 2 and all(game["completed"] for game in pair_games)
        check(pair["completed"], complete_pair, "summary pair completed")
        pair_points = sum(game["candidate_points"] for game in pair_games) if complete_pair else None
        check(pair["candidate_points"], pair_points, "summary pair points")
        check(pair["candidate_score"], pair_points / 2 if complete_pair else None, "summary pair score")
    check(wdl, [summary["candidate_wins"], summary["candidate_draws"], summary["candidate_losses"]], "reported WDL")
    check(sum(points), summary["candidate_points"], "total points")
    check(sum(points) / len(points) if points else None, summary["candidate_score"], "score rate among completed games")
    verified_bootstrap = verify_bootstrap(summary)
    role_stats = {}
    for role in backtracks:
        selected = [m for m in all_moves if m["role"] == role]
        check(resources(selected), summary["resources"][role], "summary resources")
        for key, value in backtracks[role].items():
            check(value, summary["backtracks"][role][key], "summary backtracks")
        role_stats[role] = stats(selected, data["limits"]["movetime_ms"], data["limits"]["movetime_ms"] - 20)
    return {"file": str(path), "status": "PASS" if not incomplete_games else "REPLAY_VALID_RUN_INCOMPLETE",
            "dataset_role": dataset_role, "opening_path": str(opening_path),
            "opening_sha256": data["opening_file_sha256"], "preimplementation_plan_commit": PLAN_COMMIT,
            "scheduled_games": 16, "games": len(data["games"]), "completed_games": len(points),
            "unplayed_games": 16 - len(data["games"]), "incomplete_games": incomplete_games,
            "run_errors": data["errors"], "paired_openings": 8,
            "limits": data["limits"], "total_pgn_plies": total_plies,
            "engine_selected_plies": len(all_moves), "pv_plies": pv_plies,
            "candidate_wdl": wdl, "candidate_score": sum(points) / len(points) if points else None,
            "paired_opening_bootstrap95_descriptive": verified_bootstrap,
            "interpretation": ("Held-out primary comparison; separate from the development opening results. Small fixed sample, not universal strength/Elo."
                               if dataset_role == "heldout" else
                               "Development diagnostic only; these openings and seven losses informed implementation. Not held-out evidence."),
            "options_verified": {"both": {"Move Overhead": 20, "UsePositionalEvaluation": True, "UseQuiescence": True,
                                           "UseTranspositionTable": True, "Hash": 16, "UseFastMoves": True},
                                 "candidate": {"Clear Hash": {"type": "button", "default": None},
                                               "UseQuietMoveOrdering": True, "UseQuiescenceChecks": True},
                                 "requested_options": {}},
            "terminations": dict(terminations), "claims": claims,
            "statistics": role_stats, "source_checks": source_checks,
            "recorded_harness_checkout": data["harness_checkout"],
            "audit_current_checkout": {"commit": git("rev-parse", "HEAD").decode().strip(),
                                       "status_porcelain": git("status", "--porcelain").decode().strip()},
            "time_note": "Requested movetime is equal. Both engines default to Move Overhead=20ms; effective search allowance is requested time minus 20ms. Reported and wall times are measured separately."}



def verify_bootstrap(summary):
    scores = [pair["candidate_score"] for pair in summary["pairs"] if pair["completed"]]
    if len(scores) < 2:
        check(summary["paired_opening_bootstrap95_descriptive"], None, "no bootstrap with less than two complete pairs")
        return None
    reported = summary["paired_opening_bootstrap95_descriptive"]
    check(reported["seed"], 20261017, "predeclared bootstrap seed")
    check(reported["resamples"], 20000, "predeclared bootstrap count")
    check(reported["completed_pairs"], len(scores), "bootstrap paired sample count")
    rng = random.Random(20261017)
    samples = sorted(sum(rng.choices(scores, k=len(scores))) / len(scores) for _ in range(20000))
    interval = [samples[int(0.025 * (len(samples) - 1))], samples[int(0.975 * (len(samples) - 1))]]
    check(reported["interval"], interval, "independent paired-bootstrap recomputation")
    return reported


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("match", type=Path, help="one match JSON; development and heldout are audited separately")
    parser.add_argument("--dataset-role", choices=["development", "heldout"], required=True)
    parser.add_argument("--openings", type=Path, required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--baseline-commit", default=EXPECTED_COMMITS["baseline"])
    parser.add_argument("--harness-commit", help="defaults to candidate source commit")
    parser.add_argument("--movetime", type=int, choices=[40, 100], default=100)
    parser.add_argument("--output", type=Path, help="default: JSON beside the source match")
    args = parser.parse_args()
    for name in ["candidate_commit", "baseline_commit", "harness_commit"]:
        value = getattr(args, name)
        if value is None:
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            parser.error(name + " must be a full source commit")
        check(git("rev-parse", "--verify", value + "^{commit}").decode().strip(), value, name)
    args.match = args.match.resolve(strict=True)
    args.openings = args.openings.resolve(strict=True)
    expected_dataset = ROOT / "tests/data" / ("comparison_openings.json" if args.dataset_role == "development"
                                               else "heldout_openings_2026-10-17.json")
    check(args.openings, expected_dataset.resolve(strict=True), "label matches predeclared exact dataset")
    EXPECTED_COMMITS.update(baseline=args.baseline_commit, candidate=args.candidate_commit)
    subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", PLAN_COMMIT, args.candidate_commit], check=True)
    return args


def markdown(report):
    label = "보류 위치 주 비교" if report["dataset_role"] == "heldout" else "개발 위치 진단 비교"
    lines = ["# " + label + " 독립 감사", "", "이 문서는 한 묶음만 다룬다. 개발 위치와 보류 위치의 득점률을 합산하지 않는다.", "",
             "- 상태: " + report["status"],
             "- 후보 승·무·패: " + " / ".join(map(str, report["candidate_wdl"])),
             f'- 완료 {report["completed_games"]}/예정 {report["scheduled_games"]}, 미실행 {report["unplayed_games"]}',
             f'- 실제 기보 {report["total_pgn_plies"]:,}반수, PV {report["pv_plies"]:,}수 합법성 재생',
             "", "| 엔진 | 반환 수 | 평균 완료 깊이 | 평균 노드 | 평균 벽시계(ms) | 최대 벽시계(ms) | 요청 시간 초과 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for role, values in report["statistics"].items():
        if values["searched_moves"]:
            lines.append(f'| {EXPECTED_VERSIONS[role]} | {values["searched_moves"]} | {values["mean_depth"]:.6f} | {values["mean_nodes"]:.6f} | {values["mean_wall_time_ms"]:.6f} | {values["max_wall_time_ms"]:.3f} | {values["wall_over_requested_budget_moves"]} |')
    lines += ["", "출처·옵션·수순·반복 청구·자원·짝별 득점·부트스트랩을 별도 계산했다. 실행 파일을 독립 재빌드한 검사는 아니다.",
              "작고 고정된 시작 위치의 유한 표본이며 일반적인 강도나 Elo로 환산하지 않는다."]
    if report["incomplete_games"] or report["unplayed_games"]:
        lines += ["", "미완료와 미실행이 있으므로 실행 전체 성공으로 보지 않는다. 실패 대기시간은 반환 수 평균에 포함되지 않는다."]
    return "\n".join(lines) + "\n"


def main():
    args = arguments()
    report = audit(args.match, args.openings, args.dataset_role,
                   args.harness_commit or args.candidate_commit, args.movetime)
    output = args.output or args.match.with_suffix(".independent-audit.json")
    assert output.resolve() != args.match, "audit output must not replace match input"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["dataset_role", "status", "completed_games", "unplayed_games", "candidate_wdl", "candidate_score", "total_pgn_plies", "pv_plies"]}))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
