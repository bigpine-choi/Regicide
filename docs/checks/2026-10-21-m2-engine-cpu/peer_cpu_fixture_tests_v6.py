"""Offline compatibility and mutations for the independent v6 audit.

Product helpers supply synthetic records only; production peer audit imports
none of these helpers or validators. No engine or native workload is started.
"""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT/'tests'), str(ROOT/'scripts')]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prior = load('independent_v5_synthetic_tests', HERE/'peer_cpu_fixture_tests_v5.py')
audit = load('independent_v6_audit', HERE/'peer_game_audit_engine_cpu_v6.py')
prior.audit = audit
from m2_warning_load_guard_v2_tests import make_owned_fixture, make_warning_fixture, NativeFrames, Clock, load as guard_module
from m2_warning_receipts_v2 import _ownership_scope


class ExistingEndpointAndCPUChecks(prior.IndependentJobScopeTests):
    """The frozen v5 tests now exercise the new copied peer implementation."""


def samples(owned=True):
    rows, summary, _, _ = make_owned_fixture() if owned else make_warning_fixture()
    known = {}
    result = []
    for row in rows:
        if row['event'] == 'attach':
            key = tuple(row['identity']); known[key] = key
        if row['event'] != 'sample':
            continue
        # Older synthetic builders lack discovery-only progress fields. These
        # fields are explicit test data, never added to actual run records.
        scope = row['process_ownership_scope']
        scope.setdefault('operation', 'snapshot')
        scope.setdefault('phase', 'complete')
        scope.setdefault('offending_pid', None)
        result.append((copy.deepcopy(row), summary['policy'], summary['monitor_pid'], copy.deepcopy(known)))
        for item in row['processes']:
            if item['owned']:
                known[(item['pid'], item['creation_ticks'])] = tuple(item['worker_root'])
    return result


def active():
    return next(args for args in samples() if any(item['pid'] == 101 for item in args[0]['processes']))


def refresh_job(row, pids):
    ids = sorted(pids)
    row.update(pids=ids, assigned_processes=len(ids), listed_processes=len(ids), returned_bytes=8+8*len(ids))
    row['query_attempts'][-1].update(pids=ids, assigned_processes=len(ids), listed_processes=len(ids), returned_bytes=8+8*len(ids))


