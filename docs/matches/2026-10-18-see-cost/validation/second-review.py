"""Independent archived SEE cost review; imports no project audit/runner code.

Only saved JSON/PGN/logs, executable bytes and Git source are read. No engine,
build, test, profiling process or game search is launched.
"""
import collections
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics
import subprocess
import sys

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MATCH = ROOT / "build/see-cost-comparison/attempt-001"
OUTPUT = HERE / "second-review.json"
BASE = "0edc06e437b934d177fc1e9b0907600b6287be72"
CAND = "42444edc3ff1de358b97ac4d250b43bf342d1df0"
HARNESS = "25f73cacafe4a831f0b3b258fbf5b19632c369b2"
HASHES = {"baseline": "2ba41c8303f266149323b55186f5c6a806c33f73d529d6911d6ca4b33bfa53db",
          "candidate": "722b46c3b7223a3fa354a1fc504cb4cced9be0db8ea1e49d5b796b36d434a335"}
NAMES = {"baseline": "Regicide 0.5.2", "candidate": "Regicide 0.5.3"}
COMMITS = {"baseline": BASE, "candidate": CAND}
OPENING_HASH = "9dafe565121ddad88cef0ca9060bd87b73a7bc52c837240835be0cf5f599ea9e"
EXPECTED_CHECKS = {
    "native-tests", "native-search_tests", "native-evaluation_tests", "native-board_state_tests",
    "native-transposition_tests", "native-see_tests", "differential", "engine_integration",
    "game_runner_tests", "compare_engines_tests", "benchmark_search_tests", "human_play_tests",
    "handicap_play_tests", "game_archive_tests", "godot_bootstrap_tests", "godot_integration_tests",
    "handicap_godot_tests", "archive_godot_tests", "baseline-native-rules", "baseline-differential",
    "frozen-engine-source",
}
FINGERPRINTS = {}


def demand(condition, text):
    if not condition:
        raise AssertionError(text)


def digest(path):
    value = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        name = path.relative_to(ROOT).as_posix()
    except ValueError:
        name = str(path)
    FINGERPRINTS[name] = value
    return value


