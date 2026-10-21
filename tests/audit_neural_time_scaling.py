"""Independently replay same-position time-scaling diagnostics; never run engines.

Original match trajectories are development evidence after inspection.  This
auditor verifies what was searched and reported, not chess optimality or causality.
"""

import argparse
import collections
import copy
import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import chess

from audit_see_replication import checked_chain, engine_name, identity, numeric, utc
from audit_see_results import ROOT, digest, read_json, require


ROLES = ("baseline", "candidate")
TIMES = (100, 1000)
REPEATS = (1, 2, 3)
CONTINUATION_NODES = 200000


def resolved(value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def json_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()]


def same_fen(board, fen, label):
    require(board.fen(en_passant="fen") == fen, f"{label}: FEN differs from full history")


def color_name(color):
    return "white" if color else "black"


def history_board(initial_fen, history):
    require(isinstance(history, list), "history is not a list")
    board = chess.Board(initial_fen)
    require(board.is_valid(), "invalid initial board")
    for token in history:
        move = chess.Move.from_uci(token)
        require(move in board.legal_moves, f"illegal historical move {token}")
        board.push(move)
    return board


def replay_pv(board, moves):
    require(isinstance(moves, list), "PV is not a list")
    copy_board = board.copy(stack=True)
    san = []
    for token in moves:
        move = chess.Move.from_uci(token)
        require(move in copy_board.legal_moves, f"illegal PV move {token}")
        san.append(copy_board.san(move))
        copy_board.push(move)
    return san


def parse_numeric_info(line):
    """Parse only fields this engine publishes; ignore string diagnostics."""
    words = line.split()
    if not words or words[0] != "info" or "string" in words:
        return None
    result = {}
    fields = {"depth", "seldepth", "nodes", "time"}
    index = 1
    while index < len(words):
        key = words[index]
        if key in fields:
            require(index + 1 < len(words), "truncated numeric UCI field")
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


def audit_uci_stream(rows):
    require(rows, "empty raw UCI stream")
    previous_time = -math.inf
    expected_seq = rows[0]["seq"]
    require(expected_seq in (0, 1), "raw UCI sequence must start at zero or one")
    for row in rows:
        require(type(row["seq"]) is int and row["seq"] == expected_seq,
                "raw UCI sequence missing, duplicated or reordered")
        require(row["role"] in ROLES and row["direction"] in ("send", "recv"),
                "invalid raw UCI endpoint")
        stamp = numeric(row["monotonic"], "raw UCI monotonic timestamp")
        require(stamp >= previous_time, "raw UCI time reversed")
        require(isinstance(row["line"], str) and "\n" not in row["line"],
                "invalid raw UCI line")
        previous_time, expected_seq = stamp, expected_seq + 1


def position_command_board(line):
    words = line.split()
    require(words and words[0] == "position", "missing raw position command")
    if words[1] == "startpos":
        initial = chess.STARTING_FEN
        index = 2
    else:
        require(words[1] == "fen" and len(words) >= 8, "invalid raw position command")
        initial = " ".join(words[2:8])
        index = 8
    history = []
    if len(words) > index:
        require(words[index] == "moves", "invalid position history delimiter")
        history = words[index + 1:]
    return initial, history, history_board(initial, history)


def limit_parts(limit):
    if "movetime_ms" in limit:
        require(set(limit) == {"movetime_ms"} and limit["movetime_ms"] in TIMES,
                "unexpected root query budget")
        return "movetime", limit["movetime_ms"]
    if "nodes" in limit:
        require(set(limit) == {"nodes"} and limit["nodes"] == CONTINUATION_NODES,
                "unexpected continuation node budget")
        return "nodes", limit["nodes"]
    raise ValueError("query does not declare its exact limit")


