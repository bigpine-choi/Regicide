"""Focused research preparation only: no engine or external service is used."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_gambit_research as focus
import gambit_practice_memory as memory
import train_gambit_operations as training


class FocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(focus.CURRICULUM.read_text(encoding="utf-8"))
        cls.kings = min((row for row in cls.base["cases"] if row["family_id"] == "kings"),
                        key=lambda row: len(row["moves"]))
        cls.from_case = min((row for row in cls.base["cases"] if row["family_id"] == "from"),
                            key=lambda row: len(row["moves"]))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="regicide-focus-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def save(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def research(self, name, *, history=None, scores=(100, 115), depths=(4, 4)):
        directory = self.folder / name
        directory.mkdir()
        history = self.kings["moves"] if history is None else history
        board = memory._board(history)
        candidates, queries = [], []
        for move, score, depth in zip(sorted(board.legal_moves, key=lambda m: m.uci()), scores, depths):
            child = board.copy(stack=True)
            child.push(move)
            reply = min(child.legal_moves, key=lambda m: m.uci())
            query_id = "source:1:" + move.uci()
            candidates.append({"uci": move.uci(), "offline_owner_cp": score, "native_depth": depth,
                "requested_nodes": 100000, "native_nodes": 100000, "query_id": query_id,
                "pv_uci": [move.uci(), reply.uci()]})
            queries.append({"query_id": query_id, "role": "candidate_and_opponent",
                "case_id": self.kings["case_id"], "candidate_uci": move.uci(),
                "fen": child.fen(en_passant="fen"), "history_uci": [m.uci() for m in child.move_stack],
                "requested_nodes": 100000, "nodes": 100000, "depth": depth,
                "score": {"kind": "cp", "stm": -score}, "completed": True,
                "lowerbound": False, "upperbound": False,
                "bestmove_uci": reply.uci(), "pv_uci": [reply.uci()]})
        record = {"record_id": "source:1", "case_id": self.kings["case_id"], "family_id": "kings",
                  "owner": "white", "split": "train", "history_uci": list(history), "fen": board.fen(en_passant="fen"),
                  "position_key": memory.position_key(board), "candidates": candidates}
        after = board.copy(stack=True)
        for word in candidates[0]["pv_uci"]:
            after.push_uci(word)
        practice = [{"case_id": record["case_id"], "family_id": "kings", "owner": "white",
            "initial_fen": record["fen"], "initial_history_uci": list(history),
            "turns": [{"record_id": "source:1", "before_fen": record["fen"],
                "owner_move": candidates[0]["uci"], "opponent_move": candidates[0]["pv_uci"][1],
                "after_fen": after.fen(en_passant="fen")}],
            "final_fen": after.fen(en_passant="fen"), "final_history_uci": [m.uci() for m in after.move_stack]}]
        run = {"schema": "regicide.gambit-operations-practice.v2", "research_status": "complete",
               "failure": None, "all_native_processes_closed": True, "source_hashes_match": True,
               "binary_hash_matches": True, "cleanup": {"returncode_observed": True, "returncode": 0},
               "engine_identity": {"binary_sha256": "a" * 64, "evaluation_id": "classical_activity_v1"},
               "nodes": 100000, "heldout_family_ids": ["benko", "from"],
               "configured_options": {"UsePieceActivity": True}}
        for filename, value in (("run.json", run), ("records.json", [record]),
                                ("practice.json", practice), ("queries.json", queries)):
            self.save(directory / filename, value)
        return directory

    def pgn(self, name, history):
        game = chess.pgn.Game()
        node = game
        board = chess.Board()
        for word in history:
            move = chess.Move.from_uci(word)
            self.assertIn(move, board.legal_moves)
            node = node.add_variation(move)
            board.push(move)
        path = self.folder / name
        path.write_text(str(game) + "\n", encoding="utf-8")
        return path

    def output(self, name):
        return self.folder / ("out-" + name)

    def read_output(self, path):
        return json.loads((path / "curriculum.json").read_text(encoding="utf-8"))

    def test_explicit_record_preserves_inputs_full_history_and_every_holdout_case(self):
        source, output = self.research("source"), self.output("explicit")
        before = {p.name: p.read_bytes() for p in source.iterdir()}
        report = focus.prepare(output, research=source, record_ids=["source:1"])
        self.assertTrue(report["unchanged_inputs"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in source.iterdir()})
        document = self.read_output(output)
        self.assertEqual(document["cases"][0]["moves"], self.kings["moves"])
        self.assertEqual([r for r in document["cases"] if r["split"] == "holdout"],
                         [r for r in self.base["cases"] if r["split"] == "holdout"])
        self.assertFalse(document["claims"]["selected_scores_are_correct_move_labels"])
        self.assertFalse(document["focused_research"]["automatic_research_performed"])
        self.assertEqual(training.board_for(document["cases"][0]).fen(en_passant="fen"), document["cases"][0]["fen"])

    def test_gap_rule_requires_close_scores_at_the_same_completed_depth(self):
        source = self.research("close")
        report = focus.prepare(self.output("close"), research=source, gap_cp=15)
        self.assertEqual(len(report["focus_case_ids"]), 1)
        other = self.research("mixed-depth", depths=(4, 5))
        report = focus.prepare(self.output("mixed"), research=other, gap_cp=1000)
        self.assertEqual(report["focus_case_ids"], [])
        self.assertEqual(report["examined"][0]["candidate_gap"]["reason"], "different_completed_depths")

    def test_swing_uses_common_candidate_and_requires_identical_native_identity(self):
        source, other = self.research("a"), self.research("b", scores=(250, 115))
        report = focus.prepare(self.output("swing"), research=source, comparison=other, swing_cp=150)
        self.assertEqual(len(report["focus_case_ids"]), 1)
        swings = report["examined"][0]["common_candidate_swings"]
        self.assertEqual(max(row["absolute_swing_cp"] for row in swings), 150)
        run = json.loads((other / "run.json").read_text())
        run["engine_identity"]["evaluation_id"] = "other-evaluation"
        self.save(other / "run.json", run)
        with self.assertRaisesRegex(ValueError, "same engine"):
            focus.prepare(self.output("bad-identity"), research=source, comparison=other, swing_cp=1)
        self.assertFalse(self.output("bad-identity").exists())

    def test_same_fen_different_full_history_is_not_a_comparison_match(self):
        a = "e2e4 e7e5 f2f4 e5f4 b1c3 b8c6 g1f3 g8f6".split()
        b = "e2e4 e7e5 f2f4 e5f4 g1f3 g8f6 b1c3 b8c6".split()
        self.assertEqual(memory._board(a).fen(), memory._board(b).fen())
        source, other = self.research("history-a", history=a), self.research("history-b", history=b, scores=(500, 600))
        report = focus.prepare(self.output("history"), research=source, comparison=other, swing_cp=1)
        self.assertEqual(report["focus_case_ids"], [])
        self.assertEqual(report["examined"][0]["common_candidate_swings"], [])

    def test_pgn_explicit_ply_retains_full_history_without_error_claim(self):
        path = self.pgn("selected.pgn", self.kings["moves"])
        before = path.read_bytes()
        output = self.output("pgn")
        report = focus.prepare(output, pgn=path, plies=[len(self.kings["moves"])])
        self.assertEqual(path.read_bytes(), before)
        row = self.read_output(output)["cases"][0]
        self.assertEqual(row["moves"], self.kings["moves"])
        self.assertEqual(row["focus_provenance"]["reason"], "user_selection_not_automatic_error_diagnosis")
        self.assertEqual(len(report["focus_case_ids"]), 1)

    def test_known_heldout_pgn_family_is_excluded_and_original_holdouts_remain(self):
        path = self.pgn("heldout.pgn", self.from_case["moves"])
        output = self.output("holdout")
        report = focus.prepare(output, pgn=path, plies=[len(self.from_case["moves"])])
        self.assertFalse(report["focus_case_ids"])
        self.assertEqual(report["excluded"][0]["reason"], "heldout_family")
        self.assertTrue(all(row["split"] == "holdout" for row in self.read_output(output)["cases"]))

    def test_wrong_practice_history_and_paused_runs_are_rejected(self):
        source = self.research("bad-practice")
        practice = json.loads((source / "practice.json").read_text())
        practice[0]["turns"][0]["after_fen"] = chess.STARTING_FEN
        self.save(source / "practice.json", practice)
        with self.assertRaises(ValueError):
            focus.prepare(self.output("bad-practice"), research=source, record_ids=["source:1"])
        source = self.research("paused")
        run = json.loads((source / "run.json").read_text())
        run["research_status"] = "paused"
        self.save(source / "run.json", run)
        with self.assertRaises(ValueError):
            focus.prepare(self.output("paused"), research=source, record_ids=["source:1"])

    def test_nonfinite_input_and_comparison_option_changes_are_rejected(self):
        source, other = self.research("option-a"), self.research("option-b")
        run = json.loads((other / "run.json").read_text())
        run["configured_options"]["UsePieceActivity"] = False
        self.save(other / "run.json", run)
        with self.assertRaisesRegex(ValueError, "native options"):
            focus.prepare(self.output("options"), research=source, comparison=other, swing_cp=1)
        (other / "run.json").write_text('{"overflow":1e999}', encoding="utf-8")
        with self.assertRaises(ValueError):
            focus.read_json(other / "run.json")

    def test_prior_or_record_holdout_cannot_be_relabelled_as_training(self):
        for declaration in ("run", "record"):
            source = self.research("holdout-" + declaration)
            if declaration == "run":
                run = json.loads((source / "run.json").read_text())
                run["heldout_family_ids"].append("kings")
                self.save(source / "run.json", run)
            else:
                records = json.loads((source / "records.json").read_text())
                records[0]["split"] = "holdout"
                self.save(source / "records.json", records)
            output = self.output("holdout-" + declaration)
            report = focus.prepare(output, research=source, record_ids=["source:1"])
            self.assertFalse(report["focus_case_ids"])
            self.assertEqual(report["excluded"][0]["reason"], "heldout_family")
            document = self.read_output(output)
            self.assertIn("kings", document["split_policy"]["holdout_families"])
            self.assertEqual(document["families"]["kings"]["split"], "holdout")
            self.assertTrue(all(row["split"] == "holdout" for row in document["cases"]))

    def test_comparison_holdout_family_and_reserved_position_also_propagate(self):
        source, other = self.research("reservation-a"), self.research("reservation-b", scores=(300, 400))
        run = json.loads((other / "run.json").read_text())
        board = memory._board(self.kings["moves"])
        run["reserved_holdout_position_keys"] = [memory.position_key(board)]
        self.save(other / "run.json", run)
        report = focus.prepare(self.output("reserved-key"), research=source, comparison=other, swing_cp=1)
        self.assertFalse(report["focus_case_ids"])
        self.assertEqual(report["excluded"][0]["reason"], "heldout_root_or_immediate_child_overlap")
        self.assertIn(memory.position_key(board), self.read_output(self.output("reserved-key"))["reserved_holdout_position_keys"])
        run["heldout_family_ids"].append("kings")
        self.save(other / "run.json", run)
        report = focus.prepare(self.output("reserved-family"), research=source, comparison=other, swing_cp=1)
        self.assertEqual(report["excluded"][0]["reason"], "heldout_family")

    def test_candidate_cp_only_tampering_and_restricted_query_labels_are_rejected(self):
        source = self.research("tampered-cp")
        records = json.loads((source / "records.json").read_text())
        records[0]["candidates"][0]["offline_owner_cp"] += 100
        self.save(source / "records.json", records)
        with self.assertRaisesRegex(ValueError, "CP sign"):
            focus.prepare(self.output("tampered-cp"), research=source, gap_cp=1000)
        source = self.research("restricted-label")
        queries = json.loads((source / "queries.json").read_text())
        queries[0]["root_moves_uci"] = [queries[0]["bestmove_uci"]]
        self.save(source / "queries.json", queries)
        with self.assertRaisesRegex(ValueError, "unrestricted"):
            focus.prepare(self.output("restricted-label"), research=source, gap_cp=1000)

    def test_unknown_ids_missing_rules_and_existing_or_active_outputs_are_rejected(self):
        source = self.research("protected")
        active_before = memory.DEFAULT_MEMORY.read_bytes()
        for arguments in ({}, {"record_ids": ["unknown"]}):
            with self.assertRaises(ValueError):
                focus.prepare(self.output("bad-selection"), research=source, **arguments)
        output = self.output("existing")
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_bytes(b"keep")
        with self.assertRaises(ValueError):
            focus.prepare(output, research=source, record_ids=["source:1"])
        with self.assertRaises(ValueError):
            focus.prepare(memory.DEFAULT_MEMORY.parent / "focus-do-not-create", research=source, record_ids=["source:1"])
        with self.assertRaises(ValueError):
            focus.prepare(ROOT.parent / "sources/focus-do-not-create", research=source, record_ids=["source:1"])
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual(memory.DEFAULT_MEMORY.read_bytes(), active_before)

    def test_invalid_ply_or_nonstandard_pgn_root_cannot_create_a_revision(self):
        path = self.pgn("short.pgn", self.kings["moves"])
        with self.assertRaises(ValueError):
            focus.prepare(self.output("past-end"), pgn=path, plies=[100])
        game = chess.pgn.Game()
        game.setup(chess.Board("8/8/8/8/8/2k5/8/R5K1 w - - 0 1"))
        path.write_text(str(game), encoding="utf-8")
        with self.assertRaises(ValueError):
            focus.prepare(self.output("custom-root"), pgn=path, plies=[0])


if __name__ == "__main__":
    unittest.main()
