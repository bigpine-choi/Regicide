"""Prepared auditor for neural-extra-time matches against 1s classical; no engines.

Two primary opening pairs and the preselected one-pair secondary prefix are
verified independently from raw records. Engine build provenance is distinct
from the checkout and frozen uncommitted harness bytes. This source and its
self-test do not establish that a long-time match has actually been run.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
from pathlib import Path
import random
import re

import chess

from audit_see_results import (
    ROOT, ROLES, audit_game, audit_pgn, board_after, digest, effective_options,
    read_json, require, resources_summary,
)
from audit_see_replication import checked_chain, engine_name, identity, numeric, utc

HARNESS_BOOTSTRAP_SEED = 20261017
HARNESS_BOOTSTRAP_RESAMPLES = 20000
SCOPE = ("Budget-limited extra-time observation: classical always receives 1000ms, "
         "neural receives 3000ms for two pairs or 5000ms for the first one-pair prefix. "
         "No equal-time strength claim, confidence interval, Elo, or independent replication.")


def condition(freeze, movetime):
    key = str(movetime)
    require(movetime in freeze["time_controls_ms"], "unplanned time control")
    pairs = freeze["condition_opening_count"][key]
    games = freeze["games_per_time_control"][key]
    role = "primary" if movetime == freeze["primary_time_ms"] else "secondary"
    require(games == 2 * pairs, "planned games do not equal two colors per opening")
    return {"role": role, "opening_count": pairs, "games_requested": games}


def validate_plan(freeze):
    for field in ("engine_source_commit", "checkout_commit"):
        require(re.fullmatch(r"[0-9a-f]{40}", freeze[field]) is not None,
                f"{field} must be a full lowercase object ID")
    require(freeze["time_controls_ms"] == [3000, 5000]
            and freeze["primary_time_ms"] == 3000 and freeze["secondary_time_ms"] == 5000,
            "unexpected primary/secondary time controls")
    require(freeze["opening_count"] == 12
            and freeze["condition_opening_count"] == {"3000": 2, "5000": 1}
            and freeze["games_per_time_control"] == {"3000": 4, "5000": 2},
            "changed preregistered primary/secondary sample counts")
    require(freeze["comparison_mode"] == "neural_extra_time"
            and freeze["role_movetime_ms"] == {
                "3000": {"baseline": 1000, "candidate": 3000},
                "5000": {"baseline": 1000, "candidate": 5000}},
            "unexpected role-specific time allocation")
    require(utc(freeze["deadline_utc"]) == utc("2026-10-19T07:11:49Z"),
            "changed preregistered experiment deadline")
    require(freeze["max_plies"] == 500 and freeze["draw_policy"] == "claim",
            "unexpected planned ending policy")
    for movetime in freeze["time_controls_ms"]:
        condition(freeze, movetime)


def resolved(value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def bound_inputs(freeze_path, freeze):
    require(freeze["schema_version"] == 1, "unsupported neural match freeze")
    validate_plan(freeze)
    files = {str(resolved(path)): value for path, value in freeze["files"].items()}
    required_sources = {"tests/audit_neural_long_time_results.py", "tests/audit_see_results.py",
                        "tests/audit_see_replication.py", "scripts/run_neural_long_time_comparison.py",
                        "scripts/compare_engines.py", "scripts/see_clean_guard.py"}
    require({str(resolved(path)) for path in required_sources} <= set(files),
            "freeze omits executed driver, auditor or replay helper sources")
    for role in ROLES:
        item = freeze[role]
        files[str(resolved(item["binary"]))] = item["sha256"]
    for field in ("model", "openings", "prior_validation"):
        item = freeze[field]
        files[str(resolved(item["path"]))] = item["sha256"]
    require(freeze["baseline"]["sha256"] == freeze["candidate"]["sha256"],
            "the experiment must use identical engine bytes for both roles")
    require(resolved(freeze["baseline"]["binary"]) != resolved(freeze["candidate"]["binary"]),
            "role binary paths must be distinct for native PID ownership auditing")
    for path, expected in files.items():
        require(re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
                f"invalid frozen SHA256: {path}")
        require(Path(path).is_file() and digest(Path(path)) == expected,
                f"frozen input changed: {path}")
    return files


def audit_snapshot(snapshot, freeze_path, freeze, expected):
    require(snapshot["freeze_sha256"] == digest(freeze_path), "freeze receipt SHA mismatch")
    require(snapshot["engine_source_commit"] == freeze["engine_source_commit"]
            and snapshot["checkout_commit"] == freeze["checkout_commit"],
            "snapshot engine source/checkout commit mismatch")
    actual = {str(resolved(path)): value for path, value in snapshot["files"].items()}
    require(actual == expected, "snapshot does not bind exactly all frozen inputs")
    return utc(snapshot["checked_utc"])


def audit_engines(document, freeze):
    result = {}
    common = freeze["common_effective_options"]
    require(common.get("Hash") == 16 and common.get("Move Overhead") == 20,
            "planned Hash or overhead changed")
    for name in ("UsePositionalEvaluation", "UseQuiescence", "UseTranspositionTable",
                 "UseFastMoves", "UseQuietMoveOrdering", "UseQuiescenceChecks",
                 "PVS", "UseSEE", "UseIncrementalNeuralEvaluation"):
        require(common.get(name) is True, f"planned option {name} must be enabled")
    for role in ROLES:
        item = document["engines"][role]
        require(item["command"] == [str(resolved(freeze[role]["binary"])), "uci"],
                f"{role} command differs from frozen executable")
        require(item["sha256"] == freeze[role]["sha256"]
                and item["provided_source_commit"] == freeze["engine_source_commit"]
                and item["source_commit_object_verified"] is True,
                f"{role} source or executable identity mismatch")
        options = effective_options(item, "requested_options")
        neural_option = "UseNNUEEvaluation" if "UseNNUEEvaluation" in options else "UseNeuralEvaluation"
        if neural_option == "UseNNUEEvaluation":
            require(options.get("UseNeuralEvaluation") is False, "residual evaluator must be disabled for NNUE comparison")
        for name, value in common.items():
            require(options.get(name) == value, f"unequal effective {role} option {name}")
        require(options.get(neural_option) is (role == "candidate"),
                "classical/neural evaluator assignment is wrong")
        if role == "candidate":
            require(resolved(options["NeuralModelFile"]) == resolved(freeze["model"]["path"]),
                    "candidate model path mismatch")
            require(item["requested_options"] == {
                neural_option: True, "UseIncrementalNeuralEvaluation": True,
                "NeuralModelFile": str(resolved(freeze["model"]["path"]))},
                "candidate requested options contain an unplanned change")
        else:
            require(item["requested_options"] == {}, "baseline no longer uses advertised defaults")
        result[role] = {"sha256": item["sha256"], "source_commit": item["provided_source_commit"],
                        "id": item["engine_id"], "effective_options": options}
    require(result["baseline"]["id"] == result["candidate"]["id"],
            "same executable reported differing engine identities")
    return result


def bootstrap(scores, seed=HARNESS_BOOTSTRAP_SEED, resamples=HARNESS_BOOTSTRAP_RESAMPLES):
    generator = random.Random(seed)
    samples = sorted(sum(generator.choices(scores, k=len(scores))) / len(scores)
                     for _ in range(resamples))
    return [samples[int(0.025 * (resamples - 1))], samples[int(0.975 * (resamples - 1))]]


def numeric_info(line):
    """Parse this engine's final numerical UCI info independently of python-chess."""
    words = line.split()
    if not words or words[0] != "info" or "string" in words:
        return None
    result, index = {}, 1
    while index < len(words):
        key = words[index]
        if key in {"depth", "seldepth", "nodes", "time"}:
            require(index + 1 < len(words), "truncated numerical UCI field")
            result[key] = int(words[index + 1])
            index += 2
        elif key == "score":
            require(index + 2 < len(words) and words[index + 1] in ("cp", "mate"),
                    "unsupported raw UCI score")
            score = {"pov": "mover", "cp": None, "mate": None,
                     "lowerbound": False, "upperbound": False}
            score[words[index + 1]] = int(words[index + 2])
            index += 3
            while index < len(words) and words[index] in ("lowerbound", "upperbound"):
                score[words[index]] = True
                index += 1
            result["score"] = score
        elif key == "pv":
            result["pv"] = words[index + 1:]
            break
        else:
            index += 1
    return result if "depth" in result else None


