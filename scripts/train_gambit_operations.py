"""Bounded native practice after accepted sacrifices and exact-state memory.

All actual practice moves and counter-moves come from Regicide. Candidate PVs
are separately marked search evidence, not additional played games or proof.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import chess

import dynamic_play
import gambit_knowledge
import gambit_practice_memory as memory
from game_outcome import automatic_outcome
from train_gambit_policy import RuntimeTeacher

ROOT = Path(__file__).resolve().parents[1]
CURRICULUM = ROOT / "data/gambits/operations_curriculum.json"
VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 320,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def source_hashes():
    paths = ("scripts/gambit_practice_memory.py", "scripts/train_gambit_operations.py",
             "scripts/train_gambit_policy.py", "scripts/neural_search_data/engine.py",
             "scripts/dynamic_play.py", "scripts/game_outcome.py")
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}


def board_for(case):
    board = memory._board(case["moves"])
    if (board.fen(en_passant="fen") != case["fen"]
            or ("white" if board.turn else "black") != case["owner"]):
        raise ValueError("Curriculum state mismatch")
    return board


def facts(board, owner):
    color = owner == "white"
    balance = sum(value * (len(board.pieces(kind, color)) - len(board.pieces(kind, not color)))
                  for kind, value in VALUES.items())
    king = board.king(color)
    ring = chess.BB_KING_ATTACKS[king] | chess.BB_SQUARES[king]
    danger = sum(len(board.attackers(not color, target)) for target in chess.scan_forward(ring))
    own = dynamic_play._features(board, color)
    return {"material_balance_cp": balance, "material_debt_cp": max(0, -balance),
            "king_danger": danger, "owner_in_check": board.turn == color and board.is_check(),
            **own}


def classify(before, after):
    if after["material_debt_cp"] <= before["material_debt_cp"] - 75:
        return "material_recovery"
    if before["owner_in_check"] or after["king_danger"] <= before["king_danger"] - 2:
        return "defensive_transition"
    if after["developed_minors"] > before["developed_minors"]:
        return "development"
    if (after["coordinated_pressure"] > before["coordinated_pressure"]
            or after["rook_files"] > before["rook_files"]):
        return "attack_maintenance"
    return "consolidation"


def legal_line(board, moves, limit=8):
    replay, result = board.copy(stack=True), []
    for uci in moves[:limit]:
        try:
            move = chess.Move.from_uci(uci)
        except (ValueError, TypeError):
            break
        if move not in replay.legal_moves:
            break
        replay.push(move)
        result.append(uci)
        if automatic_outcome(replay) is not None:
            break
    return result, replay


def select_cases(data, per_family, seed):
    rows = data.get("cases")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Curriculum has no cases")
    grouped = defaultdict(list)
    for row in rows:
        board_for(row)
        grouped[row["family_id"]].append(row)
    result = []
    for family in sorted(grouped):
        # Cover both the first operational response and a catalog-leaf branch.
        # This uses only known shape/history, never native answer scores.
        earliest = min(grouped[family], key=lambda row: (len(row["moves"]), row["case_id"]))
        ranked = sorted(grouped[family], key=lambda row: (
            not (row.get("book_status", {}).get("has_continuation") is False
                 if isinstance(row.get("book_status"), dict) else "leaf" in str(row.get("book_status"))),
            memory.digest([seed, family, row["case_id"]])))
        result.extend([earliest, *[row for row in ranked if row["case_id"] != earliest["case_id"]]][:per_family])
    return result


def candidate_moves(board, root, *, seed, limit):
    best = chess.Move.from_uci(root["bestmove_uci"])
    candidates = [best]
    legal = list(board.legal_moves)
    forcing = sorted((move for move in legal if board.is_capture(move) or board.gives_check(move)),
                     key=lambda move: (-dynamic_play._proposal_points(board, move.uci()), move.uci()))
    geometric = sorted(legal, key=lambda move: (-dynamic_play._proposal_points(board, move.uci()),
                                               memory.digest([seed, memory.position_key(board), move.uci()])))
    for move in forcing[:1] + geometric:
        if move not in candidates:
            candidates.append(move)
        if len(candidates) >= limit:
            break
    return candidates


def run(binary, output, *, curriculum=CURRICULUM, nodes=100000, owner_turns=8,
        per_family=2, candidates=3, seed=20261105, wall_seconds=1200):
    binary, output, curriculum = Path(binary).resolve(strict=True), Path(output), Path(curriculum)
    output.mkdir(parents=True, exist_ok=False)
    data = json.loads(curriculum.read_text(encoding="utf-8"))
    cases = select_cases(data, per_family, seed)
    heldout = sorted({row["family_id"] for row in cases if row["split"] == "holdout"})
    identity = {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "evaluation_id": "classical_activity_v1"}
    manifest = {"schema": "regicide.gambit-operations-practice.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(), "seed": seed,
        "nodes": nodes, "owner_turns": owner_turns, "per_family": per_family,
        "max_candidates": candidates, "engine_identity": identity,
        "source_sha256": source_hashes(), "curriculum_sha256": hashlib.sha256(curriculum.read_bytes()).hexdigest(),
        "curriculum_path": str(curriculum), "cases": cases, "heldout_family_ids": heldout,
        "scope": "actual native play branches; exact-state analysis memory, no neural generalization",
        "minimum_stored_depth": 4, "external_engine_role": "none; native-only practice and target queries"}
    write(output / "predeclared.json", manifest)
    records, queries, practice, excluded = [], [], [], []
    teacher = RuntimeTeacher(binary, output / "native-uci.jsonl", nodes,
                             time.monotonic() + wall_seconds, search_timeout=12)
    failed = None
    try:
        with teacher:
            for index, case in enumerate(cases):
                board = board_for(case)
                owner = case["owner"]
                branch = {"case_id": case["case_id"], "family_id": case["family_id"], "split": case["split"],
                          "owner": owner, "initial_fen": board.fen(en_passant="fen"),
                          "initial_history_uci": [move.uci() for move in board.move_stack], "turns": []}
                practice.append(branch)
                for turn in range(owner_turns):
                    if automatic_outcome(board) is not None:
                        break
                    if board.turn != (owner == "white"):
                        raise ValueError("Practice owner turn mismatch")
                    prefix = f"{case['case_id']}:{turn + 1}"
                    before, before_facts = board.copy(stack=True), facts(board, owner)
                    root_query = teacher.query(board, prefix + ":root")
                    queries.append({"role": "owner_root", "case_id": case["case_id"],
                                    "fen": board.fen(en_passant="fen"),
                                    "history_uci": [m.uci() for m in board.move_stack], **root_query})
                    if not root_query.get("bestmove_uci"):
                        break
                    moves = candidate_moves(board, root_query, seed=seed, limit=candidates)
                    labels, child_queries = [], {}
                    for move in moves:
                        child = board.copy(stack=True)
                        child.push(move)
                        if automatic_outcome(child) is not None:
                            excluded.append({"query_id": prefix + ":" + move.uci(), "reason": "terminal_child"})
                            continue
                        query = teacher.query(child, prefix + ":" + move.uci())
                        child_queries[move.uci()] = query
                        queries.append({"role": "candidate_and_opponent", "case_id": case["case_id"],
                                        "candidate_uci": move.uci(), "fen": child.fen(en_passant="fen"),
                                        "history_uci": [m.uci() for m in child.move_stack], **query})
                        score = query.get("score", {})
                        line, end = legal_line(board, [move.uci(), *query.get("pv_uci", [])])
                        after_facts = facts(end, owner)
                        if (query.get("completed") is not True or query.get("lowerbound") or query.get("upperbound")
                                or score.get("kind") != "cp" or type(score.get("stm")) is not int
                                or type(query.get("depth")) is not int or query["depth"] < 4 or len(line) < 2):
                            excluded.append({"query_id": query["query_id"], "reason": "no_exact_depth4_reply_line"})
                            continue
                        labels.append({"uci": move.uci(), "offline_owner_cp": -score["stm"],
                            "native_depth": query["depth"], "native_nodes": query.get("nodes"),
                            "requested_nodes": nodes, "query_id": query["query_id"], "pv_uci": line,
                            "goal": classify(before_facts, after_facts),
                            "observations": {"before": before_facts, "after_native_pv": after_facts},
                            "line_kind": "native_search_pv_not_additional_played_game"})
                    record = {"record_id": prefix, "case_id": case["case_id"], "family_id": case["family_id"],
                        "split": case["split"], "owner": owner, "fen": board.fen(en_passant="fen"),
                        "position_key": memory.position_key(board), "history_uci": [m.uci() for m in board.move_stack],
                        "goal": labels[0]["goal"] if labels else "consolidation", "candidates": labels,
                        "curriculum_goal_tags": case.get("goal_tags", []), "source": case.get("source"),
                        "signature": case.get("signature"), "acceptance": case.get("acceptance")}
                    records.append(record)
                    played = chess.Move.from_uci(root_query["bestmove_uci"])
                    if played not in board.legal_moves:
                        raise ValueError("Native practice move is illegal")
                    board.push(played)
                    opponent_query = child_queries.get(played.uci())
                    if automatic_outcome(board) is None and opponent_query is None:
                        opponent_query = teacher.query(board, prefix + ":opponent")
                        queries.append({"role": "opponent", "case_id": case["case_id"],
                                        "fen": board.fen(en_passant="fen"),
                                        "history_uci": [m.uci() for m in board.move_stack], **opponent_query})
                    reply_uci = None
                    if automatic_outcome(board) is None and opponent_query and opponent_query.get("bestmove_uci"):
                        reply = chess.Move.from_uci(opponent_query["bestmove_uci"])
                        if reply not in board.legal_moves:
                            raise ValueError("Native opponent response is illegal")
                        reply_uci = reply.uci()
                        board.push(reply)
                    after_actual = facts(board, owner)
                    branch["turns"].append({"turn": turn + 1, "before_fen": before.fen(en_passant="fen"),
                        "owner_move": played.uci(), "opponent_move": reply_uci, "after_fen": board.fen(en_passant="fen"),
                        "actual_goal": classify(before_facts, after_actual), "before_facts": before_facts,
                        "after_actual_facts": after_actual, "record_id": prefix})
                branch.update(final_fen=board.fen(en_passant="fen"),
                              final_history_uci=[m.uci() for m in board.move_stack],
                              result=automatic_outcome(board).result() if automatic_outcome(board) else "*")
                print(json.dumps({"practice_cases": index + 1, "total_cases": len(cases),
                                  "owner_turns": sum(len(b["turns"]) for b in practice),
                                  "queries": len(queries)}), flush=True)
    except BaseException as error:
        failed = repr(error)
        raise
    finally:
        manifest.update(engine_name=teacher.identity, configured_options=teacher.options,
                        cleanup=teacher.cleanup_status, all_native_processes_closed=teacher.closed,
                        failure=failed, source_hashes_match=manifest["source_sha256"] == source_hashes(),
                        binary_hash_matches=identity["binary_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest())
        write(output / "queries.json", queries)
        write(output / "practice.json", practice)
        write(output / "records.json", records)
        write(output / "excluded.json", excluded)
        write(output / "run.json", manifest)
    return compile_memory(output)


def compile_memory(directory, memory_path=None):
    directory = Path(directory)
    run = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    records = json.loads((directory / "records.json").read_text(encoding="utf-8"))
    queries = json.loads((directory / "queries.json").read_text(encoding="utf-8"))
    practice = json.loads((directory / "practice.json").read_text(encoding="utf-8"))
    lookup = {row["query_id"]: row for row in queries}
    holdout_keys = set()
    for record in records:
        if record["split"] != "holdout":
            continue
        board = memory._board(record["history_uci"])
        holdout_keys.add(memory.position_key(board))
        for candidate in record["candidates"]:
            child = board.copy(stack=True)
            child.push_uci(candidate["uci"])
            holdout_keys.add(memory.position_key(child))
    exported, overlap_excluded, inactive_excluded = [], [], []
    for row in records:
        if row["split"] == "holdout" or not row["candidates"]:
            continue
        board = memory._board(row["history_uci"])
        if memory._context(board) is None:
            inactive_excluded.append(row["record_id"])
            continue
        keys = {memory.position_key(board)}
        for item in row["candidates"]:
            child = board.copy(stack=True)
            child.push_uci(item["uci"])
            keys.add(memory.position_key(child))
        if keys & holdout_keys:
            overlap_excluded.append(row["record_id"])
            continue
        for item in row["candidates"]:
            query = lookup[item["query_id"]]
            if (query["score"].get("kind") != "cp" or item["offline_owner_cp"] != -query["score"]["stm"]
                    or item["native_depth"] != query["depth"] or query["requested_nodes"] != run["nodes"]):
                raise ValueError("Candidate query provenance mismatch")
        exported.append(row)
    counts = {"practice_branches": len(practice), "owner_turns": sum(len(row["turns"]) for row in practice),
              "native_queries": len(queries), "requested_nodes_total": len(queries) * run["nodes"],
              "memory_records": len(exported), "memory_candidates": sum(len(row["candidates"]) for row in exported),
              "heldout_practice_records": sum(row["split"] == "holdout" for row in records),
              "overlap_excluded": len(overlap_excluded), "inactive_state_excluded": len(inactive_excluded),
              "actual_goal_counts": dict(Counter(turn["actual_goal"] for branch in practice for turn in branch["turns"])),
              "candidate_goal_counts": dict(Counter(item["goal"] for row in exported for item in row["candidates"]))}
    artifact = {"schema": memory.SCHEMA, "policy": memory.POLICY, "status": "approved",
        "scope": "exact practiced full histories only; no unseen-family generalization",
        "engine_identity": run["engine_identity"], "curriculum_sha256": run["curriculum_sha256"],
        "source_sha256": run["source_sha256"], "heldout_family_ids": run["heldout_family_ids"],
        "heldout_position_keys": sorted(holdout_keys), "counts": counts, "records": exported,
        "acceptance": {"passed": bool(exported) and run["failure"] is None and run["all_native_processes_closed"]
                       and run["source_hashes_match"] and run["binary_hash_matches"],
                       "basis": "native provenance, full-state replay, heldout exclusion; runtime verification still required"}}
    if not artifact["acceptance"]["passed"]:
        artifact["status"] = "experimental"
    artifact["memory_id"] = memory.memory_digest(artifact)
    memory._validate(artifact)
    write(directory / "operations_memory.json", artifact)
    if memory_path is not None:
        write(memory_path, artifact)
    write(directory / "audit.json", {"success": True, "counts": counts, "memory_id": artifact["memory_id"],
        "status": artifact["status"], "heldout_rows_written": 0, "overlap_excluded_ids": overlap_excluded,
        "inactive_state_excluded_ids": inactive_excluded,
        "raw_targets_sign_checked": sum(len(row["candidates"]) for row in exported),
        "all_native_processes_closed": run["all_native_processes_closed"],
        "source_hashes_match": run["source_hashes_match"], "binary_hash_matches": run["binary_hash_matches"]})
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--curriculum", type=Path, default=CURRICULUM)
    parser.add_argument("--nodes", type=int, default=100000)
    parser.add_argument("--owner-turns", type=int, default=8)
    parser.add_argument("--per-family", type=int, default=2)
    parser.add_argument("--candidates", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20261105)
    parser.add_argument("--wall-seconds", type=int, default=1200)
    parser.add_argument("--memory-path", type=Path)
    args = parser.parse_args()
    if not (args.nodes >= 100000 and 2 <= args.owner_turns <= 16 and 1 <= args.per_family <= 8
            and 1 <= args.candidates <= 6 and args.wall_seconds >= 10):
        parser.error("Use >=100000 nodes, 2..16 owner turns, 1..8 roots per family and 1..6 candidates")
    artifact = run(args.binary, args.output, curriculum=args.curriculum, nodes=args.nodes,
                   owner_turns=args.owner_turns, per_family=args.per_family, candidates=args.candidates,
                   seed=args.seed, wall_seconds=args.wall_seconds)
    if args.memory_path is not None:
        write(args.memory_path, artifact)
    print(json.dumps({"memory_id": artifact["memory_id"], "status": artifact["status"], **artifact["counts"]}), flush=True)


if __name__ == "__main__":
    main()
