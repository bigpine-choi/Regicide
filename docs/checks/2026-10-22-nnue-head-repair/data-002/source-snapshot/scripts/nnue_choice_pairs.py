"""Authenticate search-backed gap-MSE inputs without changing NNUE topology.

The teacher evaluates each forced root child. Its value is a documented
surrogate target for the actual native leaf that backed up that forced search,
not a claim that Stockfish separately evaluated the leaf. Old immediate-child
pairs remain byte-for-byte authenticated through the existing loader.
"""
from __future__ import annotations

import json
from pathlib import Path

import chess

import neural_stm as stm
import neural_training as base
import train_nnue_ordering as previous
from prepare_neural_huber import require, sha

PARTITIONS = previous.PARTITIONS
TARGET_SEMANTICS = "teacher_root_child_value_surrogate_at_backed_leaf"
NATIVE_DEPTH = 3
NATIVE_NODES = 10000


def _integer(value, message, *, minimum=None, maximum=None):
    require(type(value) is int, message)
    require(minimum is None or value >= minimum, message)
    require(maximum is None or value <= maximum, message)
    return value


def _fen_equal(board, text, message):
    require(isinstance(text, str), message)
    declared = chess.Board(text)
    require(declared.is_valid() and board.fen(en_passant="fen") == declared.fen(en_passant="fen"), message)


def _play(board, moves, message, *, identities=None, ongoing=False):
    require(isinstance(moves, list) and all(isinstance(text, str) for text in moves), message)
    for text in moves:
        move = chess.Move.from_uci(text)
        require(move in board.legal_moves, message)
        board.push(move)
        if ongoing:
            require(board.outcome(claim_draw=False) is None and board.halfmove_clock < 100
                    and not board.is_repetition(3), "Native PV reaches terminal or current draw claim")
        if identities is not None:
            identities.add(base.feature_identity(board))
    return board


def _teacher(receipt, child, history, partition, family, target, identities):
    require(isinstance(receipt, dict) and receipt.get("status") == "accepted"
            and receipt.get("reasons") == [], "Teacher receipt must be accepted exact CP")
    _fen_equal(child, receipt.get("fen"), "Teacher root-child FEN differs")
    require(receipt.get("partition") == partition and receipt.get("family_id") == family,
            "Teacher root-child source partition/family differs")
    require(receipt.get("history_plies") == len(history), "Teacher root-child history length differs")
    binary_sha = receipt.get("binary_sha256")
    require(isinstance(binary_sha, str) and len(binary_sha) == 64, "Teacher binary SHA missing")
    require(receipt.get("requested_nodes") == 100000 and receipt.get("requested_depth") == 10,
            "Teacher root-child fixed depth/node budget differs")
    require(child.outcome(claim_draw=True) is None, "Teacher root child is terminal or claimable")
    score = receipt.get("score", {})
    pov = "white" if child.turn else "black"
    require(score.get("kind") == "cp" and score.get("pov") == pov
            and score.get("mate_stm") is None and score.get("mate_white") is None,
            "Teacher score is not exact child-turn CP")
    _integer(score.get("white_cp"), "Teacher CP must be an integer", minimum=-1500, maximum=1500)
    require(score["white_cp"] == target and score.get("stm_cp") == (target if child.turn else -target),
            "Teacher normalized root-child CP differs")
    packet = receipt.get("raw_score_info_packet")
    require(isinstance(packet, dict) and not packet.get("lowerbound", False)
            and not packet.get("upperbound", False), "Teacher raw packet has a bound or is missing")
    raw_score = packet.get("score", {})
    require(raw_score.get("pov") == pov and raw_score.get("white_cp") == target
            and raw_score.get("white_mate") is None,
            "Teacher raw packet score/POV differs")
    relative = raw_score.get("relative")
    require(isinstance(relative, str) and relative.lstrip("+-").isdigit()
            and int(relative) == score["stm_cp"], "Teacher raw relative CP differs")
    depth = _integer(packet.get("depth"), "Teacher raw depth invalid", minimum=1)
    _integer(packet.get("nodes"), "Teacher raw score nodes invalid", minimum=1)
    require(receipt.get("depth") == depth and receipt.get("score_nodes") == packet["nodes"],
            "Teacher score packet depth/nodes differ")
    pv = packet.get("pv")
    require(isinstance(pv, list) and bool(pv) and receipt.get("pv_uci") == pv
            and receipt.get("bestmove_uci") == pv[0], "Teacher root-child PV differs")
    _play(child.copy(stack=True), pv, "Illegal teacher root-child PV", identities=identities)