def position_command(line):
    words = line.split()
    require(len(words) >= 2 and words[0] == "position", "missing raw position command")
    if words[1] == "startpos":
        initial, index = chess.STARTING_FEN, 2
    else:
        require(words[1] == "fen" and len(words) >= 8, "invalid raw FEN command")
        initial, index = " ".join(words[2:8]), 8
    history = []
    if len(words) > index:
        require(words[index] == "moves", "invalid full-history delimiter")
        history = words[index + 1:]
    return initial, history, board_after(initial, history)


def audit_uci(rows, document, freeze):
    """Bind every raw search and loaded-model acknowledgement to game records."""
    expected = [(game, move) for game in document["games"] for move in game["search_moves"]]
    require(rows and expected, "empty UCI stream or match")
    positions = {}
    new_games = {role: 0 for role in ROLES}
    pending, index, previous_time, previous_utc = None, 0, -1.0, None
    ack, model_set = None, None
    checksum_line = f"info string neural model checksum {freeze['model']['checksum']}"
    first_go = None
    for sequence, row in enumerate(rows, 1):
        require(type(row["seq"]) is int and row["seq"] == sequence,
                "raw UCI sequence missing, duplicated or reordered")
        require(row["role"] in ROLES and row["direction"] in ("send", "recv"),
                "invalid raw UCI endpoint")
        stamp = numeric(row["monotonic"], "raw UCI monotonic timestamp")
        timestamp = utc(row["utc"])
        require(stamp >= previous_time and (previous_utc is None or timestamp >= previous_utc),
                "raw UCI timestamps reversed")
        previous_time, previous_utc = stamp, timestamp
        role, line = row["role"], row["line"]
        require(isinstance(line, str) and "\n" not in line and "\r" not in line,
                "invalid raw UCI line")
        if row["direction"] == "send":
            if line == "ucinewgame":
                require(pending is None, "new game while a search is pending")
                new_games[role] += 1
            elif line.startswith("setoption name NeuralModelFile value "):
                require(role == "candidate" and model_set is None,
                        "unexpected or repeated loaded-model command")
                model_set = row
                require(line == "setoption name NeuralModelFile value " +
                        str(resolved(freeze["model"]["path"])), "raw selected model differs")
            elif line.startswith("position "):
                require(pending is None, "position changed while search pending")
                positions[role] = position_command(line)
            elif line.startswith("go "):
                requested = freeze["role_movetime_ms"][str(document["limits"]["movetime_ms"])][role]
                require(line.split() == ["go", "movetime", str(requested)],
                        "raw UCI search budget differs from planned role-specific movetime")
                require(pending is None and index < len(expected),
                        "overlapping or unexpected additional raw search")
                game, move = expected[index]
                require(role == move["role"] and role in positions, "raw search role/order differs")
                initial, history, board = positions[role]
                require(initial == game["initial_fen"] and history == game["uci_moves"][:move["ply"] - 1]
                        and board.fen(en_passant="fen") == move["fen_before"],
                        "raw search position/full history differs from game")
                require(new_games[role] == game["game"], "raw game lacks exactly one fresh-game reset")
                require(ack is not None and model_set is not None,
                        "a search started before selected-model load acknowledgement")
                first_go = first_go or sequence
                pending = {"role": role, "move": move, "info": None}
            elif line == "stop":
                raise ValueError("unexpected stop command in an accepted fixed-movetime block")
        elif line == checksum_line:
            require(role == "candidate" and ack is None and model_set is not None,
                    "unexpected or repeated selected-model acknowledgement")
            ack = row
        elif line.startswith("bestmove "):
            require(pending is not None and pending["role"] == role,
                    "raw bestmove without its serial pending search")
            move, info = pending["move"], pending["info"]
            require(line.split()[1] == move["move"], "raw bestmove differs from game move")
            require(info is not None and info.get("score") == move["score"]
                    and info.get("pv", []) == move["pv"], "raw score/PV differs from recorded result")
            for key in ("nodes", "depth", "seldepth"):
                require(info.get(key) == move[key], f"raw {key} differs from game search")
            require(abs(info["time"] / 1000 - move["engine_time_seconds"]) < 1e-9,
                    "raw numerical search time differs from game")
            pending, index = None, index + 1
        elif pending is not None and pending["role"] == role:
            info = numeric_info(line)
            if info is not None:
                pending["info"] = info
    require(pending is None and index == len(expected), "raw search stream does not cover all played moves")
    require(all(count == document["games_requested"] for count in new_games.values()),
            "raw fresh-game resets do not cover every game and role")
    require(ack is not None and model_set is not None and model_set["seq"] < ack["seq"] < first_go,
            "selected-model acknowledgement order differs from preflight")
    return {"status": "PASS", "rows": len(rows), "searched_moves": index,
            "condition_label_ms": document["limits"]["movetime_ms"],
            "role_movetime_ms": freeze["role_movetime_ms"][str(document["limits"]["movetime_ms"])],
            "new_game_resets": new_games, "model_ack": ack,
            "model_setoption_seq": model_set["seq"], "first_go_seq": first_go}


