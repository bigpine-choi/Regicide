"""Prepare a new, focused research revision; never run an engine or install memory.

Close scores and score changes are selection heuristics, not correct-move labels.
Every exported case retains its full legal history and the original holdout set.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path

import chess
import chess.pgn

import gambit_operations as operations
import gambit_practice_memory as memory
from gambit_research_session import atomic_write

ROOT = Path(__file__).resolve().parents[1]
CURRICULUM = ROOT / "data/gambits/operations_curriculum.json"
SCHEMA = "regicide.gambit-focused-research.v1"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("Nonfinite JSON value: " + value)
    value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs,
                       parse_constant=nonfinite)
    json.dumps(value, allow_nan=False)  # Also reject overflow such as 1e999.
    return value


def _identity(run):
    value = run.get("engine_identity")
    if (not isinstance(value, dict) or set(value) != {"binary_sha256", "evaluation_id"}
            or not isinstance(value["binary_sha256"], str) or len(value["binary_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["binary_sha256"])
            or not isinstance(value["evaluation_id"], str) or not value["evaluation_id"]):
        raise ValueError("Research has no valid engine/evaluator identity")
    return value


def _board_record(record):
    board = memory._board(record.get("history_uci"))
    if (record.get("fen") != board.fen(en_passant="fen")
            or record.get("position_key") != memory.position_key(board)
            or record.get("owner") != ("white" if board.turn else "black")):
        raise ValueError("Research record does not match its full history")
    seen = set()
    candidates = record.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Research record has no candidate list")
    for item in candidates:
        if (not isinstance(item, dict) or type(item.get("offline_owner_cp")) is not int
                or type(item.get("native_depth")) is not int or item["native_depth"] < 4):
            raise ValueError("Candidate has no completed finite CP evidence")
        uci, pv = item.get("uci"), item.get("pv_uci")
        if (not isinstance(uci, str) or uci in seen or not isinstance(pv, list)
                or not 2 <= len(pv) <= 16 or pv[0] != uci):
            raise ValueError("Candidate has a duplicate move or missing reply PV")
        seen.add(uci)
        replay = board.copy(stack=True)
        for word in pv:
            move = chess.Move.from_uci(word)
            if replay.outcome(claim_draw=False) is not None or move not in replay.legal_moves:
                raise ValueError("Candidate PV contains an illegal continuation")
            replay.push(move)
    return board


def _candidate_provenance(record, board, queries, run):
    for item in record["candidates"]:
        query = queries.get(item.get("query_id"))
        child = board.copy(stack=True)
        child.push_uci(item["uci"])
        if (query is None or query.get("role") != "candidate_and_opponent"
                or "root_moves_uci" in query or query.get("case_id") != record["case_id"]
                or query.get("candidate_uci") != item["uci"]
                or query.get("fen") != child.fen(en_passant="fen")
                or query.get("history_uci") != [move.uci() for move in child.move_stack]
                or query.get("completed") is not True
                or any(type(query.get(key, False)) is not bool or query.get(key, False)
                       for key in ("lowerbound", "upperbound"))):
            raise ValueError("Candidate does not reference a completed unrestricted child query")
        score = query.get("score", {})
        if (score.get("kind") != "cp" or type(score.get("stm")) is not int
                or -score["stm"] != item["offline_owner_cp"]
                or type(query.get("depth")) is not int or query["depth"] != item["native_depth"]
                or type(query.get("requested_nodes")) is not int or query["requested_nodes"] != run.get("nodes")
                or item.get("requested_nodes") != query["requested_nodes"]
                or type(query.get("nodes")) is not int or not 0 <= query["nodes"] <= query["requested_nodes"]
                or item.get("native_nodes") != query["nodes"]):
            raise ValueError("Candidate CP sign, depth or node evidence differs from its native query")
        pv = query.get("pv_uci")
        if (not isinstance(pv, list) or not pv or pv[0] != query.get("bestmove_uci")
                or item["pv_uci"] != [item["uci"], *pv][:8]):
            raise ValueError("Candidate reply PV differs from its native query")
        for word in pv:
            move = chess.Move.from_uci(word)
            if child.outcome(claim_draw=False) is not None or move not in child.legal_moves:
                raise ValueError("Candidate native query has an illegal PV")
            child.push(move)


def load_research(directory, inputs):
    directory = Path(directory).resolve(strict=True)
    paths = [directory / name for name in ("run.json", "records.json", "practice.json", "queries.json")]
    for path in paths:
        inputs[str(path)] = sha(path)
    run, records, practice, queries = [read_json(path) for path in paths]
    _identity(run)
    if (run.get("research_status", "complete") != "complete" or run.get("failure") is not None
            or run.get("all_native_processes_closed") is not True
            or run.get("source_hashes_match") is not True or run.get("binary_hash_matches") is not True):
        raise ValueError("Select positions from a complete, intact research run; resume paused work first")
    if run.get("schema") == "regicide.gambit-operations-practice.v2":
        cleanup = run.get("cleanup", {})
        if cleanup.get("returncode_observed") is not True or cleanup.get("returncode") != 0:
            raise ValueError("Research native process did not exit successfully")
    if not isinstance(records, list) or not isinstance(practice, list) or not isinstance(queries, list):
        raise ValueError("Research records/practice/queries must be lists")
    query_index = {}
    for query in queries:
        if (not isinstance(query, dict) or not isinstance(query.get("query_id"), str)
                or query["query_id"] in query_index):
            raise ValueError("Research query IDs must be unique strings")
        query_index[query["query_id"]] = query
    index = {}
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("record_id"), str)
                or record["record_id"] in index):
            raise ValueError("Research record IDs must be unique strings")
        board = _board_record(record)
        if record.get("split") not in ("train", "dev", "validation", "holdout"):
            raise ValueError("Research record has no declared data split")
        _candidate_provenance(record, board, query_index, run)
        index[record["record_id"]] = record
    visited = set()
    for branch in practice:
        board = memory._board(branch.get("initial_history_uci"))
        if branch.get("initial_fen") != board.fen(en_passant="fen"):
            raise ValueError("Practice initial position does not match its history")
        for turn in branch.get("turns", []):
            record_id = turn.get("record_id")
            record = index.get(record_id)
            if (record is None or record_id in visited or record.get("case_id") != branch.get("case_id")
                    or record.get("family_id") != branch.get("family_id")
                    or record.get("owner") != branch.get("owner")
                    or record["history_uci"] != [move.uci() for move in board.move_stack]
                    or record["fen"] != turn.get("before_fen") or record["fen"] != board.fen(en_passant="fen")):
                raise ValueError("Practice turn and research record differ")
            visited.add(record_id)
            for word in (turn.get("owner_move"), turn.get("opponent_move")):
                if word is not None:
                    move = chess.Move.from_uci(word)
                    if board.outcome(claim_draw=False) is not None or move not in board.legal_moves:
                        raise ValueError("Practice has an illegal actual move")
                    board.push(move)
            if turn.get("after_fen") != board.fen(en_passant="fen"):
                raise ValueError("Practice result differs from actual moves")
        if (branch.get("final_fen") != board.fen(en_passant="fen")
                or branch.get("final_history_uci") != [move.uci() for move in board.move_stack]):
            raise ValueError("Practice final history differs from actual moves")
    if visited != set(index):
        raise ValueError("Research record is missing its actual practice turn")
    return run, records


def _prior_reservations(run, records):
    declared = run.get("heldout_family_ids", [])
    reserved = run.get("reserved_holdout_position_keys", [])
    if (not isinstance(declared, list) or not all(isinstance(value, str) for value in declared)
            or not isinstance(reserved, list) or not all(isinstance(value, str) for value in reserved)):
        raise ValueError("Invalid prior research holdout declaration")
    families, keys = set(declared), set(reserved)
    families.update(row["family_id"] for row in records if row["split"] == "holdout")
    for row in run.get("cases", []):
        if row.get("split") == "holdout":
            families.add(row["family_id"])
    for record in records:
        if record["family_id"] not in families:
            continue
        board = memory._board(record["history_uci"])
        keys.add(memory.position_key(board))
        for move in board.legal_moves:
            child = board.copy(stack=True)
            child.push(move)
            keys.add(memory.position_key(child))
    return families, keys


def _state(record):
    return memory.digest([record["fen"], record["history_uci"], record["owner"]])


def _gap(record):
    ranked = sorted(record["candidates"], key=lambda row: (-row["offline_owner_cp"], row["uci"]))
    if len(ranked) < 2:
        return {"cp": None, "reason": "fewer_than_two_candidates"}
    top = ranked[:2]
    if top[0]["native_depth"] != top[1]["native_depth"]:
        return {"cp": None, "reason": "different_completed_depths"}
    return {"cp": top[0]["offline_owner_cp"] - top[1]["offline_owner_cp"],
            "depth": top[0]["native_depth"], "moves_uci": [row["uci"] for row in top]}


def _swings(record, comparison):
    current = {row["uci"]: row for row in record["candidates"]}
    rows = []
    for other in comparison:
        if _state(record) != _state(other):
            continue
        for item in other["candidates"]:
            if item["uci"] not in current:
                continue
            before = current[item["uci"]]
            rows.append({"uci": item["uci"], "source_cp": before["offline_owner_cp"],
                         "comparison_cp": item["offline_owner_cp"],
                         "source_depth": before["native_depth"], "comparison_depth": item["native_depth"],
                         "absolute_swing_cp": abs(before["offline_owner_cp"] - item["offline_owner_cp"]),
                         "comparison_record_id": other["record_id"]})
    return rows


def _pgn_positions(path, plies, game_number, inputs):
    path = Path(path).resolve(strict=True)
    inputs[str(path)] = sha(path)
    if not plies or any(type(ply) is not int or ply < 0 for ply in plies) or game_number < 1:
        raise ValueError("PGN requires explicit nonnegative --ply positions and a positive game number")
    with path.open(encoding="utf-8-sig") as stream:
        game = None
        for _ in range(game_number):
            game = chess.pgn.read_game(stream)
            if game is None:
                raise ValueError("Requested PGN game does not exist")
            if game.errors:
                raise ValueError("PGN contains parse or legality errors")
    board = game.board()
    if board.chess960 or board.fen(en_passant="fen") != chess.STARTING_FEN:
        raise ValueError("PGN must retain the standard initial position and complete history")
    moves = list(game.mainline_moves())
    if max(plies) > len(moves):
        raise ValueError("Requested PGN ply lies after the main line")
    result = []
    for ply in sorted(set(plies)):
        board = game.board()
        for move in moves[:ply]:
            if board.outcome(claim_draw=False) is not None or move not in board.legal_moves:
                raise ValueError("PGN continues after a compulsory terminal state")
            board.push(move)
        result.append((board, {"kind": "user_selected_pgn_position", "pgn_sha256": inputs[str(path)],
                               "game_number": game_number, "ply": ply,
                               "reason": "user_selection_not_automatic_error_diagnosis"}))
    return result


def prepare(output, *, research=None, case_ids=None, record_ids=None, gap_cp=None,
            comparison=None, swing_cp=None, pgn=None, plies=None, game_number=1):
    if (research is None) == (pgn is None):
        raise ValueError("Choose exactly one research directory or PGN input")
    if any(value is not None and (type(value) is not int or value < 0) for value in (gap_cp, swing_cp)):
        raise ValueError("Score thresholds must be nonnegative integer centipawns")
    if (comparison is None) != (swing_cp is None):
        raise ValueError("--comparison and --swing-cp must be used together")
    if pgn is not None and any((case_ids, record_ids, gap_cp is not None, comparison is not None)):
        raise ValueError("PGN positions use --ply rather than research-record rules")
    output = Path(output).resolve()
    protected = (ROOT / "data", memory.DEFAULT_MEMORY.parent, CURRICULUM.parent, ROOT.parent / "sources")
    if output.exists() or any(output.is_relative_to(path.resolve()) for path in protected):
        raise ValueError("Use a new research output directory outside data/ and synced sources/")
    inputs = {str(CURRICULUM.resolve()): sha(CURRICULUM)}
    base = read_json(CURRICULUM)
    payload = {key: value for key, value in base.items() if key != "curriculum_sha256"}
    expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if base.get("curriculum_sha256") != expected:
        raise ValueError("Base curriculum checksum mismatch")
    heldout = {row["family_id"] for row in base["cases"] if row["split"] == "holdout"}
    heldout |= set(base.get("split_policy", {}).get("holdout_families", []))
    if not heldout or not any(row["split"] == "holdout" for row in base["cases"]):
        raise ValueError("Full base curriculum has no heldout declaration")
    reserved = set()
    positions, examined = [], []
    if research is not None:
        run, records = load_research(research, inputs)
        prior_families, prior_keys = _prior_reservations(run, records)
        heldout.update(prior_families)
        reserved.update(prior_keys)
        case_ids, record_ids = set(case_ids or []), set(record_ids or [])
        if case_ids - {row["case_id"] for row in records} or record_ids - {row["record_id"] for row in records}:
            raise ValueError("Requested case or record ID is absent from research records")
        if not (case_ids or record_ids or gap_cp is not None or swing_cp is not None):
            raise ValueError("Select explicit IDs or at least one score-selection rule")
        others = []
        if comparison is not None:
            compare_run, others = load_research(comparison, inputs)
            if (_identity(run) != _identity(compare_run)
                    or run.get("configured_options", {}) != compare_run.get("configured_options", {})):
                raise ValueError("Comparison requires the same engine, evaluator and native options")
            prior_families, prior_keys = _prior_reservations(compare_run, others)
            heldout.update(prior_families)
            reserved.update(prior_keys)
        for record in records:
            if ((case_ids or record_ids) and record["case_id"] not in case_ids
                    and record["record_id"] not in record_ids):
                continue
            gap, swings = _gap(record), _swings(record, others)
            reasons = []
            if gap_cp is not None and gap["cp"] is not None and gap["cp"] <= gap_cp:
                reasons.append("close_stored_candidate_scores")
            if swing_cp is not None and any(row["absolute_swing_cp"] >= swing_cp for row in swings):
                reasons.append("stored_score_swing")
            if gap_cp is None and swing_cp is None:
                reasons.append("explicit_record_selection")
            detail = {"kind": "prior_research_record", "record_id": record["record_id"],
                      "case_id": record["case_id"], "family_id": record["family_id"],
                      "reasons": reasons, "candidate_gap": gap, "common_candidate_swings": swings,
                      "engine_identity": _identity(run)}
            examined.append(detail)
            if reasons:
                positions.append((_board_record(record), detail))
    else:
        positions = _pgn_positions(pgn, plies, game_number, inputs)
    if heldout - set(base["families"]):
        raise ValueError("Prior holdout family is outside the supported full base curriculum")
    holdout_cases = []
    for original in base["cases"]:
        if original["family_id"] not in heldout:
            continue
        case = copy.deepcopy(original)
        if case["split"] != "holdout":
            case["prior_base_split"] = case["split"]
            case["split"] = "holdout"
            case["holdout_basis"] = "preserved declaration from input/comparison research"
        holdout_cases.append(case)
        board = memory._board(case["moves"])
        reserved.add(memory.position_key(board))
        for move in board.legal_moves:
            child = board.copy(stack=True)
            child.push(move)
            reserved.add(memory.position_key(child))
    focused, excluded, seen = [], [], set()
    for board, provenance in positions:
        current, context = memory._context(board), operations.context(board)
        reason = None
        if current is None:
            reason = "not_a_valid_full_history_memory_state"
        elif not context.get("accepted"):
            reason = "no_verified_accepted_gambit_context"
        elif context.get("owner") != current["owner"]:
            reason = "opponent_to_move"
        elif context["family_id"] in heldout:
            reason = "heldout_family"
        elif provenance.get("family_id", context["family_id"]) != context["family_id"]:
            reason = "record_family_differs_from_actual_history"
        keys = {memory.position_key(board)}
        for move in board.legal_moves:
            child = board.copy(stack=True)
            child.push(move)
            keys.add(memory.position_key(child))
        if reason is None and keys & reserved:
            reason = "heldout_root_or_immediate_child_overlap"
        state = memory.digest([board.fen(en_passant="fen"), [move.uci() for move in board.move_stack]])
        if reason is None and state in seen:
            reason = "duplicate_full_history_position"
        if reason:
            excluded.append({"reason": reason, "selection": provenance})
            continue
        seen.add(state)
        template = next(row for row in base["cases"] if row["case_id"] == context["case_id"])
        case = copy.deepcopy(template)
        case_id = "focus-" + context["family_id"] + "-" + state[:16]
        case.update(case_id=case_id, root_fen=chess.STARTING_FEN,
            moves=list(current["history_uci"]), history_uci=list(current["history_uci"]),
            fen=current["fen"], position_key=current["position_key"], root_ply=len(board.move_stack),
            owner=current["owner"], material_balance_cp=context["material_balance_cp"],
            material_debt_cp=context["material_debt_cp"], goal_tags=[context["goal"]],
            goal_status="research topic; not a correct-move label", book_status="focused_research_position",
            has_catalog_continuation=False, source_graph_edge_exists=False,
            variation={"kind": "project_authored", "notes": "Selected prior research/PGN state; no move prescribed",
                       "response_status": "not a best-defense claim"},
            source={"kind": "focused_research_input", "base_case_id": template["case_id"],
                    "base_source": copy.deepcopy(template.get("source")),
                    "base_curriculum_sha256": base["curriculum_sha256"]},
            focus_provenance=copy.deepcopy(provenance),
            runtime_context={"active": context["active"], "reason": context["reason"],
                             "window_plies": operations.MAX_AFTER_ACCEPTANCE_PLIES})
        focused.append(case)
    document = copy.deepcopy(base)
    document.update(policy="regicide-gambit-focused-research-v1", cases=[*focused, *holdout_cases],
                    focused_research={"schema": SCHEMA, "base_curriculum_sha256": base["curriculum_sha256"],
                        "focus_case_ids": [row["case_id"] for row in focused],
                        "automatic_research_performed": False, "active_memory_changed": False})
    document["split_policy"]["holdout_families"] = sorted(heldout)
    document["split_policy"]["validation_families"] = [family for family in
        document["split_policy"].get("validation_families", []) if family not in heldout]
    document["split_policy"]["reservation_basis"] = "union of full base and prior input/comparison declarations; not globally unseen families"
    for family in heldout:
        document["families"][family]["split"] = "holdout"
    document["focused_research"]["reserved_holdout_position_keys"] = sorted(reserved)
    document["reserved_holdout_position_keys"] = sorted(reserved)
    document["counts"] = {"families": len({row["family_id"] for row in document["cases"]}),
        "cases": len(document["cases"]), "focused_cases": len(focused), "preserved_holdout_cases": len(holdout_cases),
        "by_split": dict(Counter(row["split"] for row in document["cases"]))}
    document["claims"] = {"selected_scores_are_correct_move_labels": False, "best_defense_verified": False,
                           "new_research_performed": False, "playing_strength_improved": False}
    document.pop("curriculum_sha256", None)
    document["curriculum_sha256"] = hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    report = {"schema": SCHEMA, "inputs_sha256": inputs, "tool_sha256": sha(__file__),
        "focus_case_ids": [row["case_id"] for row in focused],
        "thresholds": {"candidate_gap_cp_max": gap_cp, "absolute_score_swing_cp_min": swing_cp},
        "heldout_family_ids": sorted(heldout), "reserved_holdout_position_keys": sorted(reserved),
        "examined": examined, "excluded": excluded, "counts": document["counts"],
        "scope": "Selection heuristics/user-selected positions only; finite native scores are not ground truth. No engine was run.",
        "unchanged_inputs": all(sha(path) == expected for path, expected in inputs.items())}
    if not report["unchanged_inputs"]:
        raise ValueError("Input changed during preparation; no output created")
    output.mkdir(parents=True, exist_ok=False)
    atomic_write(output / "curriculum.json", document)
    atomic_write(output / "focus-report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--research", type=Path)
    source.add_argument("--pgn", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", dest="case_ids", action="append")
    parser.add_argument("--record", dest="record_ids", action="append")
    parser.add_argument("--gap-cp", type=int)
    parser.add_argument("--comparison", type=Path)
    parser.add_argument("--swing-cp", type=int)
    parser.add_argument("--ply", dest="plies", type=int, action="append")
    parser.add_argument("--game", dest="game_number", type=int, default=1)
    args = parser.parse_args(argv)
    report = prepare(args.output, research=args.research, pgn=args.pgn, case_ids=args.case_ids,
        record_ids=args.record_ids, gap_cp=args.gap_cp, comparison=args.comparison,
        swing_cp=args.swing_cp, plies=args.plies, game_number=args.game_number)
    print(json.dumps({"focus_case_ids": report["focus_case_ids"], "counts": report["counts"],
                      "research_performed": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
