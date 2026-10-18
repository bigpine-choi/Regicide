"""Order balance and fail-closed presentation of the cost harness."""

import json
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_neural_inference as benchmark


class CostHarnessTests(unittest.TestCase):
    def test_each_position_has_balanced_adjacent_order(self):
        rows = list(benchmark.schedule(list(range(12)), 6, 20261019))
        self.assertEqual(len(rows), 72)
        for index in range(12):
            orders = [order for _, selected, order in rows if selected == index]
            self.assertEqual(orders.count(benchmark.ROLES), 3)
            self.assertEqual(orders.count(benchmark.ROLES[::-1]), 3)

    def test_seed_reproduces_complete_schedule(self):
        self.assertEqual(list(benchmark.schedule([0, 1, 2], 6, 11)),
                         list(benchmark.schedule([0, 1, 2], 6, 11)))
        self.assertNotEqual(list(benchmark.schedule(list(range(12)), 6, 11)),
                            list(benchmark.schedule(list(range(12)), 6, 12)))

    def record(self, mode="nodes", differences=None):
        return {"arm": "learned", "mode": mode, "differences": differences or [],
                "baseline": {"wall_time_seconds": 2, "nodes": 10000},
                "candidate": {"wall_time_seconds": 1, "nodes": 10000}}

    def test_only_complete_pairs_contribute(self):
        incomplete = self.record()
        del incomplete["candidate"]
        result = benchmark.summary([self.record(), incomplete], valid=True)
        self.assertEqual(result["arms"]["learned"]["nodes"]["pairs"], 1)
        self.assertEqual(result["arms"]["learned"]["nodes"]["candidate_baseline_wall_median"], .5)

    def test_dirty_or_incomplete_run_cannot_publish_timing_median(self):
        result = benchmark.summary([self.record()], valid=False)
        self.assertFalse(result["timing_valid"])
        self.assertIsNone(result["arms"]["learned"]["nodes"]["candidate_baseline_wall_median"])
        self.assertIsNone(result["arms"]["learned"]["nodes"]["candidate_baseline_nodes_median"])

    def test_fixed_work_differences_visible(self):
        result = benchmark.summary([self.record(differences=["pv"])], valid=False)
        self.assertEqual(result["arms"]["learned"]["nodes"]["unequal_fixed_work_pairs"], 1)

    def test_time_budget_does_not_claim_same_search(self):
        row = self.record(mode="time")
        row["candidate"]["nodes"] = 12000
        result = benchmark.summary([row], valid=True)["arms"]["learned"]["time"]
        self.assertIsNone(result["unequal_fixed_work_pairs"])
        self.assertEqual(result["candidate_baseline_nodes_median"], 1.2)

    def test_native_model_rejection_prevents_engine_start_and_publishes_failed_record(self):
        # UCI setoption errors are informational, so either native parser must
        # reject a bad snapshot before any long-lived UCI process can start.
        for rejected_role in benchmark.ROLES:
            with self.subTest(rejected_role=rejected_role), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                baseline, candidate = root / "old.exe", root / "new.exe"
                model, positions = root / "source.nn", root / "positions.json"
                for path, content in ((baseline, b"baseline"), (candidate, b"candidate"),
                                      (model, b"invalid neural model"), (positions, b"{}")):
                    path.write_bytes(content)
                args = SimpleNamespace(
                    output=root / "result", model=[("learned", model)],
                    baseline=baseline, candidate=candidate,
                    baseline_commit="0" * 40, candidate_commit="1" * 40,
                    positions=positions, position_document={"positions": []},
                    selected_positions=[], repeats=2, seed=11, depth=3,
                    nodes=10000, movetime_ms=100)
                failure = RuntimeError("native parser rejected " + rejected_role)
                responses = [failure] if rejected_role == "baseline" else [{"score_cp": 0}, failure]
                with patch.object(benchmark.benchmark, "environment", return_value={}), \
                        patch.object(benchmark, "validate_native_model", side_effect=responses) as validate, \
                        patch.object(benchmark.LearningEngine, "popen_uci") as launch, \
                        patch.object(benchmark, "EngineCpuGuard") as guard, \
                        patch.object(benchmark, "WindowsProcesses") as windows, \
                        patch.object(benchmark, "LinuxProcesses") as linux:
                    with self.assertRaisesRegex(RuntimeError, "native parser rejected " + rejected_role):
                        benchmark.run(args)
                launch.assert_not_called()
                guard.assert_not_called()
                windows.assert_not_called()
                linux.assert_not_called()
                snapshot = args.output / "learned.nn"
                self.assertEqual(snapshot.read_bytes(), model.read_bytes())
                expected = [call(baseline, snapshot)]
                if rejected_role == "candidate":
                    expected.append(call(candidate, snapshot))
                self.assertEqual(validate.call_args_list, expected)
                result = json.loads((args.output / "comparison.json").read_text(encoding="utf-8"))
                self.assertFalse(result["completed"])
                self.assertFalse(result["summary"]["timing_valid"])
                self.assertEqual(result["records"], [])
                self.assertEqual(result["warmups"], [])
                self.assertIn("native parser rejected " + rejected_role, result["error"])


if __name__ == "__main__":
    unittest.main()