def audit_games(document, freeze):
    opening_path = resolved(freeze["openings"]["path"])
    require(document["opening_file_sha256"] == freeze["openings"]["sha256"],
            "match opening file differs from freeze")
    planned = condition(freeze, document["limits"]["movetime_ms"])
    all_openings = read_json(opening_path)["openings"]
    require(len(all_openings) == freeze["opening_count"], "frozen full opening inventory count differs")
    expected = all_openings[:planned["opening_count"]]
    require(len(expected) == len(document["selected_openings"]) == planned["opening_count"],
            "condition does not use exactly its preselected opening prefix")
    games = planned["games_requested"]
    require(document["games_requested"] == len(document["games"]) == games,
            "scheduled and recorded games differ from the fixed condition count")
    points, pair_scores, pair_details, terminations = [], [], [], {}
    resources = {role: [] for role in ROLES}
    plies = pv_plies = 0
    starts = set()
    for pair, opening in enumerate(expected, 1):
        selected = document["selected_openings"][pair - 1]
        require(selected["name"] == opening["name"] and selected["moves"] == opening["moves"],
                "opening schedule differs from frozen file")
        board = board_after(chess.STARTING_FEN, opening["moves"])
        require(board.outcome(claim_draw=True) is None, "already completed starting position")
        key = " ".join(board.fen(en_passant="legal").split()[:4])
        require(key not in starts, "duplicate starting position in planned pairs")
        starts.add(key)
        require(selected["fen_after_opening"] == board.fen(en_passant="fen"),
                "selected opening castling/EP/counter state differs from replay")
        paired, results, game_numbers = [], [], []
        for offset in (0, 1):
            number = (pair - 1) * 2 + offset + 1
            record = document["games"][number - 1]
            require(record["game"] == number and record["pair"] == pair
                    and record["opening"] == opening["name"]
                    and record["opening_uci_moves"] == opening["moves"], "game schedule mismatch")
            require(record["white"] == ("candidate" if offset == 0 else "baseline")
                    and record["black"] == ("baseline" if offset == 0 else "candidate"),
                    "opening pair did not swap colors")
            audited = audit_game(record)
            points.append(audited["points"])
            paired.append(audited["points"])
            results.append(record["result"])
            game_numbers.append(number)
            plies += audited["plies"]
            pv_plies += audited["pv_plies"]
            end = audited["termination"]
            terminations[end] = terminations.get(end, 0) + 1
            for role in ROLES:
                resources[role].extend(audited["resources"][role])
                values = record["resources"][role]
                require(values["moves_without_node_report"] == 0, "missing move node report")
                wall = round(sum(item["elapsed_seconds"] for item in audited["resources"][role]), 6)
                require(abs(values["elapsed_seconds"] - wall) < 1e-7,
                        "per-game resource wall time mismatch")
        pair_scores.append(sum(paired) / 2)
        pair_details.append({"pair": pair, "opening": opening["name"], "games": game_numbers,
                             "completed": True, "results": results, "candidate_points": sum(paired),
                             "candidate_score": sum(paired) / 2})
    counts = {"candidate_wins": points.count(1.0), "candidate_draws": points.count(0.5),
              "candidate_losses": points.count(0.0), "candidate_points": sum(points),
              "candidate_score": sum(points) / games, "completed_games": games,
              "incomplete_games": 0, "unplayed_games": 0, "scheduled_games": games,
              "attempted_games": games}
    summary = document["summary"]
    for name, value in counts.items():
        require(summary[name] == value, f"incorrect match summary {name}")
    require(summary["pairs"] == pair_details, "paired-game summary differs from replay")
    require(document["total_elapsed_seconds"] + 0.001 >= sum(
        game["elapsed_seconds"] for game in document["games"]), "match duration shorter than game durations")
    for role in ROLES:
        aggregate = summary["resources"][role]
        require(aggregate["searched_moves"] == len(resources[role])
                and aggregate["reported_nodes"] == sum(item["nodes"] for item in resources[role])
                and aggregate["moves_without_node_report"] == 0
                and abs(aggregate["elapsed_seconds"] - round(sum(
                    item["elapsed_seconds"] for item in resources[role]), 6)) < 1e-7,
                "aggregate move resources differ from raw move records")
    require(summary["paired_opening_bootstrap95_descriptive"] is None
            and summary["wilson95_descriptive"] is None,
            "asymmetric tiny sample must not report confidence intervals")
    interval_result = None
    return {**counts, "actual_plies": plies, "pv_plies": pv_plies, "terminations": terminations,
            "pair_scores": pair_scores, "paired_opening_bootstrap95_descriptive": interval_result,
            "resources": {role: {"requested_movetime_ms": freeze["role_movetime_ms"][
                str(document["limits"]["movetime_ms"])][role],
                **resources_summary(resources[role], freeze["role_movetime_ms"][
                    str(document["limits"]["movetime_ms"])][role])} for role in ROLES}}


# Native process evidence is replayed below; no live process inspection is used.


