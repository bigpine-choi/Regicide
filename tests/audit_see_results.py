"""Independently replay the frozen SEE-ordering comparison; never launch an engine.

Usage: python tests/audit_see_results.py build/see-evidence \
    --candidate-commit FULL_COMMIT --output build/see-evidence/independent-audit.json
The Markdown sibling and JSON output are exclusively created, never overwritten.
The original harness bootstrap (seed 20261017) is preserved; this audit reports
the predeclared SEE paired-opening bootstrap with seed 20261018 separately.
These openings were previously used for PVS development, not fresh held-out data.
"""

import argparse
import datetime
import hashlib
import io
import json
from pathlib import Path
import random
import re
import statistics
import sys

import chess
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "1441d5ec23733af570b05521620935619c39cefa"
OPENING_SHA256 = "e0a6072ecbee74da029d7e57352feac42a24db17df597d592596097429d75c95"
PROFILE_SHA256 = "e0c395ad2d531646c7ccee9d3a156f7580500f59af62186a533a49989e440905"
SEED = 20261018
RESAMPLES = 20000
ROLES = ("baseline", "candidate")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def board_after(fen, moves):
    board = chess.Board(fen)
    require(board.is_valid(), f"invalid initial position: {fen}")
    for text in moves:
        move = chess.Move.from_uci(text)
        require(move in board.legal_moves, f"illegal move {text} in {board.fen()}")
        board.push(move)
    return board


def check_pv(board, pv, selected):
    require(isinstance(pv, list), "PV is not a list")
    if pv:
        require(pv[0] == selected, "PV first move differs from selected move")
    position = board.copy(stack=True)
    for text in pv:
        move = chess.Move.from_uci(text)
        require(move in position.legal_moves, f"illegal PV move {text}")
        position.push(move)
    return len(pv)


def effective_options(engine, configured_field):
    options = {name: item["default"] for name, item in engine["advertised_options"].items()
               if item["type"] != "button"}
    for name, value in engine[configured_field].items():
        require(name in options, f"unadvertised configured option {name}")
        options[name] = value
    return options


def check_engines(engines, candidate_commit, cost=False, control=False):
    result = {}
    for role in ROLES:
        item = engines[role]
        commit = item["declared_source_commit" if cost else "provided_source_commit"]
        expected_commit = BASELINE_COMMIT if role == "baseline" else candidate_commit
        require(commit == expected_commit, f"wrong {role} source commit")
        identity = item["id" if cost else "engine_id"]
        expected_name = "Regicide 0.5.1" if role == "baseline" else "Regicide 0.5.2"
        require(identity.get("name") == expected_name, f"wrong {role} engine version")
        path = Path(item["path"] if cost else item["command"][0])
        require(path.is_file(), f"recorded binary unavailable for hash audit: {path}")
        require(digest(path) == item["sha256"], f"{role} executable hash changed")
        options = effective_options(item, "configured_options" if cost else "requested_options")
        require(options.get("Hash") == 16, f"wrong {role} Hash")
        require(options.get("Move Overhead") == 20, f"wrong {role} overhead")
        for name in ("UsePositionalEvaluation", "UseQuiescence", "UseTranspositionTable",
                     "UseFastMoves", "UseQuietMoveOrdering", "UseQuiescenceChecks"):
            require(options.get(name) is True, f"unexpected {role} option {name}")
        require(options.get("PVS") is True, f"PVS is not enabled for {role}")
        if role == "candidate":
            require(options.get("UseSEE") is (not control), "wrong candidate SEE state")
        else:
            require("UseSEE" not in options, "baseline unexpectedly advertises SEE")
        result[role] = {"commit": commit, "sha256": item["sha256"],
                        "id": identity, "effective_options": options}
    return result


def check_checkout(checkout, candidate_commit):
    require(checkout.get("commit") == candidate_commit, "harness checkout was not frozen candidate")
    require(checkout.get("status_porcelain") == "", "harness checkout was dirty")


