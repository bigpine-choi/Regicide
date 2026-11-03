"""Lifecycle and reproducibility checks without a real chess search."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chess

from scripts.neural_search_data.engine import SearchInterrupted
from scripts.neural_search_data.generate import Config, generate, load_seeds, DEFAULT_SEEDS
from scripts.neural_search_data.schema import load_dataset, sha256


class FakeTeacher:
    instances = []

    def __init__(self, engine, raw, nodes, deadline, search_timeout):
        self.raw, self.nodes = raw, nodes
        self.options, self.identity = {"fake_test_only": True}, {"name": "unit test"}
        self.closed = False
        self.cleanup_status = {"returncode": 0, "returncode_observed": True}
        self.instances.append(self)

    def __enter__(self):
        self.raw.touch(exist_ok=False)
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def query(self, board, query_id):
        moves = sorted(board.legal_moves, key=lambda move: (board.is_capture(move), move.uci()))
        move = moves[0]
        return {"query_id": query_id, "requested_nodes": self.nodes, "nodes": self.nodes,
                "depth": 2, "seldepth": 2, "pv_uci": [move.uci()], "bestmove_uci": move.uci(),
                "score": {"kind": "cp", "stm": (37 if board.turn else -37)},
                "completed": True, "lowerbound": False, "upperbound": False,
                "uci_begin_seq": 1, "uci_end_seq": 1, "wall_seconds": 0.01}


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = self.root / "fake-engine"
        self.engine.write_bytes(b"unit-test-only")
        self.config = Config(nodes=100, max_plies=6, sample_every=3, max_searches=48,
                             max_seconds=30, static_control=False)
        self.provenance = patch("scripts.neural_search_data.generate.source_provenance", return_value={})
        self.provenance.start()

    def tearDown(self):
        self.provenance.stop()
        self.tmp.cleanup()

    def run_fake(self, name="data", **kwargs):
        return generate(self.engine, self.root / name, teacher_factory=FakeTeacher,
                        config=kwargs.pop("config", self.config), **kwargs)

    def test_dataset_replay_and_reproducible_samples(self):
        one = self.run_fake("one")
        two = self.run_fake("two")
        self.assertEqual(one["status"], "complete", one)
        data = load_dataset(self.root / "one")
        for part in ("train", "validation"):
            self.assertTrue(data[part])
            self.assertEqual(one["partitions"][part]["sha256"], two["partitions"][part]["sha256"])
        self.assertEqual(one["semantic_sha256"], two["semantic_sha256"])
        self.assertTrue(all(game["result"] is None for game in self.rows("one/games.jsonl")))
        self.assertTrue(FakeTeacher.instances[-1].closed)

    def rows(self, path):
        return [json.loads(line) for line in (self.root / path).read_text(encoding="utf-8").splitlines()]

    def test_separate_honest_static_controls(self):
        def controls(engine, rows, deadline):
            return [11 if chess.Board(row["fen"]).turn else -11 for row in rows]
        result = self.run_fake(config=replace(self.config, static_control=True), control_labeler=controls)
        self.assertEqual(result["static_control_status"], "complete")
        data = load_dataset(self.root / "data")
        for row in data["train"] + data["validation"]:
            self.assertEqual(row["target_white_cp"], 37)
            self.assertEqual(row["classical_static_white_cp"], 11)
            self.assertNotIn("classical_stm_cp", row)

    def test_search_budget_preserves_partial_and_null_outcome(self):
        result = self.run_fake(config=replace(self.config, max_searches=1))
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["stop_reason"], "search_budget")
        self.assertEqual(result["stats"]["queries"], 1)
        self.assertTrue(all(game["result"] is None for game in self.rows("data/games.jsonl")))
        with self.assertRaises(ValueError):
            load_dataset(self.root / "data")

    def test_query_timeout_preserves_evidence_and_excludes_label(self):
        class Interrupted(FakeTeacher):
            def query(self, board, query_id):
                row = super().query(board, query_id)
                row["completed"] = False
                raise SearchInterrupted("search_timeout", row)
        result = generate(self.engine, self.root / "data", config=self.config, teacher_factory=Interrupted)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "search_timeout")
        self.assertEqual(result["stats"]["queries"], 1)
        self.assertFalse(self.rows("data/queries.jsonl")[0]["sampled"])
        self.assertEqual(self.rows("data/games.jsonl")[0]["generated_plies"], 0)
        self.assertTrue(Interrupted.instances[-1].closed)
        inspected = load_dataset(self.root / "data", allow_partial=True)
        self.assertEqual(inspected["train"], [])
        self.assertEqual(inspected["validation"], [])

    def test_late_wall_interrupt_keeps_previously_completed_labels(self):
        class Interrupted(FakeTeacher):
            def query(self, board, query_id):
                row = super().query(board, query_id)
                if query_id == "query-000045":
                    raise SearchInterrupted("wall_budget", row)
                return row
        result = generate(self.engine, self.root / "data", config=self.config, teacher_factory=Interrupted)
        self.assertEqual(result["status"], "budget_exhausted")
        data = load_dataset(self.root / "data", allow_partial=True)
        self.assertTrue(data["train"])
        self.assertTrue(data["validation"])
        self.assertFalse(self.rows("data/queries.jsonl")[-1]["sampled"])

    def test_depth_zero_empty_pv_can_play_but_cannot_label(self):
        class Fallback(FakeTeacher):
            def query(self, board, query_id):
                row = super().query(board, query_id)
                if query_id == "query-000000":
                    row.update(depth=0, pv_uci=[])
                return row
        result = generate(self.engine, self.root / "data", config=self.config, teacher_factory=Fallback)
        self.assertEqual(result["status"], "complete", result)
        self.assertFalse(self.rows("data/queries.jsonl")[0]["sampled"])
        self.assertEqual(self.rows("data/games.jsonl")[0]["generated_plies"], 6)
        load_dataset(self.root / "data")

    def test_depth_zero_malformed_supplied_pv_is_error(self):
        class BadFallback(FakeTeacher):
            def query(self, board, query_id):
                row = super().query(board, query_id)
                row.update(depth=0, pv_uci=["e1e8"])
                return row
        result = generate(self.engine, self.root / "data", config=self.config, teacher_factory=BadFallback)
        self.assertEqual(result["status"], "error")
        self.assertIn("Malformed engine", result["error"])

    def test_unreadable_executable_still_finalizes_error_manifest(self):
        engine_file = self.engine
        class Removed(FakeTeacher):
            def query(self, board, query_id):
                if engine_file.exists():
                    engine_file.unlink()
                return super().query(board, query_id)
        result = generate(self.engine, self.root / "data", config=self.config, teacher_factory=Removed)
        self.assertEqual(result["status"], "error")
        self.assertIn("no longer readable", result["error"])
        saved = json.loads((self.root / "data/manifest.json").read_text())
        self.assertEqual(saved["status"], "error")

    def test_completed_mate_gets_real_wdl_without_becoming_cp_target(self):
        sources = json.loads(DEFAULT_SEEDS.read_text())
        source = next(row for row in sources["sources"] if row["seed_id"] == "queen-a")
        source["initial_fen"] = "7k/8/5KQ1/8/8/8/8/8 w - - 0 1"
        seeds = self.root / "seeds.json"
        seeds.write_text(json.dumps(sources))
        class Mate(FakeTeacher):
            def query(self, board, query_id):
                row = super().query(board, query_id)
                if board.fen() == source["initial_fen"]:
                    row.update(bestmove_uci="g6g7", pv_uci=["g6g7"], score={"kind": "mate", "stm": 1})
                return row
        result = generate(self.engine, self.root / "data", config=self.config,
                          seeds_path=seeds, teacher_factory=Mate)
        self.assertEqual(result["status"], "complete", result)
        final = self.rows("data/games.jsonl")[-1]
        self.assertEqual((final["result"], final["outcome_termination"]), ("1-0", "checkmate"))
        self.assertFalse(self.rows("data/queries.jsonl")[-1]["sampled"])
        load_dataset(self.root / "data")

    def test_bad_bestmove_is_error_not_draw(self):
        class Illegal(FakeTeacher):
            def query(self, board, query_id):
                row = super().query(board, query_id)
                row["bestmove_uci"] = "e1e8"
                return row
        result = generate(self.engine, self.root / "data", config=self.config, teacher_factory=Illegal)
        self.assertEqual(result["status"], "error")
        self.assertIn("Malformed engine", result["error"])
        self.assertIsNone(self.rows("data/games.jsonl")[0]["result"])

    def test_existing_directory_never_overwritten(self):
        self.run_fake()
        before = sha256(self.root / "data/manifest.json")
        with self.assertRaises(FileExistsError):
            self.run_fake()
        self.assertEqual(before, sha256(self.root / "data/manifest.json"))

    def test_limits_and_input_validation_precede_output(self):
        for config in (replace(self.config, nodes=True), replace(self.config, max_seconds=float("nan")),
                       replace(self.config, search_timeout=0), replace(self.config, sample_every=0),
                       replace(self.config, max_seconds=3601), replace(self.config, validation_fraction=True)):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.run_fake(config=config)
        self.assertFalse((self.root / "data").exists())

    def test_seed_bank_is_new_own_legal_sources(self):
        seeds = load_seeds(DEFAULT_SEEDS)
        self.assertEqual(len(seeds), 8)
        self.assertEqual(sum(row["kind"] == "synthetic_endgame" for row in seeds), 4)


if __name__ == "__main__":
    unittest.main()