def audit_query(query, raw):
    board = history_board(query["initial_fen"], query["history"])
    same_fen(board, query["fen"], query["query_id"])
    require(query["role"] in ROLES and query["phase"] in ("root", "continuation"),
            "invalid query role or phase")
    require(query["original_mover"] in ("white", "black"), "invalid original mover")
    mode, value = limit_parts(query["limit"])
    if query["phase"] == "root":
        require(mode == "movetime" and query["repeat"] in REPEATS,
                "invalid root repeat/limit")
        require(color_name(board.turn) == query["original_mover"],
                "root mover does not match full-history board")
    else:
        require(mode == "nodes" and query["repeat"] == 1,
                "invalid continuation repeat/limit")
        require(color_name(board.turn) != query["original_mover"],
                "continuation did not change the mover")
        require(query["history"] and query["history"][-1] == query["candidate_move"],
                "continuation does not follow its declared forced candidate")
    if query.get("skipped_outcome") is not None:
        require(query["phase"] == "continuation", "root query was skipped")
        outcome = board.outcome(claim_draw=True)
        require(outcome is not None, "nonterminal continuation was skipped")
        expected = {"result": outcome.result(),
                    "winner": None if outcome.winner is None else color_name(outcome.winner),
                    "termination": outcome.termination.name.lower()}
        require(query["skipped_outcome"] == expected, "skipped outcome differs from chess rules")
        require(query.get("move_uci") is None and not query.get("pv_uci"),
                "skipped terminal row contains a searched move")
        return {"pv_plies": 0, "skipped": True}
    require(board.outcome(claim_draw=True) is None, "terminal or claimable query was searched")
    selected = chess.Move.from_uci(query["move_uci"])
    require(selected in board.legal_moves, "illegal selected diagnostic move")
    require(query["move_san"] == board.san(selected), "selected move SAN differs")
    pv = query["pv_uci"]
    require(pv and pv[0] == query["move_uci"], "PV does not start with selected move")
    require(replay_pv(board, pv) == query["pv_san"], "PV SAN differs from legal replay")
    score = query["score"]
    require(score["pov"] == "mover", "query score is not search-mover-relative")
    require((type(score["cp"]) is int) != (type(score["mate"]) is int),
            "query score must contain exactly one integer cp or mate value")
    require(type(score["lowerbound"]) is bool and type(score["upperbound"]) is bool
            and not (score["lowerbound"] and score["upperbound"]), "invalid score bounds")
    begin, end = query["uci_begin_seq"], query["uci_end_seq"]
    require(type(begin) is int and type(end) is int and begin <= end, "invalid query UCI interval")
    region = [row for row in raw if begin <= row["seq"] <= end]
    require(region and region[0]["seq"] == begin and region[-1]["seq"] == end,
            "query raw UCI interval is incomplete")
    local = [row for row in region if row["role"] == query["role"]]
    sends = [row["line"] for row in local if row["direction"] == "send"]
    require(sends.count("ucinewgame") == 1, "query lacks its own fresh-game TT reset")
    positions = [line for line in sends if line.startswith("position ")]
    require(len(positions) == 1, "query does not contain exactly one position command")
    raw_initial, raw_history, raw_board = position_command_board(positions[0])
    require(raw_initial == query["initial_fen"] and raw_history == query["history"],
            "raw UCI initial board/full history differs")
    same_fen(raw_board, query["fen"], "raw UCI")
    go = [line for line in sends if line.startswith("go ")]
    require(len(go) == 1 and go[0].split() == ["go", mode, str(value)],
            "raw UCI go budget differs from declared query")
    require(sends.index("ucinewgame") < sends.index(positions[0]) < sends.index(go[0]),
            "new-game/position/go order differs")
    receives = [row["line"] for row in local if row["direction"] == "recv"]
    best = [line for line in receives if line.startswith("bestmove ")]
    require(len(best) == 1 and best[0].split()[1] == query["move_uci"],
            "raw bestmove differs from selected move")
    infos = [info for line in receives if (info := parse_numeric_info(line)) is not None]
    require(infos, "query has no numeric raw UCI result")
    final = infos[-1]
    require(final.get("score") == score and final.get("pv", []) == pv,
            "reported score/PV differs from final raw UCI result")
    for key in ("depth", "seldepth", "nodes"):
        require(type(query[key]) is int and query[key] >= 0 and final.get(key) == query[key],
                f"reported {key} differs from final raw UCI result")
    require(abs(numeric(query["engine_time_seconds"], "engine time") * 1000 - final["time"]) < 1e-6,
            "engine time differs from raw UCI milliseconds")
    started = numeric(query["started_monotonic"], "query start")
    finished = numeric(query["finished_monotonic"], "query finish")
    require(started <= local[0]["monotonic"] <= local[-1]["monotonic"] <= finished,
            "raw UCI query interval lies outside measured search")
    require(abs(finished - started - numeric(query["elapsed_seconds"], "query elapsed")) < 1e-5,
            "query elapsed differs from monotonic bounds")
    if mode == "nodes":
        require(query["nodes"] <= value, "continuation exceeded its node limit")
    return {"pv_plies": len(pv), "skipped": False}


def modal_moves(queries):
    """Recompute the declared count-descending, UCI-ascending modal rule."""
    counts = collections.Counter(row["move_uci"] for row in queries)
    require(counts, "empty modal candidate group")
    return sorted(counts, key=lambda move: (-counts[move], move))[0]


def audit_schedule(positions, queries):
    require(len({row["query_id"] for row in queries}) == len(queries), "duplicate query ID")
    ids = [position["position_id"] for position in positions]
    require(len(ids) == len(set(ids)) and ids, "empty or duplicate selected positions")
    grouped = collections.defaultdict(list)
    continuation = collections.defaultdict(list)
    for row in queries:
        require(row["position_id"] in ids, "query refers to an unselected position")
        if row["phase"] == "root":
            grouped[(row["position_id"], row["role"], row["limit"]["movetime_ms"])].append(row)
        else:
            continuation[row["position_id"]].append(row)
    selection = {}
    for position in positions:
        position_id = position["position_id"]
        original = history_board(position["initial_fen"], position["history"])
        same_fen(original, position["fen"], position_id)
        candidates = {source["historical_move"] for source in position["sources"]}
        candidates.update(source["historical_long_search"]["move"]
                          for source in position["sources"] if "historical_long_search" in source)
        modes = {}
        for role in ROLES:
            for movetime in TIMES:
                key = (position_id, role, movetime)
                group = grouped[key]
                require(len(group) == 3 and sorted(row["repeat"] for row in group) == list(REPEATS),
                        "missing/duplicate root condition or repeat")
                for row in group:
                    require(row["initial_fen"] == position["initial_fen"]
                            and row["history"] == position["history"]
                            and row["fen"] == position["fen"], "root differs from selected full history")
                    require(row["original_mover"] == color_name(original.turn), "root original mover differs")
                mode = modal_moves(group)
                modes[f"{role}_{movetime}"] = mode
                candidates.add(mode)
        for move in candidates:
            require(chess.Move.from_uci(move) in original.legal_moves, "illegal continuation candidate")
        rows = continuation[position_id]
        observed = [(row["candidate_move"], row["role"]) for row in rows]
        require(len(observed) == len(set(observed)) and set(observed) == {
            (move, role) for move in candidates for role in ROLES},
            "continuation set differs from four modal choices plus actual move")
        for row in rows:
            require(row["initial_fen"] == position["initial_fen"]
                    and row["history"] == position["history"] + [row["candidate_move"]],
                    "continuation lost or altered the original history")
            require(row["original_mover"] == color_name(original.turn),
                    "continuation original mover differs")
        selection[position_id] = {"modal_moves": modes, "candidate_moves": sorted(candidates)}
    require(len(grouped) == len(positions) * 4, "unexpected root query condition")
    return selection


