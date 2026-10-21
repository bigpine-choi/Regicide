"""Warning-only policy and independent replay with synthetic counters/clocks."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import os
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests')]
import m2_warning_load_guard_v2 as load
import m2_warning_receipts_v2 as receipts
from m2_job_receipts_tests import NativeFrames as _NativeFrames
from m2_load_guard_tests import Clock
from see_clean_guard import ProcessInfo


class NativeFrames(_NativeFrames):
    """Synthetic explicit-Job discovery, independent of real process APIs."""
    def __init__(self, clock):
        super().__init__(clock)
        self.cohort_pids=set()
        self.process_ownership_scope=None
        self.verified_members=set()

    def is_owned_member(self, process):
        return process.identity in self.verified_members

    def snapshot(self, roots):
        self.process_ownership_scope=None
        all_live=dict(self.live)
        active={pid for pid in self.cohort_pids if pid in all_live and not all_live[pid].exited_during_snapshot}
        held_exited={p.identity:p for p in self.held.values() if p.exited_during_snapshot}
        selected={pid:p for pid,p in all_live.items() if pid in active or self.is_engine(p) or p.identity in held_exited}
        self.live=selected
        try:
            frame=super().snapshot(roots)
        finally:
            self.live=all_live
        self.held={key:p for key,p in self.held.items() if p.pid in active or key in held_exited}
        frame=replace(frame,retained_owned=tuple(self.held.values()))
        self.verified_members={p.identity for p in selected.values() if p.pid in active or p.identity in held_exited}
        def raw(p):
            return dict(pid=p.pid,parent_pid=p.parent_pid,name=p.name,creation_ticks=p.creation_ticks,
                cpu_ticks=p.cpu_ticks,error=p.error,exited_during_snapshot=p.exited_during_snapshot)
        def job():
            pids=sorted(active)
            query=dict(success=True,windows_error=0,buffer_capacity=64,buffer_bytes=8+64*8,
                returned_bytes=8+8*len(pids),assigned_processes=len(pids),listed_processes=len(pids),pids=pids)
            return dict(job_handle_kind='explicit-owned-job-handle',coordinator_pid=os.getpid(),coordinator_in_job=False,
                limit_flags=0x2000,limits_returned_bytes=144,assigned_processes=len(pids),listed_processes=len(pids),
                returned_bytes=8+8*len(pids),buffer_capacity=64,pointer_bytes=8,pids=pids,query_attempts=[query],
                read_started_monotonic=self.clock(),read_finished_monotonic=self.clock())
        reasons=[]; queries=[]
        for pid,p in sorted(selected.items()):
            reason=('current-job-member' if pid in active else 'known-held-owned-exited' if p.identity in held_exited
                    else 'declared-external-engine')
            reasons.append(dict(pid=pid,reason=reason,creation_ticks=p.creation_ticks))
            if reason != 'known-held-owned-exited':
                queries.append(dict(pid=pid,reason=reason,result=raw(p),in_owned_job_at_read=pid in active))
        globals=[dict(pid=p.pid,parent_pid=p.parent_pid,name=p.name,creation_ticks=None,cpu_ticks=None,error=None,
                     exited_during_snapshot=False) for p in all_live.values()]
        excluded=sorted(set(all_live)-set(selected))
        possible={identity[0] for identity in roots}
        for _ in all_live:
            added={pid for pid,p in all_live.items() if p.parent_pid in possible}-possible
            if not added:break
            possible.update(added)
        recovered=[dict(identity=list(identity),final=raw(p),proof='exact-held-exited-process')
                   for identity,p in held_exited.items() if p.pid in selected]
        self.process_ownership_scope=dict(schema_version=1,method=load.OWNERSHIP_METHOD,status='COMPLETE',
            operation='snapshot',phase='complete',offending_pid=None,
            coordinator_pid=os.getpid(),roots=[list(root) for root in roots],job_before=job(),job_after=job(),
            global_inventory=globals,global_inventory_attempts=[globals],selected_processes=[raw(p) for p in selected.values()],
            selection_reasons=reasons,queries=queries,held_exit_recoveries=recovered,
            retained_owned=[raw(p) for p in self.held.values()],excluded_nonengine_pids=excluded,
            outside_parent_matches=[dict(pid=pid,parent_pid=all_live[pid].parent_pid,name=all_live[pid].name,
                reason='outside-owned-job-nonengine-parent-match') for pid in excluded if pid in possible],
            missing_job_pids_with_held_final=[],unobserved_after_only_job_pids=[],unobserved_before_only_job_pids=[])
        return frame


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


def make_owned_fixture():
    """A registered cohort, stale global PPID links and retained child/root exits."""
    with tempfile.TemporaryDirectory() as directory:
        clock, steps = Clock(), 0
        provider = NativeFrames(clock)
        provider.live[991] = ProcessInfo(991, 100, 'stockfish.exe', 100002, 0)
        guard = load.WarningLoadGuard(Path(directory)/'load.jsonl', load.LoadPolicy(.2), None,
            provider, clock=clock, sleep=clock.sleep)
        guard.require_idle(5.)
        guard._write(dict(event='timer_started', interval_seconds=.5, join_bound_seconds=2.))
        provider.live[100] = ProcessInfo(100, os.getpid(), 'python.exe', 100000, 0)
        provider.cohort_pids.add(100)
        guard.attach_worker(100, 100000)
        provider.live[101] = ProcessInfo(101, 100, 'python.exe', 100001, 0)
        provider.cohort_pids.add(101)
        provider.live[777] = ProcessInfo(777, 101, 'csrss.exe', None, None, 'protected: 5')
        provider.live[778] = ProcessInfo(778, 777, 'wininit.exe', None, None, 'protected: 5')
        for _ in range(2):
            clock.sleep(.5); guard.sample('timer'); steps += 1
        provider.retire(101, cpu=50)
        # Toolhelp may still enumerate an exact exited process whose held timer
        # remains available. That path records a recovery, not a fresh query.
        provider.live[101] = provider.held[(101,100001)]
        clock.sleep(.5); guard.sample('timer'); steps += 1
        provider.live.pop(101)
        provider.retire(100, cpu=100)
        clock.sleep(.5); guard.sample('timer'); steps += 1
        guard.detach_worker(100)
        guard._write(dict(event='timer_stopped', timer_samples=steps))
        guard.close()
        summary = deepcopy(guard.summary)
        summary['timer'] = dict(started=True, samples=steps, alive=False, failure=None, join_bound_seconds=2.)
        rows = [json.loads(line) for line in guard.output.read_text().splitlines()]
        frames = [row['native_accounting'] for row in rows if row.get('native_accounting')]
        return tuple(json.loads(json.dumps((rows, summary, frames[-1]['job_after'],
            dict(asdict(guard.policy), idle_probe_seconds=5.)))))


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

    def worker(self, pid=100):
        if not self.guard.summary['preflight_complete']:
            self.guard.require_idle(2.)
        self.provider.live[pid] = ProcessInfo(pid, os.getpid(), 'python.exe', 101000, 0)
        self.provider.cohort_pids.add(pid)
        self.guard.attach_worker(pid, 101000)
        return self.provider.live[pid]

    def test_stale_global_nonengine_parent_link_is_never_measured_or_owned(self):
        self.worker()
        self.provider.live[101] = ProcessInfo(101, 100, 'python.exe', 101001, 0)
        self.provider.cohort_pids.add(101)
        # PID 101 is a new launcher; this older protected process remembers a
        # prior holder of the same numeric PID. Access would fail if queried.
        self.provider.live[777] = ProcessInfo(777, 101, 'csrss.exe', None, None, 'access denied: 5')
        self.provider.live[778] = ProcessInfo(778, 777, 'wininit.exe', None, None, 'access denied: 5')
        row = self.step()
        self.guard.assert_clean()
        self.assertEqual({p['pid'] for p in row['processes']}, {100, 101})
        scope = row['process_ownership_scope']
        self.assertEqual({p['pid'] for p in scope['queries']}, {100, 101})
        self.assertEqual(scope['excluded_nonengine_pids'], [777, 778])
        self.assertEqual({p['pid'] for p in scope['outside_parent_matches']}, {777, 778})

    def test_external_engine_matching_owned_parent_remains_external_and_fatal(self):
        clock = Clock()
        provider = NativeFrames(clock)
        provider.live[991] = ProcessInfo(991, 100, 'stockfish.exe', 101002, 0)
        guard = load.WarningLoadGuard(Path(self.temporary.name)/'external-parent.jsonl', load.LoadPolicy(.2),
            None, provider, clock=clock, sleep=clock.sleep)
        self.addCleanup(guard.close)
        guard.require_idle(2.)
        provider.live[100] = ProcessInfo(100, os.getpid(), 'python.exe', 101000, 0)
        provider.cohort_pids.add(100)
        guard.attach_worker(100, 101000)
        clock.sleep(.5)
        baseline = guard.sample()
        external = next(p for p in baseline['processes'] if p['pid'] == 991)
        self.assertFalse(external['job_scope_member'])
        self.assertFalse(external['owned'])
        # The first baseline established the external identity; activity now
        # remains fatal even though its parent number names our live worker.
        provider.live[991] = replace(provider.live[991], cpu_ticks=1)
        clock.sleep(.5)
        guard.sample()
        self.assertEqual(guard.stop_reason['kind'], 'external_cpu')

    def test_pending_job_member_before_attach_is_not_a_new_orphan_error(self):
        self.provider.live[123] = ProcessInfo(123, os.getpid(), 'python.exe', 101000, 0)
        self.provider.cohort_pids.add(123)
        row = self.step()
        self.guard.assert_clean()
        self.assertEqual(row['processes'], [])
        self.assertEqual(row['process_ownership_scope']['job_before']['pids'], [123])

    def test_incomplete_scoped_provider_fails_closed_without_advancing_count(self):
        original = self.provider.snapshot
        def failed(roots):
            frame = original(roots)
            self.provider.process_ownership_scope['status'] = 'FAILED'
            return frame
        self.provider.snapshot = failed
        count = self.guard.summary['process_ownership_scope_count']
        row = self.step()
        self.assertEqual(self.guard.stop_reason['kind'], 'monitor_error')
        self.assertFalse(row['accounting_baseline_accepted'])
        self.assertIsNone(row['process_ownership_scope_index'])
        self.assertEqual(self.guard.summary['process_ownership_scope_count'], count)

    def test_live_known_owned_orphan_still_stops_after_worker_detach(self):
        self.worker()
        self.provider.live[101] = ProcessInfo(101, 100, 'python.exe', 101001, 0)
        self.provider.cohort_pids.add(101)
        self.step()
        self.provider.retire(100, cpu=10)
        self.step()
        self.guard.detach_worker(100)
        self.step()
        self.assertEqual(self.guard.stop_reason['kind'], 'owned_orphan')

    def test_current_job_member_access_failure_is_still_fatal(self):
        self.worker()
        self.provider.live[101] = ProcessInfo(101, 100, 'python.exe', None, None, 'access denied: 5')
        self.provider.cohort_pids.add(101)
        self.step()
        self.assertEqual(self.guard.stop_reason['kind'], 'process_access')

    def test_header_and_summary_have_distinct_new_policy_schema(self):
        self.guard.close()
        header = json.loads(self.guard.output.read_text().splitlines()[0])
        for record in (header, self.guard.summary):
            self.assertEqual(record['schema_version'], 4)
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
        self.rows[0]['schema_version'] = 4
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
            process = dict(pid=991, parent_pid=1, name='stockfish.exe', creation_ticks=100,
                cpu_ticks=index, error=None, exited_during_snapshot=False)
            raw = dict(process, creation_ticks=None, cpu_ticks=None)
            scope = row['process_ownership_scope']
            scope['global_inventory'].append(raw)
            scope['global_inventory_attempts'][0].append(deepcopy(raw))
            scope['selected_processes'].append(process)
            scope['selection_reasons'].append(dict(pid=991, creation_ticks=100, reason='declared-external-engine'))
            scope['queries'].append(dict(pid=991, reason='declared-external-engine', result=process,
                                         in_owned_job_at_read=False))
            row['processes'].append(dict(pid=991, parent_pid=1, creation_ticks=100, cpu_ticks=index,
                owned=False, name='stockfish.exe', job_scope_member=False, worker_root=None,
                ancestry=None, exited=False))
        self.summary['external_cpu_increase_ticks'] = len(self.samples())-1
        with self.assertRaisesRegex(ValueError, 'clean summary contradicts'):
            self.audit()
        self.summary['valid_for_load_check'] = False
        self.summary['failure'] = dict(kind='external_cpu', reason='synthetic known engine activity')
        self.assertEqual(self.audit()['status'], 'INVALID')

    def test_scope_count_index_method_and_missing_scope_tampering_rejected(self):
        self.summary['process_ownership_scope_count'] += 1
        with self.assertRaisesRegex(ValueError, 'scope_count'):
            self.audit()
        self.summary['process_ownership_scope_count'] -= 1
        original = deepcopy(self.rows)
        for field in ('process_ownership_scope', 'process_ownership_scope_index', 'process_ownership_method'):
            self.rows = deepcopy(original)
            self.samples()[2].pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.audit()

    def test_incomplete_job_lists_and_scope_projection_tampering_rejected(self):
        original = deepcopy(self.rows)
        mutations = (
            lambda s: s['job_before'].__setitem__('assigned_processes', 1),
            lambda s: s['job_after'].__setitem__('coordinator_in_job', True),
            lambda s: s['job_before'].__setitem__('limit_flags', 0x1800),
            lambda s: s['excluded_nonengine_pids'].append(1234),
            lambda s: s.__setitem__('global_inventory_attempts', []),
            lambda s: s['outside_parent_matches'].append(dict(pid=1234)),
        )
        for index, mutate in enumerate(mutations):
            self.rows = deepcopy(original)
            mutate(self.samples()[2]['process_ownership_scope'])
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'process ownership scope'):
                self.audit()

    def test_unrelated_process_cpu_query_cannot_be_resealed_clean(self):
        scope = self.samples()[2]['process_ownership_scope']
        raw = dict(pid=778, parent_pid=777, name='csrss.exe', creation_ticks=None,
            cpu_ticks=None, error=None, exited_during_snapshot=False)
        scope['global_inventory'].append(raw)
        scope['global_inventory_attempts'][0].append(deepcopy(raw))
        scope['queries'].append(dict(pid=778, reason='current-job-member',
            result=dict(raw, creation_ticks=101, cpu_ticks=0), in_owned_job_at_read=True))
        with self.assertRaisesRegex(ValueError, 'CPU queried an excluded'):
            self.audit()

    def test_real_producer_owned_tree_and_stale_parent_edges_replay(self):
        self.rows, self.summary, self.final_job, self.policy = make_owned_fixture()
        result = self.audit()
        self.assertEqual(result['status'], 'PASS')
        scoped = next(row for row in self.samples() if row['roots'])
        self.assertEqual({p['pid'] for p in scoped['processes'] if p['owned']}, {100, 101})
        self.assertEqual({p['pid'] for p in scoped['processes'] if not p['owned']}, {991})
        self.assertEqual(scoped['process_ownership_scope']['excluded_nonengine_pids'], [777, 778])
        exited = next(row['process_ownership_scope'] for row in self.samples()
                      if row['process_ownership_scope']['held_exit_recoveries'])
        self.assertEqual(exited['held_exit_recoveries'][0]['identity'], [101,100001])
        self.assertNotIn(101, {q['pid'] for q in exited['queries']})

    def test_forged_ownership_or_removed_named_engine_is_rejected(self):
        self.rows, self.summary, self.final_job, self.policy = make_owned_fixture()
        original = deepcopy(self.rows)
        scoped = next(row for row in self.samples() if row['roots'])
        external = next(p for p in scoped['processes'] if p['pid'] == 991)
        external.update(owned=True, job_scope_member=True, worker_root=[100,100000], ancestry=[991,100])
        with self.assertRaisesRegex(ValueError, 'classification/projection'):
            self.audit()
        self.rows = deepcopy(original)
        scoped = next(row for row in self.samples() if row['roots'])
        scope = scoped['process_ownership_scope']
        for key in ('selected_processes', 'selection_reasons', 'queries'):
            scope[key] = [p for p in scope[key] if p['pid'] != 991]
        scoped['processes'] = [p for p in scoped['processes'] if p['pid'] != 991]
        with self.assertRaisesRegex(ValueError, 'declared engine omitted'):
            self.audit()

    def test_asynchronous_unobserved_birth_and_exit_are_recorded_without_identity_adoption(self):
        for before, after, field, reason in (
            ([], [123], 'unobserved_after_only_job_pids', 'born-after-inventory-identity-not-adopted'),
            ([123], [], 'unobserved_before_only_job_pids', 'ended-between-inventories-identity-not-adopted')):
            self.rows, self.summary, self.final_job, self.policy = make_warning_fixture()
            row = self.samples()[2]; scope = row['process_ownership_scope']
            for key, pids in (('job_before', before), ('job_after', after)):
                job = scope[key]
                job.update(pids=pids, assigned_processes=len(pids), listed_processes=len(pids),
                           returned_bytes=8+8*len(pids))
                job['query_attempts'][-1].update(pids=pids, assigned_processes=len(pids),
                    listed_processes=len(pids), returned_bytes=8+8*len(pids))
            scope['global_inventory_attempts'] = [[], []]
            scope[field] = [dict(pid=123, in_job_before=bool(before), in_job_after=bool(after), reason=reason)]
            with self.subTest(field=field):
                self.assertEqual(self.audit()['status'], 'PASS')
                self.assertEqual(row['processes'], [])
            # A PID in both complete Job lists may not be dismissed as a
            # process which existed only across one asynchronous boundary.
            other = 'job_before' if not before else 'job_after'
            job = scope[other]
            job.update(pids=[123], assigned_processes=1, listed_processes=1, returned_bytes=16)
            job['query_attempts'][-1].update(pids=[123], assigned_processes=1, listed_processes=1, returned_bytes=16)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'unobserved'):
                self.audit()

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
