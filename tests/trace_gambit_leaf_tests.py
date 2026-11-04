"""Optional real CLI contracts; set REGICIDE_TRACE_BINARY to the research driver."""
import json
import os
import subprocess
import unittest

import chess

BINARY = os.environ.get("REGICIDE_TRACE_BINARY")


@unittest.skipUnless(BINARY, "REGICIDE_TRACE_BINARY is not set")
class TraceContracts(unittest.TestCase):
    def run_trace(self, root="e2e4", path="e2e4", depth=1, nodes=10000):
        text = "\t".join([chess.STARTING_FEN, "", root, path]) + "\n"
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        return subprocess.run([BINARY, "--depth", str(depth), "--nodes", str(nodes)],
                              input=text, capture_output=True, text=True, timeout=30, **flags)

    def test_happy_exact_observer_and_legal_result(self):
        process = self.run_trace()
        self.assertEqual(process.returncode, 0, process.stderr)
        row = json.loads(process.stdout)
        self.assertTrue(row["position_unchanged"] and row["requested_iteration_completed"])
        self.assertEqual(row["search"]["bestmove"], "e2e4")
        self.assertFalse(row["causal_root_support_claim"])
        self.assertEqual(row["match_status"], "observed_static_evaluation")
        self.assertGreater(row["match_count"], 0)
        for item in row["matches"]:
            self.assertEqual(item["iteration_depth"], 1)
            self.assertEqual(item["fen"], row["target_fen"])
            self.assertEqual(item["ply"], 1)

    def test_illegal_root_or_target_mismatch_rejected(self):
        for root, path in [("e2e5", "e2e4"), ("e2e4", "d2d4")]:
            with self.subTest(root=root, path=path):
                process = self.run_trace(root, path)
                self.assertNotEqual(process.returncode, 0)
                self.assertFalse(process.stdout)

    def test_illegal_target_path_rejected(self):
        process = self.run_trace(path="e2e4 d2d4")
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(process.stdout)

    def test_node_cap_marks_incomplete_without_fake_evidence(self):
        process = self.run_trace(depth=6, nodes=1)
        self.assertEqual(process.returncode, 0, process.stderr)
        row = json.loads(process.stdout)
        self.assertLessEqual(row["search"]["nodes"], 1)
        self.assertFalse(row["requested_iteration_completed"])
        self.assertEqual(row["match_status"], "unavailable")
        self.assertFalse(row["matches"])


if __name__ == "__main__":
    unittest.main()