def independent_selection(short, long):
    """Recompute selection from original games, without importing the driver."""
    require(len(short["games"]) == len(long["games"]) == 16, "original game count changed")
    changed, positions, by_history = [], [], {}

    def append(game, search, rule, extra=None):
        history = game["uci_moves"][:search["ply"] - 1]
        board = history_board(game["initial_fen"], history)
        same_fen(board, search["fen_before"], "original game selection")
        key = (game["initial_fen"], tuple(history))
        if key not in by_history:
            record = {"position_id": f"P{len(positions) + 1:02}",
                      "initial_fen": game["initial_fen"], "history": history,
                      "fen": board.fen(en_passant="fen"),
                      "original_mover": color_name(board.turn), "sources": []}
            positions.append(record)
            by_history[key] = record
        source = {"game": game["game"], "pair": game["pair"], "opening": game["opening"],
                  "selection_rule": rule, "source_movetime_ms": 100, "ply": search["ply"],
                  "historical_role": search["role"], "historical_move": search["move"],
                  "historical_search": search}
        if extra:
            source.update(extra)
        by_history[key]["sources"].append(source)

    def cp(search):
        return search["score"]["cp"] if search["score"] else None

    for short_game, long_game in zip(short["games"], long["games"]):
        require(short_game["game"] == long_game["game"]
                and short_game["pair"] == long_game["pair"]
                and short_game["white"] == long_game["white"]
                and short_game["black"] == long_game["black"], "original paired schedule differs")
        if short_game["candidate_points"] == long_game["candidate_points"]:
            continue
        changed.append({"game": short_game["game"], "pair": short_game["pair"],
                        "opening": short_game["opening"],
                        "candidate_color": "white" if short_game["white"] == "candidate" else "black",
                        "short_points": short_game["candidate_points"],
                        "long_points": long_game["candidate_points"]})
        different = [i for i, (a, b) in enumerate(zip(short_game["uci_moves"], long_game["uci_moves"]))
                     if a != b]
        require(different, "changed game has no divergent move")
        ply = different[0] + 1
        short_search = next(row for row in short_game["search_moves"] if row["ply"] == ply)
        long_search = next(row for row in long_game["search_moves"] if row["ply"] == ply)
        append(short_game, short_search, "first_divergence", {"historical_long_search": long_search})
        own = [row for row in short_game["search_moves"] if row["role"] == "candidate"]
        eligible = []
        for before, after in zip(own, own[1:]):
            a, b = cp(before), cp(after)
            if a is not None and b is not None and a >= -200 and b - a <= -100:
                eligible.append((b - a, before["ply"], before, after))
        if eligible:
            delta, _, before, after = sorted(eligible, key=lambda item: item[:2])[0]
            append(short_game, before, "largest_nonmate_drop_pre_at_least_minus200",
                   {"recorded_drop_cp": delta, "next_own_search": after})
        for start in range(1, len(own) - 2):
            three = own[start:start + 3]
            if all(cp(row) is not None and cp(row) <= -100 for row in three):
                append(short_game, own[start - 1], "before_first_three_own_scores_at_most_minus100",
                       {"subsequent_own_scores_cp": [cp(row) for row in three]})
                break
    return {"schema_version": 1, "changed_games": changed, "positions": positions}