def replay_monitor(summary, rows, document, movetime, source_commit, harness_commit, game_count):
    """Recompute external identity sets and native CPU deltas from every row."""
    require(summary["schema_version"] == 1, "wrong monitor schema")
    require(summary["poll_seconds"] == 0.5
            and summary["max_allowed_snapshot_gap_seconds"] == 2.0,
            "wrong CPU polling/gap policy")
    require(summary["external_cpu_tolerance_ticks"] == 0
            and summary["cpu_ticks_per_second"] == 10_000_000,
            "wrong native CPU tick policy")
    require(summary["new_or_disappearing_external_engine_is_failure"] is True,
            "external identity changes were allowed")
    require(summary["completed_idle_seconds"] >= 10, "missing ten-second idle interval")
    require(summary["contaminated"] is False and summary["valid_for_comparison"] is True
            and summary["observed_external_engine_cpu_increase"] is False,
            "monitor did not certify a clean block")
    require(summary["failure"] is None and summary["error"] is None and summary["exit_code"] == 0,
            "monitor has a failure or nonzero exit")
    require(summary["candidate_commit"] == source_commit
            and summary["baseline_commit"] == source_commit
            and summary["harness_commit"] == harness_commit
            and summary["movetime_ms"] == movetime, "monitor source/time mismatch")
    require(len(rows) == summary["snapshot_count"] and len(rows) >= 22,
            "monitor row count/idle coverage mismatch")
    require(rows[0]["phase"] == "initial" and rows[-1]["phase"] == "finished",
            "monitor start/finish rows missing")
    root_identity = (summary["monitor_root_pid"], summary["monitor_root_creation_ticks"])
    require(all(isinstance(value, int) and value > 0 for value in root_identity),
            "invalid monitor process identity")
    names = {name.casefold() for name in summary["engine_names"]}
    require({"regicide.exe", "stockfish.exe", "lc0.exe", "lc0-cpu.exe"} <= names,
            "required known engine names not monitored")
    transports = summary["transport_engines"]
    require(set(transports) == set(ROLES), "not exactly two role-labelled UCI transports")
    registered = {identity(item): role for role, item in transports.items()}
    require(len(registered) == 2, "both roles claim one UCI transport")
    owned = {identity(item): item for item in summary["owned_engines"]}
    require(len(summary["owned_engines"]) == 2 and set(owned) == set(registered),
            "excluded owned engines differ from actual UCI transports")
    for process_identity, role in registered.items():
        item = transports[role]
        require(item["command"] == document["engines"][role]["command"],
                "transport command differs from role binary")
        require(Path(item["command"][0]).name.casefold() == item["name"].casefold(),
                "transport process name differs from executable")
        checked_chain(item, root_identity)
        checked_chain(owned[process_identity], root_identity)
        for field in ("pid", "creation_ticks", "parent_pid", "name", "ancestry_to_monitor", "ancestry_processes"):
            require(item[field] == owned[process_identity][field], "transport/owned identity mismatch")
    first_external = None
    previous = None
    seen_owned = set()
    cpu_total = 0
    max_gap = 0.0
    max_duration = 0.0
    first_utc = utc(rows[0]["utc"])
    previous_utc = None
    previous_elapsed = None
    idle_complete = None
    phases = []
    for index, row in enumerate(rows):
        require(row["contaminated"] is False, "contaminated monitor row")
        elapsed = numeric(row["elapsed_seconds"], "snapshot monotonic elapsed")
        duration = numeric(row["snapshot_duration_seconds"], "snapshot duration")
        require(duration <= 2.0, "native snapshot itself exceeded the fail-closed bound")
        max_duration = max(max_duration, duration)
        timestamp = utc(row["utc"])
        # The frozen guard timestamps snapshot completion. Its first gap is
        # that initial snapshot's duration; later gaps are completion deltas.
        gap = duration if previous_elapsed is None else elapsed - previous_elapsed
        require(gap >= 0 and gap <= 2.0, "nonmonotonic or excessive snapshot gap")
        require(abs(gap - numeric(row["gap_seconds"], "snapshot gap")) <= 1e-7,
                "reported snapshot gap differs from monotonic evidence")
        require(previous_utc is None or timestamp >= previous_utc, "UTC snapshot order reversed")
        require(abs((timestamp - first_utc).total_seconds() - (elapsed - rows[0]["elapsed_seconds"])) < 2,
                "UTC and monotonic coverage diverged")
        max_gap = max(max_gap, gap)
        previous_elapsed, previous_utc = elapsed, timestamp
        current = {}
        row_identities = set()
        for process in row["processes"]:
            require(not process.get("error"), "unmeasurable engine process in raw monitor")
            process_identity = identity(process)
            require(process_identity not in row_identities, "duplicate process identity in snapshot")
            row_identities.add(process_identity)
            require(engine_name(process["name"], names), "raw snapshot includes an undeclared process")
            ticks = process["cpu_ticks"]
            require(isinstance(ticks, int) and not isinstance(ticks, bool) and ticks >= 0,
                    "invalid native process CPU counter")
            require(type(process["owned"]) is bool, "invalid owned process flag")
            require(type(process["exited_during_snapshot"]) is bool, "invalid process exit observation")
            if process["owned"]:
                require(process_identity in registered, "claimed owned process is not an actual UCI transport")
                if process_identity not in seen_owned:
                    checked_chain(process, root_identity)
                elif process["ancestry_to_monitor"] is not None:
                    checked_chain(process, root_identity)
                seen_owned.add(process_identity)
            else:
                require(process_identity not in registered, "actual transport labelled external")
                require(process["exited_during_snapshot"] is False,
                        "external engine exited during the native snapshot")
                require(process["ancestry_to_monitor"] is None, "external process claims monitor ancestry")
                require(process["ancestry_processes"] is None, "external process claims ancestor evidence")
                current[process_identity] = ticks
        if first_external is None:
            first_external = dict(current)
            require(not seen_owned, "a measured engine existed before the idle interval")
        require(set(current) == set(first_external), "external engine identity set changed")
        delta = 0
        if previous is not None:
            for process_identity, ticks in current.items():
                require(ticks >= previous[process_identity], "external CPU counter decreased")
                delta += ticks - previous[process_identity]
        require(delta == row["external_cpu_delta_ticks"], "raw external CPU delta was misreported")
        require(delta == 0, "external engine consumed native CPU ticks")
        cpu_total += delta
        previous = current
        phases.append(row["phase"])
        if row["phase"] == "idle-complete":
            require(idle_complete is None and not seen_owned, "idle phase repeated or engines launched too early")
            idle_complete = row
    require(idle_complete is not None and idle_complete["elapsed_seconds"] - rows[0]["elapsed_seconds"] >= 10,
            "raw evidence does not cover the required idle interval")
    require(seen_owned == set(registered), "a registered UCI transport never appeared in raw evidence")
    expected_external = {(item["pid"], item["creation_ticks"]): item["initial_cpu_ticks"]
                         for item in summary["external_engine_identities"]}
    require(len(expected_external) == len(summary["external_engine_identities"])
            and expected_external == first_external, "summary external identities/counters differ from first row")
    require(cpu_total == summary["external_cpu_increase_ticks"] == 0, "summary CPU tick total mismatch")
    require(abs(max_gap - summary["max_snapshot_gap_seconds"]) <= 1e-7,
            "summary maximum snapshot gap mismatch")
    require(abs(max_duration - summary["max_snapshot_duration_seconds"]) <= 1e-7,
            "summary maximum snapshot duration mismatch")
    before, after = phases.count("before-game"), phases.count("after-game")
    require(before == after == game_count, "monitor does not cover every fixed scheduled game")
    game_phases = [phase for phase in phases if phase in {"before-game", "after-game"}]
    require(game_phases == [phase for _ in range(game_count) for phase in ("before-game", "after-game")],
            "game guard phase order differs from serial play")
    match_start = utc(document["started_at"])
    match_end = match_start + datetime.timedelta(seconds=document["total_elapsed_seconds"])
    require(utc(summary["started_utc"]) <= first_utc <= utc(idle_complete["utc"]) <= match_start
            and match_end <= utc(rows[-1]["utc"]) <= utc(summary["finished_utc"]),
            "monitor does not cover the entire match including engine launch/cleanup")
    return {"status": "PASS", "snapshots": len(rows), "external_processes": len(first_external),
            "external_cpu_increase_ticks": cpu_total, "max_snapshot_gap_seconds": max_gap,
            "max_snapshot_duration_seconds": max_duration,
            "observed_idle_seconds": idle_complete["elapsed_seconds"] - rows[0]["elapsed_seconds"],
            "owned_transport_identities": {role: list(identity(item)) for role, item in transports.items()},
            "scope": summary["scope"]}


