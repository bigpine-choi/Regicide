"""Independently audit finished LMR records. Never starts an engine or build.

Run only after all search/match measurements have finished. Writes new audit
JSON/Markdown in this directory, refusing to replace prior audit evidence.
"""
from pathlib import Path
import argparse
import collections
import datetime
import hashlib
import json
import math
import statistics
import subprocess

import chess
import chess.pgn


HERE = Path(__file__).resolve().parent
ROOT = next((p for p in (HERE, *HERE.parents, Path.cwd(), *Path.cwd().parents)
             if (p / ".git").exists()), HERE.parents[1])
CODE = "157305d804bc1828641a7e8d9b9b793b7746bcc3"
BASE = "5b5b5deedbef96133836609ec00229c7a548a15a"
CANDIDATE_SHA = "17b079ba319ba7ce8a94c0a78a201d40840b57cda452c9965d5a3d0039400ed1"
BASE_SHA = "b9a7c733388714f4b11d4678f270cc56d8c68e4bc015992e6e08a7b4bf2ab6dc"
FIELDS = ("score", "move", "depth", "nodes", "seldepth", "pv", "tt_counters")
CONFIGS = [(f"off-equivalence-depth{d}", d, False) for d in (3, 4, 5)] + [
    (f"on-cost-depth{d}", d, True) for d in (5, 6)]
CHECKS = 0
FAILURES = []
USED = set()
BINARY_OBSERVATIONS = []


def read(path):
    USED.add(path)
    return path.read_text(encoding="utf-8-sig", errors="replace")


def load(path):
    USED.add(path)
    return json.loads(read(path))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_binary(label, path, expected):
    path = Path(path)
    if path.is_file():
        actual = sha(path)
        check(label + " observed executable bytes", actual == expected)
        BINARY_OBSERVATIONS.append({"label": label, "bytes_available": True,
                                    "observed_sha256": actual, "expected_sha256": expected})
    else:
        BINARY_OBSERVATIONS.append({"label": label, "bytes_available": False,
                                    "expected_sha256": expected,
                                    "scope": "Executable absent/redacted; metadata identity only, no byte verification."})


def check(label, value):
    global CHECKS
    CHECKS += 1
    if not value:
        FAILURES.append(label)


def near(left, right):
    return math.isclose(left, right, rel_tol=2e-9, abs_tol=1e-9)


def board_for(position):
    board = chess.Board(position.get("fen", chess.STARTING_FEN))
    for token in position.get("moves", []):
        move = chess.Move.from_uci(token)
        check("legal declared position history", move in board.legal_moves)
        board.push(move)
    return board


def pv_is_legal(fen, moves, selected):
    board = chess.Board(fen)
    if selected is None or chess.Move.from_uci(selected) not in board.legal_moves:
        return False
    for token in moves:
        move = chess.Move.from_uci(token)
        if move not in board.legal_moves:
            return False
        board.push(move)
    return not moves or moves[0] == selected