def independent_long_loss_control(short, long):
    """Inspect the worse long-time game using the same two loss-scene rules.

    This is a separately declared posthoc scope supplement.  It neither modifies
    the primary fifteen scenes nor adds searches to the original strength test.
    """
    short_game = next(game for game in short["games"] if game["game"] == 15)
    game = next(game for game in long["games"] if game["game"] == 15)
    require(game["candidate_points"] < short_game["candidate_points"],
            "long-loss control game is not a deterioration")
    changed = [{"game": game["game"], "pair": game["pair"], "opening": game["opening"],
                "candidate_color": "white" if game["white"] == "candidate" else "black",
                "short_points": short_game["candidate_points"], "long_points": game["candidate_points"]}]
    positions, by_history = [], {}

    def append(search, rule, extra):
        history = game["uci_moves"][:search["ply"] - 1]
        board = history_board(game["initial_fen"], history)
        same_fen(board, search["fen_before"], "long-loss control")
        key = (game["initial_fen"], tuple(history))
        if key not in by_history:
            record = {"position_id": f"S{len(positions) + 1:02}", "initial_fen": game["initial_fen"],
                      "history": history, "fen": board.fen(en_passant="fen"),
                      "original_mover": color_name(board.turn), "sources": []}
            positions.append(record)
            by_history[key] = record
        source = {"game": game["game"], "pair": game["pair"], "opening": game["opening"],
                  "selection_rule": rule, "source_movetime_ms": 1000, "ply": search["ply"],
                  "historical_role": search["role"], "historical_move": search["move"],
                  "historical_search": search, **extra}
        by_history[key]["sources"].append(source)

    own = [row for row in game["search_moves"] if row["role"] == "candidate"]
    cp = lambda row: row["score"]["cp"] if row["score"] else None
    falls = [(cp(after) - cp(before), before["ply"], before, after)
             for before, after in zip(own, own[1:]) if cp(before) is not None and cp(after) is not None
             and cp(before) >= -200 and cp(after) - cp(before) <= -100]
    require(falls, "long-loss control has no eligible maximum drop")
    delta, _, before, after = min(falls, key=lambda item: item[:2])
    append(before, "largest_nonmate_drop_pre_at_least_minus200",
           {"recorded_drop_cp": delta, "next_own_search": after})
    found = False
    for start in range(1, len(own) - 2):
        three = own[start:start + 3]
        if all(cp(row) is not None and cp(row) <= -100 for row in three):
            append(own[start - 1], "before_first_three_own_scores_at_most_minus100",
                   {"subsequent_own_scores_cp": [cp(row) for row in three]})
            found = True
            break
    require(found, "long-loss control lacks three consecutive losing scores")
    return {"schema_version": 1, "changed_games": changed, "positions": positions}


def audit_query_order(positions, queries, selection):
    expected = []
    for index, position in enumerate(positions):
        for repeat in REPEATS:
            conditions = [("baseline", 100), ("candidate", 100),
                          ("baseline", 1000), ("candidate", 1000)]
            if (index + repeat) % 2 == 0:
                conditions = list(reversed(conditions))
            expected.extend(("root", position["position_id"], role, repeat, tc, None)
                            for role, tc in conditions)
    for position in positions:
        position_id = position["position_id"]
        expected.extend(("continuation", position_id, role, 1, CONTINUATION_NODES, move)
                        for move in selection[position_id]["candidate_moves"] for role in ROLES)
    actual = [(row["phase"], row["position_id"], row["role"], row["repeat"],
               row["limit"].get("movetime_ms", row["limit"].get("nodes")), row.get("candidate_move"))
              for row in queries]
    require(actual == expected, "actual query order differs from the predeclared condition order")
    require([row["query_id"] for row in queries] == [f"Q{i:04}" for i in range(1, len(queries) + 1)],
            "query IDs differ from complete serial schedule")


