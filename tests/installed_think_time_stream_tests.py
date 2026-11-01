"""Installed streamed evaluation and live time settings; fake transport only."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest

import evaluation_state_tests as evaluation


class InstalledStreamingTimeTests(unittest.TestCase):
    def setUp(self):
        evaluation.EvaluationStateTests.setUp(self)

    def test_setting_does_not_reset_live_evaluation_hint_or_active_stream(self):
        self.game.new_game("white", 3000, self.game.state()["revision"])
        self.game.human_move("e2e4", self.game.state()["revision"])
        token = self.game._game_token
        self.peer.next_move = "e7e5"
        self.peer.gate = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as workers:
            search = workers.submit(self.game.engine_move, self.game.state()["revision"])
            try:
                self.assertTrue(self.peer.waiting.wait(2))
                state = self.game.state()
                score, hint = self.game.evaluation(), self.game.hint()
                changed = self.game.set_think_time(5000, state["game_id"],
                                                   state["engine"]["settings_revision"])
                self.assertEqual(self.game.evaluation(), score)
                self.assertEqual(self.game.hint(), hint)
                self.assertEqual(score["status"], "searching")
                self.assertEqual(changed["engine"]["active_movetime_ms"], 3000)
                self.assertEqual(changed["engine"]["movetime_ms"], 5000)
                self.assertEqual(changed["revision"], state["revision"])
                self.assertEqual(changed["board"], state["board"])
                self.assertIs(self.game._game_token, token)
                self.assertFalse(search.done())
            finally:
                self.peer.gate.set()
            first = search.result(timeout=3)
        self.assertEqual(first["engine"]["movetime_ms"], 5000)
        self.assertIsNone(first["engine"]["active_movetime_ms"])
        self.assertEqual(self.game.evaluation()["source"], "search")
        self.assertEqual(self.game.evaluation()["fen"], first["fen"])
        # The engine-root score is not carried onto the next human position.
        self.assertIsNone(self.game.evaluation()["cp"])
        self.game.human_move("g1f3", first["revision"])
        self.peer.gate = None
        self.peer.next_move = "b8c6"
        self.game.engine_move(self.game.state()["revision"])
        self.assertEqual([call[1].time for call in self.peer.analysis_calls], [3.0, 5.0])
        self.assertTrue(all(call[2] is token for call in self.peer.analysis_calls))
        saved = json.loads((self.game.save_dir / f"{self.game.game_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["engine_movetimes_ms"], {"2": 3000, "4": 5000})


if __name__ == "__main__":
    unittest.main(verbosity=2)
