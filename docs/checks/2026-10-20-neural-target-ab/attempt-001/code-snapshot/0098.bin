"""Budget and role-clock regressions; no engine or fresh opening is searched."""
import copy
import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_neural_long_time_comparison as runner


class FakeBudget:
    def __init__(self, expire_after=False):
        self.calls = 0
        self.expire_after = expire_after

    def check(self):
        self.calls += 1
        if self.expire_after and self.calls > 1:
            raise runner.BudgetExpired("test deadline")


class FakeEngine:
    def __init__(self):
        self.calls = []

    def play(self, board, limit, **keywords):
        self.calls.append((board, limit, keywords))
        return "legal-result-fixture"


class DriverTests(unittest.TestCase):
    def test_baseline_receives_one_second_despite_candidate_label(self):
        engine = FakeEngine()
        wrapped = runner.BudgetEngine(engine, FakeBudget(), 1000)
        value = wrapped.play("board-history", runner.comparison.chess.engine.Limit(time=5), game="token")
        self.assertEqual(value, "legal-result-fixture")
        self.assertEqual(engine.calls[0][0], "board-history")
        self.assertEqual(engine.calls[0][1].time, 1)
        self.assertEqual(engine.calls[0][2], {"game": "token"})

    def test_candidate_keeps_each_agreed_budget(self):
        for milliseconds in (3000, 5000):
            engine = FakeEngine()
            wrapped = runner.BudgetEngine(engine, FakeBudget(), milliseconds)
            wrapped.play("board", runner.comparison.chess.engine.Limit(time=1))
            self.assertEqual(engine.calls[0][1].time, milliseconds / 1000)

    def test_deadline_does_not_publish_inflight_move(self):
        engine = FakeEngine()
        wrapped = runner.BudgetEngine(engine, FakeBudget(expire_after=True), 1000)
        with self.assertRaises(runner.BudgetExpired):
            wrapped.play("board", runner.comparison.chess.engine.Limit(time=1))
        self.assertEqual(len(engine.calls), 1)

    def test_expired_budget_starts_no_search(self):
        end = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        with self.assertRaises(ValueError):
            runner.Budget(end.isoformat())

    def test_future_budget_can_be_closed_without_start(self):
        end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=10)
        budget = runner.Budget(end.isoformat())
        budget.check()
        budget.close()


if __name__ == "__main__":
    unittest.main()
