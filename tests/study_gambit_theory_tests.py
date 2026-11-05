"""Two-budget label integrity and leakage guards for offline theory study."""
import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import chess
import study_gambit_theory as study


def case(history=(), *, name="case", family="evans", split="train", book=None):
    board = chess.Board()
    for uci in history:
        board.push_uci(uci)
    legal = sorted(move.uci() for move in board.legal_moves)
    return {"case_id": name, "family_id": family, "split": split,
            "owner": "white", "turn": "white" if board.turn else "black",
            "stage": "response", "history_uci": list(history),
            "fen": board.fen(en_passant="fen"), "position_key": study.position_key(board),
            "book_candidates": legal[:1] if book is None else book,
            "source_lines": ["test-fixture"], "source_urls": ["test-only"]}


class FakeAnalyzer:
    nodes = 100
    minimum_depth = 4
    identity = {"binary_sha256": "fixture-not-a-real-engine"}
    options = {}

    def __init__(self, mode="normal", baseline=None):
        self.mode, self.baseline, self.calls = mode, baseline, []

    def analyze(self, board, *, nodes=None, root_moves=None):
        self.calls.append((board.fen(), nodes))
        legal = sorted(move.uci() for move in board.legal_moves)
        cp = 700 if board.turn else -70
        result = {"status": "complete", "cp": cp, "mate": None,
                  "depth": 5 if nodes > self.nodes else 4, "nodes": nodes,
                  "pv_uci": legal[:1], "bestmove_uci": self.baseline or legal[0],
                  "query_id": "q" + str(len(self.calls))}
        # The first query discovers a legal baseline and is not itself a label.
        if len(self.calls) > 1:
            if self.mode == "unstable" and nodes > self.nodes:
                result["cp"] += 81
            elif self.mode == "mate":
                result.update(cp=None, mate=3)
            elif self.mode == "shallow":
                result["depth"] = 3
            elif self.mode == "unknown":
                result["status"] = "unknown"
            elif self.mode == "bound":
                result["lowerbound"] = True
            elif self.mode == "boolean":
                result["cp"] = True
            elif self.mode == "lower_depth" and nodes > self.nodes:
                result["depth"] = 4
            elif self.mode == "lower_depth":
                result["depth"] = 5
        return result


def training_fixture(*, count=4, reverse_holdout=False):
    groups = []
    n = len(study.learning.FEATURE_NAMES)
    for split in study.SPLITS:
        for index in range(count):
            flip = split == "holdout" and reverse_holdout
            rows = []
            for order in (0, 1):
                value = 120 if (order == 1 if flip else order == 0) else 0
                rows.append({"uci": "a2a3" if order == 0 else "b2b3",
                             "owner_cp": value, "mover_cp": value,
                             "features": [1.0 if order == 0 else -1.0] + [0.0] * (n - 1),
                             "original_order": order,
                             "native_baseline": order == (1 if flip else 0)})
            groups.append({"case_id": split + str(index), "family_id": split + "family",
                           "position_key": split + "key" + str(index),
                           "split": split, "candidates": rows})
    data = {"schema": study.SCHEMA, "seed": 43, "groups": groups,
            "engine_identity": {"binary_sha256": "fixture"}}
    data["semantic_sha256"] = study.digest(data)
    return data