def audit_game(record):
    require(record["initial_fen"] == chess.STARTING_FEN, "unexpected initial FEN")
    prefix = record["opening_uci_moves"]
    moves = record["uci_moves"]
    require(moves[:len(prefix)] == prefix, "opening prefix mismatch")
    board = board_after(record["initial_fen"], prefix)
    require(board.fen(en_passant="fen") == record["opening_fen"], "opening FEN mismatch")
    require(len(record["search_moves"]) == len(moves) - len(prefix), "search record count mismatch")
    pv_plies = 0
    resources = {role: [] for role in ROLES}
    for index, (text, item) in enumerate(zip(moves[len(prefix):], record["search_moves"]), len(prefix) + 1):
        require(board.outcome(claim_draw=True) is None, "continued after claimable termination")
        role = record["white" if board.turn else "black"]
        require(item["ply"] == index and item["move"] == text and item["role"] == role,
                "search move order/role mismatch")
        require(item["color"] == ("white" if board.turn else "black"), "search color mismatch")
        require(item["fen_before"] == board.fen(en_passant="fen"), "search FEN mismatch")
        move = chess.Move.from_uci(text)
        require(move in board.legal_moves, f"illegal actual move {text}")
        pv_plies += check_pv(board, item["pv"], text)
        for name in ("nodes", "depth", "seldepth"):
            require(isinstance(item[name], int) and not isinstance(item[name], bool) and item[name] >= 0,
                    f"invalid/missing {name} report")
        for name in ("elapsed_seconds", "engine_time_seconds"):
            require(isinstance(item[name], (int, float)) and item[name] >= 0, f"invalid/missing {name}")
        resources[role].append(item)
        board.push(move)
    require(record["plies"] == len(moves) <= 500, "wrong ply count/cap")
    require(record["final_fen"] == board.fen(en_passant="fen"), "final FEN mismatch")
    require(record["completed"] is True and record["error"] is None, "unfinished/error game")
    outcome = board.outcome(claim_draw=True)
    require(outcome is not None, "completed game has no independently recognized outcome")
    require(record["result"] == outcome.result(), "result mismatch")
    require(record["termination"] == outcome.termination.name.lower(), "termination mismatch")
    points = 0.5 if outcome.winner is None else float(
        (outcome.winner == chess.WHITE) == (record["white"] == "candidate"))
    require(record["candidate_points"] == points, "candidate points mismatch")
    for role in ROLES:
        reported = record["resources"][role]
        require(reported["searched_moves"] == len(resources[role]), "resource move total mismatch")
        require(reported["reported_nodes"] == sum(item["nodes"] for item in resources[role]),
                "resource node total mismatch")
    return {"points": points, "pv_plies": pv_plies, "plies": len(moves),
            "resources": resources, "termination": outcome.termination.name.lower()}


def resources_summary(items, movetime):
    def mean(name):
        return statistics.mean(item[name] for item in items) if items else None
    return {"searched_moves": len(items), "nodes_total": sum(item["nodes"] for item in items),
            "mean_nodes": mean("nodes"), "mean_completed_depth": mean("depth"),
            "mean_seldepth": mean("seldepth"), "mean_wall_ms": mean("elapsed_seconds") * 1000 if items else None,
            "max_wall_ms": max(item["elapsed_seconds"] for item in items) * 1000 if items else None,
            "mean_internal_ms": mean("engine_time_seconds") * 1000 if items else None,
            "max_internal_ms": max(item["engine_time_seconds"] for item in items) * 1000 if items else None,
            "wall_over_requested_count": sum(item["elapsed_seconds"] > movetime / 1000 + 1e-9 for item in items),
            "internal_over_allocated_count": sum(item["engine_time_seconds"] > (movetime - 20) / 1000 + 1e-9
                                                  for item in items)}


def paired_bootstrap(pair_scores):
    generator = random.Random(SEED)
    samples = sorted(sum(generator.choices(pair_scores, k=len(pair_scores))) / len(pair_scores)
                     for _ in range(RESAMPLES))
    return {"seed": SEED, "resamples": RESAMPLES, "pairs": len(pair_scores),
            "interval": [samples[int(0.025 * (RESAMPLES - 1))], samples[int(0.975 * (RESAMPLES - 1))]],
            "scope": "Descriptive fixed-opening paired percentile bootstrap, not universal strength or Elo."}


