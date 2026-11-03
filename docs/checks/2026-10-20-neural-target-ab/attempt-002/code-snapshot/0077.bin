"""Audit one frozen neural/classical paired match without launching any engine.

Only immutable records are read. The optional self-test uses synthetic process
evidence and the previously public Fool's Mate, never fresh match openings.
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
SCOPE = ("One fixed set of eight previously unused opening pairs; descriptive "
         "paired percentile interval only, not a universal strength or Elo interval. "
         "The same openings at different time controls are not independent samples.")


def resolved(value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def bound_inputs(freeze_path, freeze):
    require(freeze["schema_version"] == 1, "unsupported neural match freeze")
    require(re.fullmatch(r"[0-9a-f]{40}", freeze["engine_source_commit"]) is not None,
            "source commit must be a full lowercase object ID")
    require(freeze["time_controls_ms"] == [100, 1000]
            and freeze["max_plies"] == 500 and freeze["draw_policy"] == "claim",
            "unexpected planned time controls or ending policy")
    files = {str(resolved(path)): value for path, value in freeze["files"].items()}
    required_sources = {"tests/audit_neural_strength_results.py", "tests/audit_see_results.py",
                        "tests/audit_see_replication.py", "scripts/run_neural_strength_comparison.py",
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
    require(snapshot["engine_source_commit"] == freeze["engine_source_commit"],
            "snapshot source commit mismatch")
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
        for name, value in common.items():
            require(options.get(name) == value, f"unequal effective {role} option {name}")
        require(options.get("UseNeuralEvaluation") is (role == "candidate"),
                "classical/neural evaluator assignment is wrong")
        if role == "candidate":
            require(resolved(options["NeuralModelFile"]) == resolved(freeze["model"]["path"]),
                    "candidate model path mismatch")
            require(item["requested_options"] == {
                "UseNeuralEvaluation": True, "UseIncrementalNeuralEvaluation": True,
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


def audit_games(document, freeze):
    opening_path = resolved(freeze["openings"]["path"])
    require(document["opening_file_sha256"] == freeze["openings"]["sha256"],
            "match opening file differs from freeze")
    expected = read_json(opening_path)["openings"]
    require(len(expected) == len(document["selected_openings"]) == 8,
            "not exactly eight fixed starting positions")
    require(document["games_requested"] == len(document["games"]) == 16,
            "not exactly sixteen scheduled and recorded games")
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
              "candidate_score": sum(points) / 16, "completed_games": 16,
              "incomplete_games": 0, "unplayed_games": 0, "scheduled_games": 16,
              "attempted_games": 16}
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
    interval = bootstrap(pair_scores)
    recorded = summary["paired_opening_bootstrap95_descriptive"]
    require(recorded["seed"] == HARNESS_BOOTSTRAP_SEED
            and recorded["resamples"] == HARNESS_BOOTSTRAP_RESAMPLES
            and recorded["completed_pairs"] == 8 and recorded["interval"] == interval,
            "harness paired bootstrap differs from independent replay")
    return {**counts, "actual_plies": plies, "pv_plies": pv_plies, "terminations": terminations,
            "pair_scores": pair_scores, "paired_opening_bootstrap95_descriptive": {
                "interval": interval, "seed": HARNESS_BOOTSTRAP_SEED,
                "resamples": HARNESS_BOOTSTRAP_RESAMPLES, "completed_pairs": 8, "scope": SCOPE},
            "resources": {role: resources_summary(resources[role], document["limits"]["movetime_ms"])
                          for role in ROLES}}


# Native process evidence is replayed below; no live process inspection is used.


def replay_monitor(summary, rows, document, movetime, source_commit):
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
            and summary["harness_commit"] == source_commit
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
    require(before == after == 16, "monitor does not cover all sixteen games")
    game_phases = [phase for phase in phases if phase in {"before-game", "after-game"}]
    require(game_phases == [phase for _ in range(16) for phase in ("before-game", "after-game")],
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
    require(movetime in freeze["time_controls_ms"], "unplanned time control")
    stem = attempt / f"time{movetime}"
    match_path, pgn_path = stem.with_suffix(".json"), stem.with_suffix(".pgn")
    monitor_path, log_path = stem.with_suffix(".monitor.json"), stem.with_suffix(".monitor.jsonl")
    document, monitor = read_json(match_path), read_json(monitor_path)
    require(document["completed"] is True and document["valid_for_comparison"] is True
            and document["exit_code"] == 0 and not document["errors"], "match was incomplete or invalid")
    require(document["limits"] == {"nodes": None, "depth": None, "movetime_ms": movetime}
            and document["max_plies"] == 500 and document["draw_policy"] == "claim",
            "unequal/unplanned time or adjudication limits")
    require(document["harness_source_commit"] == freeze["engine_source_commit"]
            and document["harness_checkout"]["commit"] == freeze["engine_source_commit"],
            "recorded engine/harness source commit differs from freeze")
    require(document["selected_model"] == freeze["model"], "selected model metadata differs")
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
    require({path.name for path in (match_path, pgn_path, monitor_path, log_path)}
            <= set(state["artifact_sha256"]), "driver omitted a required raw artifact fingerprint")
    game_result = audit_games(document, freeze)
    require(audit_pgn(pgn_path, document) == 16, "PGN does not contain exactly sixteen matching games")
    require(document["external_engine_guard"] == monitor, "embedded native monitor summary differs")
    require(monitor["monitor_log_sha256"] == digest(log_path)
            and monitor["freeze_manifest_sha256"] == digest(freeze_path), "monitor input/log SHA differs")
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8-sig").splitlines()]
    monitor_result = replay_monitor(monitor, rows, document, movetime, freeze["engine_source_commit"])
    # A second current check also detects accidental mutation by an audit helper.
    require(bound_inputs(freeze_path, freeze) == inputs, "frozen inputs changed while auditing")
    return {"schema_version": 1, "status": "PASS",
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "freeze_sha256": digest(freeze_path), "driver_state_sha256": digest(state_path),
            "raw_artifact_sha256": {path.name: digest(path)
                for path in (match_path, pgn_path, monitor_path, log_path)},
            "attempt": str(attempt), "movetime_ms": movetime,
            "engine_source_commit": freeze["engine_source_commit"], "engines": engines,
            "selected_model": freeze["model"], "frozen_inputs_checked": len(inputs),
            **game_result, "monitor": monitor_result, "temporary_wake_restored": True,
            "scope": SCOPE,
            "time_scope": ("Equal requested UCI movetime; both subtract 20ms Move Overhead. "
                           "Observed wall overruns are reported, not adjudicated as clock forfeits."),
            "validation_scope": ("All actual and PV plies, outcomes, PGN, option defaults/overrides, "
                                 "frozen byte fingerprints and every native CPU snapshot replayed. "
                                 "Opening freshness is established by the separately frozen selection audit.")}


def synthetic_monitor():
    origin = datetime.datetime(2026, 10, 18, tzinfo=datetime.timezone.utc)
    stamp = lambda seconds: (origin + datetime.timedelta(seconds=seconds)).isoformat()
    owned = [{"pid": pid, "creation_ticks": pid, "parent_pid": 1, "name": "regicide.exe",
              "ancestry_to_monitor": [pid, 1], "ancestry_processes": [
                  {"pid": pid, "parent_pid": 1, "creation_ticks": pid},
                  {"pid": 1, "parent_pid": 0, "creation_ticks": 1}]} for pid in (2, 3)]
    transports = {role: dict(item, command=[f"/{role}/regicide.exe", "uci"])
                  for role, item in zip(ROLES, owned)}
    rows = []
    for index in range(64):
        phase = ("initial" if index == 0 else "finished" if index == 63
                 else "idle-complete" if index == 20 else "running")
        if 22 <= index < 54:
            phase = "before-game" if index % 2 == 0 else "after-game"
        processes = [{"pid": 10, "creation_ticks": 5, "parent_pid": 9, "name": "stockfish.exe",
                      "cpu_ticks": 42, "owned": False, "exited_during_snapshot": False,
                      "ancestry_to_monitor": None, "ancestry_processes": None}]
        if 21 <= index < 62:
            processes.extend(dict(item, cpu_ticks=index, owned=True, exited_during_snapshot=False)
                             for item in owned)
        rows.append({"utc": stamp(index * 0.5 + 0.01), "elapsed_seconds": index * 0.5 + 0.01,
                     "phase": phase, "gap_seconds": 0.01 if index == 0 else 0.5,
                     "snapshot_duration_seconds": 0.01, "processes": processes,
                     "external_cpu_delta_ticks": 0, "contaminated": False})
    summary = {"schema_version": 1, "started_utc": stamp(-0.1), "finished_utc": stamp(32),
               "monitor_root_pid": 1, "monitor_root_creation_ticks": 1, "poll_seconds": 0.5,
               "max_allowed_snapshot_gap_seconds": 2.0, "cpu_ticks_per_second": 10_000_000,
               "external_cpu_tolerance_ticks": 0, "completed_idle_seconds": 10,
               "contaminated": False, "valid_for_comparison": True,
               "observed_external_engine_cpu_increase": False, "failure": None, "error": None, "exit_code": 0,
               "candidate_commit": "a" * 40, "baseline_commit": "a" * 40,
               "harness_commit": "a" * 40, "movetime_ms": 100, "snapshot_count": len(rows),
               "engine_names": ["regicide.exe", "stockfish.exe", "lc0.exe", "lc0-cpu.exe"],
               "new_or_disappearing_external_engine_is_failure": True, "owned_engines": owned,
               "transport_engines": transports, "external_cpu_increase_ticks": 0,
               "max_snapshot_gap_seconds": 0.5, "max_snapshot_duration_seconds": 0.01,
               "external_engine_identities": [{"pid": 10, "creation_ticks": 5, "initial_cpu_ticks": 42}],
               "scope": "synthetic process fixture only"}
    document = {"engines": {role: {"command": item["command"]} for role, item in transports.items()},
                "started_at": stamp(10.1), "total_elapsed_seconds": 20}
    return summary, rows, document


def self_test():
    summary, rows, document = synthetic_monitor()
    require(replay_monitor(summary, rows, document, 100, "a" * 40)["snapshots"] == 64,
            "clean synthetic native monitor rejected")
    failures = []
    def reject(label, alter):
        state, snapshots, match = copy.deepcopy((summary, rows, document))
        alter(state, snapshots, match)
        try:
            replay_monitor(state, snapshots, match, 100, "a" * 40)
        except (ValueError, KeyError):
            failures.append(label)
        else:
            raise AssertionError("accepted corrupted synthetic evidence: " + label)
    reject("external CPU", lambda s, r, m: r[30]["processes"][0].update(cpu_ticks=43))
    reject("new external PID", lambda s, r, m: r[30]["processes"].append(dict(r[30]["processes"][0], pid=11)))
    reject("external PID disappearance", lambda s, r, m: r[30]["processes"].pop(0))
    reject("PID reuse", lambda s, r, m: r[30]["processes"][0].update(creation_ticks=6))
    reject("owned ancestry lie", lambda s, r, m: s["owned_engines"][0].update(ancestry_to_monitor=[2, 9]))
    reject("wrong transport", lambda s, r, m: s["transport_engines"]["candidate"].update(command=["/baseline/regicide.exe", "uci"]))
    reject("third descendant engine", lambda s, r, m: s["owned_engines"].append(dict(s["owned_engines"][0], pid=4)))
    reject("sampling gap", lambda s, r, m: r[30].update(elapsed_seconds=20))
    reject("forged gap", lambda s, r, m: r[30].update(gap_seconds=0))
    reject("missing snapshot", lambda s, r, m: r.pop(30))
    reject("missing game interval", lambda s, r, m: r[30].update(phase="running"))
    reject("insufficient idle", lambda s, r, m: s.update(completed_idle_seconds=9.9))
    reject("missing cleanup coverage", lambda s, r, m: m.update(total_elapsed_seconds=30))
    reject("wrong source", lambda s, r, m: s.update(candidate_commit="b" * 40))
    reject("dirty snapshot", lambda s, r, m: r[30].update(contaminated=True))
    require(bootstrap([0, 1]) == [0.0, 1.0], "whole-pair bootstrap arithmetic changed")
    # The saved historical fixture uses no new opening or engine process.
    historical = ROOT / "docs/matches/2026-10-18-see-replication/attempt-002/time100.json"
    historical_document = read_json(historical)
    replayed = [audit_game(item) for item in historical_document["games"]]
    require(len(replayed) == audit_pgn(historical.with_suffix(".pgn"), historical_document) == 16,
            "historical game/PGN replay failed")
    historical_freeze = {"openings": {"path": "tests/data/see_replication_openings_2026-10-18.json",
                                      "sha256": historical_document["opening_file_sha256"]}}
    require(audit_games(historical_document, historical_freeze)["completed_games"] == 16,
            "independent paired schedule/summary replay failed")
    replay_failures = []
    for label, alter in (
        ("color pair", lambda d: d["games"][1].update(white="candidate", black="baseline")),
        ("WDL summary", lambda d: d["summary"].update(candidate_wins=999)),
        ("bootstrap interval", lambda d: d["summary"]["paired_opening_bootstrap95_descriptive"].update(interval=[0, 0])),
        ("illegal actual move", lambda d: d["games"][0]["uci_moves"].__setitem__(-1, "a1a8")),
        ("final castling rights", lambda d: d["games"][0].update(final_fen=chess.STARTING_FEN)),
        ("incomplete game", lambda d: d["games"][0].update(completed=False)),
    ):
        changed = copy.deepcopy(historical_document)
        alter(changed)
        try:
            audit_games(changed, historical_freeze)
        except (ValueError, KeyError):
            replay_failures.append(label)
        else:
            raise AssertionError("accepted corrupted historical replay: " + label)
    return {"status": "PASS", "native_corruption_rejections": failures,
            "game_corruption_rejections": replay_failures,
            "historical_games_replayed": len(replayed),
            "historical_actual_plies": sum(item["plies"] for item in replayed),
            "historical_pv_plies": sum(item["pv_plies"] for item in replayed),
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