def audit_sidecar(path, expected, expected_hashes, settings, mode, depth=None, expected_exit=0):
    data = load(path)
    check(str(path.name) + " capture complete", data["mode"] == mode and
          data["runner_exit_code"] == expected_exit and data["uncaught_error"] is None and
          data["factory_complete"] and data["telemetry_complete_for_returned_plays"] and
          not data["capture_errors"] and len(data["plays"]) == len(expected))
    identities = {item["role"]: item for item in data["engines"]}
    check("two creation roles", [x["role"] for x in data["engines"]] == ["baseline", "candidate"])
    for role, identity in identities.items():
        check("sidecar binary/option identity", identity["binary_sha256"] == expected_hashes[role]
              and identity["startup_completed"] and identity["startup_error"] is None
              and identity["requested_options"] == identity["applied_options"] == settings[role])
    by_role = collections.Counter()
    reductions = collections.Counter()
    researches = collections.Counter()
    for sequence, (play, target) in enumerate(zip(data["plays"], expected), 1):
        role, kind, fen, root, history, result = target
        by_role[role] += 1
        label = f"{path.name} play {sequence}"
        check(label + " identity/history", play["sequence"] == sequence and play["role"] == role
              and play["process_search_index"] == by_role[role] and play["sample_kind"] == kind
              and play["fen"] == fen and play["root_fen"] == root and play["full_history"] == history
              and play["binary_sha256"] == expected_hashes[role])
        check(label + " result", play["selected_move"] == result["move"]
              and all(play[key] == result.get(key) for key in
                      ("nodes", "depth", "seldepth", "engine_time_seconds"))
              and play["returned_from_play"] and play["bestmove_received"]
              and play["error"] is None and not play["counter_errors"])
        limit = play["requested_limit"]
        check(label + " limit", limit.get("nodes") is None and
              (limit.get("depth") == depth and limit.get("time") is None if mode == "benchmark"
               else limit.get("depth") is None and near(limit.get("time"), 0.2)))
        identity = identities[role]
        if not identity["advertises_lmr"]:
            check(label + " absent remains null", play["lmr"] is None and
                  play["lmr_status"] == "not_advertised" and play["lmr_info_lines"] is None)
        else:
            counter = play["lmr"]
            ok = counter is not None and play["lmr_status"] == "reported" and play["lmr_info_lines"] > 0
            check(label + " reported", ok)
            if ok:
                check(label + " counter range", 0 <= counter["researches"] <= counter["reductions"])
                enabled = identity["applied_options"].get("UseLMR", identity["default_lmr"])
                if not enabled:
                    check(label + " OFF counters", counter == {"reductions": 0, "researches": 0})
                reductions[role] += counter["reductions"]
                researches[role] += counter["researches"]
    return {"plays": len(data["plays"]), "per_role": dict(by_role),
            "reductions": dict(reductions), "researches": dict(researches)}