def audit_pgn(path, document):
    source = io.StringIO(path.read_text(encoding="utf-8-sig"))
    total = 0
    for record in document["games"]:
        game = chess.pgn.read_game(source)
        require(game is not None and not game.errors, "PGN missing game or parse errors")
        require(game.board().fen(en_passant="fen") == record["initial_fen"], "PGN initial FEN mismatch")
        require([move.uci() for move in game.mainline_moves()] == record["uci_moves"], "PGN moves mismatch")
        require(game.end().board().fen(en_passant="fen") == record["final_fen"], "PGN final FEN mismatch")
        expected = {"Round": str(record["game"]), "Pair": str(record["pair"]),
                    "Opening": record["opening"], "OpeningPlies": str(len(record["opening_uci_moves"])),
                    "Result": record["result"], "Termination": "normal", "DrawPolicy": "claim",
                    "TerminationReason": record["termination"],
                    "RequestedMoveTimeMS": str(document["limits"]["movetime_ms"]),
                    "RequestedNodes": "-", "RequestedDepth": "-"}
        for color in ("White", "Black"):
            role = record[color.lower()]
            engine = document["engines"][role]
            expected[color] = f"{role}: {engine['engine_id']['name']}"
            expected[color + "SHA256"] = engine["sha256"]
            expected[color + "SourceCommit"] = engine["provided_source_commit"]
        for key, value in expected.items():
            require(game.headers.get(key) == value, f"PGN header mismatch {key}")
        total += 1
    require(chess.pgn.read_game(source) is None, "unexpected extra PGN games")
    return total


def audit_match(path, candidate_commit, movetime, opening_file):
    document = read_json(path)
    require(document["completed"] is True and document["exit_code"] == 0 and not document["errors"],
            "match did not complete cleanly")
    check_checkout(document["harness_checkout"], candidate_commit)
    engines = check_engines(document["engines"], candidate_commit)
    require(document["limits"] == {"nodes": None, "depth": None, "movetime_ms": movetime}, "wrong time limit")
    require(document["max_plies"] == 500 and document["draw_policy"] == "claim", "wrong adjudication limits")
    require(document["opening_file_sha256"] == digest(opening_file) == OPENING_SHA256,
            "opening file differs from predeclared reused PVS set")
    expected = read_json(opening_file)["openings"]
    require(len(expected) == 8 and len(document["selected_openings"]) == 8, "not all eight openings selected")
    require(document["games_requested"] == len(document["games"]) == 16, "not 16 games")
    points, pair_scores, terminations = [], [], {}
    resources = {role: [] for role in ROLES}
    total_plies = pv_plies = 0
    for pair, opening in enumerate(expected, 1):
        selected = document["selected_openings"][pair - 1]
        require(selected["name"] == opening["name"] and selected["moves"] == opening["moves"],
                "selected opening mismatch")
        paired = []
        for offset in (0, 1):
            number = (pair - 1) * 2 + offset + 1
            record = document["games"][number - 1]
            require(record["game"] == number and record["pair"] == pair, "game/pair schedule mismatch")
            require(record["opening"] == opening["name"] and record["opening_uci_moves"] == opening["moves"],
                    "actual opening mismatch")
            require(record["white"] == ("candidate" if offset == 0 else "baseline")
                    and record["black"] == ("baseline" if offset == 0 else "candidate"), "colors not swapped")
            result = audit_game(record)
            points.append(result["points"])
            paired.append(result["points"])
            total_plies += result["plies"]
            pv_plies += result["pv_plies"]
            terminations[result["termination"]] = terminations.get(result["termination"], 0) + 1
            for role in ROLES:
                resources[role].extend(result["resources"][role])
        pair_scores.append(sum(paired) / 2)
    summary = document["summary"]
    counts = {"candidate_wins": points.count(1.0), "candidate_draws": points.count(0.5),
              "candidate_losses": points.count(0.0), "candidate_points": sum(points),
              "candidate_score": sum(points) / 16, "completed_games": 16,
              "incomplete_games": 0, "unplayed_games": 0}
    for key, value in counts.items():
        require(summary[key] == value, f"harness summary mismatch {key}")
    require(audit_pgn(path.with_suffix(".pgn"), document) == 16, "PGN count mismatch")
    return {"status": "PASS", "input_json_sha256": digest(path), "input_pgn_sha256": digest(path.with_suffix(".pgn")),
            "movetime_ms": movetime, "engines": engines, **counts, "actual_plies": total_plies,
            "pv_plies": pv_plies, "terminations": terminations, "pair_scores": pair_scores,
            "paired_opening_bootstrap95": paired_bootstrap(pair_scores),
            "harness_bootstrap_preserved": summary["paired_opening_bootstrap95_descriptive"],
            "resources": {role: resources_summary(resources[role], movetime) for role in ROLES}}


