"""Early balance work uses real time while preserving clock pressure."""
from pathlib import Path
import sys
import unittest

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import engine_pacing as pacing


class RecoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board()
        self.move = chess.Move.from_uci("e2e4")
        self.clock = {"enabled": True, "status": "running", "running_color": "white",
                      "white_ms": 600000, "black_ms": 600000, "turn_elapsed_ms": 200}
        self.plan = {"policy": pacing.POLICY, "target_total_ms": 700, "reserve_ms": 10000,
                     "color": "white", "enabled": True, "opening_budget": {"cap_ms": 700},
                     "before_fen": self.board.fen(en_passant="fen"), "root_ply": 0}

    def recover(self, cp, **kwargs):
        packet = {"depth": 3, "score": chess.engine.PovScore(chess.engine.Cp(cp), self.board.turn),
                  "pv": [self.move]}
        packet.update(kwargs.pop("packet", {}))
        return pacing.recovery_plan(self.board, self.plan, kwargs.get("clock", self.clock),
                                    baseline_info=packet, baseline_move=self.move)

    def test_short_opening_cannot_disable_out_of_band_recovery(self):
        for cp, direction in ((250, "ahead"), (-250, "behind")):
            result = self.recover(cp)
            self.assertTrue(result["balance_recovery"]["active"])
            self.assertEqual(result["balance_recovery"]["direction"], direction)
            self.assertGreater(result["balance_recovery"]["budget_ms"], 4000)
            self.assertTrue(result["balance_recovery"]["opening_cap_bypassed"])
            self.assertEqual(pacing.remaining_wait_ms(result, self.clock), 0)

    def test_in_band_and_unverified_score_never_extend_opening(self):
        for cp in (-100, 0, 100):
            result = self.recover(cp)
            self.assertFalse(result["balance_recovery"]["active"])
            self.assertEqual(result["target_total_ms"], 700)
        for packet in ({"lowerbound": True}, {"multipv": 2}, {"pv": []}, {"depth": 0},
                       {"pv": [chess.Move.from_uci("d2d4")]}):
            self.assertFalse(self.recover(400, packet=packet)["balance_recovery"]["active"])

    def test_black_score_uses_engine_pov(self):
        self.board.push_uci("e2e4")
        self.move = chess.Move.from_uci("e7e5")
        self.clock["running_color"] = "black"
        for cp, direction in ((200, "ahead"), (-200, "behind")):
            recovery = self.recover(cp)["balance_recovery"]
            self.assertTrue(recovery["active"])
            self.assertEqual(recovery["initial_cp_own"], cp)
            self.assertEqual(recovery["direction"], direction)

    def test_previously_idle_long_target_becomes_bounded_search(self):
        self.plan["target_total_ms"] = 21000
        behind = self.recover(-250)["balance_recovery"]
        ahead = self.recover(250)["balance_recovery"]
        self.assertGreater(behind["budget_ms"], 20000)
        self.assertLessEqual(ahead["budget_ms"], 12000)
        self.assertEqual(self.plan["target_total_ms"], 21000)

    def test_pressure_continuous_and_future_increment_unspendable(self):
        self.plan["target_total_ms"] = 45000
        for cp in (-200, 200):
            budgets = []
            for current in (600000, 180001, 180000, 179999, 120000, 60000, 30000, 10000, 1):
                clock = {**self.clock, "white_ms": current, "increment_ms": 10000}
                budget = self.recover(cp, clock=clock)["balance_recovery"]["budget_ms"]
                budgets.append(budget)
                self.assertLessEqual(budget, max(0, current - 10000))
            self.assertEqual(budgets, sorted(budgets, reverse=True))
            self.assertLessEqual(abs(budgets[2] - budgets[3]), 1)
            self.assertEqual(budgets[-2:], [0, 0])

    def test_each_colors_move16_and_custom_roots_cannot_restart_close_phase(self):
        for ply in (28, 29, 30, 31, 40, 41):
            self.board = chess.Board()
            for index in range(ply):
                self.board.push_uci(("g1f3", "g8f6", "f3g1", "f6g8")[index % 4])
            self.move = next(iter(self.board.legal_moves))
            color = "white" if self.board.turn else "black"
            self.clock["running_color"] = color
            self.plan.update(color=color, root_ply=ply, before_fen=self.board.fen(en_passant="fen"))
            recovery = self.recover(200)["balance_recovery"]
            self.assertEqual(recovery["active"], ply < 30)
            if ply >= 30:
                self.assertEqual(recovery["reason"], "outside_close_phase")
                self.assertEqual(recovery["budget_ms"], 0)

        self.board = chess.Board("8/8/8/8/8/4k3/8/KQ6 w - - 0 1")
        self.move = chess.Move.from_uci("b1b2")
        self.assertFalse(self.recover(200)["balance_recovery"]["active"])

    def test_sparring_recovery_uses_its_five_minute_clock_boundary(self):
        self.plan.update(target_total_ms=45000,pressure_start_ms=pacing.SPARRING_PRESSURE_START_MS)
        for cp in (-200,200):
            budgets=[]
            for current in (600000,300001,300000,299999,180000,60000,30000,10000,1):
                clock={**self.clock,"white_ms":current,"increment_ms":10000}
                budget=self.recover(cp,clock=clock)["balance_recovery"]["budget_ms"]
                budgets.append(budget)
                self.assertLessEqual(budget,max(0,current-pacing.LOW_CLOCK_MS))
            self.assertEqual(budgets,sorted(budgets,reverse=True))
            self.assertLessEqual(abs(budgets[2]-budgets[3]),1)
            self.assertEqual(budgets[-2:],[0,0])
            expected=(30000 if cp<0 else 12000)*(.6**2)
            self.assertAlmostEqual(budgets[4],expected,delta=1)

    def test_inactive_clock_cannot_authorize_extra_search(self):
        for clock in (None, {}, {**self.clock, "enabled": False},
                      {**self.clock, "status": "paused"}, {**self.clock, "running_color": "black"}):
            self.assertFalse(self.recover(200, clock=clock)["balance_recovery"]["active"])


if __name__ == "__main__":
    unittest.main()
