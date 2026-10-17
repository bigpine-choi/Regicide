"""Independent, read-only replay of controlled-match JSON and PGN evidence."""
import collections
import datetime
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
EXPECTED_COMMITS = {"baseline": "ef52f14569222acbfaa512245bab81558795180f",
                    "candidate": "4a0f95a035b1cc0eff8e8d56a14ea898621eab64"}
EXPECTED_VERSIONS = {"baseline": "Regicide 0.4.0", "candidate": "Regicide 0.4.1"}
RETRY_PLAN_COMMIT = "1836ca54447d55887bcfb49ffdfdb587fe388895"


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
    assert data["exit_code"] in [0, 2]
    check(data["completed"], all(game["completed"] for game in data["games"]), "recorded run completion")
    if data["completed"]:
        check(data["exit_code"], 0, "successful exit")
        check(data["errors"], [], "no errors")
    else:
        check(data["exit_code"], 2, "incomplete run exit")
        assert data["errors"], "incomplete run must expose its error"
    check(data["harness_checkout"]["status_porcelain"], "", "clean checkout recorded at start")
    check(data["limits"]["nodes"], None, "no node cap")
    check(data["limits"]["depth"], None, "no depth cap")
    assert data["limits"]["movetime_ms"] in [40, 100]
    check(data["draw_policy"], "claim", "claim policy")
    check(len(data["selected_openings"]), 8, "eight fixed openings")
    check(data["games_requested"], 16, "sixteen scheduled games")
    check(len(data["games"]), 16, "sixteen recorded games")
    check(digest(Path(data["opening_source"])), data["opening_file_sha256"], "opening file SHA256")
    original_openings = json.loads(Path(data["opening_source"]).read_text(encoding="utf-8"))["openings"]
    for supplied, original in zip(data["selected_openings"], original_openings[:8]):
        check(supplied["name"], original["name"], "predeclared opening order")
        check(supplied["moves"], original["moves"], "predeclared opening moves")
        opening_board = chess.Board()
        for move in original["moves"]:
            opening_board.push_uci(move)
        check(supplied["fen_after_opening"], opening_board.fen(en_passant="fen"), "predeclared opening FEN")
    source_checks = {}
    expected_harness = RETRY_PLAN_COMMIT if path.stem == "time100-retry" else EXPECTED_COMMITS["candidate"]
    check(data["harness_checkout"]["commit"], expected_harness, "frozen harness commit")
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
            assert record["error"] is None and record["plies"] < data["max_plies"]
            value = 0.5 if outcome.winner is None else float((outcome.winner == chess.WHITE) == (record["white"] == "candidate"))
            check(value, record["candidate_points"], "candidate points")
            points.append(value)
        else:
            assert outcome is None and record["error"]
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
    for field in ["scheduled_games", "attempted_games"]:
        check(summary[field], 16, "summary " + field)
    check(summary["unplayed_games"], 0, "summary unplayed")
    for index, pair in enumerate(summary["pairs"], 1):
        pair_games = data["games"][(index - 1) * 2:index * 2]
        check(pair["pair"], index, "summary pair order")
        check(pair["games"], [game["game"] for game in pair_games], "summary pair games")
        check(pair["results"], [game["result"] for game in pair_games], "summary pair results")
        complete_pair = all(game["completed"] for game in pair_games)
        check(pair["completed"], complete_pair, "summary pair completed")
        pair_points = sum(game["candidate_points"] for game in pair_games) if complete_pair else None
        check(pair["candidate_points"], pair_points, "summary pair points")
        check(pair["candidate_score"], pair_points / 2 if complete_pair else None, "summary pair score")
    check(wdl, [summary["candidate_wins"], summary["candidate_draws"], summary["candidate_losses"]], "reported WDL")
    check(sum(points), summary["candidate_points"], "total points")
    check(sum(points) / len(points), summary["candidate_score"], "score rate among completed games")
    role_stats = {}
    for role in backtracks:
        selected = [m for m in all_moves if m["role"] == role]
        check(resources(selected), summary["resources"][role], "summary resources")
        for key, value in backtracks[role].items():
            check(value, summary["backtracks"][role][key], "summary backtracks")
        role_stats[role] = stats(selected, data["limits"]["movetime_ms"], data["limits"]["movetime_ms"] - 20)
    return {"file": str(path), "status": "PASS" if not incomplete_games else "REPLAY_VALID_RUN_INCOMPLETE",
            "games": 16, "completed_games": len(points), "incomplete_games": incomplete_games,
            "run_errors": data["errors"], "paired_openings": 8,
            "limits": data["limits"], "total_pgn_plies": total_plies,
            "engine_selected_plies": len(all_moves), "pv_plies": pv_plies,
            "candidate_wdl": wdl, "candidate_score": sum(points) / len(points),
            "options_verified": {"both": {"Move Overhead": 20, "UsePositionalEvaluation": True, "UseQuiescence": True,
                                           "UseTranspositionTable": True, "Hash": 16, "UseFastMoves": True},
                                 "candidate": {"Clear Hash": {"type": "button", "default": None}},
                                 "requested_options": {}},
            "terminations": dict(terminations), "claims": claims,
            "statistics": role_stats, "source_checks": source_checks,
            "recorded_harness_checkout": data["harness_checkout"],
            "audit_current_checkout": {"commit": git("rev-parse", "HEAD").decode().strip(),
                                       "status_porcelain": git("status", "--porcelain").decode().strip()},
            "time_note": "Requested movetime is equal. Both engines default to Move Overhead=20ms; effective search allowance is requested time minus 20ms. Reported and wall times are measured separately."}