def audit_cost(path, candidate_commit, depth, control):
    document = read_json(path)
    require(document["exit_code"] == 0 and document["error"] is None, "cost run failed")
    require(document["depth"] == depth and document["repeats"] == 5
            and document["warmup"] == 1 and document["seed"] == SEED, "wrong cost schedule")
    check_checkout(document["environment"]["harness_checkout"], candidate_commit)
    engines = check_engines(document["engines"], candidate_commit, cost=True, control=control)
    positions = document["positions_file"]["document"]["positions"]
    profile_file = ROOT / "tests/data/profile_positions.json"
    require(document["positions_file"]["sha256"] == digest(profile_file) == PROFILE_SHA256,
            "profile file differs from predeclared set")
    require(len(positions) == 10 and len(document["records"]) == 50, "incomplete cost samples")
    require(len(document["warmup_records"]) == 2, "wrong warmup count")
    require(document["require_same_search"] is control, "wrong search-equivalence requirement")
    generator = random.Random(SEED)
    schedule = []
    for repeat in range(5):
        indices = list(range(10))
        generator.shuffle(indices)
        for index in indices:
            order = list(ROLES if (repeat + index) % 2 == 0 else reversed(ROLES))
            schedule.append((repeat + 1, index, order))
    nodes = {role: 0 for role in ROLES}
    score_differences = pv_plies = 0
    score_difference_details = []
    ratios, per_position = [], []
    for record, (repeat, index, order) in zip(document["records"], schedule):
        position = positions[index]
        require(record["repetition"] == repeat and record["position_index"] == index
                and record["position"] == position["name"] and record["order"] == order, "cost order mismatch")
        board = board_after(position.get("fen", chess.STARTING_FEN), position.get("moves", []))
        for role in ROLES:
            item = record[role]
            require(item["depth"] == depth and item["search_fen"] == board.fen(en_passant="fen"),
                    "wrong cost depth/FEN")
            move = chess.Move.from_uci(item["move"])
            require(move in board.legal_moves and item["nodes"] > 0, "invalid cost move/nodes")
            require(not item["score"]["lowerbound"] and not item["score"]["upperbound"], "cost root score bound")
            pv_plies += check_pv(board, item["pv"], item["move"])
            nodes[role] += item["nodes"]
        equal = record["baseline"]["score"] == record["candidate"]["score"]
        require(record["scores_equal"] is equal, "recorded score equality mismatch")
        score_differences += not equal
        if not equal:
            score_difference_details.append({"position": position["name"],
                "position_index": index, "repetition": repeat,
                "baseline": {field: record["baseline"][field] for field in ("score", "move", "pv", "nodes")},
                "candidate": {field: record["candidate"][field] for field in ("score", "move", "pv", "nodes")}})
        if control:
            for field in ("score", "move", "depth", "nodes", "seldepth", "pv", "tt_counters"):
                require(record["baseline"][field] == record["candidate"][field], f"SEE-off differs in {field}")
        ratios.append(record["baseline"]["wall_time_seconds"] / record["candidate"]["wall_time_seconds"])
    for position in positions:
        records = [record for record in document["records"] if record["position"] == position["name"]]
        per_position.append({"name": position["name"], **{role: {
            "median_wall_ms": statistics.median(row[role]["wall_time_seconds"] for row in records) * 1000,
            "median_nodes": statistics.median(row[role]["nodes"] for row in records)} for role in ROLES}})
    return {"status": "PASS", "depth": depth, "see_enabled": not control,
            "input_json_sha256": digest(path), "engines": engines,
            "measured_pairs": 50, "score_differences": score_differences, "pv_plies": pv_plies,
            "score_difference_details": score_difference_details,
            "nodes_total": nodes, "node_ratio_candidate_over_baseline": nodes["candidate"] / nodes["baseline"],
            "median_paired_wall_speedup_baseline_over_candidate": statistics.median(ratios),
            "per_position": per_position}