def audit_monitor(summary, rows, queries, commands, raw):
    """Replay native CPU evidence with search boundaries instead of match counts."""
    require(summary["schema_version"] == 1 and summary["poll_seconds"] == 0.5
            and summary["max_allowed_snapshot_gap_seconds"] == 2.0, "CPU sampling policy changed")
    require(summary["cpu_ticks_per_second"] == 10_000_000
            and summary["external_cpu_tolerance_ticks"] == 0
            and summary["new_or_disappearing_external_engine_is_failure"] is True,
            "CPU exclusion/failure policy changed")
    require(summary["completed_idle_seconds"] >= 10 and summary["failure"] is None
            and summary["contaminated"] is False and summary["valid_for_comparison"] is True
            and summary["observed_external_engine_cpu_increase"] is False, "CPU guard rejected interval")
    require(rows and len(rows) == summary["snapshot_count"] and len(rows) >= 22,
            "missing CPU evidence or idle coverage")
    require(rows[0]["phase"] == "initial" and rows[-1]["phase"] == "finished", "CPU interval incomplete")
    root = (summary["monitor_root_pid"], summary["monitor_root_creation_ticks"])
    require(all(type(value) is int and value > 0 for value in root), "invalid CPU monitor identity")
    transports = summary["transport_engines"]
    require(set(transports) == set(ROLES), "CPU monitor lacks exactly two UCI roles")
    registered = {identity(row): role for role, row in transports.items()}
    owned = {identity(row): row for row in summary["owned_engines"]}
    require(len(registered) == len(summary["owned_engines"]) == 2 and set(owned) == set(registered),
            "excluded engine identities differ from actual UCI transports")
    for role, transport in transports.items():
        require(transport["command"] == commands[role], "registered transport command differs")
        require(Path(transport["command"][0]).name.casefold() == transport["name"].casefold(),
                "native engine name differs from executable")
        checked_chain(transport, root)
        checked_chain(owned[identity(transport)], root)
        for key in ("pid", "creation_ticks", "parent_pid", "name", "ancestry_to_monitor", "ancestry_processes"):
            require(transport[key] == owned[identity(transport)][key], "native transport evidence differs")
    names = {name.casefold() for name in summary["engine_names"]}
    require({"regicide.exe", "stockfish.exe", "lc0.exe", "lc0-cpu.exe"} <= names,
            "CPU monitor does not cover required known engines")
    seen_owned, original_external, previous_external = set(), None, None
    previous_elapsed, previous_utc, idle = None, None, None
    first_utc = utc(rows[0]["utc"])
    max_gap, max_duration, total_cpu = 0.0, 0.0, 0
    phases = []
    boundary_utc = {}
    for row in rows:
        require(row["contaminated"] is False, "contaminated CPU snapshot")
        elapsed = numeric(row["elapsed_seconds"], "CPU snapshot elapsed")
        duration = numeric(row["snapshot_duration_seconds"], "CPU snapshot duration")
        gap = duration if previous_elapsed is None else elapsed - previous_elapsed
        require(0 <= gap <= 2 and duration <= 2 and abs(gap - row["gap_seconds"]) <= 1e-7,
                "missing or misreported CPU sampling gap")
        timestamp = utc(row["utc"])
        require(previous_utc is None or timestamp >= previous_utc, "CPU UTC timestamps reversed")
        require(abs((timestamp - first_utc).total_seconds() - elapsed + rows[0]["elapsed_seconds"]) < 2,
                "CPU UTC/monotonic coverage diverged")
        max_gap, max_duration = max(max_gap, gap), max(max_duration, duration)
        current, encountered = {}, set()
        for process in row["processes"]:
            require(not process.get("error") and engine_name(process["name"], names),
                    "unmeasurable or unexpected process in CPU log")
            pid_identity = identity(process)
            require(pid_identity not in encountered, "duplicate process in CPU snapshot")
            encountered.add(pid_identity)
            ticks = process["cpu_ticks"]
            require(type(ticks) is int and ticks >= 0 and type(process["owned"]) is bool
                    and type(process["exited_during_snapshot"]) is bool, "invalid native CPU row")
            if process["owned"]:
                require(pid_identity in registered, "unregistered owned engine excluded from CPU check")
                if pid_identity not in seen_owned or process["ancestry_to_monitor"] is not None:
                    checked_chain(process, root)
                seen_owned.add(pid_identity)
            else:
                require(pid_identity not in registered and process["exited_during_snapshot"] is False
                        and process["ancestry_to_monitor"] is None
                        and process["ancestry_processes"] is None, "external engine identity was hidden or changed")
                current[pid_identity] = ticks
        if original_external is None:
            original_external = dict(current)
            require(not seen_owned, "search engine launched before the idle gate")
        require(set(current) == set(original_external), "external engine identity set changed")
        delta = 0
        if previous_external is not None:
            for item, ticks in current.items():
                require(ticks >= previous_external[item], "external CPU counter decreased")
                delta += ticks - previous_external[item]
        require(delta == row["external_cpu_delta_ticks"] == 0, "external engine used CPU during comparison")
        total_cpu += delta
        phases.append(row["phase"])
        if row["phase"] == "idle-complete":
            require(idle is None and not seen_owned, "idle gate repeated or engines launched during idle")
            idle = row
        if row["phase"].startswith(("before-query:", "after-query:")):
            require(row["phase"] not in boundary_utc, "duplicate query monitor boundary")
            boundary_utc[row["phase"]] = timestamp
        previous_elapsed, previous_utc, previous_external = elapsed, timestamp, current
    require(idle is not None and idle["elapsed_seconds"] - rows[0]["elapsed_seconds"] >= 10,
            "native log does not demonstrate ten-second idle gate")
    require(seen_owned == set(registered), "an excluded UCI process was never observed")
    declared_external = {identity(row): row["initial_cpu_ticks"] for row in summary["external_engine_identities"]}
    require(len(declared_external) == len(summary["external_engine_identities"])
            and declared_external == original_external, "external engine summary differs from first snapshot")
    require(total_cpu == summary["external_cpu_increase_ticks"] == 0
            and abs(max_gap - summary["max_snapshot_gap_seconds"]) <= 1e-7
            and abs(max_duration - summary["max_snapshot_duration_seconds"]) <= 1e-7,
            "CPU aggregate differs from raw snapshots")
    searched = [row for row in queries if row.get("skipped_outcome") is None]
    expected = [phase + row["query_id"] for row in searched for phase in ("before-query:", "after-query:")]
    observed = [phase for phase in phases if phase.startswith(("before-query:", "after-query:"))]
    require(observed == expected, "CPU log does not enclose the exact serial query schedule")
    for query in searched:
        region = [row for row in raw if query["uci_begin_seq"] <= row["seq"] <= query["uci_end_seq"]]
        require(boundary_utc["before-query:" + query["query_id"]] <= utc(region[0]["utc"])
                <= utc(region[-1]["utc"]) <= boundary_utc["after-query:" + query["query_id"]],
                "raw UCI exchange lies outside native query boundaries")
    require(utc(summary["started_utc"]) <= first_utc <= utc(idle["utc"])
            and utc(rows[-1]["utc"]) <= utc(summary["finished_utc"]), "native interval timestamps differ")
    return {"status": "PASS", "snapshots": len(rows), "searched_queries": len(searched),
            "external_engine_cpu_ticks": total_cpu, "max_gap_seconds": max_gap,
            "max_snapshot_duration_seconds": max_duration, "scope": summary["scope"]}