def audit_search(stem, depth, selective):
    data = load(HERE / (stem + ".json"))
    records = data["records"]
    check(stem + " completion/policy", data["exit_code"] == 0 and data["error"] is None and
          len(records) == 30 and data["depth"] == depth and data["repeats"] == 3 and
          data["require_same_search"] == (not selective) and data["allow_score_differences"] == selective)
    hashes = {role: engine["sha256"] for role, engine in data["engines"].items()}
    for role, engine in data["engines"].items():
        expected_code = CODE if selective or role == "candidate" else BASE
        options = engine["configured_options"]
        check(stem + role + " identity", engine["declared_source_commit"] == expected_code and
              options["Hash"] == 16 and options["Move Overhead"] == 20)
        verify_binary(stem + role, engine["path"], hashes[role])
        if role == "candidate":
            check("candidate fixed binary", hashes[role] == CANDIDATE_SHA and options["UseLMR"] == selective)
        elif selective:
            check("same binary OFF baseline", hashes[role] == CANDIDATE_SHA and options["UseLMR"] is False)
        else:
            check("v057 baseline bytes declared consistently", hashes[role] == BASE_SHA)
    expected = []
    positions = data["positions_file"]["document"]["positions"]
    for warmup in data["warmup_records"]:
        board = board_for(positions[warmup["position_index"]])
        expected.append((warmup["role"], "warmup", board.fen(en_passant="fen"),
                         board.root().fen(en_passant="fen"), [m.uci() for m in board.move_stack], warmup["result"]))
    wall, nodes = [], []
    graph_mismatch = score_mismatch = 0
    for index, record in enumerate(records):
        left, right = record["baseline"], record["candidate"]
        differences = [key for key in FIELDS if left.get(key) != right.get(key)]
        graph_mismatch += bool(differences)
        score_mismatch += left["score"] != right["score"]
        check(stem + f" graph flags {index}", record["search_differences"] == differences and
              record["search_equal"] == (not differences) and record["scores_equal"] == (left["score"] == right["score"]))
        if not selective:
            check(stem + f" exact OFF graph {index}", not differences)
        board = board_for(positions[record["position_index"]])
        for role in record["order"]:
            result = record[role]
            tt, memory = result["tt_counters"], result["tt_memory"]
            check(stem + f" TT range/reuse {index} {role}",
                  0 <= tt["cutoffs"] <= tt["hits"] <= tt["probes"] and
                  0 <= tt["stores"] <= tt["probes"] and memory["allocations"] == 0 and
                  memory["reuses"] == 1 and memory["clears"] == 0 and 0 < memory["bytes"] <= 16 * 1024 * 1024)
            check(stem + f" legal depth {index} {role}", result["depth"] == depth and
                  result["search_fen"] == board.fen(en_passant="fen") and
                  pv_is_legal(result["search_fen"], result["pv"], result["move"]))
            expected.append((role, "measured", board.fen(en_passant="fen"),
                             board.root().fen(en_passant="fen"), [m.uci() for m in board.move_stack], result))
        wall.append(right["wall_time_seconds"] / left["wall_time_seconds"])
        nodes.append(right["nodes"] / left["nodes"])
    summary = data["summary"]
    check(stem + " recomputed summary", summary["completed_pairs"] == len(records) and
          summary["score_mismatches"] == score_mismatch and summary["search_graph_mismatches"] == graph_mismatch and
          near(statistics.median([1 / x for x in wall]), summary["median_paired_wall_speedup_baseline_over_candidate"]) and
          near(statistics.median(nodes), summary["median_paired_node_ratio_candidate_over_baseline"]))
    for role in ("baseline", "candidate"):
        for key in ("probes", "hits", "cutoffs", "stores"):
            check(stem + role + key, summary["transposition_counters"][role][key] ==
                  sum(record[role]["tt_counters"][key] for record in records))
    settings = {role: item["configured_options"] for role, item in data["engines"].items()}
    sidecar = audit_sidecar(HERE / (stem + "-lmr.json"), expected, hashes, settings, "benchmark", depth)
    return {"file": stem, "pairs": len(records), "LMR_ON": selective,
            "score_differences": score_mismatch, "graph_differences": graph_mismatch,
            "median_new_over_old_wall": statistics.median(wall),
            "median_new_over_old_nodes": statistics.median(nodes), "sidecar": sidecar}


