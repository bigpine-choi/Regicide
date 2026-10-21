"""Target-adapter, frozen-input and clustered-report regression checks."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import neural_target_ab as ab


def row(family="f1", kind="selfplay_start", static=10, search=40):
    return {"fen": chess.STARTING_FEN, "family_id": family, "kind": kind,
            "classical_static_white_cp": static, "target_white_cp": search}


class PairedTrainingTests(unittest.TestCase):
    def test_only_targets_change_and_source_rows_remain_intact(self):
        records = [row(), row(static=-20, search=100)]
        old = json.dumps(records, sort_keys=True)
        xa, ya = ab.pack(records, ab.TARGETS["A"])
        xb, yb = ab.pack(records, ab.TARGETS["B"])
        np.testing.assert_array_equal(xa, xb)
        np.testing.assert_array_equal(ya, [10, -20])
        np.testing.assert_array_equal(yb, [40, 100])
        self.assertEqual(json.dumps(records, sort_keys=True), old)

    def test_black_target_is_not_accidentally_negated_twice(self):
        record = row(static=-70, search=110)
        board = chess.Board()
        board.push_uci("e2e4")
        record["fen"] = board.fen()
        _, value = ab.pack([record], ab.TARGETS["B"])
        self.assertEqual(value.tolist(), [110])

    def test_bad_target_types_or_silent_fallback_rejected(self):
        for bad in (True, None, 1.5, "10", 28001):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ab.pack([row(search=bad)], ab.TARGETS["B"])
        with self.assertRaises(ValueError):
            ab.pack([row()], "classical_stm_cp")

    def test_batch_order_is_paired_but_seeds_are_distinct(self):
        orders = [ab.order_hash(257, seed) for seed in ab.SEEDS]
        self.assertEqual(len(set(orders)), 3)
        self.assertEqual(orders[0], ab.order_hash(257, ab.SEEDS[0]))
        self.assertNotEqual(orders[0], ab.order_hash(257, ab.SEEDS[0], 119))

    def test_config_keeps_target_independent_hyperparameters(self):
        a, b = ab.config("A", ab.SEEDS[0]), ab.config("B", ab.SEEDS[0])
        a.pop("candidate_id")
        b.pop("candidate_id")
        self.assertEqual(a, b)
        self.assertEqual((a["epochs"], a["input_scale"], a["batch_size"]), (120, 32, 128))
        with self.assertRaises(ValueError):
            ab.config("A", 123)

    def test_family_macro_is_not_position_weighted(self):
        rows = [row("a"), row("a"), row("a"), row("b")]
        metrics = ab.metric([0, 0, 0, 100], [0]*4, rows)
        self.assertEqual(metrics["mae_cp"], 25)
        self.assertEqual(metrics["family_macro_mae_cp"], 50)

    def test_uncertainty_reuses_family_and_all_three_seeds(self):
        records = [row("one"), row("two", "synthetic_endgame")]
        matrix = {}
        for seed in ab.SEEDS:
            for arm, error in (("A", 20), ("B", 10)):
                matrix[f"{arm}-{seed}"] = {"search100k": {"family_mae_cp": {"one": error, "two": error}}}
        result = ab.bootstrap_delta(records, matrix)
        self.assertEqual(result["mean_delta_cp"], 10)
        self.assertEqual(result["bootstrap_percentile95_cp"], [10, 10])

    def test_frozen_file_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "weights"
            path.write_text("old", encoding="utf-8")
            files = {"weights": ab.entry(path)}
            ab.verify_files(files)
            path.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Frozen file changed"):
                ab.verify_files(files)

    def test_exclusive_output_rejects_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            ab.write_new(path, {"original": True})
            with self.assertRaises(FileExistsError):
                ab.write_new(path, {"original": False})
            self.assertTrue(ab.read(path)["original"])

    def test_empty_and_unrepresented_sources_fail_quality(self):
        self.assertFalse(ab.quality({"train": []}, ("train",))["train"]["pass"])
        self.assertFalse(ab.quality({"test": [row() for _ in range(500)]}, ("test",))["test"]["pass"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