def audit_block(freeze_path, attempt):
    freeze_path, attempt = Path(freeze_path).resolve(), Path(attempt).resolve()
    freeze = read_json(freeze_path)
    inputs = bound_inputs(freeze_path, freeze)
    state_path = attempt / "driver-state.json"
    state = read_json(state_path)
    require(state["schema_version"] == 1 and state["valid_for_comparison"] is True
            and state["exit_code"] == 0 and state["error"] is None
            and state["sequential_workloads"] is True, "driver did not finish a valid serial block")
    require(resolved(state["freeze_path"]) == freeze_path
            and state["freeze_sha256"] == digest(freeze_path), "driver freeze identity differs")
    movetime = state["movetime_ms"]
    planned = condition(freeze, movetime)
    require(state["condition_role"] == planned["role"]
            and state["engine_source_commit"] == freeze["engine_source_commit"]
            and state["checkout_commit"] == freeze["checkout_commit"],
            "driver primary/secondary or source/checkout differs from freeze")
    stem = attempt / f"time{movetime}"
    match_path, pgn_path = stem.with_suffix(".json"), stem.with_suffix(".pgn")
    monitor_path, log_path = stem.with_suffix(".monitor.json"), stem.with_suffix(".monitor.jsonl")
    document, monitor = read_json(match_path), read_json(monitor_path)
    require(document["completed"] is True and document["valid_for_comparison"] is True
            and document["exit_code"] == 0 and not document["errors"], "match was incomplete or invalid")
    require(document["limits"] == {"nodes": None, "depth": None, "movetime_ms": movetime}
            and document["max_plies"] == 500 and document["draw_policy"] == "claim",
            "unequal/unplanned time or adjudication limits")
    require(document["harness_source_commit"] == freeze["checkout_commit"]
            and document["harness_checkout"]["commit"] == freeze["checkout_commit"]
            and document["engine_source_commit"] == freeze["engine_source_commit"]
            and document["condition_role"] == planned["role"],
            "recorded harness checkout/primary-secondary assignment differs from freeze")
    require(document["selected_model"] == freeze["model"], "selected model metadata differs")
    require(document["comparison_mode"] == freeze["comparison_mode"]
            and document["role_movetime_ms"] == freeze["role_movetime_ms"][str(movetime)]
            and isinstance(document["limits_scope"], str) and document["limits_scope"],
            "missing or wrong asymmetric time-allocation metadata")
    require(utc(document["started_at"]) + datetime.timedelta(seconds=document["total_elapsed_seconds"])
            <= utc(freeze["deadline_utc"]), "completed block exceeded the preregistered deadline")
    raw_uci_path = attempt / "uci.jsonl"
    require(raw_uci_path.is_file() and digest(raw_uci_path) == state["uci_log_sha256"]
            == document["uci_log_sha256"], "raw UCI log identity differs")
    require(state["model_uci_ack"] == document["model_uci_ack"],
            "embedded/state loaded-model acknowledgement differs")
    for name in ("frozen_before", "frozen_after", "engine_options_preflight"):
        require(document[name] == state[name], f"embedded/state {name} differs")
    before = audit_snapshot(state["frozen_before"], freeze_path, freeze, inputs)
    after = audit_snapshot(state["frozen_after"], freeze_path, freeze, inputs)
    require(utc(state["started_utc"]) <= before <= utc(document["started_at"])
            and utc(document["started_at"]) + datetime.timedelta(seconds=document["total_elapsed_seconds"])
            <= after <= utc(state["finished_utc"]), "frozen snapshots do not bracket the match")
    validation_path = resolved(freeze["prior_validation"]["path"])
    validation = read_json(validation_path)
    require(validation["all_passed"] is True and utc(validation["completed_utc"]) <= before,
            "regression validation did not pass before match")
    require(validation["engine_source_commit"] == freeze["engine_source_commit"]
            and validation["binary_sha256"] == freeze["baseline"]["sha256"],
            "pre-match validation identifies another engine")
    require(len(validation["checks"]) >= 6, "missing prerequisite regression checks")
    for check in validation["checks"]:
        name = check["name"]
        require(Path(name).name == name and check["exit_code"] == 0
                and digest(validation_path.parent / name) == check["log_sha256"],
                "prerequisite validation log identity or result differs")
    previous_manifest = validation["prior_precision_manifest"]
    require(digest(resolved(previous_manifest["path"])) == previous_manifest["sha256"],
            "prior precision/model validation manifest changed")
    model_io = validation["model_io_preflight"]
    require(model_io["model_sha256"] == freeze["model"]["sha256"]
            and model_io["expected_checksum"] == model_io["observed_checksum"] == freeze["model"]["checksum"],
            "preflight did not confirm the frozen loaded model")
    handshake = resolved(model_io["uci_output_path"])
    require(digest(handshake) == model_io["uci_output_sha256"], "raw UCI model handshake changed")
    checksum_line = f"info string neural model checksum {freeze['model']['checksum']}"
    require(checksum_line in handshake.read_text(encoding="utf-8-sig").splitlines(),
            "raw UCI handshake lacks the selected model checksum")
    engines = audit_engines(document, freeze)
    preflight = state["engine_options_preflight"]
    for role in ROLES:
        item = document["engines"][role]
        defaults = {name: value["default"] for name, value in item["advertised_options"].items()
                    if value["type"] != "button"}
        require(preflight["advertised_defaults"][role] == defaults
                and preflight["requested_options"][role] == item["requested_options"]
                and preflight["effective_options"][role] == engines[role]["effective_options"],
                "driver option preflight differs from independently computed engine options")
    require(preflight["advertised_defaults"]["baseline"]
            == preflight["advertised_defaults"]["candidate"], "advertised engine defaults differ")
    wake = state["wake_state"]
    require(wake["requested_state"] == "ES_CONTINUOUS | ES_SYSTEM_REQUIRED"
            and wake["display_required"] is False and wake["persistent_power_policy_changed"] is False
            and isinstance(wake["prior_wake_state"], int) and wake["prior_wake_state"] > 0
            and wake["restore_requested_state"] == wake["prior_wake_state"] | 0x80000000
            and wake["restore_succeeded"] is True, "temporary wake requirement was not restored")
    for name, expected in state["artifact_sha256"].items():
        path = attempt / name
        require(Path(name).name == name and path.is_file() and digest(path) == expected,
                f"driver artifact fingerprint differs: {name}")
    require({path.name for path in (match_path, pgn_path, monitor_path, log_path, raw_uci_path)}
            <= set(state["artifact_sha256"]), "driver omitted a required raw artifact fingerprint")
    game_result = audit_games(document, freeze)
    require(audit_pgn(pgn_path, document) == planned["games_requested"],
            "PGN game count differs from preregistered condition")
    require(document["external_engine_guard"] == monitor, "embedded native monitor summary differs")
    require(monitor["monitor_log_sha256"] == digest(log_path)
            and monitor["freeze_manifest_sha256"] == digest(freeze_path), "monitor input/log SHA differs")
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8-sig").splitlines()]
    monitor_result = replay_monitor(monitor, rows, document, movetime,
                                    freeze["engine_source_commit"], freeze["checkout_commit"],
                                    planned["games_requested"])
    raw_uci_rows = [json.loads(line) for line in raw_uci_path.read_text(encoding="utf-8-sig").splitlines()]
    raw_uci_result = audit_uci(raw_uci_rows, document, freeze)
    acknowledgement = state["model_uci_ack"]
    for field in ("role", "line", "seq"):
        require(acknowledgement[field] == raw_uci_result["model_ack"][field],
                f"loaded-model acknowledgement {field} differs from raw UCI")
    require(acknowledgement["pid"] == monitor["transport_engines"]["candidate"]["pid"],
            "loaded-model acknowledgement PID differs from measured candidate transport")
    # A second current check also detects accidental mutation by an audit helper.
    require(bound_inputs(freeze_path, freeze) == inputs, "frozen inputs changed while auditing")
    return {"schema_version": 1, "status": "PASS",
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "freeze_sha256": digest(freeze_path), "driver_state_sha256": digest(state_path),
            "raw_artifact_sha256": {path.name: digest(path)
                for path in (match_path, pgn_path, monitor_path, log_path, raw_uci_path)},
            "attempt": str(attempt), "movetime_ms": movetime,
            "engine_source_commit": freeze["engine_source_commit"],
            "checkout_commit": freeze["checkout_commit"], "condition": planned, "engines": engines,
            "comparison_mode": freeze["comparison_mode"],
            "role_movetime_ms": freeze["role_movetime_ms"][str(movetime)],
            "deadline_utc": freeze["deadline_utc"],
            "selected_model": freeze["model"], "frozen_inputs_checked": len(inputs),
            **game_result, "monitor": monitor_result, "raw_uci": raw_uci_result,
            "temporary_wake_restored": True,
            "scope": SCOPE,
            "time_scope": ("Classical receives 1000ms, neural receives the 3000/5000ms condition label; "
                           "both subtract 20ms Move Overhead. Extra-time observation, not equal-time "
                           "strength. Wall overruns use role-specific requested limits and are "
                           "reported, not adjudicated as clock forfeits."),
            "validation_scope": ("All actual and PV plies, outcomes, PGN, raw UCI history/budget/"
                                 "bestmove/numeric results, live-load ACK, option defaults/overrides, "
                                 "frozen byte fingerprints and every native CPU snapshot replayed. "
                                 "Opening freshness is established by the separately frozen selection audit.")}