def audit_run_state(path, candidate_commit, costs, matches):
    state = read_json(path)
    require(state["candidate_commit"] == candidate_commit and state["baseline_commit"] == BASELINE_COMMIT,
            "run-state source mismatch")
    require(state["exit_code"] == 0 and state["error"] is None, "runner failed")
    require(state["opening_sha256"] == OPENING_SHA256 and state["profile_positions_sha256"] == PROFILE_SHA256,
            "run-state data fingerprint mismatch")
    require(state["sequential_workloads"] is True, "sequential execution not recorded")
    require(state["persistent_power_policy_changed"] is False and state["display_required"] is False,
            "unexpected persistent power/display configuration")
    require(state["prior_wake_state"] != 0 and state["restore_succeeded"] is True,
            "temporary wake state did not restore")
    names = ["cost-depth3", "cost-depth4", "control-depth3", "control-depth4", "time100", "time1000"]
    require([step["name"] for step in state["steps"]] == names, "wrong workload order")
    prior_finished = datetime.datetime.fromisoformat(state["started_utc"])
    for step in state["steps"]:
        require(step["exit_code"] == 0, f"runner step failed: {step['name']}")
        started = datetime.datetime.fromisoformat(step["started_utc"])
        finished = datetime.datetime.fromisoformat(step["finished_utc"])
        require(prior_finished <= started <= finished, "overlapping or inverted workload timestamps")
        prior_finished = finished
    require(prior_finished <= datetime.datetime.fromisoformat(state["finished_utc"]), "runner end timestamp mismatch")
    for condition in costs + matches:
        for role in ROLES:
            require(condition["engines"][role]["sha256"] == state["binary_sha256"][role],
                    "binary hash differs across conditions")
    snapshots = state["external_engine_snapshots"]
    require(snapshots and all(isinstance(item["processes"], list) for item in snapshots),
            "missing external engine load snapshots")
    observed = any(item.get("cpu_increase_seconds", 0) > state["external_engine_busy_delta_seconds"]
                   and (item["phase"].endswith("-running") or item["phase"].endswith("-finished"))
                   for item in snapshots)
    require(state["observed_external_engine_cpu_increase"] is observed, "external CPU flag mismatch")
    return {"status": "PASS", "input_json_sha256": digest(path),
            "temporary_wake_restored": True, "sequential_steps": names,
            "external_engine_snapshots": len(snapshots),
            "external_engine_cpu_increase_observed_during_measurement": observed,
            "load_scope": "Sampled other Regicide process CPU only; not proof of globally exclusive PC load."}


