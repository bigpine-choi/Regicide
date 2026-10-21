"""Diagnose an unfinished extra-time block; never certify a completed match.

No engines, GUI, process launch or artifact mutation. A successful prefix replay
still reports INCOMPLETE / DIAGNOSTIC_ONLY. Unpublished UCI tail searches and
native CPU counters are not promoted to completed-block audit evidence.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import io
import json
from pathlib import Path

import chess
import chess.pgn

import audit_neural_extra_time_results as complete
from audit_see_results import (
    ROOT, ROLES, audit_game, board_after, check_pv, digest, read_json, require,
    resources_summary,
)
from audit_see_replication import numeric, utc


def recorded_resources(record, role):
    if "resources" in record:
        return record["resources"][role]
    # The harness creates the next scheduled record before the budget check.
    # If that check aborts before play_one, no resource field exists yet.
    require(not record["completed"] and not record["search_moves"],
            "searched game omitted resource records")
    return {"searched_moves": 0, "reported_nodes": 0,
            "moves_without_node_report": 0, "elapsed_seconds": 0}


def prefix_game(record):
    if record["completed"]:
        return {"kind": "completed_game", **audit_game(record)}
    require(record["result"] == "*" and record["candidate_points"] is None,
            "unfinished game was assigned a result or points")
    require(record["initial_fen"] == chess.STARTING_FEN, "unexpected initial FEN")
    opening, moves = record["opening_uci_moves"], record["uci_moves"]
    require(moves[:len(opening)] == opening, "unfinished opening prefix differs")
    board = board_after(record["initial_fen"], opening)
    require(board.fen(en_passant="fen") == record["opening_fen"], "unfinished opening FEN differs")
    require(len(record["search_moves"]) == len(moves) - len(opening), "unfinished search count differs")
    resources, pv_plies = {role: [] for role in ROLES}, 0
    for index, (token, item) in enumerate(zip(moves[len(opening):], record["search_moves"]), len(opening) + 1):
        require(board.outcome(claim_draw=True) is None, "recorded move follows a claimable outcome")
        role = record["white" if board.turn else "black"]
        require(item["ply"] == index and item["move"] == token and item["role"] == role
                and item["color"] == ("white" if board.turn else "black")
                and item["fen_before"] == board.fen(en_passant="fen"), "unfinished move state differs")
        move = chess.Move.from_uci(token)
        require(move in board.legal_moves, "illegal recorded unfinished-game move")
        pv_plies += check_pv(board, item["pv"], token)
        for field in ("nodes", "depth", "seldepth"):
            require(type(item[field]) is int and item[field] >= 0, f"invalid recorded {field}")
        for field in ("elapsed_seconds", "engine_time_seconds"):
            require(numeric(item[field], field) >= 0, f"invalid recorded {field}")
        resources[role].append(item)
        board.push(move)
    require(record["plies"] == len(moves) <= 500 and record["final_fen"] == board.fen(en_passant="fen"),
            "unfinished final FEN or ply count differs")
    for role in ROLES:
        items, aggregate = resources[role], recorded_resources(record, role)
        require(aggregate["searched_moves"] == len(items)
                and aggregate["reported_nodes"] == sum(item["nodes"] for item in items)
                and aggregate["moves_without_node_report"] == 0
                and abs(aggregate["elapsed_seconds"] - round(sum(item["elapsed_seconds"] for item in items), 6)) < 1e-7,
                "unfinished per-role resources differ")
    return {"kind": "unfinished_prefix", "points": None, "plies": len(moves),
            "pv_plies": pv_plies, "resources": resources,
            "unstarted_placeholder": not record["search_moves"],
            "termination": record["termination"], "recorded_error": record["error"],
            "terminal_or_claimable_at_last_recorded_board": board.outcome(claim_draw=True) is not None}


def pgn_prefix(path, document):
    stream = io.StringIO(path.read_text(encoding="utf-8-sig"))
    for record in document["games"]:
        game = chess.pgn.read_game(stream)
        require(game is not None and not game.errors, "missing or invalid prefix PGN")
        require([move.uci() for move in game.mainline_moves()] == record["uci_moves"]
                and game.end().board().fen(en_passant="fen") == record["final_fen"],
                "prefix PGN moves/FEN differ")
        require(game.headers.get("Result") == record["result"]
                and game.headers.get("Round") == str(record["game"])
                and game.headers.get("Termination") == ("normal" if record["completed"] else "unterminated"),
                "prefix PGN outcome/round differs")
    require(chess.pgn.read_game(stream) is None, "extra prefix PGN game")


def diagnose(freeze_path, attempt):
    freeze_path, attempt = Path(freeze_path).resolve(), Path(attempt).resolve()
    freeze = read_json(freeze_path)
    inputs = complete.bound_inputs(freeze_path, freeze)
    state_path = attempt / "driver-state.json"
    state = read_json(state_path)
    require(state["schema_version"] == 1 and state["finished_utc"] is not None
            and state["valid_for_comparison"] is False and state["exit_code"] != 0,
            "prefix diagnostic requires a finished, invalid/incomplete driver block")
    require(complete.resolved(state["freeze_path"]) == freeze_path
            and state["freeze_sha256"] == digest(freeze_path), "driver freeze identity differs")
    movetime = state["movetime_ms"]
    planned = complete.condition(freeze, movetime)
    stem = attempt / f"time{movetime}"
    match_path, pgn_path = stem.with_suffix(".json"), stem.with_suffix(".pgn")
    monitor_path, monitor_log = stem.with_suffix(".monitor.json"), stem.with_suffix(".monitor.jsonl")
    uci_path = attempt / "uci.jsonl"
    document, monitor = read_json(match_path), read_json(monitor_path)
    require(document["completed"] is False and document["valid_for_comparison"] is False
            and document["exit_code"] != 0, "do not relabel a complete match as a prefix")
    require(document["comparison_mode"] == freeze["comparison_mode"] == "neural_extra_time"
            and document["role_movetime_ms"] == freeze["role_movetime_ms"][str(movetime)]
            and document["games_requested"] == planned["games_requested"]
            and document["limits"] == {"nodes": None, "depth": None, "movetime_ms": movetime}
            and document["max_plies"] == 500 and document["draw_policy"] == "claim",
            "unfinished block changed frozen conditions")
    require(document["summary"]["paired_opening_bootstrap95_descriptive"] is None
            and document["summary"]["wilson95_descriptive"] is None, "unfinished pilot reports an interval")
    before = complete.audit_snapshot(state["frozen_before"], freeze_path, freeze, inputs)
    after_checked = state["frozen_after"] is not None
    if after_checked:
        complete.audit_snapshot(state["frozen_after"], freeze_path, freeze, inputs)
    gate = complete.audit_validation_gates(freeze, before)
    complete.audit_engines(document, freeze)
    required = {path.name for path in (match_path, pgn_path, monitor_path, monitor_log, uci_path)}
    require(required <= set(state["artifact_sha256"]), "driver omitted a raw artifact hash")
    for name, expected in state["artifact_sha256"].items():
        require(Path(name).name == name and digest(attempt / name) == expected,
                f"driver artifact hash differs: {name}")
    require(state["uci_log_sha256"] == document["uci_log_sha256"] == digest(uci_path)
            and monitor["monitor_log_sha256"] == digest(monitor_log)
            and monitor["freeze_manifest_sha256"] == digest(freeze_path)
            and document["external_engine_guard"] == monitor, "raw log identity differs")
    openings = read_json(complete.resolved(freeze["openings"]["path"]))["openings"][:planned["opening_count"]]
    require(document["selected_openings"] == openings, "unfinished opening prefix differs")
    resources, details = {role: [] for role in ROLES}, []
    require(0 < len(document["games"]) <= planned["games_requested"], "unexpected attempted game count")
    for number, record in enumerate(document["games"], 1):
        pair, offset = (number - 1) // 2, (number - 1) % 2
        opening = openings[pair]
        require(record["game"] == number and record["pair"] == pair + 1
                and record["opening"] == opening["name"] and record["opening_uci_moves"] == opening["moves"]
                and record["white"] == ("candidate" if offset == 0 else "baseline")
                and record["black"] == ("baseline" if offset == 0 else "candidate"),
                "unfinished schedule/colors differ")
        result = prefix_game(record)
        # Keep a single replay result, without mutating the raw document.
        for role, items in result["resources"].items():
            aggregate = recorded_resources(record, role)
            require(aggregate["moves_without_node_report"] == 0
                    and abs(aggregate["elapsed_seconds"] - round(sum(
                        item["elapsed_seconds"] for item in items), 6)) < 1e-7,
                    "recorded completed/prefix resource duration differs")
            resources[role].extend(items)
        details.append({key: value for key, value in result.items() if key != "resources"})
    pgn_prefix(pgn_path, document)
    completed = sum(record["completed"] for record in document["games"])
    require(document["summary"]["completed_games"] == completed
            and document["summary"]["incomplete_games"] == len(details) - completed
            and document["summary"]["unplayed_games"] == planned["games_requested"] - len(details),
            "unfinished summary counts differ")
    for role, items in resources.items():
        aggregate = document["summary"]["resources"][role]
        require(aggregate["searched_moves"] == len(items)
                and aggregate["reported_nodes"] == sum(item["nodes"] for item in items)
                and aggregate["moves_without_node_report"] == 0
                and abs(aggregate["elapsed_seconds"] - round(sum(
                    item["elapsed_seconds"] for item in items), 6)) < 1e-7,
                "unfinished summary resources differ")
    expired = state["budget_exhausted"] is True
    require(not expired or utc(state["finished_utc"]) >= utc(freeze["deadline_utc"]),
            "budget expiry was recorded before the fixed deadline")
    rows = [json.loads(line) for line in uci_path.read_text(encoding="utf-8-sig").splitlines()]
    monitor_rows = [json.loads(line) for line in monitor_log.read_text(encoding="utf-8-sig").splitlines()]
    require(monitor_rows and monitor_rows[-1]["phase"] == "finished", "monitor cleanup row missing")
    wake = state["wake_state"]
    wake_restored = (wake["restore_succeeded"] is True and type(wake["prior_wake_state"]) is int
                     and wake["restore_requested_state"] == wake["prior_wake_state"] | 0x80000000)
    return {"schema_version": 1, "status": "INCOMPLETE", "scope": "DIAGNOSTIC_ONLY",
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "auditor_sha256": digest(Path(__file__)), "freeze_sha256": digest(freeze_path),
            "driver_state_sha256": digest(state_path), "frozen_inputs_verified": len(inputs),
            "frozen_after_record_present": after_checked, "two_layer_gate_checks": {
                "new": len(gate["new_checks_verified"]), "reused": len(gate["reused_checks_verified"])},
            "comparison_mode": freeze["comparison_mode"], "role_movetime_ms": document["role_movetime_ms"],
            "scheduled_games": planned["games_requested"], "completed_games": completed,
            "unfinished_games": len(details) - completed,
            "unplayed_games": planned["games_requested"] - len(details), "recorded_prefixes": details,
            "resources": {role: resources_summary(items, document["role_movetime_ms"][role])
                          for role, items in resources.items()},
            "budget_exhausted_recorded": expired, "deadline_utc": freeze["deadline_utc"],
            "cleanup_observations": {"wake_restored": wake_restored,
                "raw_quit_roles": sorted({row["role"] for row in rows if row["direction"] == "send" and row["line"] == "quit"}),
                "final_monitor_owned_engine_count": sum(bool(item["owned"]) for item in monitor_rows[-1]["processes"]),
                "scope": "Saved cleanup observations; no live process inspection."},
            "stop_commands_observed": [row["seq"] for row in rows if row["direction"] == "send" and row["line"] == "stop"],
            "unverified_scope": ["Raw UCI searches not published to search_moves, including any deadline-stop tail.",
                "Full raw-UCI numeric/result equivalence and every native CPU tick; their hashes are bound, counters are not replayed by this prefix helper.",
                "The unfinished game outcome; no point, draw, complete-pair statistic, interval or strength conclusion is assigned."],
            "equivalent_to_completed_block_audit": False}


def self_test():
    old = read_json(ROOT / "docs/matches/2026-10-19-neural-strength/attempt-001-time100/time100.json")
    record = copy.deepcopy(old["games"][0])
    count = len(record["opening_uci_moves"]) + 3
    record.update(uci_moves=record["uci_moves"][:count], search_moves=record["search_moves"][:3],
                  plies=count, result="*", completed=False, candidate_points=None,
                  termination="budget_exhausted", error="synthetic budget stop")
    record["final_fen"] = board_after(record["initial_fen"], record["uci_moves"]).fen(en_passant="fen")
    for role in ROLES:
        items = [item for item in record["search_moves"] if item["role"] == role]
        record["resources"][role] = {"searched_moves": len(items), "reported_nodes": sum(item["nodes"] for item in items),
            "moves_without_node_report": 0, "elapsed_seconds": round(sum(item["elapsed_seconds"] for item in items), 6)}
    require(prefix_game(record)["kind"] == "unfinished_prefix", "clean prefix rejected")
    unstarted = copy.deepcopy(record)
    unstarted.update(uci_moves=unstarted["opening_uci_moves"], search_moves=[],
                     plies=len(unstarted["opening_uci_moves"]), final_fen=unstarted["opening_fen"])
    del unstarted["resources"]
    require(prefix_game(unstarted)["unstarted_placeholder"], "valid unstarted budget record rejected")
    rejected = []
    for label, edit in (
        ("assigned result", lambda d: d.update(result="1/2-1/2")),
        ("assigned points", lambda d: d.update(candidate_points=0.5)),
        ("illegal move", lambda d: d["uci_moves"].__setitem__(-1, "a1a8")),
        ("false final FEN", lambda d: d.update(final_fen=chess.STARTING_FEN)),
        ("illegal PV", lambda d: d["search_moves"][0].update(pv=["a1a8"])),
        ("false nodes", lambda d: d["resources"]["candidate"].update(reported_nodes=0)),
    ):
        changed = copy.deepcopy(record)
        edit(changed)
        try:
            prefix_game(changed)
        except (ValueError, KeyError):
            rejected.append(label)
        else:
            raise AssertionError("accepted corrupted prefix: " + label)
    return {"status": "DIAGNOSTIC_ONLY", "self_test_completed": True,
            "corruption_rejections": rejected, "engines_used": False,
            "unstarted_placeholder_accepted": True,
            "fresh_match_artifacts_used": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--attempt", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        result = self_test()
    else:
        if args.freeze is None or args.attempt is None or args.output is None:
            parser.error("use --freeze FILE --attempt DIR --output FILE, or --self-test")
        require(not args.output.exists(), "preserve a prior diagnostic")
        result = diagnose(args.freeze, args.attempt)
    if args.output is not None:
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