def advertised_options(raw, role):
    defaults = {}
    for row in raw:
        if row["role"] != role or row["direction"] != "recv" or not row["line"].startswith("option name "):
            continue
        name, tail = row["line"][len("option name "):].split(" type ", 1)
        parts = tail.split()
        option_type = parts[0]
        if option_type == "button":
            continue
        require("default" in parts, "advertised non-button option lacks a default")
        begin = parts.index("default") + 1
        ends = [parts.index(token) for token in ("min", "max", "var") if token in parts]
        value = " ".join(parts[begin:min(ends) if ends else len(parts)])
        if option_type == "check":
            require(value in ("true", "false"), "invalid advertised check default")
            value = value == "true"
        elif option_type == "spin":
            value = int(value)
        require(name not in defaults, "UCI option advertised twice")
        defaults[name] = value
    require(defaults, "no raw UCI advertised options")
    return defaults


def audit_options(state, protocol, freeze, raw, queries):
    reported = state["options"]
    checksum = str(freeze["model"]["checksum"])
    neural_option = ("UseNNUEEvaluation" if "UseNNUEEvaluation" in reported["advertised_defaults"]["candidate"]
                     else "UseNeuralEvaluation")
    expected_options = {neural_option: True, "UseIncrementalNeuralEvaluation": True,
                        "NeuralModelFile": str(resolved(freeze["model"]["path"]))}
    first_query_seq = min(row["uci_begin_seq"] for row in queries if row.get("skipped_outcome") is None)
    defaults_by_role, effective_by_role = {}, {}
    for role in ROLES:
        defaults = advertised_options(raw, role)
        require(defaults == reported["advertised_defaults"][role], "reported defaults differ from raw UCI")
        effective = dict(defaults)
        for row in raw:
            if row["role"] != role or row["direction"] != "send" or not row["line"].startswith("setoption name "):
                continue
            require(row["seq"] < first_query_seq, "engine option changed during diagnostic queries")
            text = row["line"][len("setoption name "):]
            require(" value " in text, "unexpected option button in search protocol")
            name, value = text.split(" value ", 1)
            require(name in defaults, "raw setoption was not advertised")
            if type(defaults[name]) is bool:
                require(value in ("true", "false"), "invalid raw Boolean setoption")
                value = value == "true"
            elif type(defaults[name]) is int:
                value = int(value)
            effective[name] = value
        expected = {**defaults, **(expected_options if role == "candidate" else {})}
        require(effective == expected == reported["effective_options"][role],
                "raw, declared and expected effective options differ")
        for name, value in protocol["common_options"].items():
            require(effective.get(name) == value, "common options changed")
        require(defaults.get(neural_option) is False
                and defaults.get("NeuralModelFile") == "<builtin>", "evaluator defaults changed")
        defaults_by_role[role], effective_by_role[role] = defaults, effective
        require(state["commands"][role] == [str(resolved(freeze[role]["binary"])), "uci"],
                "actual command differs from frozen role binary")
    require(defaults_by_role["baseline"] == defaults_by_role["candidate"], "same binaries advertised different defaults")
    require(reported["requested_options"] == {"baseline": {}, "candidate": expected_options},
            "unplanned evaluator configuration")
    acknowledgements = [row for row in raw if row["role"] == "candidate" and row["direction"] == "recv"
                        and row["line"] == "info string neural model checksum " + checksum]
    require(len(acknowledgements) == 1 and acknowledgements[0] == state["model_ack"]
            and acknowledgements[0]["seq"] < first_query_seq,
            "actual selected model acknowledgement missing or mismatched")
    failures = ("search failed", "invalid", "unsupported", "unknown command", "missing or extra")
    require(not any(row["direction"] == "recv" and row["line"].startswith("info string ")
                    and any(word in row["line"].lower() for word in failures) for row in raw),
            "engine reported a protocol/model/search error")
    return {"status": "PASS", "effective_options": effective_by_role,
            "model_ack_seq": acknowledgements[0]["seq"], "model_checksum": checksum}


def audit_static(output, freeze, queries):
    expected_fens = set()
    for row in queries:
        board = history_board(row["initial_fen"], row["history"])
        expected_fens.add(board.fen(en_passant="fen"))
        for token in row.get("pv_uci", []):
            board.push_uci(token)
            expected_fens.add(board.fen(en_passant="fen"))
    fens = sorted(expected_fens)
    document = read_json(output / "static.json")
    require(document["schema_version"] == 1 and document["outside_timed_monitor"] is True,
            "static evaluations not separated from timed searches")
    command = document["command"]
    require(len(command) == 4 and resolved(command[0]) == resolved(freeze["baseline"]["binary"])
            and command[1] == "eval-batch" and command[2] in ("--neural-model", "--nnue-model")
            and resolved(command[3]) == resolved(freeze["model"]["path"]), "static command/model differs")
    require((output / "static-inputs.txt").read_text(encoding="utf-8-sig") == "\n".join(fens) + "\n",
            "static input set differs from all query/PV positions")
    tsv = (output / "static.tsv").read_text(encoding="utf-8-sig").splitlines()
    require(len(tsv) == len(fens) == len(document["positions"]), "static row count differs")
    parsed = []
    for fen, line in zip(fens, tsv):
        columns = line.split("\t")
        require(len(columns) == 3 and all(re.fullmatch(r"-?\d+", value) for value in columns),
                "invalid static evaluation output")
        classical, full, incremental = map(int, columns)
        require(full == incremental, "learned full and incremental scores differ")
        parsed.append({"fen": fen, "classical": classical, "full": full, "incremental": incremental})
    require(parsed == document["positions"], "static JSON differs from raw eval-batch output")
    differences = [row["full"] - row["classical"] for row in parsed]
    return {"status": "PASS", "positions": len(fens), "full_incremental_mismatches": 0,
            "max_absolute_static_difference_cp": max(map(abs, differences), default=0),
            "scope": "Both scores are mover-relative; evaluator disagreement is not ground-truth error."}