def synthetic_monitor(game_count=4, movetime=3000):
    """Variable-count native evidence only; no new openings or engines."""
    from audit_neural_strength_results import synthetic_monitor as old_fixture
    summary, old_rows, document = old_fixture()
    origin = utc(old_rows[0]["utc"]) - datetime.timedelta(seconds=0.01)
    count = 32 + 2 * game_count
    rows = []
    for index in range(count):
        template_index = 0 if index <= 20 else 21 if index < count - 2 else 63
        row = copy.deepcopy(old_rows[template_index])
        row["utc"] = (origin + datetime.timedelta(seconds=index * 0.5 + 0.01)).isoformat()
        row["elapsed_seconds"] = index * 0.5 + 0.01
        row["gap_seconds"] = 0.01 if index == 0 else 0.5
        row["phase"] = ("initial" if index == 0 else "finished" if index == count - 1
                        else "idle-complete" if index == 20 else "running")
        if 22 <= index < 22 + 2 * game_count:
            row["phase"] = "before-game" if index % 2 == 0 else "after-game"
        rows.append(row)
    summary.update(snapshot_count=count, movetime_ms=movetime,
                   harness_commit="b" * 40,
                   finished_utc=(origin + datetime.timedelta(seconds=count * 0.5)).isoformat())
    document["total_elapsed_seconds"] = (count - 3) * 0.5 - 10.1
    return summary, rows, document


