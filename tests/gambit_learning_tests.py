"""Ordering-model boundary and deterministic training behavior, without engines."""
import copy
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gambit_learning as policy
import train_gambit_policy as training


IDENTITY = {"binary_sha256": "a" * 64, "evaluation_id": "classical_activity_v1"}


def board_after(moves):
    board = chess.Board()
    for uci in moves.split():
        board.push_uci(uci)
    return board


def model(**changes):
    value = {"schema": policy.SCHEMA, "policy": policy.POLICY,
             "feature_schema": policy.FEATURE_SCHEMA, "feature_names": list(policy.FEATURE_NAMES),
             "feature_sources": policy.feature_sources(), "engine_identity": IDENTITY,
             "weights": [1.] + [0.] * (len(policy.FEATURE_NAMES) - 1),
             "means": [0.] * len(policy.FEATURE_NAMES), "scales": [1.] * len(policy.FEATURE_NAMES),
             "status": "promoted", "acceptance": {"passed": True}, **changes}
    value["model_id"] = policy.model_digest(value)
    return value


class RankingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="regicide-gambit-rank-test-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "policy.json"
        self.board = board_after("e2e4 d7d5")
        self.rows = [{"uci": "b1c3", "source": "existing"}, {"uci": "e4d5", "source": "existing"}]

    def save(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")
        policy._read.cache_clear()

    def rank(self, **kwargs):
        return policy.rank_with_metadata(self.board, self.rows, model_path=self.path,
                                         engine_identity=IDENTITY, **kwargs)

    def test_only_order_changes_and_receipt_is_not_an_evaluation(self):
        self.save(model())
        before, original = self.board.copy(stack=True), copy.deepcopy(self.rows)
        rows, receipt = self.rank()
        self.assertEqual([row["uci"] for row in rows], ["e4d5", "b1c3"])
        self.assertTrue(receipt["active"])
        self.assertTrue(receipt["applied"])
        self.assertEqual(sorted(rows, key=lambda row: row["uci"]), sorted(original, key=lambda row: row["uci"]))
        self.assertEqual(self.rows, original)
        self.assertEqual(self.board.fen(), before.fen())
        self.assertEqual(self.board.move_stack, before.move_stack)
        self.assertNotIn("cp", receipt)
        self.assertIn("not centipawns", receipt["units"])
        rows[0]["source"] = "changed"
        self.assertEqual(self.rows, original)

    def test_missing_or_experimental_models_keep_the_supplied_order(self):
        for value in (None, model(status="experimental"), model(acceptance={"passed": False})):
            with self.subTest(value=value and value["status"]):
                if value is not None:
                    self.save(value)
                rows, receipt = self.rank()
                self.assertEqual(rows, self.rows)
                self.assertFalse(receipt["active"])

    def test_runtime_identity_is_explicit_and_exact(self):
        self.save(model())
        for identity in (None, {**IDENTITY, "binary_sha256": "b" * 64},
                         {**IDENTITY, "evaluation_id": "neural_foo"}, {**IDENTITY, "extra": 1}):
            rows, receipt = policy.rank_with_metadata(self.board, self.rows, model_path=self.path,
                                                      engine_identity=identity)
            self.assertEqual(rows, self.rows)
            self.assertEqual(receipt["reason"], "runtime_identity_mismatch")
            self.assertFalse(receipt["active"])

    def test_bad_model_checksum_features_and_nonfinite_weights_fail_closed(self):
        variants = [model(schema="foreign"), model(feature_schema="foreign"), model(feature_sources={}),
                    model(weights=[True] * len(policy.FEATURE_NAMES)), model(scales=[0.] * len(policy.FEATURE_NAMES)),
                    model(weights=[0.]), {**model(), "model_id": "0" * 64},
                    {**model(), "weights": [math.inf] * len(policy.FEATURE_NAMES)}]
        for value in variants:
            self.save(value)
            rows, receipt = self.rank()
            self.assertEqual(rows, self.rows)
            self.assertFalse(receipt["active"])

    def test_illegal_duplicate_and_foreign_proposals_never_reach_features(self):
        self.save(model())
        for rows in ([{"uci": "e2e5"}], [{"uci": "e4d5"}, {"uci": "e4d5"}],
                     [{"uci": "no move"}], [{"uci": True}], [{"other": "e4d5"}], [],
                     [{"uci": "b1c3"}] * (policy.MAX_CANDIDATES + 1)):
            result, receipt = policy.rank_with_metadata(self.board, rows, model_path=self.path,
                                                        engine_identity=IDENTITY)
            self.assertEqual(result, rows)
            self.assertFalse(receipt["active"])

    def test_ties_preserve_caller_priority_and_identity_changes_with_weights(self):
        base = model(weights=[0.] * len(policy.FEATURE_NAMES))
        self.save(base)
        rows, receipt = self.rank()
        self.assertEqual(rows, self.rows)
        self.assertTrue(receipt["active"])
        self.assertFalse(receipt["applied"])
        self.assertEqual(policy.model_identity(self.path)["model_id"], base["model_id"])
        changed = model()
        self.assertNotEqual(changed["model_id"], base["model_id"])
        self.save(changed)
        self.assertEqual(policy.model_identity(self.path)["model_id"], changed["model_id"])

    def test_file_size_limit_and_malformed_json_are_inactive(self):
        for payload in ("{", " " * (policy.MAX_MODEL_BYTES + 1)):
            self.path.write_text(payload, encoding="utf-8")
            policy._read.cache_clear()
            rows, receipt = self.rank()
            self.assertEqual(rows, self.rows)
            self.assertFalse(receipt["active"])


class FeatureTests(unittest.TestCase):
    def test_legal_history_is_immutable_and_features_are_bounded(self):
        board = board_after("e2e4 e7e5 g1f3 b8c6 f1c4 g8f6")
        fen, history = board.fen(), list(board.move_stack)
        for move in list(board.legal_moves):
            values = policy.features(board, move)
            self.assertEqual(len(values), len(policy.FEATURE_NAMES))
            self.assertTrue(all(math.isfinite(v) and abs(v) <= 32 for v in values))
        self.assertEqual((board.fen(), board.move_stack), (fen, history))

    def test_captures_en_passant_promotion_and_checks_are_not_filtered(self):
        ep = board_after("e2e4 a7a6 e4e5 d7d5")
        self.assertEqual(policy.features(ep, "e5d6")[0], 1.)
        promotion = chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1")
        values = policy.features(promotion, "a7a8q")
        self.assertEqual(values[4], 8.)
        self.assertEqual(values[2], 1.)
        capture = board_after("e2e4 d7d5")
        self.assertEqual(policy.features(capture, "e4d5")[0], 1.)

    def test_immediate_opponent_mate_is_a_real_legal_reply_feature(self):
        board = board_after("e2e4 e7e5 f1c4 b8c6 d1h5")
        danger = dict(zip(policy.FEATURE_NAMES, policy.features(board, "g8f6")))
        safe = dict(zip(policy.FEATURE_NAMES, policy.features(board, "g7g6")))
        self.assertEqual(danger["reply_mate"], 1.)
        self.assertEqual(safe["reply_mate"], 0.)
        child = board.copy(stack=True)
        child.push_uci("g8f6")
        child.push_uci("h5f7")
        self.assertTrue(child.is_checkmate())

    def test_color_mirror_keeps_owner_based_feature_meaning(self):
        board = board_after("e2e4 d7d5")
        move = chess.Move.from_uci("e4d5")
        mirrored = board.mirror()
        other = chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square))
        self.assertEqual(policy.features(board, move), policy.features(mirrored, other))

    def test_transposition_identity_keeps_side_castling_and_legal_ep(self):
        before = board_after("g1f3 g8f6 b1c3 b8c6")
        transposed = board_after("b1c3 b8c6 g1f3 g8f6")
        self.assertEqual(policy.position_key(before), policy.position_key(transposed))
        changed = before.copy(stack=False)
        changed.turn = not changed.turn
        self.assertNotEqual(policy.position_key(before), policy.position_key(changed))
        changed = before.copy(stack=False)
        changed.castling_rights = chess.BB_EMPTY
        self.assertNotEqual(policy.position_key(before), policy.position_key(changed))


