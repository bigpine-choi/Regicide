"""History, target provenance, backed score and held-out boundaries; no engines."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import neural_stm as stm
import neural_training as base
import nnue_choice_pairs as choice
from prepare_neural_huber import sha


def tiny_model():
    weights = stm.with_turn_parameters({
        "input_weights": np.zeros((768, 16), dtype=np.float64),
        "hidden_bias": np.zeros(16, dtype=np.float64),
        "output_weights": np.zeros(16, dtype=np.float64)})
    weights["hidden_bias"][0] = 10
    weights["input_weights"][8, 0] = 1
    weights["output_weights"][0] = 2
    return stm.quantize(weights, 32)


def board_from(initial, history):
    board = chess.Board(initial)
    for text in history:
        board.push_uci(text)
    return board


def quiet_move(board):
    return min((move for move in board.legal_moves if not board.gives_check(move)), key=lambda move: move.uci())


def backed_pair(*, initial=chess.STARTING_FEN, history=(), partition="train", family="new-train",
                moves=None, paths=None, root_id="new-root", targets=(120, -40), model=None):
    model = tiny_model() if model is None else model
    root = board_from(initial, history)
    if moves is None:
        moves = [move.uci() for move in sorted(root.legal_moves, key=lambda move: move.uci())
                 if not root.gives_check(move)][:2]
    pair = {"kind": "search_backed_gap", "root_id": root_id,
            "initial_fen": initial, "root_history": list(history), "root_fen": root.fen(en_passant="fen"),
            "root_sign": 1 if root.turn else -1, "family_id": family, "partition": partition,
            "move_a": moves[0], "move_b": moves[1], "target_gap_white_cp": targets[0] - targets[1]}
    for index, side in enumerate(("a", "b")):
        child = root.copy(stack=True)
        child.push_uci(moves[index])
        teacher_move = quiet_move(child).uci()
        target = targets[index]
        pov = "white" if child.turn else "black"
        teacher_stm = target if child.turn else -target
        receipt = {"status": "accepted", "reasons": [], "fen": child.fen(en_passant="fen"),
                   "partition": partition, "family_id": family, "history_plies": len(history) + 1,
                   "binary_sha256": "a" * 64, "score": {"kind": "cp", "pov": pov,
                   "white_cp": target, "stm_cp": teacher_stm, "mate_stm": None, "mate_white": None},
                   "depth": 10, "score_nodes": 100, "requested_nodes": 100000, "requested_depth": 10,
                   "pv_uci": [teacher_move], "bestmove_uci": teacher_move,
                   "raw_score_info_packet": {"depth": 10, "nodes": 100, "pv": [teacher_move],
                       "score": {"pov": pov, "relative": str(teacher_stm), "white_cp": target, "white_mate": None}}}
        pv = list(paths[index]) if paths is not None else [moves[index]]
        leaf = root.copy(stack=True)
        if paths is None:
            leaf.push_uci(pv[0])
            for _ in range(2):
                move = quiet_move(leaf)
                pv.append(move.uci())
                leaf.push(move)
        else:
            for text in pv:
                leaf.push_uci(text)
        white = stm.quantized_white(leaf, model)
        raw = white if leaf.turn else -white
        witness = {"fen": leaf.fen(en_passant="fen"), "history": list(history) + pv,
                   "path_from_root": pv, "evaluation_index": 4, "iteration_depth": 3,
                   "ply": len(pv), "qply": len(pv) - 3, "phase": "quiescence_stand_pat",
                   "raw_score_cp": raw, "score_cp": raw, "white_score_cp": white,
                   "turn": "white" if leaf.turn else "black", "history_size": len(history) + len(pv),
                   "repetition_count": 1, "in_check": False, "terminal": False,
                   "current_draw_claim": False, "draw_claim_adjusted": False}
        support = {"schema_version": 1, "id": root_id + "-" + moves[index],
                   "initial_fen": initial, "root_history": list(history), "root_fen": root.fen(en_passant="fen"),
                   "forced_root_move": moves[index], "model_path": "unit-model.nnue", "model_sha256": "b" * 64,
                   "nodes_limit": 10000, "depth_limit": 3,
                   "options": {"nnue": True, "nnue_incremental": True, "quiescence": True,
                               "transposition": False, "pvs": False, "lmr": False, "full_window_forced_root": True},
                   "search": {"depth": 3, "score_cp": pair["root_sign"] * white, "white_score_cp": white,
                              "bestmove": moves[index], "pv": pv, "nodes": 900,
                              "tt_cutoffs": 0, "pvs_probes": 0, "lmr_reductions": 0, "stopped": False},
                   "accepted": True, "reason": "exact_static_support", "support_leaf": witness,
                   "observations_count": 7, "support_observation_index": 3, "support_observation_index_base": 0}
        pair[side] = {"fen": leaf.fen(en_passant="fen"), "history": list(history) + pv,
                      "feature_hash": base.feature_identity(leaf), "partition": partition, "family_id": family,
                      "target_white_cp": target, "teacher_root_child_white_cp": target,
                      "target_semantics": choice.TARGET_SEMANTICS,
                      "root_child": {"fen": child.fen(en_passant="fen"), "history": list(history) + [moves[index]],
                                     "teacher_receipt": receipt}, "support_receipt": support}
    return pair


def old_pair(initial, partition, family):
    root = chess.Board(initial)
    moves = [move.uci() for move in sorted(root.legal_moves, key=lambda move: move.uci())][:2]
    pair = {"initial_fen": initial, "root_history": [], "root_fen": root.fen(en_passant="fen"),
            "root_sign": 1 if root.turn else -1, "root_id": "old-" + partition,
            "family_id": family, "partition": partition, "move_a": moves[0], "move_b": moves[1],
            "target_gap_white_cp": 100}
    for side, move, target in zip(("a", "b"), moves, (100, 0)):
        child = root.copy(stack=True)
        child.push_uci(move)
        pair[side] = {"fen": child.fen(en_passant="fen"), "history": [move],
                      "target_white_cp": target, "feature_hash": base.feature_identity(child)}
    return pair


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


class ChoicePairTests(unittest.TestCase):
    def test_accepts_true_leaf_support_for_both_root_colors_and_arbitrary_teacher_gap_sign(self):
        model = tiny_model()
        for history, targets in (([], (120, -40)), (["a2a3"], (-120, -40)), (["a2a3"], (-40, -120))):
            with self.subTest(history=history, targets=targets):
                explicit = ({"moves": ["e7e5", "c7c5"],
                             "paths": [["e7e5", "b2b3", "d7d6"], ["c7c5", "b2b3", "d7d6"]]}
                            if history else {})
                pair = backed_pair(history=history, targets=targets, model=model, **explicit)
                identities = choice.validate_pair(pair, model, model_sha256="b" * 64)
                self.assertIn(pair["a"]["feature_hash"], identities)
                self.assertIn(base.feature_identity(chess.Board(pair["root_fen"])), identities)
                self.assertGreaterEqual(len(identities), 5)
                if history:
                    self.assertNotEqual(pair["a"]["support_receipt"]["search"]["white_score_cp"], 0)

    def test_surrogate_is_explicit_and_teacher_root_child_packet_must_authenticate_exact_cp(self):
        pair, model = backed_pair(), tiny_model()
        mutations = [
            lambda p: p["a"].pop("target_semantics"),
            lambda p: p["a"].update(teacher_white_cp=120),
            lambda p: p["a"].update(teacher_root_child_white_cp=121),
            lambda p: p["a"]["root_child"]["teacher_receipt"]["raw_score_info_packet"].update(lowerbound=True),
            lambda p: p["a"]["root_child"]["teacher_receipt"]["raw_score_info_packet"]["score"].update(white_cp=121),
            lambda p: p["a"]["root_child"]["teacher_receipt"]["raw_score_info_packet"]["score"].update(relative="9"),
            lambda p: p["a"]["root_child"]["teacher_receipt"].update(status="excluded"),
            lambda p: p["a"]["root_child"]["teacher_receipt"].update(history_plies=999),
            lambda p: p["a"]["root_child"]["teacher_receipt"].update(partition="tuning"),
            lambda p: p["a"]["root_child"]["teacher_receipt"].update(requested_depth=9),
            lambda p: p["a"]["root_child"]["teacher_receipt"].update(requested_nodes=99999),
        ]
        for change in mutations:
            broken = deepcopy(pair)
            change(broken)
            with self.subTest(change=change), self.assertRaises(ValueError):
                choice.validate_pair(broken, model)

    def test_source_and_leaf_replay_and_support_score_cannot_be_substituted(self):
        pair, model = backed_pair(), tiny_model()
        mutations = [
            lambda p: p.update(root_history=["a1a8"]),
            lambda p: p["a"].update(history=["e2e4"]),
            lambda p: p["a"].update(fen=chess.STARTING_FEN),
            lambda p: p["a"].update(feature_hash="a" * 64),
            lambda p: p["a"]["root_child"].update(history=[]),
            lambda p: p["a"]["support_receipt"]["search"].update(white_score_cp=999),
            lambda p: p["a"]["support_receipt"]["support_leaf"].update(raw_score_cp=999),
            lambda p: p["a"]["support_receipt"]["support_leaf"].update(score_cp=999),
            lambda p: p["a"]["support_receipt"]["support_leaf"].update(evaluation_index=5),
            lambda p: p["a"]["support_receipt"]["support_leaf"].update(repetition_count=2),
        ]
        for change in mutations:
            broken = deepcopy(pair)
            change(broken)
            with self.subTest(change=change), self.assertRaises(ValueError):
                choice.validate_pair(broken, model)

    def test_interrupted_bound_optimized_checked_or_draw_adjusted_support_rejected(self):
        pair, model = backed_pair(), tiny_model()
        mutations = [
            lambda p: p["a"]["support_receipt"]["search"].update(stopped=True),
            lambda p: p["a"]["support_receipt"]["search"].update(depth=2),
            lambda p: p["a"]["support_receipt"]["search"].update(tt_cutoffs=1),
            lambda p: p["a"]["support_receipt"]["search"].update(pvs_probes=1),
            lambda p: p["a"]["support_receipt"]["options"].update(transposition=True),
            lambda p: p["a"]["support_receipt"]["options"].update(full_window_forced_root=False),
            lambda p: p["a"]["support_receipt"]["support_leaf"].update(draw_claim_adjusted=True),
            lambda p: p["a"]["support_receipt"]["support_leaf"].update(in_check=True),
            lambda p: p["a"]["support_receipt"]["support_leaf"].update(qply=5),
        ]
        for change in mutations:
            broken = deepcopy(pair)
            change(broken)
            with self.subTest(change=change), self.assertRaises(ValueError):
                choice.validate_pair(broken, model)

    def test_transposed_sibling_leaf_cannot_create_contradictory_training_targets(self):
        pair = backed_pair(moves=["g1f3", "b1c3"],
                           paths=[["g1f3", "b8c6", "b1c3"], ["b1c3", "b8c6", "g1f3"]])
        with self.assertRaisesRegex(ValueError, "identical canonical"):
            choice.validate_pair(pair, tiny_model())

    def _bank(self, path):
        model = tiny_model()
        stm.save_model(model, path / "warm.nnue")
        model_sha = sha(path / "warm.nnue")
        (path / "blocked.json").write_text("[]\n", encoding="utf-8")
        manifest = {"pair_files": {}, "pair_rows": {}, "backed_pair_files": {},
                    "backed_pair_rows": {}, "combined_pair_rows": {},
                    "warm_model": {"path": str(path / "warm.nnue"), "sha256": model_sha},
                    "gate": {"blocked_features": str(path / "blocked.json"),
                             "blocked_features_sha256": sha(path / "blocked.json")}}
        fens = {"train": "6k1/7p/8/8/8/8/P7/1K1N4 w - - 0 1",
                "tuning": "6k1/6pp/8/8/8/8/P7/1K1NB3 w - - 0 1",
                "test": "6k1/5ppp/8/8/8/8/PP6/1K1NB3 w - - 0 1"}
        rows = {part: [{"fen": fen, "family_id": "point-" + part}] for part, fen in fens.items()}
        backed = {}
        for part in ("train", "tuning"):
            old = old_pair(fens[part], part, "old-" + part)
            name = f"pairs-{part}.jsonl"
            write_rows(path / name, [old])
            manifest["pair_files"][name], manifest["pair_rows"][part] = sha(path / name), 1
            history = [] if part == "train" else ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6"]
            new = backed_pair(history=history, partition=part, family="new-" + part,
                              root_id="new-" + part, model=model)
            for side in ("a", "b"):
                new[side]["support_receipt"]["model_sha256"] = model_sha
            backed[part] = new
            name = f"pairs-backed-{part}.jsonl"
            write_rows(path / name, [new])
            manifest["backed_pair_files"][name], manifest["backed_pair_rows"][part] = sha(path / name), 1
            manifest["combined_pair_rows"][part] = 2
        return rows, manifest, backed

    def test_loader_keeps_old_bytes_and_appends_verified_pairs_with_all_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            rows, manifest, backed = self._bank(path)
            originals = {part: (path / f"pairs-{part}.jsonl").read_bytes() for part in ("train", "tuning")}
            pairs, hashes = choice.load_pairs(path, rows, manifest)
            self.assertEqual({part: len(value) for part, value in pairs.items()}, {"train": 2, "tuning": 2})
            self.assertEqual(len(hashes), 4)
            self.assertEqual(pairs["train"][1], backed["train"])
            for part in ("train", "tuning"):
                self.assertEqual((path / f"pairs-{part}.jsonl").read_bytes(), originals[part])

    def test_loader_rejects_native_pv_overlap_with_test_and_reserved_gate(self):
        for boundary in ("test", "gate"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                rows, manifest, backed = self._bank(path)
                native = backed["train"]["a"]["support_receipt"]
                board = board_from(native["initial_fen"], native["root_history"] + native["search"]["pv"][:2])
                if boundary == "test":
                    rows["test"].append({"fen": board.fen(en_passant="fen"), "family_id": "test-pv-exposure"})
                else:
                    (path / "blocked.json").write_text(json.dumps([base.feature_identity(board)]), encoding="utf-8")
                    manifest["gate"]["blocked_features_sha256"] = sha(path / "blocked.json")
                with self.assertRaisesRegex(ValueError, "cross partitions|reserved gate"):
                    choice.load_pairs(path, rows, manifest)

    def test_loader_rejects_tampered_bytes_duplicate_comparisons_and_wrong_model_binding(self):
        for tamper in ("bytes", "duplicate", "model"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                rows, manifest, backed = self._bank(path)
                name = "pairs-backed-train.jsonl"
                if tamper == "bytes":
                    with (path / name).open("a", encoding="utf-8") as stream:
                        stream.write("\n")
                else:
                    records = [backed["train"]]
                    if tamper == "duplicate":
                        records *= 2
                        manifest["backed_pair_rows"]["train"] = 2
                        manifest["combined_pair_rows"]["train"] = 3
                    else:
                        records[0]["a"]["support_receipt"]["model_sha256"] = "f" * 64
                    write_rows(path / name, records)
                    manifest["backed_pair_files"][name] = sha(path / name)
                with self.assertRaises(ValueError):
                    choice.load_pairs(path, rows, manifest)


if __name__ == "__main__":
    unittest.main()