def read(path):
    digest(path)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def utc(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    demand(result.utcoffset() == timedelta(0), "non-UTC timestamp")
    return result


def close(a, b, label, tolerance=1e-8):
    demand(math.isfinite(a) and math.isfinite(b) and abs(a-b) <= tolerance, label)


def identity(process):
    result = process["pid"], process["creation_ticks"]
    demand(all(type(n) is int and n > 0 for n in result), "bad process identity")
    return result


def ancestry(process, monitor):
    chain = process["ancestry_to_monitor"]
    proof = process["ancestry_processes"]
    demand(len(chain) >= 2 and len(set(chain)) == len(chain), "invalid ancestry chain")
    demand(chain == [p["pid"] for p in proof] and chain[0] == process["pid"]
           and chain[1] == process["parent_pid"] and chain[-1] == monitor[0], "ancestry endpoints")
    demand(identity(proof[0]) == identity(process) and identity(proof[-1]) == monitor, "ancestry identity")
    for child, parent in zip(proof, proof[1:]):
        demand(child["parent_pid"] == parent["pid"] and child["creation_ticks"] >= parent["creation_ticks"],
               "ancestry parent/creation order")


def cpu_review(ms, document):
    summary = read(MATCH / f"time{ms}.monitor.json")
    log = MATCH / f"time{ms}.monitor.jsonl"
    demand(digest(log) == summary["monitor_log_sha256"], "monitor log hash")
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8-sig").splitlines()]
    demand(len(rows) == summary["snapshot_count"] and len(rows) > 22, "monitor row count")
    demand(summary == document["external_engine_guard"], "embedded monitor differs")
    demand(summary["poll_seconds"] == .5 and summary["max_allowed_snapshot_gap_seconds"] == 2
           and summary["external_cpu_tolerance_ticks"] == 0 and summary["cpu_ticks_per_second"] == 10000000,
           "native monitor policy")
    demand(summary["completed_idle_seconds"] >= 10 and summary["new_or_disappearing_external_engine_is_failure"],
           "idle or identity policy")
    demand(summary["baseline_commit"] == BASE and summary["candidate_commit"] == CAND
           and summary["harness_commit"] == HARNESS and summary["movetime_ms"] == ms, "monitor source")
    demand(summary["contaminated"] is False and summary["valid_for_comparison"] is True
           and summary["observed_external_engine_cpu_increase"] is False and summary["error"] is None
           and summary["failure"] is None and summary["exit_code"] == 0, "monitor result")
    monitor = summary["monitor_root_pid"], summary["monitor_root_creation_ticks"]
    demand(set(summary["transport_engines"]) == set(HASHES), "exact two transports")
    owned = {identity(p): p for p in summary["owned_engines"]}
    demand(len(owned) == len(summary["owned_engines"]) == 2, "exact two owned engines")
    transports = summary["transport_engines"]
    demand({identity(t) for t in transports.values()} == set(owned), "owned and transport identities differ")
    for role, process in transports.items():
        demand(process["command"] == document["engines"][role]["command"], "transport command role")
        ancestry(process, monitor)
    external = {identity(p): p["initial_cpu_ticks"] for p in summary["external_engine_identities"]}
    demand(len(external) == len(summary["external_engine_identities"]) == 5 and not set(external) & set(owned),
           "external identity set")
    previous = None
    phases = collections.Counter()
    observed_owned = set()
    previous_owned_cpu = {}
    maximum_gap = maximum_duration = 0
    origin = utc(summary["started_utc"])
    for number, row in enumerate(rows):
        phases[row["phase"]] += 1
        duration, elapsed = row["snapshot_duration_seconds"], row["elapsed_seconds"]
        demand(0 <= duration <= 2 and 0 <= row["gap_seconds"] <= 2, "sampling ceiling")
        close(row["gap_seconds"], duration if previous is None else elapsed-previous["elapsed_seconds"],
              "first snapshot duration/subsequent elapsed delta")
        close((utc(row["utc"])-origin).total_seconds(), elapsed, "wall/elapsed snapshot timestamp", .1)
        if previous is not None:
            demand(elapsed > previous["elapsed_seconds"] and utc(row["utc"]) > utc(previous["utc"]),
                   "non-increasing monitor chronology")
        demand(row["contaminated"] is False and row["external_cpu_delta_ticks"] == 0, "dirty native row")
        current_ids = [identity(p) for p in row["processes"]]
        demand(len(current_ids) == len(set(current_ids)), "duplicate process within snapshot")
        current_external = {}
        current_owned = set()
        for process in row["processes"]:
            key = identity(process)
            name = process["name"].casefold()
            demand(name in {"lc0.exe", "lc0-cpu.exe"} or
                   (name.endswith(".exe") and name.startswith(("regicide", "stockfish"))), "unexpected discovery name")
            if key in owned:
                demand(process["owned"] is True, "owned row flag differs from actual transport")
                ancestry(process, monitor)
                demand(process["parent_pid"] == owned[key]["parent_pid"], "owned parent changed")
                current_owned.add(key)
                observed_owned.add(key)
                demand(process["cpu_ticks"] >= previous_owned_cpu.get(key, 0), "owned CPU reversed")
                previous_owned_cpu[key] = process["cpu_ticks"]
            else:
                demand(process["owned"] is False and not process["exited_during_snapshot"], "external hidden/exit")
                demand(process["ancestry_to_monitor"] is None and process["ancestry_processes"] is None,
                       "external process claims owned ancestry")
                current_external[key] = process["cpu_ticks"]
        demand(current_external == external, f"external identity/CPU changed at row {number}")
        if row["phase"] in {"before-game", "after-game"}:
            demand(current_owned == set(owned), "game boundary lacks both real transports")
            demand(not any(p["exited_during_snapshot"] for p in row["processes"]), "transport exit during game")
        maximum_gap = max(maximum_gap, row["gap_seconds"])
        maximum_duration = max(maximum_duration, duration)
        previous = row
    demand(observed_owned == set(owned) and rows[0]["phase"] == "initial" and rows[-1]["phase"] == "finished",
           "monitor ownership/start/end")
    demand(phases["before-game"] == phases["after-game"] == 16 and phases["idle-complete"] == 1,
           "game boundary coverage")
    close(maximum_gap, summary["max_snapshot_gap_seconds"], "maximum gap summary")
    close(maximum_duration, summary["max_snapshot_duration_seconds"], "maximum duration summary")
    demand(summary["external_cpu_increase_ticks"] == 0, "nonzero external CPU summary")
    demand(utc(rows[0]["utc"]) <= utc(document["started_at"])
           and utc(rows[-1]["utc"]) >= utc(document["started_at"])+timedelta(seconds=document["total_elapsed_seconds"]),
           "monitor does not cover complete match")
    demand(utc(rows[-1]["utc"]) <= utc(summary["finished_utc"]), "last row after finished")
    return {"snapshots": len(rows), "external_engines": len(external), "external_cpu_increase_ticks": 0,
            "real_owned_transports": len(owned), "maximum_gap_seconds": maximum_gap,
            "maximum_snapshot_duration_seconds": maximum_duration,
            "idle_seconds": summary["completed_idle_seconds"], "phases": dict(phases)}


def resource_counts(records):
    return {"searched_moves": len(records), "reported_nodes": sum(s["nodes"] for s in records),
            "moves_without_node_report": 0, "elapsed_seconds": round(sum(s["elapsed_seconds"] for s in records), 6)}


def full_resource(records, ms):
    return {**resource_counts(records),
            "mean_nodes": statistics.mean(s["nodes"] for s in records),
            "mean_depth": statistics.mean(s["depth"] for s in records),
            "mean_wall_ms": statistics.mean(s["elapsed_seconds"]*1000 for s in records),
            "maximum_wall_ms": max(s["elapsed_seconds"]*1000 for s in records),
            "mean_engine_ms": statistics.mean(s["engine_time_seconds"]*1000 for s in records),
            "maximum_engine_ms": max(s["engine_time_seconds"]*1000 for s in records),
            "requested_wall_overruns": sum(s["elapsed_seconds"]*1000 > ms+1e-6 for s in records),
            "internal_allocation_overruns": sum(s["engine_time_seconds"]*1000 > ms-20+1e-6 for s in records)}


def bootstrap(scores, seed):
    rng = random.Random(seed)
    samples = sorted(sum(rng.choices(scores, k=8))/8 for _ in range(20000))
    return [samples[int(.025*19999)], samples[int(.975*19999)]]


def match_review(ms, freeze, openings):
    document = read(MATCH / f"time{ms}.json")
    demand(document["completed"] is True and document["exit_code"] == 0 and document["errors"] == []
           and document["valid_for_comparison"] is True, "match result")
    demand(document["harness_checkout"] == {"commit": HARNESS, "status_porcelain": "",
           "scope": "harness checkout at match start; not proof of either binary's build source"}
           and document["harness_source_commit"] == HARNESS, "frozen clean harness")
    demand(document["limits"] == {"nodes": None, "depth": None, "movetime_ms": ms}
           and document["draw_policy"] == "claim" and document["max_plies"] == 500, "time/adjudication policy")
    expected_opening_document = json.loads(json.dumps(openings))
    for opening in expected_opening_document["openings"]:
        position = chess.Board()
        for text in opening["moves"]:
            position.push_uci(text)
        opening["fen_after_opening"] = position.fen(en_passant="fen")
    demand(document["opening_file_sha256"] == OPENING_HASH and document["opening_document"] == expected_opening_document,
           "fresh opening document and independently derived added FENs")
    demand(document["games_requested"] == len(document["games"]) == 16 and len(document["selected_openings"]) == 8,
           "game schedule size")
    for role, engine in document["engines"].items():
        demand(role in HASHES and engine["provided_source_commit"] == COMMITS[role]
               and engine["sha256"] == HASHES[role] and engine["engine_id"]["name"] == NAMES[role], "engine identity")
        demand(Path(engine["command"][0]).resolve() == (ROOT/freeze[role]["binary"]).resolve(), "engine path")
        options = {name: spec["default"] for name, spec in engine["advertised_options"].items() if spec["type"] != "button"}
        options.update(engine["requested_options"])
        demand(options == freeze["effective_options"], "effective options differ from both-SEE policy")
    demand(set(document["engines"]) == set(HASHES), "engine roles")
    with (MATCH/f"time{ms}.pgn").open(encoding="utf-8-sig") as stream:
        pgns = []
        while (game := chess.pgn.read_game(stream)) is not None:
            demand(not game.errors, "PGN parser error")
            pgns.append(game)
    digest(MATCH/f"time{ms}.pgn")
    demand(len(pgns) == 16, "PGN count")
    all_records = {role: [] for role in HASHES}
    terminations = collections.Counter()
    points = []
    actual_plies = pv_plies = 0
    for index, (game, pgn) in enumerate(zip(document["games"], pgns)):
        pair = index//2+1
        opening = openings["openings"][pair-1]
        demand(game["game"] == index+1 and game["pair"] == pair and game["opening"] == opening["name"]
               and game["opening_uci_moves"] == opening["moves"], "per-game opening schedule")
        selected = document["selected_openings"][pair-1]
        demand(selected["name"] == opening["name"] and selected["moves"] == opening["moves"], "selected opening")
        white = "candidate" if index%2 == 0 else "baseline"
        demand(game["white"] == white and game["black"] == ("baseline" if white == "candidate" else "candidate"),
               "paired colors")
        demand(game["completed"] is True and game["error"] is None and game["plies"] <= 500, "unfinished game")
        demand(game["initial_fen"] == chess.STARTING_FEN, "initial position")
        demand(game["uci_moves"][:len(opening["moves"])] == opening["moves"], "opening prefix")
        demand(len(game["uci_moves"]) == game["plies"] and
               len(game["search_moves"]) == game["plies"]-len(opening["moves"]), "actual/search move count")
        board = chess.Board(game["initial_fen"])
        searched = iter(game["search_moves"])
        for ply, uci in enumerate(game["uci_moves"], 1):
            demand(board.is_valid(), "invalid replay position")
            if ply > len(opening["moves"]):
                demand(board.outcome(claim_draw=True) is None, "game played on after available ending")
                record = next(searched)
                role = game["white" if board.turn else "black"]
                demand(record["ply"] == ply and record["role"] == role
                       and record["color"] == ("white" if board.turn else "black")
                       and record["move"] == uci and record["fen_before"] == board.fen(en_passant="fen"), "move role/FEN")
                demand(record["pv"] and record["pv"][0] == uci, "PV selected move")
                variation = board.copy(stack=True)
                for text in record["pv"]:
                    move = chess.Move.from_uci(text)
                    demand(move in variation.legal_moves, "illegal published PV")
                    variation.push(move)
                    pv_plies += 1
                demand(type(record["nodes"]) is int and record["nodes"] > 0 and
                       type(record["depth"]) is int and record["depth"] >= 0 and
                       record["seldepth"] >= record["depth"], "node/depth report")
                demand(record["elapsed_seconds"] >= 0 and record["engine_time_seconds"] >= 0, "negative time")
                score = record["score"]
                demand(score["pov"] == "mover" and (score["cp"] is None) != (score["mate"] is None), "score shape")
                all_records[role].append(record)
            move = chess.Move.from_uci(uci)
            demand(move in board.legal_moves, "illegal actual move")
            board.push(move)
            if ply == len(opening["moves"]):
                demand(game["opening_fen"] == board.fen(en_passant="fen"), "opening FEN")
            actual_plies += 1
        demand(board.fen(en_passant="fen") == game["final_fen"], "final FEN")
        ending = board.outcome(claim_draw=True)
        demand(ending is not None and ending.result() == game["result"]
               and ending.termination.name.lower() == game["termination"], "termination adjudication")
        point = .5 if ending.winner is None else float(game["white" if ending.winner else "black"] == "candidate")
        demand(point == game["candidate_points"], "candidate game points")
        points.append(point)
        terminations[game["termination"]] += 1
        demand([move.uci() for move in pgn.mainline_moves()] == game["uci_moves"], "PGN and JSON moves")
        demand(pgn.headers["Round"] == str(index+1) and pgn.headers["Pair"] == str(pair)
               and pgn.headers["Result"] == game["result"] and pgn.headers["Opening"] == opening["name"]
               and pgn.headers["TerminationReason"] == game["termination"] and pgn.headers["DrawPolicy"] == "claim"
               and pgn.headers["RequestedMoveTimeMS"] == str(ms), "PGN metadata")
        for color in ("White", "Black"):
            role = game[color.lower()]
            demand(pgn.headers[color] == f"{role}: {NAMES[role]}" and pgn.headers[color+"SHA256"] == HASHES[role]
                   and pgn.headers[color+"SourceCommit"] == COMMITS[role], "PGN engine provenance")
        for role in HASHES:
            subset = [r for r in game["search_moves"] if r["role"] == role]
            demand(resource_counts(subset) == game["resources"][role], "game resource summary")
    summary = document["summary"]
    counts = {"candidate_wins": points.count(1), "candidate_draws": points.count(.5),
              "candidate_losses": points.count(0), "candidate_points": sum(points),
              "candidate_score": sum(points)/16, "completed_games": 16, "incomplete_games": 0,
              "unplayed_games": 0, "scheduled_games": 16, "attempted_games": 16}
    demand(all(summary[k] == value for k, value in counts.items()), "WDL/score/schedule summary")
    pair_scores = [sum(points[n:n+2])/2 for n in range(0,16,2)]
    for n, pair in enumerate(summary["pairs"]):
        demand(pair["pair"] == n+1 and pair["candidate_score"] == pair_scores[n]
               and pair["candidate_points"] == pair_scores[n]*2 and pair["completed"] is True,
               "pair summary")
    demand(summary["paired_opening_bootstrap95_descriptive"]["interval"] == bootstrap(pair_scores, 20261017),
           "preserved harness bootstrap")
    for role in HASHES:
        demand(resource_counts(all_records[role]) == summary["resources"][role], "full resource summary")
    return {"movetime_ms": ms, **counts, "actual_plies": actual_plies, "pv_plies": pv_plies,
            "terminations": dict(terminations), "pair_scores": pair_scores,
            "experiment_paired_bootstrap95": {"seed": 20261020, "resamples": 20000,
                                             "interval": bootstrap(pair_scores, 20261020)},
            "resources": {role: full_resource(records, ms) for role, records in all_records.items()},
            "native_cpu": cpu_review(ms, document)}


def main():
    demand(not OUTPUT.exists(), "Preserve an existing second-review.json")
    freeze_path = ROOT/"tests/data/see_cost_freeze_2026-10-18.json"
    freeze = read(freeze_path)
    opening_path = ROOT/freeze["openings"]["path"]
    demand(digest(opening_path) == OPENING_HASH, "held-out opening hash")
    openings = read(opening_path)
    demand(len(openings["openings"]) == 8 and set(freeze["required_validation_checks"]) == EXPECTED_CHECKS,
           "frozen openings/gates")
    for role in HASHES:
        demand(freeze[role]["commit"] == COMMITS[role] and freeze[role]["sha256"] == HASHES[role]
               and freeze[role]["engine_name"] == NAMES[role], "freeze engine identity")
        demand(digest(ROOT/freeze[role]["binary"]) == HASHES[role], "actual C1/baseline executable changed")
    for commit in (BASE, CAND, HARNESS):
        subprocess.run(["git", "cat-file", "-e", commit+"^{commit}"], cwd=ROOT, check=True)
    source_scopes = ["src", "include", "CMakeLists.txt", "scripts/build.ps1"]
    subprocess.run(["git", "diff", "--quiet", CAND, "--", *source_scopes], cwd=ROOT, check=True)
    state = read(MATCH/"run-state.json")
    attempts = sorted(p.name for p in MATCH.parent.iterdir() if p.is_dir() and re.fullmatch("attempt-[0-9]{3}",p.name))
    demand(attempts == ["attempt-001"], "unexpected additional attempt; inventory needs review")
    demand(state["attempt"] == "attempt-001" and state["candidate_commit"] == CAND
           and state["baseline_commit"] == BASE and state["harness_commit"] == HARNESS
           and state["binary_sha256"] == HASHES and state["opening_sha256"] == OPENING_HASH
           and state["freeze_manifest_sha256"] == digest(freeze_path), "attempt frozen inputs")
    demand(state["contaminated"] is False and state["exit_code"] == 0 and state["error"] is None
           and state["restore_succeeded"] is True and not state["persistent_power_policy_changed"], "runner result/power")
    demand(state["adopted_blocks"] == {"time100": "time100.json", "time1000": "time1000.json"}
           and [step["name"] for step in state["steps"]] == ["time100", "time1000"], "first complete clean blocks")
    previous_end = utc(state["started_utc"])
    for step in state["steps"]:
        demand(previous_end <= utc(step["started_utc"]) <= utc(step["finished_utc"]) <= utc(state["finished_utc"]),
               "overlapping time blocks")
        previous_end = utc(step["finished_utc"])
        demand(step["exit_code"] == 0 and step["valid_for_comparison"] is True
               and step["contaminated"] is False and len(step["artifact_sha256"]) == 6, "step result")
        for filename, expected in step["artifact_sha256"].items():
            demand(digest(MATCH/filename) == expected, "runner raw artifact fingerprint")
        demand((MATCH/(step["name"]+".stderr.log")).read_bytes() == b"", "nonempty engine helper stderr")
    validation_path = Path(state["validation_path"])
    demand(digest(validation_path) == state["validation_sha256"], "validation summary frozen hash")
    validation = read(validation_path)
    demand(validation["all_passed"] is True and validation["engine_source_commit"] == CAND
           and validation["binary_sha256"] == HASHES and validation["checks_count"] == 21
           and validation["suite_count"] == 20 and len(validation["checks"]) == 21
           and {g["name"] for g in validation["checks"]} == EXPECTED_CHECKS, "21 gate summary identity")
    groups = assertions = skipped = 0
    coverage = {}
    for gate in validation["checks"]:
        log = ROOT/gate["log"]
        demand(digest(log) == gate["sha256"] and gate["result"] == "passed" and gate["exit_code"] == 0,
               "gate bytes/result mismatch")
        demand(utc(gate["started_utc"]) <= utc(gate["ended_utc"]) <= utc(validation["completed_utc"]), "gate time")
        skipped += gate["skipped_cases"]
        if gate["name"].startswith("native-"):
            summaries = re.findall(r"(?m)^(\d+) groups, (\d+) assertions, (\d+) failures\s*$", log.read_text(encoding="utf-8-sig"))
            demand(len(summaries) == 1 and int(summaries[0][2]) == 0, "native gate coverage")
            g, a, _ = map(int,summaries[0])
            coverage[gate["name"]] = {"groups":g,"assertions":a}
            groups += g
            assertions += a
    demand(groups == validation["candidate_native_groups"] and assertions == validation["candidate_native_assertions"]
           and skipped == validation["skipped_case_count"] == 1, "gate coverage/skip totals")
    demand(next(g for g in validation["checks"] if g["name"] == "game_archive_tests")["skipped_cases"] == 1,
           "unexpected skipped suite")
    demand(utc(validation["completed_utc"]) < utc(state["started_utc"]), "validation overlaps match")
    results = [match_review(ms,freeze,openings) for ms in (100,1000)]
    digest(Path(__file__))
    output = {"status":"PASS", "method":"Independent stdlib/python-chess implementation; no project audit/runner imports; no engine executed",
              "baseline_commit":BASE,"candidate_commit":CAND,"harness_commit":HARNESS,"binary_sha256":HASHES,
              "python_version":sys.version,"chess_version":chess.__version__,
              "source_script":Path(__file__).relative_to(ROOT).as_posix(),
              "source_sha256":FINGERPRINTS[Path(__file__).relative_to(ROOT).as_posix()],
              "validation":{"gates":21,"native_groups":groups,"native_assertions":assertions,
                            "skipped_cases":skipped,"native_coverage":coverage},
              "attempts":attempts,"matches":results,"input_sha256":FINGERPRINTS,
              "limitations":["Replay verifies recorded legality and adjudication, not optimal move quality.",
                             "Native sampled discovery excludes observed named external CPU only; unknown/transient processes and other host load remain outside this scope.",
                             "Timed resources come from different played positions; they are not controlled fixed-position cost estimates."]}
    with OUTPUT.open("x",encoding="utf-8",newline="\n") as stream:
        stream.write(json.dumps(output,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"status":"PASS","games":32,"actual_plies":sum(r["actual_plies"] for r in results),
                      "pv_plies":sum(r["pv_plies"] for r in results),
                      "native_cpu_snapshots":sum(r["native_cpu"]["snapshots"] for r in results),
                      "matches":[{k:r[k] for k in ("movetime_ms","candidate_wins","candidate_draws","candidate_losses","candidate_score")} for r in results]},indent=2))


if __name__ == "__main__":
    main()