def synthetic_uci():
    """Four historical searched plies as protocol evidence, never a new search."""
    path = ROOT / "docs/matches/2026-10-19-neural-strength/attempt-001-time100/time100.json"
    historical = read_json(path)
    game = copy.deepcopy(historical["games"][0])
    game["search_moves"] = game["search_moves"][:4]
    document = {"games": [game], "games_requested": 1, "limits": {"movetime_ms": 3000}}
    freeze = {"model": {"path": "synthetic-only-model.nnue", "checksum": 123},
              "role_movetime_ms": {"3000": {"baseline": 1000, "candidate": 3000}}}
    rows, seen = [], set()
    origin = datetime.datetime(2026, 10, 19, tzinfo=datetime.timezone.utc)

    def add(role, direction, line):
        sequence = len(rows) + 1
        rows.append({"seq": sequence, "monotonic": sequence / 10,
                     "utc": (origin + datetime.timedelta(seconds=sequence / 10)).isoformat(),
                     "role": role, "direction": direction, "line": line})

    add("candidate", "send", "setoption name NeuralModelFile value " +
        str(resolved(freeze["model"]["path"])))
    add("candidate", "recv", "info string neural model checksum 123")
    for move in game["search_moves"]:
        role = move["role"]
        if role not in seen:
            add(role, "send", "ucinewgame")
            seen.add(role)
        history = game["uci_moves"][:move["ply"] - 1]
        add(role, "send", "position startpos moves " + " ".join(history))
        add(role, "send", "go movetime " + str(freeze["role_movetime_ms"]["3000"][role]))
        score = move["score"]
        kind = "cp" if score["cp"] is not None else "mate"
        bound = " lowerbound" if score["lowerbound"] else " upperbound" if score["upperbound"] else ""
        add(role, "recv", f"info depth {move['depth']} seldepth {move['seldepth']} "
            f"nodes {move['nodes']} time {round(move['engine_time_seconds'] * 1000)} "
            f"score {kind} {score[kind]}{bound} pv " + " ".join(move["pv"]))
        add(role, "recv", "bestmove " + move["move"])
    return rows, document, freeze