class ProviderScopeChecks(unittest.TestCase):
    def test_full_guard_warning_and_signed_cpu_replay_and_scope_count_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp); clock = Clock(); provider = NativeFrames(clock)
            guard = guard_module.WarningLoadGuard(directory/'load.jsonl', guard_module.LoadPolicy(.2), None,
                provider, clock=clock, sleep=clock.sleep)
            guard.require_idle(5.)
            guard._write(dict(event='timer_started', interval_seconds=.5, join_bound_seconds=2.))
            for busy, job in ((.1, 10000), (.9, 0), (.9, 0), (.9, 0), (.9, 0)):
                provider.busy_fraction = busy; provider.job_cpu += job
                clock.sleep(.5); guard.sample('timer')
            guard._write(dict(event='timer_stopped', timer_samples=5))
            guard.sample('phase-completed'); guard.close()
            summary = copy.deepcopy(guard.summary)
            summary['timer'] = dict(started=True, samples=5, alive=False, failure=None, join_bound_seconds=2.)
            rows = [json.loads(line) for line in (directory/'load.jsonl').read_text().splitlines()]
            last_utc = rows[0]['utc']
            for row in rows:
                row.setdefault('utc', last_utc); last_utc = row['utc']
                if row['event'] == 'sample':
                    for key in ('job_before', 'job_after'):
                        row['native_accounting'][key].update(terminated_processes=0,
                            pre_assignment_cpu_lower_ticks_total=0, pre_assignment_cpu_upper_ticks_total=0)
            (directory/'load.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
            final = next(row for row in reversed(rows) if row['event'] == 'sample')['native_accounting']['job_after']
            def write(name, value):
                (directory/name).write_text(json.dumps(value), encoding='utf-8')
            write('load-summary.json', summary); write('job-final-accounting.json', final)
            write('run-started.json', dict(pid=summary['monitor_pid'], started_utc=rows[0]['utc']))
            report = dict(started_utc=rows[0]['utc'], guard_log_sha256=audit.sha(directory/'load.jsonl'),
                guard_summary_sha256=audit.sha(directory/'load-summary.json'),
                job_final_accounting_sha256=audit.sha(directory/'job-final-accounting.json'))
            plan = json.loads(json.dumps(dict(load_policy=summary['policy'], idle_probe_seconds=5.)))
            result = audit.guard_records(directory, plan, report)[0]
            self.assertGreater(result['residual_load_warning_window_count'], 0)
            self.assertGreater(result['accounting_underflow_ticks'], 0)
            self.assertEqual(result['provider_scope_count'], summary['process_ownership_scope_count'])
            summary['process_ownership_scope_count'] -= 1
            write('load-summary.json', summary)
            # A fresh audit invocation has a fresh immutable-input registry.
            audit.helper.FILES.clear()
            report['guard_summary_sha256'] = audit.sha(directory/'load-summary.json')
            with self.assertRaises(Exception):audit.guard_records(directory, plan, report)

    def test_actual_guard_serialized_synthetic_provider_matches_independent_reconstruction(self):
        cases = samples() + samples(False)
        self.assertGreater(len(cases), 25)
        for row, policy, coordinator, known in cases:
            with self.subTest(index=row['process_ownership_scope_index'], owned=bool(row['roots'])):
                result = audit.provider_scope_records(row, policy, coordinator, known)
                self.assertEqual(result['counts']['selected_process_records'], len(row['process_ownership_scope']['selected_processes']))

    def test_same_global_parent_never_makes_outside_named_engine_owned(self):
        row, policy, coordinator, known = active()
        result = audit.provider_scope_records(row, policy, coordinator, known)
        self.assertNotIn((991, 100002), result['proven_owned'])
        item = next(item for item in row['processes'] if item['pid'] == 991)
        self.assertFalse(item['owned'])
        self.assertEqual(result['counts']['outside_parent_matches'], 2)

    def test_raw_job_inventory_query_classification_mutations_fail(self):
        keys = ('scope_method', 'breakaway', 'coordinator', 'count', 'truncated', 'attempts', 'union',
                'outside_query', 'missing_selected', 'excluded', 'outside_matches', 'membership',
                'owned', 'cpu', 'retained', 'query_reason', 'process_error', 'unobserved_persistent')
        for key in keys:
            with self.subTest(key=key):
                row, policy, coordinator, known = active(); scope = row['process_ownership_scope']
                if key == 'scope_method':scope['method'] = 'numeric-parent'
                elif key == 'breakaway':scope['job_before']['limit_flags'] |= 0x800
                elif key == 'coordinator':scope['job_before']['coordinator_in_job'] = True
                elif key == 'count':scope['job_after']['listed_processes'] += 1
                elif key == 'truncated':scope['job_after']['returned_bytes'] = 8
                elif key == 'attempts':scope['job_before']['query_attempts'] = []
                elif key == 'union':scope['global_inventory'].pop()
                elif key == 'outside_query':
                    item = copy.deepcopy(scope['queries'][0]); item['pid'] = 777
                    item['result'] = dict(pid=777, parent_pid=101, name='csrss.exe', creation_ticks=1,
                                          cpu_ticks=0, error=None, exited_during_snapshot=False)
                    scope['queries'].append(item)
                elif key == 'missing_selected':scope['selected_processes'].pop()
                elif key == 'excluded':scope['excluded_nonengine_pids'] = []
                elif key == 'outside_matches':scope['outside_parent_matches'] = []
                elif key == 'membership':scope['queries'][0]['in_owned_job_at_read'] = False
                elif key == 'owned':row['processes'][0]['owned'] = not row['processes'][0]['owned']
                elif key == 'cpu':scope['selected_processes'][0]['cpu_ticks'] += 1
                elif key == 'retained':scope['retained_owned'] = []
                elif key == 'query_reason':scope['queries'][0]['reason'] = 'numeric-parent'
                elif key == 'process_error':scope['selected_processes'][0]['error'] = 'OpenProcess error 5'
                else:
                    for job in ('job_before', 'job_after'):refresh_job(scope[job], scope[job]['pids']+[999])
                    scope['unobserved_before_only_job_pids'] = [dict(pid=999, in_job_before=True,
                        in_job_after=False, reason='ended-between-inventories-identity-not-adopted')]
                with self.assertRaises(Exception):audit.provider_scope_records(row, policy, coordinator, known)

    def test_boundary_birth_and_exit_remain_unidentified_but_preserve_full_job_cpu(self):
        for direction in ('before', 'after'):
            with self.subTest(direction=direction):
                row, policy, coordinator, known = active(); scope = row['process_ownership_scope']
                job = scope['job_'+direction]; refresh_job(job, job['pids']+[999])
                scope['global_inventory_attempts'].append(copy.deepcopy(scope['global_inventory_attempts'][0]))
                scope['unobserved_'+direction+'_only_job_pids'].append(dict(pid=999,
                    in_job_before=direction == 'before', in_job_after=direction == 'after',
                    reason=('ended-between-inventories-identity-not-adopted' if direction == 'before'
                            else 'born-after-inventory-identity-not-adopted')))
                result = audit.provider_scope_records(row, policy, coordinator, known)
                self.assertFalse(any(key[0] == 999 for key in result['proven_owned']))
                self.assertEqual(result['counts']['unobserved_'+direction+'_only_pids'], 1)
                scope['global_inventory_attempts'].pop()
                with self.assertRaises(Exception):audit.provider_scope_records(row, policy, coordinator, known)

    def test_true_native_job_membership_can_adopt_late_named_engine(self):
        row, policy, coordinator, known = active(); scope = row['process_ownership_scope']
        for query in scope['queries']:
            if query['pid'] == 991:query['in_owned_job_at_read'] = True
        for reason in scope['selection_reasons']:
            if reason['pid'] == 991:reason['reason'] = 'current-job-member'
        process = next(item for item in row['processes'] if item['pid'] == 991)
        process.update(owned=True, job_scope_member=True, worker_root=[100, 100000], ancestry=[991, 100])
        result = audit.provider_scope_records(row, policy, coordinator, known)
        self.assertIn((991, 100002), result['proven_owned'])

    def test_exact_held_exited_cpu_is_owned_and_unproven_recovery_fails(self):
        row, policy, coordinator, known = active(); scope = row['process_ownership_scope']
        known[(101, 100001)] = (100, 100000)
        refresh_job(scope['job_before'], [100]); refresh_job(scope['job_after'], [100])
        scope['queries'] = [query for query in scope['queries'] if query['pid'] != 101]
        final = next(item for item in scope['selected_processes'] if item['pid'] == 101)
        final['exited_during_snapshot'] = True
        scope['held_exit_recoveries'].append(dict(identity=[101, 100001], final=copy.deepcopy(final), proof='exact-held-exited-process'))
        next(item for item in scope['selection_reasons'] if item['pid'] == 101)['reason'] = 'known-held-owned-exited'
        next(item for item in scope['retained_owned'] if item['pid'] == 101)['exited_during_snapshot'] = True
        next(item for item in row['retained_owned'] if item['pid'] == 101)['exited'] = True
        next(item for item in row['processes'] if item['pid'] == 101).update(exited=True, ancestry=None)
        result = audit.provider_scope_records(row, policy, coordinator, known)
        self.assertIn((101, 100001), result['proven_owned'])
        del known[(101, 100001)]
        with self.assertRaises(Exception):audit.provider_scope_records(row, policy, coordinator, known)


if __name__ == '__main__':
    unittest.main()
