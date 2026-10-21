"""Accounting partition, finite windows, identity and failure regressions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests')]
import m2_job_load_guard as load
from m2_load_guard_tests import Clock, Provider


class AccountingProvider(Provider):
    def __init__(self, clock):
        super().__init__(clock)
        self.job_cpu = self.monitor_cpu = 0
        self.monitor_pid, self.monitor_creation = 51, 200
        self.accounting_frame = None
    def snapshot(self, roots):
        self.accounting_frame = None
        snapshot = super().snapshot(roots)
        monitor = {'pid': self.monitor_pid, 'creation_ticks': self.monitor_creation, 'cpu_ticks': self.monitor_cpu}
        job = {'cpu_ticks': self.job_cpu, 'coordinator_in_job': False}
        self.accounting_frame = {'job_before': dict(job), 'job_after': dict(job),
                                 'monitor_before': dict(monitor), 'monitor_after': dict(monitor)}
        return snapshot


class JobLoadGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.provider = AccountingProvider(self.clock)
        self.guard = load.JobLoadGuard(Path(self.temp.name)/'load.jsonl', load.LoadPolicy(.2), None,
                                      self.provider, clock=self.clock, sleep=self.clock.sleep)
        self.addCleanup(self.guard.close)
        self.guard.sample('baseline')
    def step(self, busy=.7, job=25000, coordinator=5000):
        self.provider.busy_fraction = busy
        self.provider.job_cpu += job
        self.provider.monitor_cpu += coordinator
        self.clock.sleep(.5)
        return self.guard.sample()
    def test_short_unobserved_job_cpu_and_monitor_are_separate(self):
        for _ in range(4):
            row = self.step()
        h = row['host']
        self.assertEqual(h['observed_process_delta_ticks_diagnostic'], 0)
        self.assertEqual(h['experiment_job_delta_ticks'], 25000)
        self.assertEqual(h['coordinator_delta_ticks'], 5000)
        self.assertEqual(h['residual_signed_delta_ticks'], 5000)
        self.assertAlmostEqual(h['window_fraction'], .1)
        self.assertIsNone(self.guard.stop_reason)
    def test_real_residual_threshold_still_stops(self):
        for _ in range(4):
            row = self.step(job=15000)
        self.assertAlmostEqual(row['host']['window_fraction'], .3)
        self.assertEqual(self.guard.stop_reason['kind'], 'host_nonworker_cpu')
    def test_clip_after_signed_window_sum(self):
        self.step(busy=.1, job=10000, coordinator=0)  # -5000
        row = self.step(busy=.3, job=0, coordinator=0)  # +15000
        self.assertEqual(row['host']['window_nonworker_ticks'], 10000)
        self.assertAlmostEqual(row['host']['window_fraction'], .1)
    def test_negative_window_is_reported_and_clipped_once(self):
        row = self.step(busy=.1, job=10000, coordinator=0)
        self.assertEqual(row['host']['residual_signed_delta_ticks'], -5000)
        self.assertEqual(row['host']['window_fraction'], 0)
        self.assertEqual(self.guard.summary['accounting_underflow_ticks'], 5000)
    def test_job_counter_regression_fails_closed(self):
        self.step()
        self.provider.job_cpu = -1
        self.clock.sleep(.5)
        self.guard.sample()
        self.assertEqual(self.guard.stop_reason['kind'], 'monitor_error')
    def test_monitor_identity_change_fails_closed(self):
        self.provider.monitor_creation += 1
        self.step()
        self.assertEqual(self.guard.stop_reason['kind'], 'monitor_error')
    def test_raw_frame_and_method_are_persisted(self):
        self.step()
        rows = [json.loads(s) for s in self.guard.output.read_text().splitlines()]
        self.assertEqual(rows[0]['schema_version'], 2)
        self.assertEqual(rows[-1]['native_accounting']['job_after']['cpu_ticks'], 25000)
        self.assertEqual(rows[-1]['accounting_method'], load.METHOD)
    def test_failed_identity_does_not_advance_native_baseline(self):
        self.step()
        before = self.guard.previous_accounting
        self.provider.monitor_creation += 1
        failed = self.step()
        self.assertFalse(failed['accounting_baseline_accepted'])
        self.assertIs(self.guard.previous_accounting, before)
        self.provider.monitor_creation -= 1
        row = self.step()
        self.assertEqual(row['host']['experiment_job_delta_ticks'], 50000)
        self.assertFalse(self.guard.summary['valid_for_load_check'])
    def test_zero_tick_adjacent_samples_do_not_invent_cpu(self):
        row = self.guard.sample()
        self.assertEqual(row['host']['total_delta_ticks'], 0)
        self.assertEqual(row['host']['experiment_job_delta_ticks'], 0)
        self.assertEqual(row['host']['coordinator_delta_ticks'], 0)
        self.assertIsNone(self.guard.stop_reason)


if __name__ == '__main__':
    unittest.main()