def verify_interruption_and_retry(evidence_dir, retry_report):
    original = json.loads((evidence_dir / "time100.json").read_text(encoding="utf-8"))
    assert not original["completed"] and original["exit_code"] == 2
    check(original["summary"]["completed_games"], 15, "preserved original completed games")
    check(original["summary"]["incomplete_games"], 1, "preserved original incomplete games")
    events_path = evidence_dir / "suspend-events.json"
    events = json.loads(events_path.read_text(encoding="utf-8"))
    by_id = {event["id"]: event for event in events}
    assert "Idle Timeout" in by_id[506]["message"]
    start = datetime.datetime.fromisoformat(by_id[506]["time_created"])
    end = datetime.datetime.fromisoformat(by_id[507]["time_created"])
    assert start < end
    for event in events:
        check(event["provider"], "Microsoft-Windows-Kernel-Power", "power event provider")
    match_start = datetime.datetime.fromisoformat(original["started_at"])
    match_end = match_start + datetime.timedelta(seconds=original["total_elapsed_seconds"])
    assert match_start < start < end < match_end, "standby must fall inside original match period"
    reproduction_path = evidence_dir / "debug/raw-reproduction.json"
    reproductions = json.loads(reproduction_path.read_text(encoding="utf-8"))
    check(len(reproductions), 110, "raw bounded reproduction count")
    failed_game = original["games"][-1]
    repro_pv_plies = 0
    for record in reproductions:
        check(record["success"], True, "raw reproduction succeeded")
        if record["search"] == "fen-import":
            board = chess.Board(failed_game["final_fen"])
        else:
            board = chess.Board(failed_game["initial_fen"])
            for text in failed_game["uci_moves"][:record["plies"]]:
                board.push_uci(text)
        move = chess.Move.from_uci(record["bestmove"].split()[1])
        assert move in board.legal_moves
        for line in record["final_info"]:
            if " pv " in line:
                variation = board.copy()
                for text in line.split(" pv ", 1)[1].split():
                    pv_move = chess.Move.from_uci(text)
                    assert pv_move in variation.legal_moves
                    variation.push(pv_move)
                    repro_pv_plies += 1
    guard_path = evidence_dir / "wake-guard.json"
    guard = json.loads(guard_path.read_text(encoding="utf-8-sig"))
    check(guard["requested_state"], "ES_CONTINUOUS | ES_SYSTEM_REQUIRED", "temporary wake guard")
    check(guard["display_required"], False, "no display wake requirement")
    check(guard["persistent_power_policy_changed"], False, "no persistent power policy change")
    check(guard["restore_succeeded"], True, "wake guard restore")
    check(guard["restore_requested_state"], guard["prior_state"] | 2147483648, "restore previous execution state")
    check(guard["comparison_exit_code"], 0 if not retry_report["incomplete_games"] else 2, "retry exit matches guard log")
    check(git("rev-parse", "--verify", RETRY_PLAN_COMMIT + "^{commit}").decode().strip(), RETRY_PLAN_COMMIT, "predeclared retry plan commit")
    return {"excluded_from_primary_score": "time100.json/.pgn: original 15 completed plus one unscored timeout preserved, not pooled into retry score",
            "original_candidate_wdl": [6, 2, 7], "original_incomplete_games": 1,
            "power_events": events, "power_events_sha256": digest(events_path),
            "standby_duration_seconds": (end - start).total_seconds(),
            "reproduction_searches": len(reproductions), "reproduction_failures": 0,
            "reproduction_pv_plies_checked": repro_pv_plies,
            "reproduction_max_elapsed_seconds": max(record["elapsed"] for record in reproductions),
            "reproduction_sha256": digest(reproduction_path),
            "retry_plan_commit": RETRY_PLAN_COMMIT,
            "wake_guard": guard, "wake_guard_sha256": digest(guard_path),
            "inference": "Recorded standby overlaps the failed run and 110 bounded searches do not reproduce an engine hang; the retry controls the environmental interruption without changing engine binaries/options."}


