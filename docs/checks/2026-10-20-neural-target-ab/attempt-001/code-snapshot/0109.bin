"""Adversarial semantic tests for the independent paired-target auditor."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

import chess

import audit_neural_target_ab as audit


def sample(*, black: bool = False, family: str = "family", partition: str = "train") -> tuple[dict, dict]:
    initial = chess.STARTING_FEN
    history = ["e2e4"] if black else []
    board = audit.board_from(initial, history)
    move = "g8f6" if black else "g1f3"
    query = {"query_id": "query-" + family, "fen": board.fen(en_passant="fen"),
             "initial_fen": initial, "history": history, "feature_hash": audit.feature_hash(board),
             "state_hash": audit.state_hash(initial, history), "family_id": family,
             "game_id": "game-" + family, "ply": 0, "partition": partition, "kind": "selfplay_start",
             "depth": 4, "nodes": 10000, "requested_nodes": 10000, "completed": True,
             "seldepth": 6,
             "sampled": True, "excluded_reasons": [], "score": {"kind": "cp", "stm": 71},
             "pv_uci": [move], "bestmove_uci": move}
    sign = -1 if black else 1
    row = {key: copy.deepcopy(value) for key, value in query.items()
           if key not in ("completed", "sampled", "excluded_reasons", "score")}
    row.update({"search_stm_cp": 71, "target_white_cp": 71 * sign,
                "classical_static_stm_cp": 13, "classical_static_white_cp": 13 * sign})
    return row, query


class PairedAuditTests(unittest.TestCase):
    def test_white_and_black_root_perspectives(self) -> None:
        for black in (False, True):
            row, query = sample(black=black)
            self.assertEqual(audit.audit_row(row, query, "train", {"family": "train"}), row["feature_hash"])

    def test_target_substitution_and_type_are_rejected(self) -> None:
        for key, value in (("target_white_cp", 13), ("search_stm_cp", 13),
                           ("classical_static_white_cp", -13), ("target_white_cp", True)):
            row, query = sample()
            row[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                audit.audit_row(row, query, "train", {"family": "train"})

    def test_pv_leaf_fen_cannot_receive_root_target(self) -> None:
        row, query = sample()
        board = chess.Board(row["fen"])
        board.push_uci(query["bestmove_uci"])
        row["fen"] = board.fen(en_passant="fen")
        row["feature_hash"] = audit.feature_hash(board)
        with self.assertRaises(ValueError):
            audit.audit_row(row, query, "train", {"family": "train"})

    def test_rehashed_wrong_split_is_rejected(self) -> None:
        row, query = sample(partition="validation")
        # All row and query metadata agrees: independent master-family split
        # still has to reject this edit rather than relying only on checksums.
        with self.assertRaisesRegex(ValueError, "partition"):
            audit.audit_row(row, query, "validation", {"family": "train"})

    def test_illegal_pv_bound_unfinished_and_budget_are_rejected(self) -> None:
        for edit in ({"pv_uci": ["e2e5"], "bestmove_uci": "e2e5"}, {"upperbound": True},
                     {"completed": False}, {"nodes": 10001}, {"depth": 0}):
            row, query = sample()
            query.update(edit)
            for key in edit:
                if key in row:
                    row[key] = copy.deepcopy(query[key])
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                audit.audit_row(row, query, "train", {"family": "train"})

    def test_rehashed_prior_input_and_duplicate_are_rejected(self) -> None:
        row, query = sample()
        with self.assertRaisesRegex(ValueError, "leaked"):
            audit.audit_partitions({"train": [row]}, {query["query_id"]: query}, {"family": "train"},
                                   {row["feature_hash"]}, minima=False)
        duplicate = copy.deepcopy(row)
        duplicate["query_id"] = "second-query"
        query2 = copy.deepcopy(query)
        query2["query_id"] = "second-query"
        with self.assertRaisesRegex(ValueError, "leaked"):
            audit.audit_partitions({"train": [row, duplicate]},
                                   {query["query_id"]: query, query2["query_id"]: query2},
                                   {"family": "train"}, set(), minima=False)
        self.assertEqual(audit.feature_hash(chess.Board(row["fen"]).mirror()), row["feature_hash"])

    def test_history_cannot_continue_after_draw_claim(self) -> None:
        with self.assertRaisesRegex(ValueError, "outcome"):
            audit.board_from("7k/8/8/8/8/8/8/6K1 w - - 0 1", ["g1f1"])

    def test_cp_metrics_and_family_paired_bootstrap(self) -> None:
        metrics = audit.error_metrics([50, -100, 0], [-50, 100, 0])
        self.assertEqual(metrics["mae_cp"], 100)
        self.assertAlmostEqual(metrics["rmse_cp"], (50000 / 3) ** 0.5)
        self.assertEqual(metrics["opposite_sign_ref_abs_ge_50_fraction"], 1)
        rows = [{"family_id": "normal", "kind": "selfplay_start"},
                {"family_id": "endgame", "kind": "synthetic_endgame"}]
        predictions = {f"{arm}-{seed}": [5, 5] if arm == "A" else [3, 3]
                       for arm in ("A", "B") for seed in audit.SEEDS}
        report = audit.family_comparison(rows, predictions, [0, 0])
        self.assertEqual(report["bootstrap"]["interval"], [2, 2])
        self.assertEqual(report["bootstrap"]["mean_delta_a_minus_b_cp"], 2)
        del predictions["B-20261020"]
        with self.assertRaisesRegex(ValueError, "missing paired"):
            audit.family_comparison(rows, predictions, [0, 0])

    def test_recomputed_metric_rejects_plausible_wrong_score(self) -> None:
        rows = [{"family_id": "f"}, {"family_id": "g"}]
        correct = {"count": 2, "mae_cp": 50., "rmse_cp": (5000.) ** .5,
                   "family_mae_cp": {"f": 0., "g": 100.}, "family_macro_mae_cp": 50.,
                   "p95_absolute_error_cp": 95., "absolute_error_ge100_fraction": .5,
                   "opposite_sign_margin50_fraction": 0.}
        audit.check_metric(correct, [0, 200], [0, 100], rows)
        correct["family_macro_mae_cp"] = 49.
        with self.assertRaisesRegex(ValueError, "macro"):
            audit.check_metric(correct, [0, 200], [0, 100], rows)

    def test_raw_uci_score_link_is_semantic(self) -> None:
        _, query = sample()
        query.update(uci_begin_seq=1, uci_end_seq=5)
        lines = [("send", "ucinewgame"), ("send", "position startpos"), ("send", "go nodes 10000"),
                 ("recv", "info depth 4 seldepth 6 score cp 71 time 5 nodes 10000 pv g1f3"),
                 ("recv", "bestmove g1f3")]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"uci.jsonl"
            path.write_text("".join(json.dumps({"seq": index, "direction": direction, "line": line})+"\n"
                                    for index, (direction, line) in enumerate(lines, 1)), encoding="utf-8")
            self.assertEqual(audit.audit_teacher_uci(path, [query])["queries"], 1)
            query["score"]["stm"] = 70
            with self.assertRaisesRegex(ValueError, "score"):
                audit.audit_teacher_uci(path, [query])


if __name__ == "__main__":
    unittest.main()