def validate_pair(pair, warm_model, *, model_sha256=None):
    """Return every root/child/native-PV/teacher-PV canonical feature identity."""
    require(isinstance(pair, dict) and pair.get("kind") == "search_backed_gap", "Unknown backed pair kind")
    partition, family = pair.get("partition"), pair.get("family_id")
    require(partition in ("train", "tuning"), "Backed pair partition must be train or tuning")
    require(isinstance(family, str) and bool(family), "Backed pair source family missing")
    require(isinstance(pair.get("root_id"), str) and bool(pair["root_id"]), "Backed pair root id missing")
    root = chess.Board(pair["initial_fen"])
    require(root.is_valid(), "Invalid backed pair initial position")
    history = pair.get("root_history")
    _play(root, history, "Illegal backed pair root history")
    _fen_equal(root, pair.get("root_fen"), "Backed pair root replay differs")
    require(root.outcome(claim_draw=False) is None and root.halfmove_clock < 100
            and not root.is_repetition(3), "Backed pair root is terminal or currently claimable")
    root_sign = _integer(pair.get("root_sign"), "Backed pair root sign invalid")
    require(root_sign == (1 if root.turn else -1), "Backed pair root sign differs")
    require(pair.get("move_a") != pair.get("move_b"), "Backed pair moves must differ")
    identities, leaf_identities = {base.feature_identity(root)}, []
    for side in ("a", "b"):
        row = pair[side]
        require(isinstance(row, dict), "Backed leaf row missing")
        require(row.get("target_semantics") == TARGET_SEMANTICS,
                "Backed leaf must explicitly declare teacher-root-child surrogate target")
        target = _integer(row.get("target_white_cp"), "Backed surrogate target needs bounded exact CP",
                          minimum=-1500, maximum=1500)
        require(row.get("teacher_root_child_white_cp") == target,
                "Backed surrogate/root-child target differs")
        # A literal teacher_white_cp on this leaf would falsely assert an
        # independent teacher-leaf evaluation. Its provenance is root_child.
        require("teacher_white_cp" not in row, "Do not label root-child surrogate as literal teacher leaf CP")
        require(row.get("partition", partition) == partition and row.get("family_id", family) == family,
                "Backed leaf partition/family differs")
        forced = pair["move_" + side]
        child = root.copy(stack=True)
        _play(child, [forced], "Illegal backed forced root move", identities=identities, ongoing=True)
        child_record = row.get("root_child", {})
        child_history = history + [forced]
        require(child_record.get("history") == child_history, "Teacher root-child full history differs")
        _fen_equal(child, child_record.get("fen"), "Teacher root-child declared FEN differs")
        _teacher(child_record.get("teacher_receipt"), child, child_history, partition, family, target, identities)
        support = row.get("support_receipt")
        require(isinstance(support, dict) and support.get("accepted") is True
                and support.get("reason") == "exact_static_support", "Native support receipt is not accepted")
        require(support.get("initial_fen") == pair["initial_fen"]
                and support.get("root_history") == history, "Native support source history differs")
        _fen_equal(root, support.get("root_fen"), "Native support source FEN differs")
        require(support.get("forced_root_move") == forced, "Native support forced move differs")
        receipt_sha = support.get("model_sha256")
        require(isinstance(receipt_sha, str) and len(receipt_sha) == 64, "Native support model SHA missing")
        require(model_sha256 is None or receipt_sha == model_sha256, "Native support model SHA differs")
        require(support.get("nodes_limit") == NATIVE_NODES and support.get("depth_limit") == NATIVE_DEPTH,
                "Native support fixed depth/node budget differs")
        options = support.get("options", {})
        require(all(options.get(key) is True for key in
                    ("nnue", "nnue_incremental", "quiescence", "full_window_forced_root"))
                and all(options.get(key) is False for key in ("transposition", "pvs", "lmr")),
                "Native support exact-search options differ")
        search = support.get("search", {})
        require(search.get("depth") == NATIVE_DEPTH and search.get("stopped") is False,
                "Native support requested iteration incomplete")
        _integer(search.get("nodes"), "Native support nodes invalid", minimum=1, maximum=NATIVE_NODES)
        for field in ("tt_cutoffs", "pvs_probes", "lmr_reductions"):
            require(type(search.get(field)) is int and search[field] == 0,
                    "Native support used TT/scout/reduced search")
        require(search.get("tt_probes", 0) == 0, "Native support used TT probes")
        pv = search.get("pv")
        require(isinstance(pv, list) and len(pv) >= NATIVE_DEPTH
                and pv[0] == forced and search.get("bestmove") == forced, "Native forced root/PV differs")
        leaf = root.copy(stack=True)
        _play(leaf, pv, "Illegal native support PV", identities=identities, ongoing=True)
        require(not leaf.is_check(), "Native support leaf is checked")
        witness = support.get("support_leaf", {})
        require(witness.get("history") == history + pv and witness.get("path_from_root") == pv,
                "Native support full leaf history differs")
        _fen_equal(leaf, witness.get("fen"), "Native support witness FEN differs")
        _fen_equal(leaf, row.get("fen"), "Backed training leaf FEN differs")
        require(row.get("history") == history + pv, "Backed training leaf full history differs")
        require(witness.get("iteration_depth") == NATIVE_DEPTH and witness.get("ply") == len(pv)
                and witness.get("qply") == len(pv) - NATIVE_DEPTH
                and witness.get("phase") == "quiescence_stand_pat", "Native static witness iteration/phase differs")
        require(witness.get("turn") == ("white" if leaf.turn else "black")
                and witness.get("history_size") == len(history + pv), "Native witness turn/history size differs")
        repetition = 1
        while leaf.is_repetition(repetition + 1):
            repetition += 1
        require(witness.get("repetition_count") == repetition, "Native witness repetition count differs")
        require(all(witness.get(field) is False for field in
                    ("in_check", "terminal", "current_draw_claim", "draw_claim_adjusted")),
                "Native support witness is terminal, checked or draw adjusted")
        count = _integer(support.get("observations_count"), "Native observations count invalid", minimum=1)
        index = _integer(support.get("support_observation_index"), "Native support observation index invalid",
                         minimum=0, maximum=count - 1)
        require(support.get("support_observation_index_base") == 0
                and witness.get("evaluation_index") == index + 1, "Native support observation identity differs")
        white = stm.quantized_white(leaf, warm_model)
        raw = white if leaf.turn else -white
        require(type(witness.get("raw_score_cp")) is int and witness["raw_score_cp"] == raw
                and witness.get("score_cp") == raw and witness.get("white_score_cp") == white,
                "Native support differs from full Python NNUE refresh or was clamped")
        require(type(search.get("score_cp")) is int and search["score_cp"] == root_sign * white
                and search.get("white_score_cp") == white, "Native leaf does not back up forced root score")
        identity = base.feature_identity(leaf)
        require(row.get("feature_hash") == identity, "Backed training leaf feature identity differs")
        leaf_identities.append(identity)
    require(leaf_identities[0] != leaf_identities[1], "Backed pair leaves have identical canonical inputs")
    require(type(pair.get("target_gap_white_cp")) is int and pair["target_gap_white_cp"] ==
            pair["a"]["target_white_cp"] - pair["b"]["target_white_cp"], "Backed pair target gap differs")
    return identities


