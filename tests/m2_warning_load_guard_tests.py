"""Warning-only policy and independent replay with synthetic counters/clocks."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests')]
import m2_warning_load_guard as load
import m2_warning_receipts as receipts
from m2_job_receipts_tests import NativeFrames
from m2_load_guard_tests import Clock
from see_clean_guard import ProcessInfo


def make_warning_fixture(steps=None, *, fail_last=False, initial_busy=.1):
    """Serialize the actual new producer with entirely synthetic native data."""
    with tempfile.TemporaryDirectory() as directory:
        clock = Clock()
        provider = NativeFrames(clock)
        provider.busy_fraction = initial_busy
        guard = load.WarningLoadGuard(Path(directory)/'load.jsonl', load.LoadPolicy(.2), None,
                                      provider, clock=clock, sleep=clock.sleep)
        guard.require_idle(5.)
        guard._write(dict(event='timer_started', interval_seconds=.5, join_bound_seconds=2.))
        steps = steps if steps is not None else [(.9, 0, 0)]*4
        for busy, job, monitor in steps:
            provider.busy_fraction = busy
            provider.job_cpu += job
            provider.monitor_cpu += monitor
            clock.sleep(.5)
            guard.sample('timer')
        guard._write(dict(event='timer_stopped', timer_samples=len(steps)))
        provider.fail_next = fail_last
        guard.close()
        summary = deepcopy(guard.summary)
        summary['timer'] = dict(started=True, samples=len(steps), alive=False, failure=None,
                                join_bound_seconds=2.)
        rows = [json.loads(line) for line in guard.output.read_text().splitlines()]
        frames = [row['native_accounting'] for row in rows if row.get('native_accounting')]
        settings = dict(asdict(guard.policy), idle_probe_seconds=5.)
        return tuple(json.loads(json.dumps((rows, summary, frames[-1]['job_after'], settings))))


class WarningGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = Clock()
        self.provider = NativeFrames(self.clock)
        self.guard = load.WarningLoadGuard(Path(self.temporary.name)/'load.jsonl', load.LoadPolicy(.2), None,
            self.provider, clock=self.clock, sleep=self.clock.sleep)
        self.addCleanup(self.guard.close)
        self.guard.sample('baseline')

    def step(self, busy=.9, job=0, coordinator=0, seconds=.5):
        self.provider.busy_fraction = busy
        self.provider.job_cpu += job
        self.provider.monitor_cpu += coordinator
        self.clock.sleep(seconds)
        return self.guard.sample()

    def test_threshold_records_warning_and_sampling_continues(self):
        for _ in range(4):
            row = self.step()
        self.assertTrue(row['host']['residual_load_warning'])
        self.assertTrue(row['host']['residual_load_warning_episode_started'])
        self.assertAlmostEqual(row['host']['window_fraction'], .9)
        self.assertIsNone(self.guard.stop_reason)
        self.guard.assert_clean()
        again = self.step()
        self.assertTrue(again['host']['residual_load_warning'])
        self.assertFalse(again['host']['residual_load_warning_episode_started'])
        self.assertEqual(self.guard.summary['residual_load_warning_episode_count'], 1)
        self.assertEqual(self.guard.summary['residual_load_warning_window_count'], 2)

    def test_warning_idle_is_valid_after_normal_close(self):
        self.provider.busy_fraction = .9
        self.guard.require_idle(5.)
        self.guard.close()
        self.assertTrue(self.guard.summary['preflight_complete'])
        self.assertTrue(self.guard.summary['valid_for_load_check'])
        self.assertGreater(self.guard.summary['residual_load_warning_window_count'], 0)
        self.assertEqual(self.guard.summary['residual_cpu_policy'], 'warning-only')

    def test_warning_episodes_restart_after_window_recovers(self):
        for _ in range(4):
            self.step()
        for _ in range(4):
            recovered = self.step(busy=0)
        self.assertFalse(recovered['host']['residual_load_warning'])
        for _ in range(4):
            resumed = self.step()
        self.assertTrue(resumed['host']['residual_load_warning'])
        self.assertEqual(self.guard.summary['residual_load_warning_episode_count'], 2)

    def test_signed_negative_interval_is_not_clamped_before_window_sum(self):
        negative = self.step(busy=.1, job=10000)
        positive = self.step(busy=.3)
        self.assertEqual(negative['host']['residual_signed_delta_ticks'], -5000)
        self.assertEqual(positive['host']['window_nonworker_ticks'], 10000)
        self.assertAlmostEqual(positive['host']['window_fraction'], .1)
        self.assertEqual(self.guard.summary['accounting_underflow_ticks'], 5000)

    def test_incomplete_window_cannot_warn_even_with_high_fraction(self):
        row = self.step()
        self.assertAlmostEqual(row['host']['window_fraction'], .9)
        self.assertFalse(row['host']['window_ready'])
        self.assertFalse(row['host']['residual_load_warning'])

    def test_threshold_equality_is_not_warning(self):
        for _ in range(4):
            row = self.step(busy=.2)
        self.assertEqual(row['host']['window_fraction'], .2)
        self.assertFalse(row['host']['residual_load_warning'])

    def test_job_counter_regression_remains_fatal(self):
        self.step(job=1000)
        self.provider.job_cpu = -1
        self.step()
        self.assertEqual(self.guard.stop_reason['kind'], 'monitor_error')

    def test_coordinator_identity_change_remains_fatal(self):
        self.step()
        self.provider.monitor_cpu = 1
        # Mutate both raw coordinator identities, as a reused PID would do.
        original_snapshot = self.provider.snapshot
        def changed(roots):
            result = original_snapshot(roots)
            for field in ('monitor_before', 'monitor_after'):
                self.provider.accounting_frame[field]['creation_ticks'] += 1
            return result
        self.provider.snapshot = changed
        self.step()
        self.assertEqual(self.guard.stop_reason['kind'], 'monitor_error')

    def test_sampling_gap_remains_fatal_even_if_residual_only_warns(self):
        self.step(seconds=2.1)
        self.assertEqual(self.guard.stop_reason['kind'], 'sampling_gap')

    def test_other_failure_kinds_are_not_suppressed(self):
        for kind in ('external_cpu', 'process_access', 'receipt_error', 'worker_identity'):
            self.guard.summary['failure'] = None
            self.guard._fail(kind, 'synthetic actual failure')
            self.assertEqual(self.guard.stop_reason['kind'], kind)

    def test_real_external_counter_increase_still_stops_guard(self):
        clock = Clock()
        provider = NativeFrames(clock)
        provider.live[991] = ProcessInfo(991, 1, 'stockfish.exe', 100, 0)
        guard = load.WarningLoadGuard(Path(self.temporary.name)/'external-load.jsonl', load.LoadPolicy(.2),
            None, provider, clock=clock, sleep=clock.sleep)
        self.addCleanup(guard.close)
        guard.sample('baseline')
        provider.live[991] = ProcessInfo(991, 1, 'stockfish.exe', 100, 1)
        provider.busy_fraction = .9
        clock.sleep(.5)
        guard.sample()
        self.assertEqual(guard.stop_reason['kind'], 'external_cpu')

    def test_header_and_summary_have_distinct_new_policy_schema(self):
        self.guard.close()
        header = json.loads(self.guard.output.read_text().splitlines()[0])
        for record in (header, self.guard.summary):
            self.assertEqual(record['schema_version'], 3)
            self.assertEqual(record['residual_cpu_policy'], 'warning-only')
            self.assertEqual(record['policy']['max_nonworker_cpu_fraction'], .2)


class WarningReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/'load.jsonl'
        self.rows, self.summary, self.final_job, self.policy = make_warning_fixture()

    def audit(self):
        self.path.write_text(''.join(json.dumps(row, allow_nan=False)+'\n' for row in self.rows), encoding='utf-8')
        return receipts.validate_job_receipts(self.path, self.summary, self.final_job, self.policy)

    def samples(self):
        return [row for row in self.rows if row.get('event') == 'sample']

    def test_high_residual_replays_as_valid_warning(self):
        result = self.audit()
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(result['valid_for_load_check'])
        self.assertEqual(result['invalid_errors'], [])
        self.assertGreater(result['warnings']['window_count'], 0)
        self.assertAlmostEqual(result['warnings']['peak_fraction'], .9)

    def test_warning_raw_bool_or_episode_tampering_rejected(self):
        original = deepcopy(self.rows)
        for key in ('residual_load_warning', 'residual_load_warning_episode_started'):
            self.rows = deepcopy(original)
            row = next(row for row in self.samples() if row.get('host', {}).get('residual_load_warning'))
            row['host'][key] = not row['host'][key]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.audit()

    def test_warning_summary_counts_and_peak_tampering_rejected(self):
        original = deepcopy(self.summary)
        for key in ('residual_load_ready_window_count', 'residual_load_warning_window_count',
                    'residual_load_warning_episode_count', 'residual_load_warning_peak_fraction'):
            self.summary = deepcopy(original)
            self.summary[key] += 1
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.audit()

    def test_policy_schema_and_threshold_tampering_rejected(self):
        self.summary['residual_cpu_policy'] = 'ignore'
        with self.assertRaisesRegex(ValueError, 'residual CPU policy'):
            self.audit()
        self.summary['residual_cpu_policy'] = 'warning-only'
        self.rows[0]['schema_version'] = 2
        with self.assertRaisesRegex(ValueError, 'schema'):
            self.audit()
        self.rows[0]['schema_version'] = 3
        self.samples()[-1]['host']['residual_load_warning_threshold_fraction'] = .3
        with self.assertRaisesRegex(ValueError, 'threshold_fraction'):
            self.audit()

    def test_signed_negative_window_and_owned_coordinator_partitions_replay(self):
        self.rows, self.summary, self.final_job, self.policy = make_warning_fixture(
            steps=[(.1, 10000, 0), (.3, 0, 0), (.7, 25000, 5000), (.7, 25000, 5000)])
        result = self.audit()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['recomputed_metrics']['experiment_job_delta_ticks'], 60000)
        self.assertEqual(result['recomputed_metrics']['coordinator_delta_ticks'], 10000)
        self.assertEqual(result['recomputed_metrics']['accounting_underflow_ticks'], 5000)

    def test_final_native_failure_is_invalid_and_cannot_be_resealed_clean(self):
        self.rows, self.summary, self.final_job, self.policy = make_warning_fixture(fail_last=True)
        result = self.audit()
        self.assertEqual(result['status'], 'INVALID')
        self.assertFalse(result['valid_for_load_check'])
        self.summary['failure'] = None
        self.summary['valid_for_load_check'] = True
        with self.assertRaisesRegex(ValueError, 'clean summary contradicts'):
            self.audit()

    def test_external_known_engine_cpu_still_invalidates(self):
        for index, row in enumerate(self.samples()):
            row['processes'].append(dict(pid=991, creation_ticks=100, cpu_ticks=index,
                owned=False, name='stockfish.exe'))
        self.summary['external_cpu_increase_ticks'] = len(self.samples())-1
        with self.assertRaisesRegex(ValueError, 'clean summary contradicts'):
            self.audit()
        self.summary['valid_for_load_check'] = False
        self.summary['failure'] = dict(kind='external_cpu', reason='synthetic known engine activity')
        self.assertEqual(self.audit()['status'], 'INVALID')

    def test_native_cpu_arithmetic_or_missing_coverage_cannot_be_ignored(self):
        self.samples()[-2]['host']['coordinator_delta_ticks'] += 1
        with self.assertRaisesRegex(ValueError, 'coordinator_delta_ticks'):
            self.audit()
        self.samples()[-2]['host']['coordinator_delta_ticks'] -= 1
        self.summary['timer']['alive'] = True
        with self.assertRaisesRegex(ValueError, 'sampler has not closed'):
            self.audit()


class FrozenDependenciesTests(unittest.TestCase):
    def test_historical_accounting_and_runners_unchanged(self):
        frozen = {
            'm2_job_load_guard.py': 'c4b5546d61798cd24d82dfba42d272726a84581dd42d597ffb55497482fff743',
            'm2_job_receipts.py': '5e141c94fd4d6340c72022161e164082d611e379a0176d4d83e9fafa6c2dbd98',
            'm2_job_accounting.py': 'c18f74176a85e3cc0a7376568aa34bf55662e58977f068e763bdb3cfcd9f94ac',
            'm2_load_guard.py': '39b25242e03faef0dd65708481ea08720eadf62cf163604ba173df26725d5b13',
            'm2_formal.py': '6b1b575279e9f14a8ca26934c21ea827f2a5839ebfb4cfb2aa1b77319dcfe6fa',
            'm2_formal_v2.py': '6a3995529cf1ad9289d32b23cf114549b4f2d611074f04a985229500ebd301b4',
            'm2_formal_v3.py': '31f68fe9184792e7603119178ee32f0d458f90a0c4a8782a79f3256a0569b64c',
            'm2_job_operations.py': '72f25233bad8ad8efef926955e6b4c2e125f75f1ade1ce90fc8283b9fcfda472'}
        for name, expected in frozen.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((ROOT/'scripts'/name).read_bytes()).hexdigest(), expected)


if __name__ == '__main__':
    unittest.main()