def audit(protocol_path, output):
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    protocol = read_json(protocol_path)
    state = read_json(output / "run-state.json")
    require(protocol["schema_version"] == state["schema_version"] == 1, "unsupported diagnostic schema")
    require(protocol["root_time_controls_ms"] == list(TIMES) and protocol["root_repeats"] == 3
            and protocol["fresh_game_each_query"] is True and protocol["preserve_full_history"] is True
            and protocol["continuation_nodes"] == CONTINUATION_NODES
            and protocol["continuation_repeats"] == 1, "diagnostic schedule/policy changed")
    require(protocol["monitor"] == {"idle_seconds": 10, "poll_seconds": 0.5,
                                    "max_snapshot_gap_seconds": 2, "external_engine_cpu_ticks": 0},
            "diagnostic CPU policy changed")
    old_freeze_path = resolved(protocol["old_freeze"])
    require(old_freeze_path == ROOT / "docs/matches/2026-10-19-neural-strength/freeze.json",
            "diagnostic is not linked to the original frozen comparison")
    freeze = read_json(old_freeze_path)
    require(protocol["engine_source_commit"] == freeze["engine_source_commit"]
            and protocol["common_options"] == freeze["common_effective_options"], "engine/option baseline changed")
    require(protocol["common_options"]["Move Overhead"] == 20
            and protocol["common_options"]["Hash"] == 16, "time overhead or Hash changed")
    bound = dict(freeze["files"])
    for role in ROLES:
        bound[freeze[role]["binary"]] = freeze[role]["sha256"]
    for field in ("model", "openings", "prior_validation"):
        bound[freeze[field]["path"]] = freeze[field]["sha256"]
    require(freeze["baseline"]["sha256"] == freeze["candidate"]["sha256"], "engine bytes differ by role")
    require(all(protocol["files"].get(name) == value for name, value in bound.items()),
            "diagnostic omits or changes original frozen inputs")
    required = [old_freeze_path, resolved(protocol["positions"]),
                ROOT / "scripts/analyze_neural_time_scaling.py"]
    originals = [old_freeze_path.parent / f"attempt-001-time{tc}/time{tc}.json" for tc in TIMES]
    required.extend(originals)
    for path in required:
        require(path.relative_to(ROOT).as_posix() in protocol["files"], "protocol omits a required source/tool")
    for name, value in protocol["files"].items():
        require(re.fullmatch(r"[0-9a-f]{64}", value) is not None and digest(resolved(name)) == value,
                f"frozen diagnostic input changed: {name}")
    require(resolved(state["protocol_path"]) == protocol_path
            and state["protocol_sha256"] == digest(protocol_path), "run used another protocol")
    for snapshot in (state["before"], state["after"]):
        require(snapshot is not None and snapshot["protocol_sha256"] == digest(protocol_path)
                and snapshot["files"] == protocol["files"], "before/after frozen file receipt differs")
    require(utc(protocol["created_utc"]) <= utc(state["before"]["utc"])
            <= utc(state["started_utc"]) <= utc(state["after"]["utc"])
            <= utc(state["finished_utc"]), "protocol/run chronology differs")
    require(state["all_searches_completed"] is True and state["valid"] is True and state["error"] is None,
            "run is incomplete or invalid")
    require(state["wake_request"]["restore_succeeded"] is True, "temporary wake request was not restored")
    selected = read_json(resolved(protocol["positions"]))
    variant = protocol.get("variant", "short_changed_games")
    require(variant in ("short_changed_games", "long_loss_control"), "unsupported scene-selection variant")
    selector = independent_long_loss_control if variant == "long_loss_control" else independent_selection
    expected_selection = selector(*(read_json(path) for path in originals))
    require(selected == expected_selection, "selected scenes differ from the frozen historical rule")
    queries = json_lines(output / "queries.jsonl")
    raw = json_lines(output / "uci.jsonl")
    require(state["query_count"] == len(queries), "run-state query count differs")
    audit_uci_stream(raw)
    selection = audit_schedule(selected["positions"], queries)
    audit_query_order(selected["positions"], queries, selection)
    replayed = [audit_query(query, raw) for query in queries]
    non_skipped = [row for row in queries if row.get("skipped_outcome") is None]
    for previous, current in zip(non_skipped, non_skipped[1:]):
        require(previous["finished_monotonic"] <= current["started_monotonic"]
                and previous["uci_end_seq"] < current["uci_begin_seq"], "diagnostic queries overlapped")
    options = audit_options(state, protocol, freeze, raw, queries)
    monitor = audit_monitor(read_json(output / "monitor.json"), json_lines(output / "monitor.jsonl"),
                            queries, state["commands"], raw)
    static = audit_static(output, freeze, queries)
    artifacts = {path.name: digest(path) for path in output.iterdir() if path.is_file()}
    return {"status": "PASS", "audited_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "auditor_sha256": digest(Path(__file__)),
            "selection_variant": variant,
            "protocol_sha256": digest(protocol_path), "bound_input_files": len(protocol["files"]),
            "changed_games": len(selected["changed_games"]), "selected_positions": len(selected["positions"]),
            "queries": len(queries), "searched_queries": len(non_skipped),
            "skipped_terminal_queries": sum(item["skipped"] for item in replayed),
            "root_queries": sum(row["phase"] == "root" for row in queries),
            "continuation_queries": sum(row["phase"] == "continuation" for row in queries),
            "legal_pv_plies": sum(item["pv_plies"] for item in replayed),
            "independent_candidate_selection": selection, "options": options,
            "monitor": monitor, "static": static, "artifact_sha256": artifacts,
            "scope": "Posthoc full-history, empty-TT diagnostics. Legal PVs and same-node child references do not prove best play, causal explanation, general strength or Elo."}