def _old_features(pair):
    return {base.feature_identity(chess.Board(fen))
            for fen in (pair["root_fen"], pair["a"]["fen"], pair["b"]["fen"])}


def load_pairs(dataset, rows, manifest):
    """Append authenticated backed pairs, preserving all previous pairs intact."""
    dataset = Path(dataset)
    pairs, hashes = previous.load_pairs(dataset, rows, manifest)
    warm_ref = manifest.get("warm_model", {})
    require(isinstance(warm_ref, dict), "Choice manifest warm model binding missing")
    warm_path = Path(warm_ref.get("path", ""))
    require(warm_path.is_file() and sha(warm_path) == warm_ref.get("sha256"), "Choice warm model bytes differ")
    warm_model = stm.load_model(warm_path)
    gate_ref = manifest.get("gate", {})
    require(isinstance(gate_ref, dict), "Choice manifest reserved gate binding missing")
    blocked_path = Path(gate_ref.get("blocked_features", ""))
    require(blocked_path.is_file() and sha(blocked_path) == gate_ref.get("blocked_features_sha256"),
            "Choice reserved gate bytes differ")
    blocked_rows = json.loads(blocked_path.read_text(encoding="utf-8"))
    require(isinstance(blocked_rows, list) and all(isinstance(value, str) and len(value) == 64 for value in blocked_rows),
            "Choice reserved gate feature list invalid")
    blocked = set(blocked_rows)
    identities = {part: {base.feature_identity(chess.Board(row["fen"])) for row in rows[part]}
                  for part in PARTITIONS}
    families = {part: {row["family_id"] for row in rows[part]} for part in PARTITIONS}
    keys = set()
    for partition in ("train", "tuning"):
        for pair in pairs[partition]:
            identities[partition].update(_old_features(pair))
            families[partition].add(pair["family_id"])
            keys.add((partition, pair["root_id"], tuple(sorted((pair["move_a"], pair["move_b"])))))
        name = f"pairs-backed-{partition}.jsonl"
        expected = manifest.get("backed_pair_files", {}).get(name)
        if isinstance(expected, dict):
            expected = expected.get("sha256")
        require(isinstance(expected, str) and len(expected) == 64, "Missing backed pair SHA256")
        hashes[name] = sha(dataset / name)
        require(hashes[name] == expected, "Backed pair bytes differ from manifest")
        records = [json.loads(line) for line in (dataset / name).read_text(encoding="utf-8").splitlines()]
        count = manifest.get("backed_pair_rows", {}).get(partition)
        require(type(count) is int and count == len(records) and count > 0, "Unexpected backed pair count")
        for pair in records:
            require(pair.get("partition") == partition, "Backed pair file partition differs")
            key = (partition, pair["root_id"], tuple(sorted((pair["move_a"], pair["move_b"]))))
            require(key not in keys, "Duplicate old/backed root-move comparison")
            keys.add(key)
            identities[partition].update(validate_pair(pair, warm_model, model_sha256=warm_ref["sha256"]))
            families[partition].add(pair["family_id"])
        pairs[partition] = [*pairs[partition], *records]
        combined = manifest.get("combined_pair_rows", {}).get(partition)
        require(type(combined) is int and combined == len(pairs[partition]), "Combined pair count differs")
    require("pairs-backed-test.jsonl" not in manifest.get("backed_pair_files", {}) and
            not (dataset / "pairs-backed-test.jsonl").exists(), "Test cannot have backed optimization pairs")
    for index, left in enumerate(PARTITIONS):
        require(not identities[left] & blocked, "Point/pair/PV features overlap reserved gate")
        for right in PARTITIONS[index + 1:]:
            require(not identities[left] & identities[right], "Point/pair/PV features cross partitions")
            require(not families[left] & families[right], "Point/pair families cross partitions")
    return pairs, hashes