def audit_match():
    data = load(HERE / "match-200ms.json")
    check("match declared limits", data["limits"] == {"nodes": None, "depth": None, "movetime_ms": 200}
          and data["max_plies"] == 300 and data["draw_policy"] == "claim" and not data["errors"])
    openings = data["selected_openings"]
    check("eight paired starts", len(openings) == 8 and len(data["games"]) == 16)
    pair_counts = collections.Counter(game["pair"] for game in data["games"])
    check("all pairs played both colors", pair_counts == {index: 2 for index in range(1, 9)})
    turns = collections.Counter(board_for(item).turn for item in openings)
    check("four starts per moving color", turns[chess.WHITE] == turns[chess.BLACK] == 4)
    hashes = {role: engine["sha256"] for role, engine in data["engines"].items()}
    for role, engine in data["engines"].items():
        check("match engine provenance " + role, engine["provided_source_commit"] == (CODE if role == "candidate" else BASE)
              and engine["validated_options"]["Hash"] == 16 and engine["validated_options"]["Move Overhead"] == 20)
    check("match candidate LMR", hashes["candidate"] == CANDIDATE_SHA and
          data["engines"]["candidate"]["validated_options"]["UseLMR"] is True)
    check("match baseline bytes declared consistently", hashes["baseline"] == BASE_SHA)
    pgn_path = HERE / "match-200ms.pgn"
    USED.add(pgn_path)
    with pgn_path.open(encoding="utf-8") as stream:
        pgns = []
        while (game := chess.pgn.read_game(stream)) is not None:
            pgns.append(game)
    check("PGN game count", len(pgns) == len(data["games"]))
    expected = []
    scores = []
    unfinished = 0
    for record, pgn in zip(data["games"], pgns):
        check("no match per-game engine error", record["error"] is None)
        opening = openings[record["pair"] - 1]
        check("fixed pair/color/opening", record["opening"] == opening["name"] and
              record["opening_uci_moves"] == opening["moves"] and
              record["white"] == ("candidate" if record["game"] % 2 else "baseline"))
        check("PGN exact JSON moves/result", not pgn.errors and pgn.headers["Result"] == record["result"] and
              [m.uci() for m in pgn.mainline_moves()] == record["uci_moves"])
        check("PGN conditions/pair", pgn.headers["RequestedMoveTimeMS"] == "200" and
              pgn.headers["RequestedNodes"] == pgn.headers["RequestedDepth"] == "-" and
              pgn.headers["Pair"] == str(record["pair"]) and pgn.headers["Round"] == str(record["game"]) and
              pgn.headers["Opening"] == record["opening"] and pgn.headers["DrawPolicy"] == "claim")
        for color, role in (("White", record["white"]), ("Black", record["black"])):
            check("PGN per-side binary/code", pgn.headers[color + "SHA256"] == hashes[role] and
                  pgn.headers[color + "SourceCommit"] == (CODE if role == "candidate" else BASE))
        board = chess.Board(record["initial_fen"])
        searches = iter(record["search_moves"])
        for ply, token in enumerate(record["uci_moves"]):
            move = chess.Move.from_uci(token)
            check("legal full match history", move in board.legal_moves)
            if ply >= len(record["opening_uci_moves"]):
                search = next(searches)
                role = record["white" if board.turn else "black"]
                check("searched move matches board/history", search["ply"] == ply + 1 and search["role"] == role and
                      search["move"] == token and search["fen_before"] == board.fen(en_passant="fen") and
                      pv_is_legal(search["fen_before"], search["pv"], token))
                expected.append((role, "match", board.fen(en_passant="fen"), record["initial_fen"],
                                 record["uci_moves"][:ply], search))
            board.push(move)
        check("no extra search records", next(searches, None) is None)
        check("JSON final board", board.fen(en_passant="fen") == record["final_fen"] and
              record["plies"] == len(record["uci_moves"]))
        outcome = board.outcome(claim_draw=True)
        if record["completed"]:
            points = 0.5 if outcome and outcome.winner is None else float(
                outcome is not None and (outcome.winner == chess.WHITE) == (record["white"] == "candidate"))
            check("independent terminal adjudication", outcome is not None and outcome.result() == record["result"] and
                  outcome.termination.name.lower() == record["termination"] and points == record["candidate_points"])
            scores.append(record["candidate_points"])
        else:
            unfinished += 1
            check("unfinished cap is not a draw", record["result"] == "*" and record["candidate_points"] is None
                  and record["termination"] == "ply_cap")
            if record["termination"] == "ply_cap":
                check("capped game remains nonterminal", outcome is None and record["plies"] >= data["max_plies"])
        for role in ("baseline", "candidate"):
            moves = [move for move in record["search_moves"] if move["role"] == role]
            resource = record["resources"][role]
            check("game resource recomputation", resource["searched_moves"] == len(moves) and
                  resource["reported_nodes"] == sum(move["nodes"] for move in moves if move["nodes"] is not None) and
                  near(resource["elapsed_seconds"], round(sum(move["elapsed_seconds"] for move in moves), 6)))
    summary = data["summary"]
    wins, draws, losses = (sum(x == value for x in scores) for value in (1, 0.5, 0))
    check("WDL excludes unfinished", summary["candidate_wins"] == wins and summary["candidate_draws"] == draws and
          summary["candidate_losses"] == losses and summary["completed_games"] == len(scores) and
          summary["incomplete_games"] == unfinished and summary["candidate_points"] == sum(scores))
    check("completed-only score fraction", summary["candidate_score"] ==
          (sum(scores) / len(scores) if scores else None))
    check("match exit code follows cap/completion", data["exit_code"] == (2 if unfinished else 0) and
          data["completed"] == (unfinished == 0))
    settings = {role: item["validated_options"] for role, item in data["engines"].items()}
    sidecar = audit_sidecar(HERE / "match-200ms-lmr.json", expected, hashes, settings, "match",
                            expected_exit=data["exit_code"])
    return {"completed": len(scores), "unfinished": unfinished, "WDL": [wins, draws, losses],
            "runner_exit_code": data["exit_code"],
            "completed_game_score": sum(scores) / len(scores) if scores else None, "sidecar": sidecar,
            "strength_limit": "Only eight fixed paired starts/16 scheduled games; no universal strength or Elo estimate."}


