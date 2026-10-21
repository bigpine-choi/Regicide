"""Offline raw-CPU audit fixtures; no native API/process/real sleep is used."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests')]
import m2_job_receipts as receipts
import m2_job_load_guard as load
from m2_load_guard_tests import Clock, Provider


class NativeFrames(Provider):
    def __init__(self, clock):
        super().__init__(clock)
        self.job_cpu = self.monitor_cpu = 0
        self.query_job = self.query_monitor = 0
        self.accounting_frame = None
        self.fail_next = False

    def snapshot(self, roots):
        self.accounting_frame = None
        if self.fail_next:
            self.fail_next = False
            raise OSError('synthetic native query unavailable')
        result = super().snapshot(roots)
        bracket = dict(read_started_monotonic=self.clock(), read_finished_monotonic=self.clock())

        def job(cpu):
            return dict(user_ticks=cpu, kernel_ticks=0, cpu_ticks=cpu,
                total_processes=int(cpu > 0), active_processes=0, terminated_processes_by_limit=0,
                coordinator_in_job=False, **bracket)

        def monitor(cpu):
            return dict(user_ticks=cpu, kernel_ticks=0, cpu_ticks=cpu,
                        pid=os.getpid(), creation_ticks=100, **bracket)

        system = dict(idle_ticks=result.system.idle_ticks, kernel_ticks=result.system.kernel_ticks,
            user_ticks=result.system.user_ticks, total_ticks=result.system.total_ticks,
            busy_ticks=result.system.total_ticks-result.system.idle_ticks, **bracket)
        self.accounting_frame = dict(job_before=job(self.job_cpu),
            job_after=job(self.job_cpu+self.query_job), monitor_before=monitor(self.monitor_cpu),
            monitor_after=monitor(self.monitor_cpu+self.query_monitor), system=system)
        self.job_cpu += self.query_job
        self.monitor_cpu += self.query_monitor
        return result


def _fixture(steps=None, fail_last=False, query_job=0, query_monitor=0):
    """Use the real producer with wholly synthetic clocks/counters, then replay.

    This gives other test suites a correctly serialized current raw schema.
    Audit tampering tests below mutate only the recorded side of this fixture.
    """
    with tempfile.TemporaryDirectory() as directory:
        clock = Clock()
        provider = NativeFrames(clock)
        guard = load.JobLoadGuard(Path(directory)/'load.jsonl', load.LoadPolicy(.2), None,
                                  provider, clock=clock, sleep=clock.sleep)
        guard.require_idle(5.)
        guard._write(dict(event='timer_started', interval_seconds=.5, join_bound_seconds=2.))
        steps = steps if steps is not None else [(.7, 25000, 5000)]*4
        provider.query_job, provider.query_monitor = query_job, query_monitor
        for busy, job, monitor in steps:
            provider.busy_fraction = busy
            provider.job_cpu += job
            provider.monitor_cpu += monitor
            clock.sleep(.5)
            guard.sample('timer')
        guard._write(dict(event='timer_stopped', timer_samples=len(steps)))
        provider.query_job = provider.query_monitor = 0
        provider.fail_next = fail_last
        guard.close()
        summary = deepcopy(guard.summary)
        summary['timer'] = dict(started=True, samples=len(steps), alive=False, failure=None,
                                join_bound_seconds=2.)
        rows = [json.loads(line) for line in guard.output.read_text().splitlines()]
        valid_frames = [row['native_accounting'] for row in rows if row.get('native_accounting')]
        final_job = deepcopy(valid_frames[-1]['job_after'])
        settings = dict(asdict(guard.policy), idle_probe_seconds=5.)
        # Mirror an actual JSON receipt, including normalized policy tuples.
        return json.loads(json.dumps((rows, summary, final_job, settings)))


def make_clean_fixture():
    """Return (rows, summary, final_job, policy_dict) for formal audit tests."""
    return tuple(_fixture())


class JobReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/'load.jsonl'
        self.rows, self.summary, self.final_job, self.policy = make_clean_fixture()

    def audit(self):
        self.path.write_text(''.join(json.dumps(row, allow_nan=False)+'\n' for row in self.rows), encoding='utf-8')
        return receipts.validate_job_receipts(self.path, self.summary, self.final_job, self.policy)

    def samples(self):
        return [row for row in self.rows if row.get('event') == 'sample']

    def test_clean_raw_fixture_replays_full_idle_timer_and_totals(self):
        result = self.audit()
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(result['valid_for_load_check'])
        self.assertEqual(result['invalid_errors'], [])
        self.assertEqual(result['counts']['samples'], self.summary['snapshot_count'])
        self.assertEqual(result['recomputed_metrics']['experiment_job_delta_ticks'], 100000)
        self.assertEqual(result['recomputed_metrics']['coordinator_delta_ticks'], 20000)

    def test_empty_fake_clean_log_cannot_pass(self):
        self.rows = [{}]
        with self.assertRaisesRegex(ValueError, 'policy'):
            self.audit()

    def test_missing_native_frame_or_baseline_flag_rejected(self):
        original = deepcopy(self.rows)
        for field in ('native_accounting', 'accounting_baseline_accepted'):
            self.rows = deepcopy(original)
            self.samples()[2].pop(field)
            with self.assertRaises(ValueError):
                self.audit()

    def test_final_job_below_last_raw_cpu_or_inside_job_rejected(self):
        self.final_job['user_ticks'] -= 1
        self.final_job['cpu_ticks'] -= 1
        with self.assertRaisesRegex(ValueError, 'below last raw'):
            self.audit()
        self.final_job['user_ticks'] += 1
        self.final_job['cpu_ticks'] += 1
        self.final_job['coordinator_in_job'] = True
        with self.assertRaisesRegex(ValueError, 'coordinator'):
            self.audit()

    def test_coordinator_identity_change_cannot_be_resealed(self):
        frame = self.samples()[-2]['native_accounting']
        for key in ('monitor_before', 'monitor_after'):
            frame[key]['creation_ticks'] += 1
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.audit()

    def test_summary_delta_maxgap_fraction_and_count_tampering_rejected(self):
        original = deepcopy(self.summary)
        for key in ('experiment_job_delta_ticks', 'coordinator_delta_ticks',
                    'accounting_underflow_ticks', 'max_sample_gap_seconds',
                    'max_nonworker_cpu_fraction', 'snapshot_count'):
            self.summary = deepcopy(original)
            self.summary[key] += 1
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'summary ' + key):
                self.audit()

    def test_raw_host_arithmetic_and_system_mismatch_rejected(self):
        self.samples()[-2]['host']['residual_signed_delta_ticks'] += 1
        with self.assertRaisesRegex(ValueError, 'host residual'):
            self.audit()
        self.samples()[-2]['host']['residual_signed_delta_ticks'] -= 1
        self.samples()[-2]['system']['idle_ticks'] += 1
        with self.assertRaisesRegex(ValueError, 'system differs'):
            self.audit()

    def test_signed_clipping_after_window_sum_and_midpoint_replay(self):
        self.rows, self.summary, self.final_job, self.policy = _fixture(
            steps=[(.1, 10000, 0), (.3, 0, 0), (.1, 0, 0), (.1, 0, 0)])
        result = self.audit()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['recomputed_metrics']['accounting_underflow_ticks'], 5000)
        negative = [row for row in self.samples() if row.get('host', {}).get('residual_signed_delta_ticks', 0) < 0]
        self.assertEqual(len(negative), 1)
        self.assertEqual(negative[0]['host']['nonworker_delta_ticks'], -5000)
        self.rows, self.summary, self.final_job, self.policy = _fixture(query_job=100, query_monitor=50)
        self.assertEqual(self.audit()['status'], 'PASS')
        self.assertEqual(self.summary['experiment_job_delta_ticks'], 100400)

    def test_threshold_failure_is_auditable_invalid_and_cannot_claim_clean(self):
        self.rows, self.summary, self.final_job, self.policy = _fixture(steps=[(.9, 0, 0)]*4)
        result = self.audit()
        self.assertEqual(result['status'], 'INVALID')
        self.assertFalse(result['valid_for_load_check'])
        self.assertIn('rolling residual threshold exceeded', result['invalid_errors'])
        self.summary['valid_for_load_check'] = True
        self.summary['failure'] = None
        with self.assertRaisesRegex(ValueError, 'clean summary contradicts'):
            self.audit()

    def test_explicit_failed_native_sample_is_invalid_not_silently_accepted(self):
        self.rows, self.summary, self.final_job, self.policy = _fixture(fail_last=True)
        result = self.audit()
        self.assertEqual(result['status'], 'INVALID')
        self.assertTrue(any('unaccepted native sample' in error for error in result['invalid_errors']))
        self.assertEqual(result['counts']['accepted_samples'], self.summary['snapshot_count'])
        self.assertEqual(result['counts']['samples'], self.summary['snapshot_count']+1)
        self.samples()[-1]['failure'] = None
        with self.assertRaisesRegex(ValueError, 'unaccepted sample'):
            self.audit()

    def test_missing_final_job_is_explicit_invalid_and_forbidden_for_clean(self):
        self.final_job = None
        with self.assertRaisesRegex(ValueError, 'clean summary contradicts'):
            self.audit()
        self.summary['valid_for_load_check'] = False
        self.summary['failure'] = dict(kind='cleanup_unknown', reason='native query timed out')
        result = self.audit()
        self.assertIn('final Job accounting missing', result['invalid_errors'])

    def test_missing_timer_or_idle_coverage_cannot_pass_clean(self):
        self.rows = [row for row in self.rows if row.get('event') != 'timer_started']
        with self.assertRaisesRegex(ValueError, 'timer start/stop'):
            self.audit()
        self.rows, self.summary, self.final_job, self.policy = make_clean_fixture()
        self.summary['completed_idle_seconds'] = 500
        with self.assertRaisesRegex(ValueError, 'idle duration'):
            self.audit()

    def test_external_cpu_summary_and_zero_policy_are_independent(self):
        for i, sample in enumerate(self.samples()):
            sample['processes'].append(dict(pid=991, creation_ticks=100,
                cpu_ticks=i, owned=False, name='stockfish.exe'))
        with self.assertRaisesRegex(ValueError, 'summary external_cpu'):
            self.audit()
        self.summary['external_cpu_increase_ticks'] = len(self.samples())-1
        with self.assertRaisesRegex(ValueError, 'clean summary contradicts'):
            self.audit()
        self.summary['valid_for_load_check'] = False
        self.summary['failure'] = dict(kind='external_cpu', reason='known engine consumed CPU')
        self.assertEqual(self.audit()['status'], 'INVALID')

    def test_frozen_policy_nan_and_bracket_regression_rejected(self):
        self.policy['max_nonworker_cpu_fraction'] = .5
        with self.assertRaisesRegex(ValueError, 'policy differs'):
            self.audit()
        self.policy['max_nonworker_cpu_fraction'] = .2
        self.samples()[0]['native_accounting']['job_after']['read_finished_monotonic'] = -1
        with self.assertRaises(ValueError):
            self.audit()

    def test_missing_or_alive_sampler_summary_cannot_pass_clean(self):
        timer = self.summary.pop('timer')
        with self.assertRaisesRegex(ValueError, 'sampler summary missing'):
            self.audit()
        self.summary['timer'] = dict(timer, alive=True)
        with self.assertRaisesRegex(ValueError, 'sampler has not closed'):
            self.audit()

    def test_half_tick_sums_are_exact_even_for_large_cumulative_totals(self):
        # Reconciliation tolerance belongs only to interpolated windows. It
        # must not let one native tick be hidden in a large summary total.
        with self.assertRaises(ValueError):
            receipts._same(1_000_000_000_001, 1_000_000_000_000, 'CPU total')

    def test_cpu_component_sum_and_native_read_order_rejected(self):
        frame = self.samples()[0]['native_accounting']
        frame['job_after']['cpu_ticks'] += 1
        with self.assertRaisesRegex(ValueError, 'CPU sum'):
            self.audit()
        frame['job_after']['cpu_ticks'] -= 1
        frame['monitor_before']['read_started_monotonic'] = 1
        frame['monitor_before']['read_finished_monotonic'] = 1
        with self.assertRaisesRegex(ValueError, 'bracket order'):
            self.audit()


if __name__ == '__main__':
    unittest.main()
