"""Five-minute pressure reaches the actual final sparring audit gates."""
from contextlib import contextmanager
import unittest

import sparring_quality_host_tests as quality
import sparring_safety_host_tests as safety


@contextmanager
def fixture(kind):
    case=kind(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


class SparringAuditClockTests(unittest.TestCase):
    def test_quality_gate_shrinks_from_five_minutes_and_keeps_its_small_floor(self):
        for remaining,expected in ((300001,8000),(300000,8000),(299999,7999),
                                   (180000,2880),(60000,320),(30000,250),(10000,0)):
            with self.subTest(remaining=remaining),fixture(quality.QualityHostTests) as case:
                board,moves=case.prepare(ply=40)
                case.game._clock._balances["white" if board.turn else "black"]=remaining
                state=case.game.engine_move(case.game.revision)
                case.assert_played(state,moves[0])
                receipt=case.saved_decision()["quality_guard"]
                self.assertEqual(receipt["budget_ms"],expected)
                self.assertLessEqual(expected,max(0,remaining-10150))
                if expected>250:
                    self.assertEqual(receipt["status"],"verified")
                elif expected:
                    # The exact minimum cannot fund setup plus a complete
                    # audit. Preserve the legal move without claiming safety.
                    self.assertEqual(receipt["status"],"unverified")
                    self.assertFalse(receipt["applied"])
                else:
                    self.assertEqual(receipt["reason"],"insufficient_budget")

    def test_tactical_gate_keeps_minimum_check_and_never_spends_the_clock_reserve(self):
        for remaining,expected in ((300001,12000),(300000,12000),(299999,11999),
                                   (180000,4320),(60000,600),(30000,600),(10000,0)):
            with self.subTest(remaining=remaining),fixture(safety.SafetyHostTests) as case:
                board=case.prepare()
                case.game._clock._balances["white" if board.turn else "black"]=remaining
                state=case.game.engine_move(case.game.revision)
                self.assertIsNone(state["error"])
                receipt=case.saved_decision()["tactical_safety"]
                self.assertEqual(receipt["budget_ms"],expected)
                self.assertLessEqual(expected,max(0,remaining-10150))
                self.assertEqual(state["last_move"],"d4c4" if expected else "b5a7")
                self.assertEqual(case.waits,[])


if __name__=="__main__":
    unittest.main()