def add_interpretation(report):
    report["load_interpretation"] = []
    for item in report["CPU_observations"]:
        browser = item["CPU_seconds"]["browser_cpu_seconds"]
        report["load_interpretation"].append({
            "file": item["file"], "observed_browser_CPU_seconds": browser,
            "observed_browser_average_logical_CPU_equivalents": browser / item["covered_seconds"],
            "observed_browser_average_host_capacity_percent": item["host_capacity_percent"]["browser_cpu_seconds"],
            "interpretation": "Nonzero observed shared load; incomplete process sampling cannot prove isolation or attribute outcome differences."})
    observed = report["match"]
    score = observed["completed_game_score"]
    score_text = f"{score:.2%}" if score is not None else "not available (no completed games)"
    match_load = next(x for x in report["load_interpretation"] if x["file"] == "match-load.json")
    report["audit_script_sha256"] = sha(Path(__file__))
    report["conclusions"] = {
        "OFF": "90 recorded search pairs preserve the seven exact graph fields with LMR disabled.",
        "ON_cost": "60 fixed-depth pairs measure selective search cost only; score differences are allowed. Lower nodes or shorter fixed-depth time are not a playing-strength result.",
        "match": f"Observed W/D/L {observed['WDL']} in {observed['completed']} completed games, score {score_text}; {observed['unfinished']} unfinished excluded. This small fixed sample and shared load do not establish general superiority.",
        "confounding_load": f"The match monitor observed {match_load['observed_browser_CPU_seconds']:.2f} browser CPU-seconds, about {match_load['observed_browser_average_host_capacity_percent']:.2f}% of 16-logical-CPU capacity on average. The observed browser load must be considered. This was not an idle or fully controlled host. Neither an intrinsic LMR advantage/disadvantage nor attribution to browser interference is established."}


def markdown(report):
    match = report["match"]
    score_text = f"{match['completed_game_score']:.2%}" if match["completed_game_score"] is not None else "산출 불가"
    lines = ["# LMR 독립 실험 감사", "", f"상태: {report['status']}. {report['checks']}개 항목, 실패 {len(report['failed_checks'])}개.", "",
             f"LMR OFF의 기존 버전 비교 {report['OFF_exact_pairs']}쌍은 기록된 탐색 그래프가 일치했다. ON 비용 비교는 {report['ON_cost_pairs']}쌍이며 같은 깊이의 평가값 차이는 허용했다.", ""]
    for row in report["fixed_depth"]:
        if row["LMR_ON"]:
            lines.append(f"- {row['file']}: ON/OFF 시간 중앙 비율 {row['median_new_over_old_wall']:.5f}, 노드 비율 {row['median_new_over_old_nodes']:.5f}. 1보다 작으면 ON 비용이 낮다.")
    lines += ["", f"완료 대국 {match['completed']}판: {match['WDL'][0]}승 {match['WDL'][1]}무 {match['WDL'][2]}패, 득점률 {score_text}. 미완료 {match['unfinished']}판은 무승부와 점수 분모에서 제외했다.", "",
              "8개 시작점의 소표본이다. 이번 결과는 기력 우위를 입증하지 않았으며 Elo를 추정하지 않는다.", ""]
    for item in report["load_interpretation"]:
        lines.append(f"- {item['file']}: 브라우저 누적 {item['observed_browser_CPU_seconds']:.2f} CPU초, 16개 논리 CPU의 전체 처리 용량 기준 평균 {item['observed_browser_average_host_capacity_percent']:.2f}%가 관측됐다.")
    lines += ["", "특히 대국 중 브라우저 부하가 상당했다. 무부하 또는 완전히 통제된 기력 비교가 아니다. 프로세스 관측에는 누락이 있으며, 결과 차이를 LMR이나 브라우저 부하 한쪽의 원인으로 확정할 수 없다.", "",
              "PGN·JSON의 전체 합법 수, 종료 판정, 선후공, 요청 시간과 각 검색의 LMR 기록을 대조했다. 자세한 수치·실행 파일 정체성·관측 한계는 JSON을 참조한다.", ""]
    return "\n".join(lines)


