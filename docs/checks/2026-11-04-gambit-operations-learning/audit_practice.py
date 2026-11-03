"""Independent replay and raw-UCI audit for native operational practice."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import gambit_practice_memory as memory
import train_gambit_operations as practice
from game_outcome import automatic_outcome


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def audit(directory):
    run = read(directory / "run.json")
    records = read(directory / "records.json")
    queries = read(directory / "queries.json")
    branches = read(directory / "practice.json")
    artifact = read(directory / "operations_memory.json")
    memory._validate(artifact)
    raw = [json.loads(line) for line in (directory / "native-uci.jsonl").read_text(encoding="utf-8").splitlines()]
    by_seq = {row["seq"]: row for row in raw}
    by_query = {row["query_id"]: row for row in queries}
    by_record = {row["record_id"]: row for row in records}
    require(len(by_query) == len(queries), "duplicate query id")
    require(len(by_record) == len(records), "duplicate record id")
    require(run["all_native_processes_closed"] and run["cleanup"].get("returncode") == 0,
            "native process did not close cleanly")
    for query in queries:
        board = memory._board(query["history_uci"])
        require(board.fen(en_passant="fen") == query["fen"], "query position mismatch")
        lines = [by_seq[index] for index in range(query["uci_begin_seq"], query["uci_end_seq"] + 1)]
        sent = [item["line"] for item in lines if item["direction"] == "send"]
        received = [item["line"] for item in lines if item["direction"] == "recv"]
        require("go nodes " + str(run["nodes"]) in sent, "fixed node command missing")
        position = "position startpos" + (" moves " + " ".join(query["history_uci"]) if query["history_uci"] else "")
        require(position in sent, "full history UCI position missing")
        require(any(line.startswith("bestmove " + str(query["bestmove_uci"])) for line in received),
                "native bestmove not in raw UCI")
        require(query["completed"] and query["requested_nodes"] == run["nodes"], "incomplete query")
        move = chess.Move.from_uci(query["bestmove_uci"])
        require(move in board.legal_moves, "illegal native bestmove")
    labels = 0
    heldout_keys = set()
    for record in records:
        board = memory._board(record["history_uci"])
        require(record["fen"] == board.fen(en_passant="fen"), "record position mismatch")
        require(record["owner"] == ("white" if board.turn else "black"), "wrong owner")
        if record["split"] == "holdout":
            heldout_keys.add(memory.position_key(board))
            # Protect all legal children, including those whose shallow labels
            # were excluded from the stored candidates.
            for move in board.legal_moves:
                child = board.copy(stack=True)
                child.push(move)
                heldout_keys.add(memory.position_key(child))
        for candidate in record["candidates"]:
            query = by_query[candidate["query_id"]]
            require(query["history_uci"] == record["history_uci"] + [candidate["uci"]], "candidate history mismatch")
            require(query["score"]["kind"] == "cp" and type(query["score"]["stm"]) is int,
                    "non-CP target")
            require(candidate["offline_owner_cp"] == -query["score"]["stm"], "wrong CP perspective")
            require(not query["lowerbound"] and not query["upperbound"] and query["depth"] >= 4,
                    "shallow or bound target")
            require(candidate["native_depth"] == query["depth"], "depth mismatch")
            line, end = practice.legal_line(board, candidate["pv_uci"])
            require(line == candidate["pv_uci"] and len(line) >= 2, "illegal PV")
            require(candidate["goal"] == practice.classify(practice.facts(board, record["owner"]),
                                                          practice.facts(end, record["owner"])), "goal mismatch")
            labels += 1
    played_moves = 0
    for branch in branches:
        board = memory._board(branch["initial_history_uci"])
        require(board.fen(en_passant="fen") == branch["initial_fen"], "initial branch mismatch")
        for turn in branch["turns"]:
            require(board.fen(en_passant="fen") == turn["before_fen"], "turn before mismatch")
            before = practice.facts(board, branch["owner"])
            root = by_query[turn["record_id"] + ":root"]
            require(root["bestmove_uci"] == turn["owner_move"], "owner move not native")
            board.push_uci(turn["owner_move"])
            played_moves += 1
            if turn["opponent_move"] is not None:
                query = by_query.get(turn["record_id"] + ":" + turn["owner_move"],
                                     by_query.get(turn["record_id"] + ":opponent"))
                require(query and query["bestmove_uci"] == turn["opponent_move"], "opponent move not native")
                board.push_uci(turn["opponent_move"])
                played_moves += 1
            after = practice.facts(board, branch["owner"])
            require(turn["after_fen"] == board.fen(en_passant="fen"), "turn after mismatch")
            require(before == turn["before_facts"] and after == turn["after_actual_facts"], "facts mismatch")
            require(turn["actual_goal"] == practice.classify(before, after), "actual goal mismatch")
        require(board.fen(en_passant="fen") == branch["final_fen"], "final branch mismatch")
        result = automatic_outcome(board)
        require(branch["result"] == (result.result() if result else "*"), "invented completion")
    overlaps = []
    for record in artifact["records"]:
        require(record["split"] != "holdout" and record["family_id"] not in artifact["heldout_family_ids"],
                "heldout family exported")
        board = memory._board(record["history_uci"])
        keys = {memory.position_key(board)}
        for candidate in record["candidates"]:
            child = board.copy(stack=True)
            child.push_uci(candidate["uci"])
            keys.add(memory.position_key(child))
        if keys & heldout_keys:
            overlaps.append(record["record_id"])
    require(not overlaps, "runtime memory overlaps heldout root or legal child")
    before = hashlib.sha256((directory / "operations_memory.json").read_bytes()).hexdigest()
    rebuilt = practice.compile_memory(directory)
    after = hashlib.sha256((directory / "operations_memory.json").read_bytes()).hexdigest()
    require(before == after and rebuilt["memory_id"] == artifact["memory_id"], "memory compilation not reproducible")
    source_match = all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha
                       for name, sha in run["source_sha256"].items())
    require(source_match, "practice source changed")
    result = {"success": True, "queries_verified": len(queries), "exact_candidate_targets_verified": labels,
        "practice_branches": len(branches), "actual_owner_turns": sum(len(b["turns"]) for b in branches),
        "actual_played_plies": played_moves, "memory_records": len(artifact["records"]),
        "heldout_root_and_all_legal_child_overlaps": len(overlaps),
        "memory_compilation_byte_reproduced": before == after, "source_hashes_match": source_match,
        "all_native_processes_closed": run["all_native_processes_closed"], "memory_id": artifact["memory_id"]}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    value = audit(args.directory)
    practice.write(args.directory / "independent-audit.json", value)
    print(json.dumps(value))
