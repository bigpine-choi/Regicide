"""Synthetic Windows CPU/identity receipts; never start or kill user processes."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import m2_load_guard as load

WALL = 100_000
RATE = 100_000


class Clock:
    def __init__(self):
        self.now = 0.
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class Provider:
    platform = 'win32'
    def __init__(self, clock):
        self.clock = clock
        self.live, self.held = {}, {}
        self.total, self.busy, self.last = 1_000_000, 20_000, clock()
        self.busy_fraction = .1
        self.hook = None
        self.closed = False
        self.released = []
    def is_engine(self, process):
        return process.name.casefold().startswith(('regicide', 'stockfish', 'lc0', 'sunfish'))
    def identity(self, pid):
        return self.live[pid].identity
    def watch_root(self, identity):
        process = self.live[identity[0]]
        if process.identity != identity or process.parent_pid != os.getpid() or process.error:
            raise RuntimeError('synthetic worker identity/parent mismatch')
        self.held[identity] = process
    def snapshot(self, roots):
        self.total += round((self.clock() - self.last) * RATE)
        self.busy += round((self.clock() - self.last) * RATE * self.busy_fraction)
        self.last = self.clock()
        for process in self.live.values():
            if not process.error and any(load.descendant_chain(process, self.live, root) is not None for root in roots):
                self.held[process.identity] = process
        frame = load.LoadSnapshot(dict(self.live), load.SystemTimes(self.total - self.busy, self.total, 0),
                                  WALL + round(self.clock() * 1000), tuple(self.held.values()))
        return self.hook(frame) if self.hook else frame
    def retire(self, pid, cpu=None):
        process = self.live.pop(pid)
        if process.identity in self.held:
            self.held[process.identity] = replace(process, cpu_ticks=process.cpu_ticks if cpu is None else cpu,
                                                  exited_during_snapshot=True)
    def close(self):
        self.closed = True
    def release_finished(self, identities):
        for identity in identities:
            if not self.held[identity].exited_during_snapshot:
                raise RuntimeError('cannot release a live process')
            del self.held[identity]
            self.released.append(identity)


class LoadGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='regicide-m2-load-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.clock = Clock()
        self.provider = Provider(self.clock)
        self.guard = load.M2LoadGuard(self.folder / 'load.jsonl', load.LoadPolicy(.2), self.provider,
                                     clock=self.clock, sleep=self.clock.sleep)
        self.addCleanup(self.guard.close)
    def idle(self):
        self.guard.require_idle(2.)
    def advance(self, seconds=.5):
        self.clock.sleep(seconds)
        return self.guard.sample()
    def worker(self, pid=100, created=None):
        created = created if created is not None else WALL + round(self.clock() * 1000) + 1
        self.provider.live[pid] = load.ProcessInfo(pid, os.getpid(), 'python.exe', created, 0)
        self.guard.attach_worker(pid, created)
        return self.provider.live[pid]
    def child(self, pid, parent, cpu=0, created=None, name='regicide.exe'):
        root = self.provider.live[parent]
        process = load.ProcessInfo(pid, parent, name, created if created is not None else root.creation_ticks + 1, cpu)
        self.provider.live[pid] = process
        return process

    def test_idle_probe_logs_policy_raw_counters_and_full_host_window(self):
        self.idle()
        self.guard.assert_clean()
        self.guard.close()
        self.assertTrue(self.guard.summary['preflight_complete'])
        self.assertTrue(self.guard.summary['valid_for_load_check'])
        self.assertTrue(self.provider.closed)
        records = [json.loads(line) for line in self.guard.output.read_text().splitlines()]
        self.assertEqual(records[0]['event'], 'policy')
        self.assertEqual(records[0]['policy']['max_nonworker_cpu_fraction'], .2)
        self.assertEqual(records[1]['system']['kernel_ticks'], 1_000_000)
        host = [record['host'] for record in records if 'host' in record][-1]
        self.assertTrue(host['window_ready'])
        self.assertAlmostEqual(host['window_fraction'], .1)

    def test_four_worker_cohort_excludes_each_proven_descendant_once(self):
        self.idle()
        workers = [self.worker(pid) for pid in (100, 200, 300, 400)]
        self.provider.busy_fraction = .5
        for worker in workers:
            self.child(worker.pid + 1, worker.pid, 5000)
        record = self.advance()
        self.guard.assert_clean()
        self.assertEqual(record['host']['owned_delta_ticks'], 20_000)
        self.assertEqual(record['host']['nonworker_delta_ticks'], 5000)
        self.assertEqual(sum(row.get('owned', False) for row in record['processes']), 8)
        for worker in workers:
            self.provider.retire(worker.pid + 1)
            self.provider.retire(worker.pid)
        self.advance()
        self.guard.assert_clean()
        for worker in workers:
            self.guard.detach_worker(worker.pid)
        self.guard.close()
        self.assertTrue(self.guard.summary['valid_for_load_check'])

    def test_exited_root_and_child_final_cpu_are_read_before_detach(self):
        self.idle()
        worker = self.worker()
        self.child(101, worker.pid, 1000)
        self.advance()
        self.provider.retire(101, 6000)
        self.provider.retire(worker.pid, 100)
        self.provider.busy_fraction = .25
        record = self.advance()
        self.guard.assert_clean()
        self.assertEqual(record['host']['owned_delta_ticks'], 5100)
        self.assertTrue(all(row['exited'] for row in record['retained_owned']))
        self.assertNotIn((101, worker.creation_ticks + 1), self.provider.held)
        self.assertIn(worker.identity, self.provider.held)
        self.guard.detach_worker(worker.pid)
        self.assertNotIn(worker.identity, self.provider.held)
        self.guard.close()
        self.assertTrue(self.guard.summary['valid_for_load_check'])

    def test_many_completed_cohorts_keep_handles_and_repeated_frames_bounded(self):
        self.idle()
        for cohort in range(40):
            worker = self.worker(100 + cohort * 3)
            first = self.child(worker.pid + 1, worker.pid, 100)
            second = self.child(worker.pid + 2, first.pid, 100)
            self.advance()
            self.provider.retire(first.pid, 500)
            self.provider.retire(second.pid, 800)
            self.provider.retire(worker.pid, 10)
            final = self.advance()
            self.guard.assert_clean()
            self.assertEqual(final['host']['owned_delta_ticks'], 1110)
            self.assertEqual(len(final['retained_owned']), 3)
            self.assertEqual(set(self.provider.held), {worker.identity})
            self.guard.detach_worker(worker.pid)
            self.assertFalse(self.provider.held)
            empty = self.advance()
            self.assertEqual(empty['retained_owned'], [])
            self.assertEqual(empty['host']['owned_delta_ticks'], 0)
        self.assertEqual(len(self.provider.released), 120)
        self.assertEqual(len(self.guard.known_owned), 120)
        self.guard.close()
        self.assertTrue(self.guard.summary['valid_for_load_check'])

    def test_detach_requires_accounted_final_root_timer(self):
        self.idle()
        worker = self.worker()
        self.advance()
        self.provider.retire(worker.pid, 100)
        with self.assertRaises(load.LoadGuardViolation):
            self.guard.detach_worker(worker.pid)
        self.assertEqual(self.guard.stop_reason['kind'], 'worker_detach')
        self.assertIn(worker.pid, self.guard.roots)

    def test_reused_worker_pid_is_not_an_owned_identity(self):
        self.idle()
        worker = self.worker()
        self.advance()
        self.provider.retire(worker.pid)
        self.provider.live[worker.pid] = replace(worker, creation_ticks=worker.creation_ticks + 1000)
        self.advance()
        self.assertEqual(self.guard.stop_reason['kind'], 'worker_identity')

    def test_reused_parent_pid_does_not_own_an_older_engine(self):
        self.idle()
        worker = self.worker()
        self.child(101, worker.pid, created=worker.creation_ticks - 1)
        record = self.advance()
        engine = next(row for row in record['processes'] if row['pid'] == 101)
        self.assertFalse(engine['owned'])
        self.assertEqual(self.guard.stop_reason['kind'], 'external_identity')

    def test_stable_sleeping_external_engine_is_allowed_but_one_cpu_tick_is_not(self):
        external = load.ProcessInfo(999, 1, 'stockfish.exe', WALL - 1, 7)
        self.provider.live[999] = external
        self.idle()
        self.guard.assert_clean()
        self.provider.live[999] = replace(external, cpu_ticks=8)
        self.advance()
        self.assertEqual(self.guard.stop_reason['kind'], 'external_cpu')
        self.assertEqual(self.guard.summary['external_cpu_increase_ticks'], 1)
        with self.assertRaises(load.LoadGuardViolation):
            self.guard.assert_clean()

    def test_external_new_disappeared_reused_and_mid_snapshot_exit_fail_closed(self):
        for variant in ('new', 'disappeared', 'reused', 'exited'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                clock = Clock()
                provider = Provider(clock)
                external = load.ProcessInfo(999, 1, 'stockfish.exe', WALL - 1, 7)
                if variant != 'new':
                    provider.live[999] = external
                guard = load.M2LoadGuard(Path(temporary) / 'load.jsonl', load.LoadPolicy(.2), provider,
                                         clock=clock, sleep=clock.sleep)
                try:
                    guard.require_idle(2)
                    if variant == 'disappeared':
                        del provider.live[999]
                    else:
                        provider.live[999] = replace(external, creation_ticks=WALL + 1) if variant == 'reused' else replace(
                            external, exited_during_snapshot=variant == 'exited')
                    clock.sleep(.5)
                    guard.sample()
                    self.assertEqual(guard.stop_reason['kind'], 'external_identity')
                finally:
                    guard.close()

    def test_process_access_and_retained_timer_errors_fail_closed(self):
        self.provider.live[999] = load.ProcessInfo(999, 1, 'stockfish.exe', error='OpenProcess denied')
        self.guard.sample()
        self.assertEqual(self.guard.stop_reason['kind'], 'process_access')
        record = json.loads(self.guard.output.read_text().splitlines()[-1])
        self.assertEqual(record['processes'][0]['name'], 'stockfish.exe')
        self.assertEqual(record['processes'][0]['parent_pid'], 1)
        # The first failure cannot be erased by later clean snapshots.
        self.provider.live.clear()
        self.advance()
        self.assertEqual(self.guard.stop_reason['kind'], 'process_access')

    def test_retained_timer_error_cannot_hide_final_owned_cpu(self):
        self.idle()
        worker = self.worker()
        self.advance()
        self.provider.retire(worker.pid)
        self.provider.held[worker.identity] = replace(self.provider.held[worker.identity], error='timer denied')
        self.advance()
        self.assertEqual(self.guard.stop_reason['kind'], 'owned_timer')

    def test_snapshot_exception_and_excessive_sample_duration_fail_closed(self):
        self.provider.hook = Mock(side_effect=OSError('enumeration denied'))
        self.guard.sample()
        self.assertEqual(self.guard.stop_reason['kind'], 'monitor_error')
        self.assertIn('enumeration denied', self.guard.stop_reason['reason'])

    def test_gap_bound_and_slow_native_sample_are_both_enforced(self):
        self.guard.sample()
        self.advance(2.01)
        self.assertEqual(self.guard.stop_reason['kind'], 'sampling_gap')
        with tempfile.TemporaryDirectory() as temporary:
            clock, provider = Clock(), Provider(Clock())
            provider.clock = clock
            provider.hook = lambda frame: (clock.sleep(2.01), frame)[1]
            guard = load.M2LoadGuard(Path(temporary) / 'load.jsonl', load.LoadPolicy(.2), provider,
                                     clock=clock, sleep=clock.sleep)
            try:
                guard.sample()
                self.assertEqual(guard.stop_reason['kind'], 'sampling_gap')
            finally:
                guard.close()

    def test_host_threshold_uses_rolling_two_seconds_not_one_sample(self):
        self.idle()
        self.provider.busy_fraction = .3
        first = self.advance()
        self.guard.assert_clean()
        self.assertAlmostEqual(first['host']['window_fraction'], .15)
        self.advance()
        self.guard.assert_clean()
        third = self.advance()
        self.assertAlmostEqual(third['host']['window_fraction'], .25)
        self.assertEqual(self.guard.stop_reason['kind'], 'host_nonworker_cpu')

    def test_adjacent_quantized_zero_delta_samples_preserve_full_window_and_threshold(self):
        # Timer + main-loop samples 10ms apart may share GetSystemTimes ticks.
        # These short intervals add coverage, but add no denominator/numerator.
        for index in range(4):
            start = index * .5
            self.guard._host_window(start, start + .49, 49_000, 14_700, 0)
            record = self.guard._host_window(start + .49, start + .5, 0, 0, 0)
        self.assertTrue(record['window_ready'])
        self.assertAlmostEqual(record['window_seconds'], 2.)
        self.assertAlmostEqual(record['window_total_ticks'], 196_000)
        self.assertAlmostEqual(record['window_fraction'], .3)
        self.assertEqual(self.guard.stop_reason['kind'], 'host_nonworker_cpu')

    def test_old_process_first_seen_has_no_guessed_historical_cpu_subtraction(self):
        self.idle()
        worker = self.worker(created=WALL + 1)
        self.provider.live[worker.pid] = replace(worker, cpu_ticks=100_000)
        record = self.advance()
        self.assertEqual(record['host']['owned_delta_ticks'], 0)
        self.guard.assert_clean()
        self.provider.live[worker.pid] = replace(worker, cpu_ticks=100_010)
        record = self.advance()
        self.assertEqual(record['host']['owned_delta_ticks'], 10)

    def test_detached_live_descendant_is_not_hidden_as_free_worker_cpu(self):
        self.idle()
        worker = self.worker()
        self.child(101, worker.pid)
        self.advance()
        self.provider.retire(worker.pid)
        self.advance()
        self.guard.detach_worker(worker.pid)
        self.advance()
        self.assertEqual(self.guard.stop_reason['kind'], 'owned_orphan')

    def test_host_kernel_includes_idle_and_is_not_double_counted(self):
        frames = [load.LoadSnapshot({}, load.SystemTimes(1000, 1200, 100), WALL),
                  load.LoadSnapshot({}, load.SystemTimes(1900, 2400, 200), WALL + 500)]
        self.provider.hook = lambda _frame: frames.pop(0)
        self.guard.sample()
        record = self.advance()
        self.assertEqual(record['host']['total_delta_ticks'], 1300)
        self.assertEqual(record['host']['busy_delta_ticks'], 400)
        self.provider.hook = None

    def test_counter_regression_and_host_counter_stall_are_monitor_failures(self):
        self.idle()
        previous = self.guard.previous
        self.provider.hook = lambda frame: replace(frame, system=load.SystemTimes(0, 1, 0))
        self.advance()
        self.assertEqual(self.guard.stop_reason['kind'], 'monitor_error')
        self.assertIn('regressed', self.guard.stop_reason['reason'])
        with tempfile.TemporaryDirectory() as temporary:
            clock, provider = Clock(), Provider(Clock())
            provider.clock = clock
            provider.hook = lambda frame: replace(frame, system=previous.system)
            guard = load.M2LoadGuard(Path(temporary) / 'load.jsonl', load.LoadPolicy(.2), provider,
                                     clock=clock, sleep=clock.sleep)
            try:
                with self.assertRaises(load.LoadGuardViolation):
                    guard.require_idle(2)
                self.assertIn('did not advance', guard.stop_reason['reason'])
                self.assertFalse(guard.summary['preflight_complete'])
            finally:
                guard.close()

    def test_preflight_and_worker_registration_errors_cannot_claim_validity(self):
        with self.assertRaises(RuntimeError):
            self.worker()
        self.idle()
        with self.assertRaises(load.LoadGuardViolation):
            self.guard.attach_worker(200, WALL)
        self.assertEqual(self.guard.stop_reason['kind'], 'worker_registration')
        self.guard.close()
        self.assertFalse(self.guard.summary['valid_for_load_check'])

    def test_close_with_an_active_worker_is_not_a_valid_load_receipt(self):
        self.idle()
        self.worker()
        self.guard.close()
        self.assertFalse(self.guard.summary['valid_for_load_check'])
        self.assertEqual(self.guard.stop_reason['kind'], 'active_workers_on_close')

    def test_policy_and_platform_require_explicit_supported_conditions(self):
        for kwargs in ({'max_nonworker_cpu_fraction': float('nan')}, {'max_nonworker_cpu_fraction': True},
                       {'max_nonworker_cpu_fraction': .2, 'interval_seconds': .1},
                       {'max_nonworker_cpu_fraction': .2, 'host_window_seconds': 0},
                       {'max_nonworker_cpu_fraction': .2, 'max_sample_gap_seconds': .1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                load.LoadPolicy(**kwargs)
        with patch('see_clean_guard.sys.platform', 'linux'):
            with self.assertRaises(RuntimeError):
                load.M2LoadGuard(self.folder / 'unsupported.jsonl', load.LoadPolicy(.2))
        self.assertFalse((self.folder / 'unsupported.jsonl').exists())
        names, pids = ['regicide.exe'], [123]
        policy = load.LoadPolicy(.2, engine_names=names, extra_engine_pids=pids)
        names.clear()
        pids.clear()
        self.assertEqual(policy.engine_names, ('regicide.exe',))
        self.assertEqual(policy.extra_engine_pids, (123,))

    def test_receipt_io_failure_is_sticky_and_old_outputs_are_not_overwritten(self):
        with self.assertRaises(FileExistsError):
            load.M2LoadGuard(self.guard.output, load.LoadPolicy(.2), self.provider)
        stream = self.guard.stream
        self.guard.stream = Mock()
        self.guard.stream.write.side_effect = OSError('disk full')
        self.guard.sample()
        self.assertEqual(self.guard.stop_reason['kind'], 'receipt_error')
        self.guard.stream = stream

    def test_native_root_registration_requires_the_actual_monitor_parent(self):
        provider = load.WindowsLoadProvider.__new__(load.WindowsLoadProvider)
        worker = load.ProcessInfo(100, os.getpid() + 1, 'python.exe', WALL, 0)
        provider.native = Mock()
        provider.native.snapshot.return_value = {100: worker}
        provider.native.times.return_value = worker
        provider._retain = Mock()
        with self.assertRaisesRegex(RuntimeError, 'direct subprocess'):
            provider.watch_root(worker.identity)
        provider._retain.assert_not_called()

    def native_provider(self):
        provider = load.WindowsLoadProvider.__new__(load.WindowsLoadProvider)
        provider.native = Mock()
        provider.owned_handles, provider.retired_handles = {}, {}
        provider.retirement_events = []
        inventory, timers = {}, {}
        provider.native.snapshot.side_effect = lambda: dict(inventory)
        provider.native.times.side_effect = lambda process: inventory[process.pid]
        provider.native._handle_times.side_effect = lambda handle, _process: timers[handle]
        provider.native.kernel.CloseHandle.return_value = True
        provider.native.kernel.GetSystemTimes.return_value = True
        provider.native.engine_handles = {}
        return provider, inventory, timers

    def test_native_retirement_waits_two_absent_inventories_and_closes_matching_cache_only(self):
        provider, inventory, timers = self.native_provider()
        old = load.ProcessInfo(100, os.getpid(), 'regicide.exe', WALL, 123, exited_during_snapshot=True)
        reused = replace(old, creation_ticks=WALL + 100, exited_during_snapshot=False)
        provider.owned_handles = {old.identity: (10, old)}
        provider.native.engine_handles = {100: (20, reused)}
        timers[10] = old
        provider.release_finished((old.identity,))
        provider.native.kernel.CloseHandle.assert_not_called()
        self.assertFalse(provider.owned_handles)
        provider.snapshot(())
        provider.native.kernel.CloseHandle.assert_not_called()
        provider.snapshot(())
        provider.native.kernel.CloseHandle.assert_called_once_with(10)
        self.assertEqual(provider.native.engine_handles, {100: (20, reused)})
        provider.owned_handles[old.identity] = 30, old
        timers[30] = old
        provider.native.engine_handles = {100: (40, old)}
        provider.release_finished((old.identity,))
        provider.snapshot(())
        provider.snapshot(())
        self.assertFalse(provider.native.engine_handles)
        self.assertFalse(provider.owned_handles)
        self.assertFalse(provider.retired_handles)
        self.assertEqual([call.args[0] for call in provider.native.kernel.CloseHandle.call_args_list], [10, 40, 30])
        provider.owned_handles[reused.identity] = 50, reused
        timers[50] = reused
        with self.assertRaisesRegex(RuntimeError, 'live owned'):
            provider.release_finished((reused.identity,))
        self.assertIn(reused.identity, provider.owned_handles)

    def test_native_stale_access_denied_is_recovered_only_with_held_exact_exited_proof(self):
        provider, inventory, timers = self.native_provider()
        final = load.ProcessInfo(100, 90, 'regicide.exe', WALL, 123, exited_during_snapshot=True)
        provider.owned_handles[final.identity] = 10, final
        provider.native.engine_handles[100] = 20, final
        timers[10] = final
        provider.release_finished((final.identity,))
        inventory[100] = load.ProcessInfo(100, 90, 'regicide.exe', error='OpenProcess error 5')
        for _ in range(3):
            frame = provider.snapshot(())
            self.assertEqual(frame.processes[100], final)
            self.assertEqual(frame.retained_owned, ())
            self.assertEqual(frame.retired_handle_count, 1)
            recovery = next(event for event in frame.retirement_events if event['event'] == 'stale_owned_entry')
            self.assertEqual(recovery['identity'], final.identity)
            self.assertEqual(recovery['original_error'], 'OpenProcess error 5')
            provider.release_finished((final.identity,))
            provider.native.kernel.CloseHandle.assert_not_called()
        inventory.clear()
        provider.snapshot(())
        self.assertIn(final.identity, provider.retired_handles)
        frame = provider.snapshot(())
        self.assertFalse(provider.retired_handles)
        self.assertEqual(frame.retired_handle_count, 0)
        self.assertIn('retired_timer_closed', [event['event'] for event in frame.retirement_events])

    def test_full_guard_repeated_retired_stale_rows_keep_zero_delta_and_validity(self):
        self.idle()
        provider, inventory, timers = self.native_provider()
        # Exercise the real provider plus complete M2LoadGuard accounting and
        # post-persist retirement callback; only native OS I/O is mocked.
        original = self.provider
        def host_times(idle, kernel, user):
            source = original.snapshot(()).system
            for pointer, value in ((idle, source.idle_ticks), (kernel, source.kernel_ticks),
                                   (user, source.user_ticks)):
                pointer._obj.dwLowDateTime = value & 0xffffffff
                pointer._obj.dwHighDateTime = value >> 32
            return True
        provider.native.kernel.GetSystemTimes.side_effect = host_times
        provider.native.kernel.OpenProcess.side_effect = lambda _access, _inherit, pid: {100: 10, 101: 20}[pid]
        root = load.ProcessInfo(100, os.getpid(), 'python.exe', WALL + 2001, 0)
        child = load.ProcessInfo(101, 100, 'regicide.exe', WALL + 2002, 100)
        inventory.update({100: root, 101: child})
        timers.update({10: root, 20: child})
        self.guard.provider = provider
        self.guard.attach_worker(*root.identity)
        self.advance()
        final = replace(child, cpu_ticks=400, exited_during_snapshot=True)
        timers[20] = final
        inventory[101] = final
        provider.native.engine_handles[101] = 30, final
        record = self.advance()
        self.guard.assert_clean()
        self.assertEqual(record['host']['owned_delta_ticks'], 300)
        self.assertIn(child.identity, provider.retired_handles)
        inventory[101] = load.ProcessInfo(101, 100, 'regicide.exe', error='OpenProcess error 5')
        for _ in range(3):
            record = self.advance()
            self.guard.assert_clean()
            self.assertEqual(record['host']['owned_delta_ticks'], 0)
            row = next(row for row in record['processes'] if row['pid'] == 101)
            self.assertTrue(row['owned'])
            self.assertTrue(row['exited'])
            self.assertEqual(row['cpu_ticks'], 400)
            self.assertIn(child.identity, provider.retired_handles)
        del inventory[101]
        self.advance()
        self.advance()
        self.guard.assert_clean()
        self.assertNotIn(child.identity, provider.retired_handles)
        final_root = replace(root, cpu_ticks=10, exited_during_snapshot=True)
        inventory[100] = timers[10] = final_root
        self.advance()
        self.guard.detach_worker(100)
        self.guard.close()
        self.assertTrue(self.guard.summary['valid_for_load_check'])

    def test_repeated_retirement_still_rejects_a_changed_final_timer(self):
        provider, _inventory, timers = self.native_provider()
        final = load.ProcessInfo(100, 90, 'regicide.exe', WALL, 123, exited_during_snapshot=True)
        provider.owned_handles[final.identity] = 10, final
        timers[10] = final
        provider.release_finished((final.identity,))
        timers[10] = replace(final, cpu_ticks=124)
        with self.assertRaisesRegex(RuntimeError, 'repeated retirement'):
            provider.release_finished((final.identity,))

    def test_native_live_or_unproved_access_denied_stays_a_failure(self):
        provider, inventory, timers = self.native_provider()
        live = load.ProcessInfo(100, 90, 'regicide.exe', WALL, 123)
        denied = load.ProcessInfo(100, 90, 'regicide.exe', error='OpenProcess error 5')
        inventory[100] = denied
        provider.owned_handles[live.identity] = 10, live
        timers[10] = live
        with self.assertRaisesRegex(RuntimeError, 'held-exited proof'):
            provider.snapshot(())
        provider.owned_handles.clear()
        self.assertEqual(provider.snapshot(()).processes[100], denied)
        provider.native.kernel.CloseHandle.assert_not_called()

    def test_native_first_final_timer_can_recover_stale_entry_before_retirement(self):
        provider, inventory, timers = self.native_provider()
        live = load.ProcessInfo(100, 90, 'regicide.exe', WALL, 10)
        final = replace(live, cpu_ticks=123, exited_during_snapshot=True)
        provider.owned_handles[live.identity] = 10, live
        timers[10] = final
        inventory[100] = load.ProcessInfo(100, 90, 'regicide.exe', error='OpenProcess error 5')
        frame = provider.snapshot(())
        self.assertEqual(frame.processes[100], final)
        self.assertEqual(frame.retained_owned, (final,))
        provider.release_finished((final.identity,))
        self.assertIn(final.identity, provider.retired_handles)

    def test_native_disappeared_retired_pid_can_later_be_a_fresh_unowned_identity(self):
        provider, inventory, timers = self.native_provider()
        old = load.ProcessInfo(100, 90, 'regicide.exe', WALL, 123, exited_during_snapshot=True)
        provider.owned_handles[old.identity] = 10, old
        provider.native.engine_handles[100] = 20, old
        timers[10] = old
        provider.release_finished((old.identity,))
        provider.snapshot(())
        provider.snapshot(())
        fresh = replace(old, creation_ticks=WALL + 100, cpu_ticks=0, exited_during_snapshot=False)
        inventory[100] = fresh
        frame = provider.snapshot(())
        self.assertEqual(frame.processes[100].identity, fresh.identity)
        self.assertNotEqual(frame.processes[100].identity, old.identity)
        self.assertFalse(frame.retained_owned)
        self.assertFalse(frame.retirement_events)

    def test_native_retired_identity_conflict_cannot_reuse_a_still_held_pid(self):
        provider, inventory, timers = self.native_provider()
        final = load.ProcessInfo(100, 90, 'python.exe', WALL, 123, exited_during_snapshot=True)
        provider.owned_handles[final.identity] = 10, final
        timers[10] = final
        provider.release_finished((final.identity,))
        inventory[100] = replace(final, creation_ticks=WALL + 1, exited_during_snapshot=False)
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            provider.snapshot(())

    def test_native_many_completed_tombstones_do_not_accumulate_after_absence(self):
        provider, _inventory, timers = self.native_provider()
        for index in range(80):
            final = load.ProcessInfo(100 + index, 90, 'regicide.exe', WALL + index, 123,
                                      exited_during_snapshot=True)
            handle = 1000 + index
            timers[handle] = final
            provider.owned_handles[final.identity] = handle, final
            provider.release_finished((final.identity,))
            first = provider.snapshot(())
            self.assertEqual(first.retired_handle_count, 1)
            self.assertFalse(first.retained_owned)
            second = provider.snapshot(())
            self.assertEqual(second.retired_handle_count, 0)
            self.assertFalse(second.retained_owned)
        self.assertEqual(provider.native.kernel.CloseHandle.call_count, 80)


class GuardMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='regicide-m2-timer-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def monitor(self, *, full_window=False, gap=2.):
        provider = Provider(time.monotonic)
        policy = load.LoadPolicy(.2, host_window_seconds=2. if full_window else .5,
                                 max_sample_gap_seconds=gap)
        monitor = load.GuardMonitor(self.folder / 'load.jsonl', policy, provider)
        self.addCleanup(monitor.close)
        monitor.require_idle(policy.host_window_seconds)
        return monitor, provider

    def test_timer_continues_while_runner_does_no_poll_for_more_than_gap_bound(self):
        monitor, _provider = self.monitor(full_window=True)
        monitor.start_timer()
        time.sleep(2.3)  # Simulate runner blocked in hashing/auditing; no sample calls.
        monitor.assert_clean()
        monitor.close()
        summary = monitor.summary
        self.assertTrue(summary['valid_for_load_check'])
        self.assertGreaterEqual(summary['timer']['samples'], 4)
        self.assertFalse(summary['timer']['alive'])
        self.assertLess(summary['max_sample_gap_seconds'], 2.)
        records = [json.loads(line) for line in monitor.output.read_text().splitlines()]
        self.assertGreaterEqual(sum(row.get('phase') == 'timer' for row in records), 4)
        self.assertEqual(sum(row['event'] == 'timer_stopped' for row in records), 1)

    def test_live_sampler_query_error_is_sticky_and_invalidates_close(self):
        monitor, provider = self.monitor()
        provider.hook = Mock(side_effect=OSError('live timer access denied'))
        monitor.start_timer()
        monitor._thread.join(timeout=2.)
        with self.assertRaises(load.LoadGuardViolation):
            monitor.assert_clean()
        monitor.close()
        self.assertFalse(monitor.summary['valid_for_load_check'])
        self.assertEqual(monitor.stop_reason['kind'], 'monitor_error')
        self.assertFalse(monitor.summary['timer']['alive'])
        self.assertIn('live timer access denied', monitor.stop_reason['reason'])

    def test_start_failure_and_unstarted_timer_cannot_claim_validity(self):
        monitor, _provider = self.monitor()
        with patch.object(load.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('no thread resource')
            with self.assertRaises(load.LoadGuardViolation):
                monitor.start_timer()
        monitor.close()
        self.assertEqual(monitor.stop_reason['kind'], 'monitor_thread_start')
        self.assertFalse(monitor.summary['valid_for_load_check'])

    def test_stuck_native_sample_has_bounded_close_and_deferred_handle_cleanup(self):
        monitor, provider = self.monitor(gap=1.)
        entered, release = threading.Event(), threading.Event()
        def blocked(frame):
            entered.set()
            release.wait(timeout=3.)
            return frame
        provider.hook = blocked
        monitor.start_timer()
        self.assertTrue(entered.wait(timeout=2.))
        started = time.monotonic()
        monitor.close()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(provider.closed)
        self.assertFalse(monitor.summary['valid_for_load_check'])
        self.assertEqual(monitor.summary['timer']['failure']['kind'], 'monitor_thread_stop')
        with self.assertRaises(load.LoadGuardViolation):
            monitor.assert_clean()
        release.set()
        monitor._thread.join(timeout=2.)
        self.assertFalse(monitor._thread.is_alive())
        self.assertTrue(provider.closed)
        self.assertFalse(monitor.summary['valid_for_load_check'])

    def test_provider_close_failure_remains_invalid_with_timer_stopped(self):
        monitor, provider = self.monitor()
        provider.close = Mock(side_effect=OSError('close denied'))
        monitor.start_timer()
        monitor.close()
        self.assertFalse(monitor.summary['valid_for_load_check'])
        self.assertEqual(monitor.stop_reason['kind'], 'monitor_close')
        self.assertFalse(monitor.summary['timer']['alive'])

    def test_main_requested_native_sample_has_bounded_response_and_safe_cleanup(self):
        monitor, provider = self.monitor(gap=1.)
        entered, release = threading.Event(), threading.Event()
        called_by = []
        def blocked(frame):
            called_by.append(threading.current_thread().name)
            entered.set()
            release.wait(timeout=4.)
            return frame
        provider.hook = blocked
        monitor.start_timer()
        started = time.monotonic()
        with self.assertRaises(load.LoadGuardViolation):
            monitor.sample('main-request-before-first-timer')
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(entered.is_set())
        self.assertEqual(called_by, ['M2LoadGuard'])
        self.assertEqual(monitor.stop_reason['kind'], 'monitor_request_timeout')
        monitor.close()
        self.assertFalse(provider.closed)
        self.assertFalse(monitor.summary['valid_for_load_check'])
        release.set()
        monitor._thread.join(timeout=2.)
        self.assertFalse(monitor._thread.is_alive())
        self.assertTrue(provider.closed)

    def test_registration_waits_for_inflight_sample_and_final_detach_stays_serial(self):
        monitor, provider = self.monitor()
        entered, release, registered = (threading.Event() for _ in range(3))
        def blocked(frame):
            entered.set()
            release.wait(timeout=2.)
            return frame
        provider.hook = blocked
        monitor.start_timer()
        self.assertTrue(entered.wait(timeout=2.))
        worker = load.ProcessInfo(100, os.getpid(), 'python.exe', WALL + round(time.monotonic()*1000) + 1, 0)
        provider.live[100] = worker
        errors = []
        def attach():
            try:
                monitor.attach_worker(*worker.identity)
                registered.set()
            except Exception as error:
                errors.append(error)
        registrar = threading.Thread(target=attach)
        registrar.start()
        self.assertFalse(registered.wait(timeout=.05))
        release.set()
        registrar.join(timeout=2.)
        self.assertTrue(registered.is_set())
        self.assertFalse(errors)
        provider.hook = None
        monitor.sample('root-live')
        provider.retire(worker.pid, 10)
        monitor.sample('root-final')
        monitor.detach_worker(worker.pid)
        monitor.close()
        self.assertTrue(monitor.summary['valid_for_load_check'])

    def test_constructor_failure_never_starts_a_thread(self):
        with patch.object(load, 'M2LoadGuard', side_effect=OSError('initial access denied')), \
             patch.object(load.threading, 'Thread') as thread:
            with self.assertRaisesRegex(OSError, 'initial access denied'):
                load.GuardMonitor(self.folder / 'load.jsonl', load.LoadPolicy(.2))
            thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