def main():
    global HERE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=HERE,
                        help="Finished raw or redacted-public evidence directory")
    parser.add_argument("--output-prefix", default="independent-lmr-audit",
                        help="New JSON/Markdown output stem; existing evidence is never replaced")
    args = parser.parse_args()
    HERE = args.evidence.expanduser().resolve()
    if Path(args.output_prefix).name != args.output_prefix:
        parser.error("--output-prefix must be a filename stem, not a path")
    outputs = [HERE / (args.output_prefix + ".json"), HERE / (args.output_prefix + ".md")]
    if any(p.exists() for p in outputs):
        raise RuntimeError("Use new output paths; existing audits must be preserved")
    required = [HERE / (stem + suffix) for stem, _, _ in CONFIGS for suffix in (".json", "-lmr.json")]
    required += [HERE / p for p in ("match-200ms.json", "match-200ms.pgn", "match-200ms-lmr.json", "search-load.json", "match-load.json")]
    if any(not p.exists() or not p.stat().st_size for p in required):
        raise RuntimeError("All final nonempty experiment records are required before audit")
    provenance = load(HERE / "provenance.json")
    check("fixed source provenance", provenance["code_commit"] == CODE and provenance["baseline_commit"] == BASE and
          provenance["candidate_exe_sha256"] == CANDIDATE_SHA and provenance["baseline_exe_sha256"] == BASE_SHA)
    for source in provenance["source_files"]:
        frozen = subprocess.check_output(["git", "show", f"{CODE}:{source['path']}"], cwd=ROOT)
        allowed_hashes = {hashlib.sha256(frozen).hexdigest(),
                          hashlib.sha256(frozen.replace(b"\n", b"\r\n")).hexdigest()}
        check("frozen source " + source["path"], source["sha256"] in allowed_hashes)
    searches = [audit_search(*config) for config in CONFIGS]
    match = audit_match()
    # Use the six frozen-binary suite logs once. Earlier broad source-equivalent
    # checks have a different PE hash and are disclosed separately below.
    import re
    native = {}
    for name in ("tests", "search_tests", "evaluation_tests", "board_state_tests", "transposition_tests", "see_tests"):
        text = read(HERE / ("frozen-regicide_" + name + ".log"))
        rows = re.findall(r"(\d+) groups, (\d+) assertions, (\d+) failures", text)
        check("frozen native suite " + name, len(rows) == 1 and rows[0][2] == "0")
        if rows:
            native[name] = {"groups": int(rows[0][0]), "assertions": int(rows[0][1]), "failures": int(rows[0][2])}
    frozen_uci = read(HERE / "frozen-engine-integration.log")
    frozen_rules = read(HERE / "frozen-differential.log")
    check("frozen UCI26", frozen_uci.count("PASS UCI: ") == 26 and "PASS engine integration: 26 groups" in frozen_uci)
    check("frozen differential2997", "PASS differential: 2997 cases" in frozen_rules)
    broad = load(HERE / "protocol-tools-summary.json")
    check("broad tools same-source binary scope", broad["binary"]["sha256"] == provenance["pre_freeze_final_source_exe_sha256"]
          and broad["unique_unittest_passed"] == 101 and broad["unique_unittest_skipped"] == 1 and
          broad["harness_cases_passed"] == 40 and broad["all_exit_codes"] == 0)
    loads = []
    for filename in ("search-load.json", "match-load.json"):
        data = load(HERE / filename)
        seconds = sum(s["interval_seconds"] for s in data["samples"])
        expected_exit = match["runner_exit_code"] if filename == "match-load.json" else 0
        check(filename + " expected runner exit", data["exit_code"] == expected_exit and data["logical_cpus"] == 16)
        totals = {key: sum(s[key] for s in data["samples"]) for key in data["samples"][0] if key.endswith("cpu_seconds")}
        loads.append({"file": filename, "samples": len(data["samples"]), "covered_seconds": seconds,
                      "CPU_seconds": totals, "host_capacity_percent": {k: 100 * v / seconds / 16 for k, v in totals.items()}})
    report = {"schema": "regicide-independent-lmr-audit-v1", "audit_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "code_commit": CODE, "candidate_sha256": CANDIDATE_SHA, "checks": CHECKS, "failed_checks": FAILURES,
              "status": "passed_with_small_sample_limits" if not FAILURES else "failed", "fixed_depth": searches,
              "OFF_exact_pairs": sum(s["pairs"] for s in searches if not s["LMR_ON"]), "ON_cost_pairs": sum(s["pairs"] for s in searches if s["LMR_ON"]),
              "match": match, "CPU_observations": loads,
              "validation_binary_scope": {"frozen": provenance["candidate_exe_sha256"],
                  "pre_freeze_same_source": provenance.get("pre_freeze_final_source_exe_sha256"),
                  "build_note": provenance.get("build_note")},
              "observed_executable_byte_checks": BINARY_OBSERVATIONS,
              "frozen_correctness": {"native_suites": native,
                  "native_assertions": sum(x["assertions"] for x in native.values()),
                  "UCI_groups": 26, "differential_cases": 2997},
              "broad_same_source_compatibility": {"unit_passed": 101, "unit_skipped": 1,
                  "harness_and_Node_cases": 40, "scope": "Not added to native assertion totals; child suites counted once."},
              "limits": ["CPU aggregates omit processes absent/inaccessible at either endpoint; no idle, affinity, frequency or thermal guarantee.",
                         "Regicide group includes benchmark engines; wrapper CPU excludes some descendants. Remaining load is not exact external interference.",
                         "LMR final counters/identity are matched per returned play; monotonic-line checks rely on the saved sidecar capture error flags, not a separately retained full raw UCI stream.",
                         "Broad backend/Godot compatibility used the pre-freeze same-source link; frozen native/rules/UCI confirmations are separate. Binary identity is not a reproducible-build guarantee.",
                         "OFF graph equality, ON fixed-depth cost, and equal-time small-match score are distinct evidence. Unfinished '*' games never count as draws."],
              "input_hash_scope": "Hashes below identify the exact current evidence copy. Redacted public bytes differ from raw evidence hashes; declared engine/source hashes remain identities of original bytes.",
              "public_manifest_sha256": sha(HERE / "sha256.json") if (HERE / "sha256.json").is_file() else None,
              "evidence_sha256": {str(p.relative_to(HERE)): sha(p) for p in sorted(USED)}}
    add_interpretation(report)
    outputs[0].write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    outputs[1].write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": CHECKS, "failures": FAILURES, "match": match}))
    return int(bool(FAILURES))


if __name__ == "__main__":
    raise SystemExit(main())