def self_test():
    # Preserve the previously committed raw-PGN, PV, native-CPU and corruption
    # tests, then exercise every changed sample-count/provenance contract here.
    from audit_neural_strength_results import self_test as prior_self_test
    prior = prior_self_test()
    plan = {"engine_source_commit": "a" * 40, "checkout_commit": "b" * 40,
            "time_controls_ms": [3000, 5000], "primary_time_ms": 3000,
            "secondary_time_ms": 5000, "opening_count": 12,
            "condition_opening_count": {"3000": 2, "5000": 1},
            "games_per_time_control": {"3000": 4, "5000": 2},
            "max_plies": 500, "draw_policy": "claim", "comparison_mode": "neural_extra_time",
            "role_movetime_ms": {"3000": {"baseline": 1000, "candidate": 3000},
                                 "5000": {"baseline": 1000, "candidate": 5000}},
            "deadline_utc": "2026-10-19T07:11:49Z"}
    validate_plan(plan)
    rejected = []
    for label, edit in (
        ("swapped primary", lambda f: f.update(primary_time_ms=5000)),
        ("changed secondary sample", lambda f: f["condition_opening_count"].update({"5000": 5})),
        ("changed primary game count", lambda f: f["games_per_time_control"].update({"3000": 22})),
        ("wrong checkout object", lambda f: f.update(checkout_commit="bb3100")),
        ("unplanned time", lambda f: f.update(time_controls_ms=[100, 1000])),
        ("changed ending policy", lambda f: f.update(draw_policy="automatic")),
        ("wrong comparison mode", lambda f: f.update(comparison_mode="equal_time")),
        ("wrong baseline time", lambda f: f["role_movetime_ms"]["3000"].update(baseline=3000)),
        ("changed deadline", lambda f: f.update(deadline_utc="2026-10-19T08:11:49Z")),
    ):
        changed = copy.deepcopy(plan)
        edit(changed)
        try:
            validate_plan(changed)
        except (ValueError, KeyError):
            rejected.append(label)
        else:
            raise AssertionError("accepted corrupted long-time plan: " + label)
    counts = []
    for movetime in (3000, 5000):
        planned = condition(plan, movetime)
        games = planned["games_requested"]
        summary, rows, document = synthetic_monitor(games, movetime)
        require(replay_monitor(summary, rows, document, movetime, "a" * 40,
                               "b" * 40, games)["status"] == "PASS",
                "clean variable-count long-time monitor rejected")
        for label, altered_summary, altered_rows, expected_games in (
            (f"{movetime} wrong game count", summary, rows, games + 2),
            (f"{movetime} build-checkout confusion", dict(summary, harness_commit="a" * 40), rows, games),
            (f"{movetime} missing boundary", summary,
             [dict(row, phase="running") if i == 22 else row for i, row in enumerate(rows)], games),
        ):
            try:
                replay_monitor(altered_summary, altered_rows, document, movetime,
                               "a" * 40, "b" * 40, expected_games)
            except (ValueError, KeyError):
                rejected.append(label)
            else:
                raise AssertionError("accepted corrupted long-time monitor: " + label)
        counts.append({"movetime_ms": movetime, **planned, "snapshots": len(rows)})
    raw, fixture, raw_freeze = synthetic_uci()
    raw_result = audit_uci(raw, fixture, raw_freeze)
    require(raw_result["searched_moves"] == 4, "clean historical raw-UCI fixture rejected")
    sends = lambda r, prefix: next(row for row in r if row["direction"] == "send"
                                  and row["line"].startswith(prefix))
    receives = lambda r, prefix: next(row for row in r if row["direction"] == "recv"
                                     and row["line"].startswith(prefix))
    raw_rejected = []
    for label, edit in (
        ("missing sequence", lambda r: r[3].update(seq=99)),
        ("reversed timestamp", lambda r: r[3].update(monotonic=0)),
        ("wrong movetime", lambda r: sends(r, "go ").update(line="go movetime 5000")),
        ("missing full history", lambda r: sends(r, "position ").update(line="position startpos")),
        ("wrong bestmove", lambda r: receives(r, "bestmove ").update(line="bestmove a1a8")),
        ("wrong nodes", lambda r: receives(r, "info depth ").update(
            line=receives(r, "info depth ")["line"].replace("nodes ", "nodes 9"))),
        ("wrong score", lambda r: receives(r, "info depth ").update(
            line=receives(r, "info depth ")["line"].replace("score cp ", "score cp 9"))),
        ("wrong PV", lambda r: receives(r, "info depth ").update(
            line=receives(r, "info depth ")["line"].split(" pv ")[0] + " pv a1a8")),
        ("missing fresh game", lambda r: sends(r, "ucinewgame").update(line="isready")),
        ("wrong model path", lambda r: sends(r, "setoption ").update(
            line="setoption name NeuralModelFile value changed.nnue")),
        ("wrong model acknowledgement", lambda r: receives(r, "info string ").update(
            line="info string neural model checksum 124")),
    ):
        changed = copy.deepcopy(raw)
        edit(changed)
        try:
            audit_uci(changed, fixture, raw_freeze)
        except (ValueError, KeyError):
            raw_rejected.append(label)
        else:
            raise AssertionError("accepted corrupted raw-UCI fixture: " + label)
    # A historical prefix verifies the changed one-/two-pair summary branches.
    # The extra candidate openings are mocked and never searched or selected.
    from unittest.mock import patch
    historical_path = ROOT / "docs/matches/2026-10-19-neural-strength/attempt-001-time100/time100.json"
    historical = read_json(historical_path)
    inventory = read_json(ROOT / "tests/data/neural_strength_openings_2026-10-19.json")
    inventory["openings"] = (inventory["openings"] * 2)[:12]
    summary_cases = []
    for movetime in (3000, 5000):
        planned = condition(plan, movetime)
        pairs, games = planned["opening_count"], planned["games_requested"]
        truncated = copy.deepcopy(historical)
        truncated["limits"]["movetime_ms"] = movetime
        truncated["games"] = truncated["games"][:games]
        truncated["selected_openings"] = truncated["selected_openings"][:pairs]
        truncated["games_requested"] = games
        points = [game["candidate_points"] for game in truncated["games"]]
        summary = truncated["summary"]
        summary.update(candidate_wins=points.count(1.0), candidate_draws=points.count(0.5),
                       candidate_losses=points.count(0.0), candidate_points=sum(points),
                       candidate_score=sum(points) / games, completed_games=games,
                       incomplete_games=0, unplayed_games=0, scheduled_games=games,
                       attempted_games=games, pairs=summary["pairs"][:pairs])
        pair_scores = [pair["candidate_score"] for pair in summary["pairs"]]
        summary["paired_opening_bootstrap95_descriptive"] = None
        summary["wilson95_descriptive"] = None
        for role in ROLES:
            moves = [move for game in truncated["games"] for move in game["search_moves"]
                     if move["role"] == role]
            summary["resources"][role] = {
                "searched_moves": len(moves), "reported_nodes": sum(move["nodes"] for move in moves),
                "moves_without_node_report": 0,
                "elapsed_seconds": round(sum(move["elapsed_seconds"] for move in moves), 6)}
        schedule_freeze = {**plan, "openings": {
            "path": "tests/data/neural_strength_openings_2026-10-19.json",
            "sha256": historical["opening_file_sha256"]}}
        import sys
        with patch.object(sys.modules[__name__], "read_json", return_value=inventory):
            audited = audit_games(truncated, schedule_freeze)
            require(audited["completed_games"] == games, "changed-count historical schedule rejected")
            require(audited["paired_opening_bootstrap95_descriptive"] is None,
                    "asymmetric fixture was assigned a confidence interval")
            for field in ("paired_opening_bootstrap95_descriptive", "wilson95_descriptive"):
                summary[field] = {"interval": [pair_scores[0]] * 2}
                try:
                    audit_games(truncated, schedule_freeze)
                except ValueError:
                    rejected.append(f"{movetime} asymmetric {field} presented as uncertainty")
                else:
                    raise AssertionError("accepted an asymmetric confidence interval")
                summary[field] = None
        summary_cases.append({"movetime_ms": movetime, "historical_games": games,
                              "completed_pairs": pairs,
                              "reports_interval": audited["paired_opening_bootstrap95_descriptive"] is not None})
    return {"status": "PASS", "prior_raw_replay_self_test": prior,
            "long_time_corruption_rejections": rejected, "synthetic_conditions": counts,
            "raw_uci_corruption_rejections": raw_rejected,
            "historical_raw_uci_fixture_searches": raw_result["searched_moves"],
            "historical_prefix_summary_cases": summary_cases,
            "fresh_openings_or_engines_used": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--attempt", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        result = self_test()
    else:
        if args.freeze is None or args.attempt is None or args.output is None:
            parser.error("use --freeze FILE --attempt DIR --output FILE, or --self-test")
        require(not args.output.exists(), "preserve the previous audit report")
        result = audit_block(args.freeze, args.attempt)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