def markdown(result):
    lines = ["# SEE 독립 결과 감사", "", "전체 판정: **PASS**. 엔진을 실행하지 않고 저장된 결과를 재생했습니다.", "",
             "| 조건 | 후보 승·무·패 | 득점률 | 시작 쌍 참고 95% 구간 |", "|---|---:|---:|---:|"]
    for match in result["matches"]:
        low, high = match["paired_opening_bootstrap95"]["interval"]
        lines.append(f"| {match['movetime_ms']}ms | {match['candidate_wins']}·{match['candidate_draws']}·{match['candidate_losses']} "
                     f"| {match['candidate_score']:.3%} | {low:.3%} ~ {high:.3%} |")
    lines += ["", "부트스트랩은 사전 선언 seed 20261018, 20,000회입니다. 기존 대국 도구의 seed 20261017 요약은 원자료에 보존했습니다.",
              "8개 고정 시작 쌍의 기술적 참고 구간이며 보편적인 강도나 Elo 구간이 아닙니다.",
              "시작 위치는 이전 PVS 개발 비교에 사용된 자료이며 새로운 보류 표본이 아닙니다.", "",
              "| 비용 조건 | 후보/기준 전체 노드 | 기준/후보 시간 중앙 비율 | 점수 차이 |", "|---|---:|---:|---:|"]
    for cost in result["costs"]:
        lines.append(f"| 깊이 {cost['depth']}, SEE {'켬' if cost['see_enabled'] else '끔'} "
                     f"| {cost['node_ratio_candidate_over_baseline']:.6f} "
                     f"| {cost['median_paired_wall_speedup_baseline_over_candidate']:.6f} | {cost['score_differences']} |")
    lines += ["", "| 대국 조건 | 역할 | 선택 수 | 평균 완료 깊이 | 평균 노드 | 평균/최대 벽시계 ms | 내부/벽시계 예산 초과 수 |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for match in result["matches"]:
        for role, item in match["resources"].items():
            lines.append(f"| {match['movetime_ms']}ms | {role} | {item['searched_moves']} "
                         f"| {item['mean_completed_depth']:.4f} | {item['mean_nodes']:.1f} "
                         f"| {item['mean_wall_ms']:.3f}/{item['max_wall_ms']:.3f} "
                         f"| {item['internal_over_allocated_count']}/{item['wall_over_requested_count']} |")
    return "\n".join(lines) + "\n"


def self_test():
    board = chess.Board()
    require(check_pv(board, ["e2e4", "e7e5"], "e2e4") == 2, "valid PV rejected")
    for pv in (["e2e5"], ["d2d4"]):
        try:
            check_pv(board, pv, "e2e4")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid/mismatching PV accepted")
    require(board_after(chess.STARTING_FEN, ["f2f3", "e7e5", "g2g4", "d8h4"]).is_checkmate(),
            "mate replay failed")
    interval = paired_bootstrap([0.5] * 8)
    require(interval["interval"] == [0.5, 0.5] and interval["seed"] == SEED, "bootstrap mismatch")
    print("PASS: independent audit pure-helper checks (no engine launched)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=ROOT / "build/see-evidence")
    parser.add_argument("--candidate-commit")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.candidate_commit or not re.fullmatch(r"[0-9a-f]{40}", args.candidate_commit):
        parser.error("--candidate-commit requires the full lowercase frozen source commit")
    try:
        outputs = [] if args.output is None else [args.output, args.output.with_suffix(".md")]
        require(len(set(outputs)) == len(outputs), "JSON output and Markdown sibling must differ")
        for path in outputs:
            require(not path.exists(), f"preserve previous output: {path}")
        result = {"status": "PASS", "candidate_commit": args.candidate_commit,
                  "baseline_commit": BASELINE_COMMIT, "chess_version": chess.__version__,
                  "scope": "Independent replay and metadata/hash consistency; reused PVS development openings, not held-out strength or independent binary rebuild.",
                  "see_call_telemetry": "Not collected by the shared match/cost parser; ordering counters are validated separately by C++/UCI tests.",
                  "costs": [], "matches": []}
        for control in (False, True):
            for depth in (3, 4):
                name = f"{'control' if control else 'cost'}-depth{depth}.json"
                result["costs"].append(audit_cost(args.directory / name, args.candidate_commit, depth, control))
        for movetime in (100, 1000):
            result["matches"].append(audit_match(args.directory / f"time{movetime}.json", args.candidate_commit,
                                                movetime, ROOT / "tests/data/pvs_openings_2026-10-18.json"))
        result["runner"] = audit_run_state(args.directory / "run-state.json", args.candidate_commit,
                                          result["costs"], result["matches"])
        body = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(body)
            with args.output.with_suffix(".md").open("x", encoding="utf-8") as stream:
                stream.write(markdown(result))
            print(markdown(result))
        else:
            print(body)
        return 0
    except (ValueError, KeyError, TypeError, OSError, ZeroDivisionError) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