def synthetic_query():
    board = history_board(chess.STARTING_FEN, ["e2e4"])
    query = {"query_id": "Q0001", "phase": "root", "position_id": "P01", "role": "candidate",
             "repeat": 1, "limit": {"movetime_ms": 100}, "initial_fen": chess.STARTING_FEN,
             "history": ["e2e4"], "fen": board.fen(en_passant="fen"), "original_mover": "black",
             "candidate_move": None, "move_uci": "e7e5", "move_san": "e5",
             "pv_uci": ["e7e5", "g1f3"], "pv_san": ["e5", "Nf3"],
             "score": {"pov": "mover", "cp": 35, "mate": None, "lowerbound": False, "upperbound": False},
             "depth": 3, "seldepth": 6, "nodes": 100, "engine_time_seconds": 0.08,
             "elapsed_seconds": 0.08, "started_monotonic": 1.0, "finished_monotonic": 1.08,
             "uci_begin_seq": 1, "uci_end_seq": 7}
    lines = [("send", "ucinewgame"), ("send", "isready"), ("recv", "readyok"),
             ("send", "position startpos moves e2e4"), ("send", "go movetime 100"),
             ("recv", "info depth 3 seldepth 6 score cp 35 nodes 100 time 80 pv e7e5 g1f3"),
             ("recv", "bestmove e7e5")]
    raw = [{"seq": i, "monotonic": 1 + i / 100, "utc": "2026-10-19T00:00:00+00:00",
            "role": "candidate", "direction": direction, "line": line}
           for i, (direction, line) in enumerate(lines, 1)]
    return query, raw


def self_test():
    query, raw = synthetic_query()
    audit_uci_stream(raw)
    require(audit_query(query, raw)["pv_plies"] == 2, "legal self-test fixture rejected")
    rejected = []

    def reject(label, mutation, checker=None):
        q, r = copy.deepcopy(query), copy.deepcopy(raw)
        mutation(q, r)
        try:
            (checker or audit_query)(q, r)
        except (ValueError, KeyError, StopIteration, AssertionError):
            rejected.append(label)
        else:
            raise ValueError("adversarial mutation was accepted: " + label)

    reject("lost full history", lambda q, r: q.update(history=[]))
    reject("illegal PV", lambda q, r: q.update(pv_uci=["e7e5", "a1a8"]))
    reject("wrong SAN", lambda q, r: q.update(move_san="e6"))
    reject("wrong score perspective", lambda q, r: q["score"].update(pov="white"))
    reject("missing fresh-game reset", lambda q, r: r[0].update(line="isready"))
    reject("raw budget differs", lambda q, r: r[4].update(line="go movetime 1000"))
    reject("raw score differs", lambda q, r: q["score"].update(cp=36))
    reject("raw bestmove differs", lambda q, r: r[-1].update(line="bestmove c7c5"))
    require(modal_moves([{"move_uci": "e7e5"}, {"move_uci": "c7c5"}]) == "c7c5",
            "modal lexical tie-breaking differs")
    # Exercise independent selection on frozen historical inputs without searching.
    old = ROOT / "docs/matches/2026-10-19-neural-strength"
    historical = [old / f"attempt-001-time{tc}/time{tc}.json" for tc in TIMES]
    evidence = None
    if all(path.is_file() for path in historical):
        selection = independent_selection(*(read_json(path) for path in historical))
        supplement = independent_long_loss_control(*(read_json(path) for path in historical))
        evidence = {"changed_games": len(selection["changed_games"]),
                    "positions": len(selection["positions"]),
                    "long_loss_control_positions": len(supplement["positions"])}
    return {"status": "PASS", "adversarial_rejections": len(rejected), "mutations": rejected,
            "historical_selection": evidence, "engines_launched": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--attempt", "--run", dest="attempt", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        result = self_test()
    else:
        require(args.protocol is not None and args.attempt is not None,
                "--protocol and --attempt are required")
        result = audit(args.protocol, args.attempt)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