class TheoryStudyTests(unittest.TestCase):
    def test_cp_is_root_mover_pov_for_both_colors_and_two_budgets(self):
        for history, expected in ((["e2e4", "e7e5"], 70), (["e2e4"], -700)):
            analyzer = FakeAnalyzer()
            dataset = study.study({"cases": [case(history)]}, analyzer, candidate_limit=2)
            self.assertEqual(len(dataset["groups"]), 1)
            self.assertEqual({row["owner_cp"] for row in dataset["groups"][0]["candidates"]}, {expected})
            self.assertEqual([nodes for _, nodes in analyzer.calls], [100, 100, 400, 100, 400])
            for row in dataset["groups"][0]["candidates"]:
                self.assertEqual(len(row["query_ids"]), 2)
                self.assertEqual(row["owner_cp"], row["mover_cp"])

    def test_unknown_mate_bound_shallow_boolean_and_unstable_are_not_labels(self):
        for mode in ("unknown", "mate", "bound", "shallow", "boolean", "unstable", "lower_depth"):
            with self.subTest(mode=mode):
                dataset = study.study({"cases": [case(["e2e4"])]}, FakeAnalyzer(mode), candidate_limit=2)
                self.assertEqual(dataset["groups"], [])
                self.assertGreaterEqual(len(dataset["excluded"]), 3)
                self.assertFalse(study.train_study(dataset)["model"]["active"])

    def test_native_baseline_kept_when_book_exceeds_cap(self):
        board = chess.Board()
        legal = sorted(move.uci() for move in board.legal_moves)
        analyzer = FakeAnalyzer(baseline=legal[-1])
        dataset = study.study({"cases": [case(book=legal[:8])]}, analyzer, candidate_limit=2)
        rows = dataset["groups"][0]["candidates"]
        self.assertEqual([row["uci"] for row in rows], [legal[0], legal[-1]])
        self.assertEqual(sum(row["native_baseline"] for row in rows), 1)

    def test_curriculum_tampering_fails_before_search(self):
        row = case(["e2e4"])
        row["turn"] = "white"
        analyzer = FakeAnalyzer()
        with self.assertRaisesRegex(ValueError, "mismatch"):
            study.study({"cases": [row]}, analyzer)
        self.assertEqual(analyzer.calls, [])

    def test_legal_ep_catalog_fen_keeps_replayed_full_history(self):
        history = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "b2b4"]
        row = case(history)
        board = study.replay(history)
        row["fen"] = board.fen(en_passant="legal")
        validated = study._cases({"cases": [row]})
        self.assertEqual(validated[0][1].ep_square, chess.B3)
        self.assertEqual([move.uci() for move in validated[0][1].move_stack], history)
        row["fen"] = row["fen"].replace(" KQkq - ", " KQkq c3 ")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            study._cases({"cases": [row]})

    def test_transposed_positions_across_splits_are_rejected(self):
        rows = [case(["g1f3", "g8f6", "b1c3", "b8c6"], name="a", split="train"),
                case(["b1c3", "b8c6", "g1f3", "g8f6"], name="b", family="other", split="holdout")]
        analyzer = FakeAnalyzer()
        with self.assertRaisesRegex(ValueError, "transposition"):
            study.study({"cases": rows}, analyzer)
        self.assertEqual(analyzer.calls, [])

    def test_root_child_cross_split_overlap_is_rejected(self):
        rows = [case(["e2e4"], name="a", split="train"),
                case(["e2e4", "e7e5"], name="b", family="other", split="validation")]
        with self.assertRaisesRegex(ValueError, "transposition"):
            study.study({"cases": rows}, FakeAnalyzer())

    def test_insufficient_coverage_does_not_train_or_activate(self):
        with mock.patch.object(study, "fit", wraps=study.fit) as fitting:
            result = study.train_study(training_fixture(count=3))
        fitting.assert_not_called()
        self.assertFalse(result["model"]["trained"])
        self.assertFalse(result["report"]["candidate_acceptance"])
        self.assertEqual(result["model"]["status"], "experimental")

    def test_fitting_is_deterministic_and_only_reads_training_groups(self):
        fixture = training_fixture()
        with mock.patch.object(study, "fit", wraps=study.fit) as fitting:
            result = study.train_study(fixture)
        self.assertTrue(result["model"]["trained"])
        self.assertEqual(fitting.call_count, 4)
        for call in fitting.call_args_list:
            self.assertTrue(all(group["split"] == "train" for group in call.args[0]))
        again = study.train_study(fixture)
        self.assertEqual(result, again)
        self.assertFalse(result["model"]["active"])
        self.assertFalse(result["report"]["candidate_acceptance"])

    def test_holdout_does_not_change_weights_and_can_reject_learned_policy(self):
        result = study.train_study(training_fixture())
        reversed_result = study.train_study(training_fixture(reverse_holdout=True))
        for field in ("weights", "means", "scales", "training"):
            self.assertEqual(result["model"][field], reversed_result["model"][field])
        self.assertFalse(reversed_result["report"]["gates"]["holdout_regret_vs_native_baseline"])
        metric = reversed_result["report"]["holdout"]["methods"]
        self.assertGreater(metric["learned"]["mean_regret_cp"], metric["native_baseline"]["mean_regret_cp"])

    def test_dataset_checksum_and_family_leakage_are_rejected(self):
        fixture = training_fixture()
        fixture["groups"][0]["candidates"][0]["owner_cp"] += 1
        with self.assertRaisesRegex(ValueError, "checksum"):
            study.train_study(fixture)
        fixture = training_fixture()
        fixture["groups"][4]["family_id"] = fixture["groups"][0]["family_id"]
        fixture["semantic_sha256"] = study.digest({key: value for key, value in fixture.items() if key != "semantic_sha256"})
        with self.assertRaisesRegex(ValueError, "Family"):
            study.train_study(fixture)

    def test_invalid_budgets_do_not_start_search(self):
        for kwargs in ({"candidate_limit": 1}, {"deep_multiplier": 1}, {"stability_cp": -1}, {"seed": True}):
            analyzer = FakeAnalyzer()
            with self.assertRaises(ValueError):
                study.study({"cases": [case()]}, analyzer, **kwargs)
            self.assertEqual(analyzer.calls, [])


if __name__ == "__main__":
    unittest.main()