class TrainingTests(unittest.TestCase):
    def test_fixed_seed_fit_uses_native_target_order_and_is_reproducible(self):
        groups = []
        for index in range(5):
            groups.append({"candidates": [{"owner_cp": cp, "features": [float(cp / 100)] + [0.] * (len(policy.FEATURE_NAMES) - 1)}
                                           for cp in (-150, 0, 125)]})
        first = training.fit(groups, seed=17, regularization=.01, epochs=10)
        second = training.fit(groups, seed=17, regularization=.01, epochs=10)
        self.assertEqual(first, second)
        self.assertGreater(first["weights"][0], 0.)
        self.assertEqual(first["training_pairs"], 15)

    def test_predeclared_whole_family_splits_and_transpositions_do_not_overlap(self):
        roots, audit = training.choose_roots(seed=17, roots_per_family=4)
        again, second = training.choose_roots(seed=17, roots_per_family=4)
        self.assertEqual(roots, again)
        self.assertEqual(audit, second)
        splits = {name: {row["position_key"] for row in roots if row["split"] == name}
                  for name in ("train", "validation", "holdout")}
        self.assertTrue(all(splits.values()))
        self.assertFalse(splits["train"] & splits["holdout"])
        self.assertFalse(splits["train"] & splits["validation"])
        self.assertEqual({row["family_id"] for row in roots if row["split"] == "holdout"}, {"evans", "stafford"})
        self.assertFalse({row["family_id"] for row in roots if row["split"] == "train"} & {"evans", "stafford"})

    def test_first_owner_reply_and_real_graph_choice_points_are_in_the_plan(self):
        roots, audit = training.choose_roots()
        evans = "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4 c5b6".split()
        entries = [row for row in roots if row["history_uci"] == evans]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["split"], "holdout")
        self.assertGreaterEqual(len(entries[0]["graph_candidates"]), 2)
        self.assertGreaterEqual(audit["multi_graph_counts"]["validation"], 3)
        self.assertGreaterEqual(audit["multi_graph_counts"]["holdout"], 3)


if __name__ == "__main__":
    unittest.main()