if __name__ == "__main__":
    primary = "--primary" in sys.argv[1:]
    arguments = [argument for argument in sys.argv[1:] if argument != "--primary"]
    reports = []
    for argument in arguments:
        source = Path(argument).resolve(strict=True)
        report = audit(source)
        output = source.with_suffix(".independent-audit.json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if primary:
        check(len(reports), 1, "primary mode audits one complete retry bundle")
        check(Path(reports[0]["file"]).stem, "time100-retry", "primary retry file")
        evidence_dir = Path(__file__).parent
        reports.insert(0, json.loads((evidence_dir / "time40.independent-audit.json").read_text(encoding="utf-8")))
    if len(reports) > 1:
        incomplete_count = sum(len(r["incomplete_games"]) for r in reports)
        combined = {"status": "PASS" if not incomplete_count else "REPLAY_VALID_RUN_INCOMPLETE", "games": sum(r["games"] for r in reports),
                    "completed_games": sum(r["completed_games"] for r in reports), "incomplete_games": incomplete_count,
                    "total_pgn_plies": sum(r["total_pgn_plies"] for r in reports),
                    "engine_selected_plies": sum(r["engine_selected_plies"] for r in reports),
                    "pv_plies": sum(r["pv_plies"] for r in reports),
                    "candidate_wdl": [sum(r["candidate_wdl"][index] for r in reports) for index in range(3)],
                    "interpretation": "Small fixed paired-opening experiment. Do not infer strength improvement from speed or finite WDL alone.",
                    "conditions": reports}
        for report in reports[1:]:
            check(report["source_checks"], reports[0]["source_checks"], "same binaries and source commits across time conditions")
            check(report["options_verified"], reports[0]["options_verified"], "same options across time conditions")
        basename = "primary-audit-summary" if primary else "independent-audit-summary"
        if primary:
            combined["interruption_and_retry"] = verify_interruption_and_retry(Path(__file__).parent, reports[1])
        (Path(__file__).parent / (basename + ".json")).write_text(
            json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = ["# 전이표 메모리 재사용 버전 동일 시간 대국 독립 감사", "",
                 "Python chess 1.11.2로 JSON과 PGN을 독립 재생했다. 생산 코드 수정이나 재빌드 없이 검증했다.", "",
                 "| 요청 시간/수 | 대국 수 | 0.4.1 승·무·패 | PGN 수 | 엔진 선택 수 | 합법 PV 검증 수 | 종결 |",
                 "|---|---:|---|---:|---:|---:|---|"]
        for report in reports:
            term = ", ".join(f"{key} {value}" for key, value in report["terminations"].items())
            wdl = "·".join(map(str, report["candidate_wdl"]))
            lines.append(f'| {report["limits"]["movetime_ms"]}ms | 완료 {report["completed_games"]}/시도 16 | {wdl} | {report["total_pgn_plies"]:,} | {report["engine_selected_plies"]:,} | {report["pv_plies"]:,} | {term} |')
        lines.extend([f'| 합계 | 완료 {combined["completed_games"]}/시도 {combined["games"]} | {"·".join(map(str, combined["candidate_wdl"]))} | {combined["total_pgn_plies"]:,} | {combined["engine_selected_plies"]:,} | {combined["pv_plies"]:,} | 미완료 {incomplete_count} |', "",
                      "각 조건에서 사전에 정한 8개 시작 수순마다 선후공을 바꾼 2판을 확인했다. PGN/JSON의 수순, 모든 실제 수와 PV의 합법성, FEN, 결과·종결·반복 청구, 점수, 자원 합계 및 왕복 수 통계를 재계산해 일치시켰다. 청구 가능한 종결 뒤 진행한 수는 없었다.", "",
                      "| 요청 시간 | 엔진 | 선택 수 | 평균 완료 깊이 | 평균 노드/수 | 평균 엔진 시간 | 평균 벽시계 시간 | 최대 벽시계 시간 | 요청 시간 초과 수 |",
                      "|---|---|---:|---:|---:|---:|---:|---:|---:|"])
        for report in reports:
            for role, name in [("baseline", "0.4.0"), ("candidate", "0.4.1")]:
                item = report["statistics"][role]
                lines.append(f'| {report["limits"]["movetime_ms"]}ms | {name} | {item["searched_moves"]} | {item["mean_depth"]:.6f} | {item["mean_nodes"]:.6f} | {item["mean_engine_time_ms"]:.3f}ms | {item["mean_wall_time_ms"]:.6f}ms | {item["max_wall_time_ms"]:.3f}ms | {item["wall_over_requested_budget_moves"]} |')
        lines.extend(["", "두 엔진 모두 Move Overhead=20ms이므로 40/100ms 요청의 실제 탐색 허용량은 20/80ms다. 상세 JSON에 허용량 대비 엔진 시간 초과와 요청 시간 대비 벽시계 시간 초과를 각각 기록했다. 벽시계에는 프로세스 통신 및 클라이언트 비용이 포함된다.", "",
                      "양쪽 모두 위치 평가·안정화 탐색·전이표·생성 수 빠른 적용이 켜져 있고 Hash=16MiB다. 0.4.1은 Clear Hash 버튼도 광고한다. 추가 요청 옵션, 노드 제한, 깊이 제한은 없다.", "",
                      "| 엔진 | 소스 커밋 | 실행 파일 SHA256 |", "|---|---|---|"])
        for role, item in reports[0]["source_checks"].items():
            lines.append(f'| {EXPECTED_VERSIONS[role]} | `{item["source_commit"]}` | `{item["binary_sha256"]}` |')
        lines.extend(["", "두 조건의 바이너리 해시·소스 커밋·옵션이 같다. 현재 실행 파일 SHA256, 커밋 객체, 각 버전의 빌드 관련 소스 13개를 대조했다. 줄바꿈 차이는 정규화했다. 대국 시작 시 체크아웃은 깨끗했다고 기록되어 있다. 감사는 바이너리를 독립 재빌드하지 않았으므로, 검증 범위는 기보·통계와 저장된 출처의 정합성이다.", "", "반복 청구 세부사항:"])
        for report in reports:
            for claim in report["claims"]:
                mode = "현재 위치" if claim.get("current_threefold", False) else "의도 수 " + ", ".join(claim["intended_moves"])
                lines.append(f'- 요청 {report["limits"]["movetime_ms"]}ms {claim["game"]}번: {claim["type"]}, {mode}')
        details = ", ".join("`" + Path(report["file"]).with_suffix(".independent-audit.json").name + "`" for report in reports)
        lines.extend(["", "이 고정된 소표본 대국만으로 경기력 향상을 확정하지 않는다. 메모리 작업 감소나 속도 개선은 경기력과 별도로 판단해야 하며 Elo를 산정하지 않았다.", "",
                      "재현 스크립트: `independent_match_audit.py`. 상세 기록: " + details + ", `" + basename + ".json`."])
        if incomplete_count:
            lines.extend(["", "## 미완료 실행의 제한", "", "100ms 실행에는 엔진 응답 시간 초과가 있다. 저장된 모든 수순은 유효하지만 실행 전체가 성공한 것으로 판정하지 않는다. 승·무·패는 완료한 판만 포함하며, 미완료 대국은 승패나 무승부로 환산하지 않았다. 자원 평균에는 응답을 돌려준 수만 포함되며 응답 실패의 지연은 별도 항목이다."])
            for report in reports:
                for failure in report["incomplete_games"]:
                    lines.append(f'- 요청 {report["limits"]["movetime_ms"]}ms {failure["game"]}번: {failure["plies"]}수 뒤 {failure["next_role"]} 응답 대기 중 `{failure["error"]}`. 종료 FEN `{failure["fen"]}`. 대국 경과 {failure["elapsed_seconds"]:.6f}초, 기록된 정상 응답 합계 {failure["returned_search_wall_seconds"]:.6f}초.')
        if primary:
            evidence = combined["interruption_and_retry"]
            lines.extend(["", "## 원래 100ms 실행 보존과 사전 기록된 전체 재측정", "",
                          "주 비교는 정상 완료한 원래 40ms 묶음과 이번 100ms 전체 재측정 묶음만 포함한다. 원래 100ms 실행의 완료 15판·미완료 1판 및 6승 2무 7패는 별도 원본·감사 자료에 보존했으며 재측정 득점에 합치지 않았다.", "",
                          "Windows Kernel-Power 506(Idle Timeout) 22:28:38.181과 507 재개 22:40:57.226이 원래 대국 실행 시간 안에 발생했음을 JSON 이벤트 자료로 확인했다. 별도 재현 원자료 110개 모두 정상 응답했고, 반환 수와 PV도 독립 검증했다. 최대 응답은 " + f'{evidence["reproduction_max_elapsed_seconds"] * 1000:.3f}ms' + "다. 이 증거는 외부 절전 중단이라는 해석을 지지하며 엔진 멈춤은 재현되지 않았다.", "",
                          "재측정 결정과 조건은 결과를 보기 전에 커밋 `" + RETRY_PLAN_COMMIT + "`에 기록됐다. 엔진·옵션·시작 수순·선후공·시간 조건은 동일하며 100ms 16판 전체를 새로 실행했다. 실행 중 임시 ES_SYSTEM_REQUIRED만 적용했고 화면 켜짐 요구나 영구 전원 정책은 바꾸지 않았으며, 종료 시 이전 실행 상태로 복원한 기록을 확인했다."])
        (Path(__file__).parent / (basename + ".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("COMBINED", json.dumps({key: value for key, value in combined.items() if key != "conditions"}))
