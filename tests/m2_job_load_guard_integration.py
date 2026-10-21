"""Actual Windows integration checks for new Job/monitor/residual accounting.

Accuracy diagnostics (threshold 1.0) and formal20 gate observations are
explicitly separate. Creates only its own direct gated Python workers/children.
Existing matches, failed verdicts, user applications and product files are read
only. Run only after the new accounting/guard sources are declared complete.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TICKS_PER_SECOND = 10_000_000


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


class NativeOracle:
    """Independent raw ctypes layouts, not production normalization helpers."""
    def __init__(self):
        if sys.platform != 'win32':
            raise RuntimeError('Actual Windows APIs are required')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)]*4)]
        self.kernel.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)]*3
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                        wintypes.DWORD, ctypes.c_void_p]

    @staticmethod
    def ticks(value):
        return value.dwLowDateTime | value.dwHighDateTime << 32

    def process(self, handle=None):
        values = [wintypes.FILETIME() for _ in range(4)]
        start = time.monotonic()
        if not self.kernel.GetProcessTimes(handle or self.kernel.GetCurrentProcess(), *(ctypes.byref(v) for v in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = [self.ticks(value) for value in values]
        return {'read_started_monotonic': start, 'read_finished_monotonic': time.monotonic(),
                'creation_ticks': ticks[0], 'exit_ticks': ticks[1], 'kernel_ticks': ticks[2],
                'user_ticks': ticks[3], 'cpu_ticks': ticks[2]+ticks[3]}

    def system(self):
        values = [wintypes.FILETIME() for _ in range(3)]
        start = time.monotonic()
        if not self.kernel.GetSystemTimes(*(ctypes.byref(v) for v in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        idle, kernel, user = [self.ticks(value) for value in values]
        return {'read_started_monotonic': start, 'read_finished_monotonic': time.monotonic(),
                'idle_ticks': idle, 'kernel_ticks': kernel, 'user_ticks': user,
                'total_ticks': kernel+user, 'busy_ticks': kernel+user-idle}

    def job(self, handle):
        class Accounting(ctypes.Structure):
            _fields_ = [('user', ctypes.c_longlong), ('kernel', ctypes.c_longlong),
                        ('period_user', ctypes.c_longlong), ('period_kernel', ctypes.c_longlong),
                        ('faults', wintypes.DWORD), ('processes', wintypes.DWORD),
                        ('active', wintypes.DWORD), ('limit_terminated', wintypes.DWORD)]
        value = Accounting()
        start = time.monotonic()
        if not self.kernel.QueryInformationJobObject(handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return {'read_started_monotonic': start, 'read_finished_monotonic': time.monotonic(),
                'user_ticks': value.user, 'kernel_ticks': value.kernel, 'cpu_ticks': value.user+value.kernel,
                'total_processes': value.processes, 'active_processes': value.active,
                'terminated_processes_by_limit': value.limit_terminated}


def bracket(before, independent, after, fields, context):
    for row in (before, independent, after):
        assert row['read_started_monotonic'] <= row['read_finished_monotonic'], context+' read clocks'
        for key in fields:
            assert type(row[key]) is int and row[key] >= 0, context+' raw integer '+key
        if 'cpu_ticks' in row:
            assert row['cpu_ticks'] == row['user_ticks']+row['kernel_ticks'], context+' total normalization'
    for key in fields:
        assert before[key] <= independent[key] <= after[key], context+' counter bracket '+key
    assert before['read_finished_monotonic'] <= independent['read_started_monotonic'], context+' before ordering'
    assert independent['read_finished_monotonic'] <= after['read_started_monotonic'], context+' after ordering'


def burn(seconds, *, wall=False):
    clock = time.monotonic if wall else time.process_time
    end, limit, value = clock()+seconds, time.monotonic()+max(2., 3*seconds), 1
    while clock() < end and time.monotonic() < limit:
        for _ in range(1024):
            value = (value*1664525+1013904223) & 0xffffffff


def worker(args):
    output = args.output.resolve()
    oracle = NativeOracle()
    write(output/f'ready-{args.worker}.json', {'pid': os.getpid(), 'self_cpu': oracle.process(), 'utc': utc()})
    deadline = time.monotonic()+15
    while not (output/'gate.json').is_file():
        if time.monotonic() > deadline:
            raise TimeoutError('Own root did not receive startup gate')
        time.sleep(.01)
    gate = json.loads((output/'gate.json').read_text(encoding='utf-8'))
    time.sleep(max(0., gate['start']-time.monotonic()))
    leaves = []
    if args.scenario == 'long-lived':
        burn(4., wall=True)
    else:
        for index in range(8):
            time.sleep(max(0., gate['start']+index*.5+.08-time.monotonic()))
            child = subprocess.Popen([sys.executable, '-S', '-B', str(Path(__file__).resolve()), '--leaf'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            code = child.wait(timeout=3)
            final = oracle.process(int(child._handle))
            leaves.append(dict(final, pid=child.pid, exit_code=code))
            child._handle.Close()
    write(output/f'worker-{args.worker}.json', {'pid': os.getpid(), 'leaves': leaves,
                                              'self_cpu_before_exit': oracle.process(), 'utc': utc()})
    return 0


def api_contracts(accounting, job, oracle, *, exercise_monitor):
    assert job.monitor_in_job() is False, 'Coordinator must be outside its worker Job'
    job_before = job.accounting()
    raw_job = oracle.job(job.handle)
    job_after = job.accounting()
    bracket(job_before, raw_job, job_after, ('user_ticks', 'kernel_ticks', 'cpu_ticks', 'total_processes'), 'Job')
    assert raw_job['cpu_ticks'] == 0 and raw_job['total_processes'] == 0, 'New Job must start empty'
    monitor_before = accounting.read_monitor_cpu()
    raw_monitor = oracle.process()
    monitor_after = accounting.read_monitor_cpu()
    bracket(monitor_before, raw_monitor, monitor_after, ('user_ticks', 'kernel_ticks', 'cpu_ticks'), 'coordinator')
    assert monitor_before['pid'] == monitor_after['pid'] == os.getpid(), 'Wrong coordinator PID'
    assert monitor_before['creation_ticks'] == raw_monitor['creation_ticks'] == monitor_after['creation_ticks'], 'Wrong coordinator birth identity'
    system_before = accounting.read_system()
    raw_system = oracle.system()
    system_after = accounting.read_system()
    bracket(system_before, raw_system, system_after, ('idle_ticks', 'kernel_ticks', 'user_ticks', 'total_ticks'), 'system')
    for row in (system_before, raw_system, system_after):
        assert row['total_ticks'] == row['kernel_ticks']+row['user_ticks']
        assert row['busy_ticks'] == row['total_ticks']-row['idle_ticks']
    monitor_exercise = None
    if exercise_monitor:
        before = accounting.read_monitor_cpu()
        burn(.12)
        after = accounting.read_monitor_cpu()
        assert after['cpu_ticks'] > before['cpu_ticks'], 'Known coordinator CPU workload was not counted'
        assert job.accounting()['cpu_ticks'] == 0, 'Coordinator CPU leaked into worker Job'
        monitor_exercise = {'before': before, 'after': after, 'job_remained_zero': True}
    return {'job': [job_before, raw_job, job_after],
            'coordinator': [monitor_before, raw_monitor, monitor_after],
            'system': [system_before, raw_system, system_after],
            'coordinator_cpu_exercise': monitor_exercise,
            'job_excludes_coordinator': True}


def independently_account(previous, current):
    """Raw sampled deltas; never infer background process/app identities."""
    total = current['system']['total_ticks']-previous['system']['total_ticks']
    idle = current['system']['idle_ticks']-previous['system']['idle_ticks']
    job = current['job']['cpu_ticks']-previous['job']['cpu_ticks']
    coordinator = current['coordinator']['cpu_ticks']-previous['coordinator']['cpu_ticks']
    assert total >= 0 and 0 <= idle <= total and job >= 0 and coordinator >= 0
    busy = total-idle
    residual = busy-job-coordinator
    underflow = max(0, -residual)
    assert job+coordinator+residual == busy
    return {'total_delta_ticks': total, 'busy_delta_ticks': busy, 'job_delta_ticks': job,
            'coordinator_delta_ticks': coordinator, 'residual_delta_ticks': residual,
            'accounting_underflow_ticks': underflow}


def frame_point(frame):
    return {'system': frame['system'],
            'job': {'cpu_ticks': (frame['job_before']['cpu_ticks']+frame['job_after']['cpu_ticks'])/2},
            'coordinator': {'cpu_ticks': (frame['monitor_before']['cpu_ticks']+frame['monitor_after']['cpu_ticks'])/2}}


def replay_guard_math(path, window_seconds=2.):
    """Use only JSONL raw counters/timing, independently of guard math helpers."""
    rows = [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]
    previous, end, intervals, samples = None, 0., [], 0
    job_delta_sum = coordinator_delta_sum = 0.
    min_signed, max_fraction, ready_windows = 0., 0., 0
    job_at_start = job_at_end = None
    failures = []
    for row in rows:
        if row['event'] != 'sample':
            continue
        end += row['gap_seconds']
        frame = row['native_accounting']
        point = frame_point(frame)
        if job_at_start is None:
            job_at_start = point['job']['cpu_ticks']
        job_at_end = point['job']['cpu_ticks']
        if previous is not None:
            before, left = previous
            parts = independently_account(before, point)
            actual = row['host']
            expected_fields = {'total_delta_ticks': parts['total_delta_ticks'], 'busy_delta_ticks': parts['busy_delta_ticks'],
                               'experiment_job_delta_ticks': parts['job_delta_ticks'],
                               'coordinator_delta_ticks': parts['coordinator_delta_ticks'],
                               'residual_signed_delta_ticks': parts['residual_delta_ticks'],
                               'nonworker_delta_ticks': parts['residual_delta_ticks']}
            for key, value in expected_fields.items():
                assert abs(actual[key]-value) < 1e-6, 'Raw accounting mismatch: '+key
            job_delta_sum += parts['job_delta_ticks']
            coordinator_delta_sum += parts['coordinator_delta_ticks']
            min_signed = min(min_signed, parts['residual_delta_ticks'])
            intervals.append((left, end, parts['total_delta_ticks'], parts['residual_delta_ticks']))
            cutoff = end-window_seconds
            intervals = [item for item in intervals if item[1] > cutoff]
            seconds = total = signed = 0.
            for a, b, count, other in intervals:
                overlap = max(0., b-max(a, cutoff))
                if overlap and b > a:
                    seconds += overlap
                    total += count*overlap/(b-a)
                    signed += other*overlap/(b-a)
            ready = seconds >= window_seconds-1e-6 and total > 0
            fraction = max(0., signed)/total if total > 0 else None
            assert actual['window_ready'] is ready
            assert abs(actual['window_nonworker_ticks']-signed) <= max(1e-5, abs(signed)*1e-8)
            assert abs(actual['window_total_ticks']-total) <= max(1e-5, abs(total)*1e-8)
            if fraction is not None:
                assert abs(actual['window_fraction']-fraction) < 1e-8
                if ready:
                    max_fraction = max(max_fraction, fraction)
                    ready_windows += 1
        if row['failure']:
            failures.append(row['failure'])
        previous, samples = (point, end), samples+1
    assert job_delta_sum == job_at_end-job_at_start, 'Job deltas must telescope without losing children'
    return {'samples': samples, 'ready_windows': ready_windows, 'job_delta_sum': job_delta_sum,
            'coordinator_delta_sum': coordinator_delta_sum, 'minimum_signed_residual_interval': min_signed,
            'maximum_ready_residual_fraction': max_fraction, 'failure_observations': failures,
            'raw_counter_math_replay_passed': True}


def accuracy_case(output, scenario, accounting, guards):
    """Threshold 1.0 tests accounting, and cannot certify the formal20 rule."""
    output.mkdir()
    oracle, processes, assignments, streams = NativeOracle(), [], [], []
    job = accounting.OwnedJob()
    guard = None
    result = {'scenario': scenario, 'formal_load_validation': False,
              'diagnostic_threshold': 1.0, 'started_utc': utc()}
    try:
        result['native_api_contracts'] = api_contracts(accounting, job, oracle, exercise_monitor=True)
        guard = guards.JobGuardMonitor(output/'load.jsonl', guards.LoadPolicy(1.0), job)
        guard.require_idle(2.1)
        guard.start_timer()
        executable = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve())
        for index in range(4):
            stream = (output/f'worker-{index}.stderr.txt').open('x', encoding='utf-8')
            streams.append(stream)
            command = [executable, '-S', '-B', str(Path(__file__).resolve()),
                       '--worker', str(index), '--scenario', scenario, '--output', str(output)]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stream)
            processes.append(process)
            assigned = job.assign(process)
            assignments.append(dict(assigned, command=command))
            identity = guard.identify_worker(process.pid)
            assert identity == (process.pid, assigned['creation_ticks'])
            guard.attach_worker(process.pid, assigned['creation_ticks'])
        deadline = time.monotonic()+8
        while not all((output/f'ready-{index}.json').is_file() for index in range(4)):
            guard.assert_clean()
            assert time.monotonic() < deadline, 'Own worker startup deadline'
            assert all(process.poll() is None for process in processes), 'Own worker exited before gate'
            time.sleep(.02)
        for index, process in enumerate(processes):
            ready = json.loads((output/f'ready-{index}.json').read_text(encoding='utf-8'))
            assert ready['pid'] == process.pid
            assert ready['self_cpu']['creation_ticks'] == assignments[index]['creation_ticks']
        write(output/'gate.json', {'start': time.monotonic()+.2, 'utc': utc()})
        # This known CPU belongs to the coordinator, never to the worker Job.
        coordinator_before = oracle.process()
        burn(.10)
        coordinator_after = oracle.process()
        assert coordinator_after['cpu_ticks'] > coordinator_before['cpu_ticks']
        result['coordinator_exercise_during_guard'] = [coordinator_before, coordinator_after]
        deadline = time.monotonic()+12
        while any(process.poll() is None for process in processes):
            guard.assert_clean()
            assert time.monotonic() < deadline, 'Own workload deadline'
            time.sleep(.1)
        for process in processes:
            assert process.wait(timeout=1) == 0, 'Own root worker failure'
        final_roots = [dict(oracle.process(int(process._handle)), pid=process.pid) for process in processes]
        worker_rows = [json.loads((output/f'worker-{index}.json').read_text(encoding='utf-8')) for index in range(4)]
        leaves = [leaf for row in worker_rows for leaf in row['leaves']]
        expected_leaves = 0 if scenario == 'long-lived' else 32
        assert len(leaves) == expected_leaves
        assert all(leaf['exit_code'] == 0 and leaf['exit_ticks'] > 0 for leaf in leaves)
        for row, final in zip(worker_rows, final_roots):
            assert row['pid'] == final['pid']
            assert row['self_cpu_before_exit']['creation_ticks'] == final['creation_ticks']
            assert row['self_cpu_before_exit']['cpu_ticks'] <= final['cpu_ticks']
            assert final['exit_ticks'] > 0
        # Keep every Popen handle open while proving Job ActiveProcesses is zero.
        final_job = job.terminate_and_snapshot(timeout_seconds=5)
        independent_final = oracle.job(job.handle)
        assert independent_final['active_processes'] == final_job['active_processes'] == 0
        for key in ('user_ticks', 'kernel_ticks', 'cpu_ticks', 'total_processes'):
            assert independent_final[key] == final_job[key], 'Stable final Job oracle '+key
        assert final_job['total_processes'] == 4+expected_leaves, 'Job descendant count'
        assert final_job['cpu_ticks'] > 0
        guard.sample('all-owned-exited')
        for process in processes:
            guard.detach_worker(process.pid)
        guard.close()
        summary = guard.summary
        replay = replay_guard_math(output/'load.jsonl')
        assert summary['timer']['started'] and not summary['timer']['alive']
        assert summary['valid_for_load_check'], 'Diagnostic guard rejected: '+str(summary['failure'])
        assert replay['job_delta_sum'] == summary['experiment_job_delta_ticks'] == final_job['cpu_ticks'], 'Guard must retain all cumulative Job CPU'
        assert replay['coordinator_delta_sum'] == summary['coordinator_delta_ticks'] > 0
        root_sum = sum(row['cpu_ticks'] for row in final_roots)
        leaf_sum = sum(row['cpu_ticks'] for row in leaves)
        # Full-life process timers and Job CPU may differ at Popen/assignment
        # boundaries. Retain the actual difference and the offset diagnostics.
        result.update(passed=True, assignments=assignments, worker_records=worker_rows,
                      final_root_timers_with_handles_held=final_roots,
                      final_job=final_job, independent_final_job=independent_final,
                      root_final_cpu_ticks=root_sum, leaf_final_cpu_ticks=leaf_sum,
                      all_process_final_cpu_ticks=root_sum+leaf_sum,
                      final_process_minus_job_ticks=root_sum+leaf_sum-final_job['cpu_ticks'],
                      job_minus_root_only_ticks=final_job['cpu_ticks']-root_sum,
                      descendants_count=expected_leaves,
                      active_zero_while_root_handles_held=True,
                      guard_summary=summary, independent_replay=replay,
                      would_violate_formal20=replay['maximum_ready_residual_fraction'] > .2,
                      scope='Accounting diagnostic only. Does not certify formal20, strength, isolation or individual move contention.')
        if expected_leaves:
            assert final_job['cpu_ticks'] > root_sum, 'Job cumulative CPU must include short-lived child workload'
    except BaseException as error:
        result.update(passed=False, error_type=type(error).__name__, error=str(error))
    finally:
        try:
            cleanup = job.terminate_and_snapshot(timeout_seconds=5)
            result['cleanup'] = {'job': cleanup, 'active_zero': cleanup['active_processes'] == 0,
                                 'independent_job': oracle.job(job.handle)}
        except BaseException as error:
            result['cleanup'] = {'active_zero': False, 'error': repr(error)}
            result['passed'] = False
        if guard is not None:
            guard.close()
            result.setdefault('guard_summary', guard.summary)
        for process in processes:
            try:
                process.wait(timeout=2)
                process._handle.Close()
            except BaseException as error:
                result.setdefault('cleanup_errors', []).append(repr(error))
                result['passed'] = False
        for stream in streams:
            stream.close()
        job.close()
        result['finished_utc'] = utc()
        write(output/'result.json', result)
    return result


def termination_case(output, accounting):
    output.mkdir()
    oracle, job = NativeOracle(), accounting.OwnedJob()
    process = None
    result = {'started_utc': utc(), 'formal_load_validation': False}
    try:
        command = [str(Path(getattr(sys, '_base_executable', sys.executable)).resolve()),
                   '-S', '-B', '-c', 'import time; time.sleep(15)']
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        receipt = job.assign(process)
        before = oracle.job(job.handle)
        result.update(assignment=receipt, before=before)
        assert before['active_processes'] == 1, 'Own Job must contain the live sleeper'
        terminated = job.terminate_and_snapshot(timeout_seconds=5)
        independent = oracle.job(job.handle)
        result.update(terminated=terminated, independent_final_job=independent)
        # Job active0 is not a substitute for waiting on the root process handle.
        # Read final per-process exit time only after that handle is signalled.
        exit_code = process.wait(timeout=2)
        final_process = oracle.process(int(process._handle))
        result.update(exit_code=exit_code, final_process_with_handle_held=final_process)
        assert terminated['active_processes'] == independent['active_processes'] == 0, 'Termination must leave Job active0'
        assert terminated['total_processes'] == independent['total_processes'] == 1, 'Cumulative Job process count'
        assert terminated['cpu_ticks'] == independent['cpu_ticks'], 'Stable final Job CPU oracle'
        assert exit_code != 0, 'TerminateJobObject must end the owned sleeper'
        assert final_process['exit_ticks'] > 0, 'Signalled process must have final exit time'
        result.update(passed=True, assignment=receipt, before=before, terminated=terminated,
                      independent_final_job=independent, final_process_with_handle_held=final_process,
                      active_zero_while_root_handle_held=True)
    except BaseException as error:
        result.update(passed=False, error_type=type(error).__name__, error=str(error))
    finally:
        try:
            result['cleanup'] = job.terminate_and_snapshot(timeout_seconds=5)
        finally:
            if process is not None:
                process.wait(timeout=2)
                process._handle.Close()
            job.close()
        result['finished_utc'] = utc()
        write(output/'result.json', result)
    return result


def outside_job_case(output, accounting, guards):
    """Our own non-Job child must remain outside the subtracted Job CPU."""
    output.mkdir()
    oracle, job = NativeOracle(), accounting.OwnedJob()
    guard = guards.JobGuardMonitor(output/'load.jsonl', guards.LoadPolicy(1.0), job)
    process = None
    result = {'scenario': 'own-child-outside-job', 'started_utc': utc(),
              'formal_load_validation': False, 'diagnostic_threshold': 1.0}
    try:
        guard.require_idle(2.1)
        guard.start_timer()
        command = [str(Path(getattr(sys, '_base_executable', sys.executable)).resolve()),
                   '-S', '-B', str(Path(__file__).resolve()), '--leaf']
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert process.wait(timeout=3) == 0
        final = oracle.process(int(process._handle))
        assert final['exit_ticks'] > 0 and final['cpu_ticks'] > 0
        guard.sample('own-nonjob-child-exited')
        time.sleep(.55)
        guard.assert_clean()
        guard.close()
        summary = guard.summary
        replay = replay_guard_math(output/'load.jsonl')
        final_job = oracle.job(job.handle)
        assert final_job['cpu_ticks'] == final_job['total_processes'] == final_job['active_processes'] == 0
        assert replay['job_delta_sum'] == summary['experiment_job_delta_ticks'] == 0
        assert summary['valid_for_load_check']
        rows = [json.loads(line) for line in (output/'load.jsonl').read_text(encoding='utf-8').splitlines()]
        signed_total = sum(row.get('host', {}).get('residual_signed_delta_ticks', 0) for row in rows)
        assert signed_total > 0
        result.update(passed=True, command=command, own_outside_child_final_timer=final,
                      final_job=final_job, guard_summary=summary, independent_replay=replay,
                      outside_child_was_never_added_to_job=True,
                      residual_signed_ticks_for_observed_interval=signed_total,
                      residual_minus_own_outside_child_ticks=signed_total-final['cpu_ticks'],
                      would_violate_formal20=replay['maximum_ready_residual_fraction'] > .2,
                      scope='Only the own outside child has known attribution. Remaining signed system residual includes all other background and measurement boundaries.')
    except BaseException as error:
        result.update(passed=False, error_type=type(error).__name__, error=str(error))
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()  # This exact Popen handle was created above.
            process.wait(timeout=2)
            process._handle.Close()
        guard.close()
        result.setdefault('guard_summary', guard.summary)
        result['cleanup'] = job.terminate_and_snapshot(timeout_seconds=5)
        job.close()
        result['finished_utc'] = utc()
        write(output/'result.json', result)
    return result


def strict20_case(output, accounting, guards):
    """Observe the unchanged formal threshold without starting any workers."""
    output.mkdir()
    job = accounting.OwnedJob()
    guard = guards.JobGuardMonitor(output/'load.jsonl', guards.LoadPolicy(.2), job)
    result = {'started_utc': utc(), 'formal_load_validation': True, 'threshold': .2,
              'scope': 'Short empty-Job native idle observation; not a formal match or guarantee of future idle.'}
    try:
        guard.require_idle(2.1)
        guard.start_timer()
        time.sleep(.65)
        guard.assert_clean()
    except guards.LoadGuardViolation as error:
        result['observed_rejection'] = str(error)
    finally:
        guard.close()
        result['guard_summary'] = guard.summary
        result['independent_replay'] = replay_guard_math(output/'load.jsonl')
        result['strict20_passed'] = guard.summary['valid_for_load_check']
        result['failure_preserved'] = guard.summary['failure']
        result['cleanup'] = job.terminate_and_snapshot(timeout_seconds=5)
        job.close()
        result['finished_utc'] = utc()
        write(output/'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', type=int)
    parser.add_argument('--scenario', choices=('long-lived', 'churn'))
    parser.add_argument('--leaf', action='store_true')
    args = parser.parse_args()
    if args.leaf:
        burn(.12)
        return 0
    if args.worker is not None:
        return worker(args)
    if args.output is None:
        parser.error('--output is required')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(ROOT/'scripts'))
    import m2_job_accounting as accounting
    import m2_job_load_guard as guards
    source_paths = [Path(__file__).resolve(), ROOT/'scripts/m2_job_accounting.py',
                    ROOT/'scripts/m2_job_load_guard.py', ROOT/'scripts/m2_load_guard.py',
                    ROOT/'scripts/see_clean_guard.py']
    before = {str(path): sha(path) for path in source_paths}
    started = utc()
    results = [accuracy_case(output/'long-lived', 'long-lived', accounting, guards),
               accuracy_case(output/'churn', 'churn', accounting, guards),
               outside_job_case(output/'outside-job', accounting, guards),
               termination_case(output/'termination', accounting)]
    strict = strict20_case(output/'strict20', accounting, guards)
    after = {str(path): sha(path) for path in source_paths}
    assert before == after, 'Integration source changed while running'
    execution = {'schema_version': 1, 'started_utc': started, 'finished_utc': utc(),
                 'command': [sys.executable, *sys.argv], 'python_version': sys.version,
                 'source_sha256_before': before, 'source_sha256_after': after,
                 'source_unchanged_during_execution': True,
                 'accounting_diagnostics_passed': all(row['passed'] for row in results),
                 'formal20_short_observation_passed': strict['strict20_passed'],
                 'formal_match_played': False, 'engine_games': 0,
                 'all_owned_jobs_active_zero': all(row['cleanup'].get('active_zero', row['cleanup'].get('active_processes') == 0) for row in results)
                                               and strict['cleanup']['active_processes'] == 0,
                 'diagnostic_results': [{key: row.get(key) for key in ('scenario', 'passed', 'error', 'would_violate_formal20')} for row in results],
                 'strict20_result': strict['guard_summary']['failure'],
                 'artifact_sha256': {str(path.relative_to(output)): sha(path) for path in sorted(output.rglob('*')) if path.is_file()}}
    write(output/'execution.json', execution)
    print(json.dumps({key: execution[key] for key in ('accounting_diagnostics_passed', 'formal20_short_observation_passed', 'all_owned_jobs_active_zero', 'diagnostic_results')}))
    return 0 if execution['accounting_diagnostics_passed'] and execution['all_owned_jobs_active_zero'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
