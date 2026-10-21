"""Short diagnostic: frozen guard owned CPU versus independent Windows Job CPU.

Creates only its own four gated direct-Python roots. Native Job accounting
includes descendants that start and exit between guard samples. No engine,
user application, original match, policy threshold or repository source changes.
"""
from __future__ import annotations

import argparse
from collections import Counter
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
TICKS_PER_SECOND = 10_000_000
REFERENCES = [
    'https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information',
    'https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject',
    'https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects',
]


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def native():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)]*4)]
    return kernel


def process_times(kernel, handle):
    values = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
        raise ctypes.WinError(ctypes.get_last_error())
    ticks = [value.dwLowDateTime | value.dwHighDateTime << 32 for value in values]
    return {'creation_ticks': ticks[0], 'exit_ticks': ticks[1], 'cpu_ticks': ticks[2]+ticks[3]}


class Job:
    def __init__(self):
        self.kernel = native()
        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                        ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', ctypes.c_ulonglong*6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway permission.
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def accounting(self):
        class Accounting(ctypes.Structure):
            _fields_ = [('total_user', ctypes.c_longlong), ('total_kernel', ctypes.c_longlong),
                        ('period_user', ctypes.c_longlong), ('period_kernel', ctypes.c_longlong),
                        ('page_faults', wintypes.DWORD), ('processes', wintypes.DWORD),
                        ('active', wintypes.DWORD), ('limit_terminated', wintypes.DWORD)]
        row = Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(row), ctypes.sizeof(row), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return {'utc': utc(), 'monotonic': time.monotonic(), 'user_ticks': row.total_user,
                'kernel_ticks': row.total_kernel, 'cpu_ticks': row.total_user+row.total_kernel,
                'total_processes': row.processes, 'active_processes': row.active,
                'limit_terminated_processes': row.limit_terminated}

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def burn_cpu(seconds, *, wall=False):
    clock = time.monotonic if wall else time.process_time
    deadline, safety = clock()+seconds, time.monotonic()+max(2., seconds*3)
    value = 1
    while clock() < deadline and time.monotonic() < safety:
        for _ in range(1024):
            value = (value*1664525+1013904223) & 0xffffffff
    return value


def worker(args):
    directory = Path(args.output)
    write(directory/f'ready-{args.worker}.json', {'pid': os.getpid(), 'utc': utc()})
    gate = directory/'gate.json'
    deadline = time.monotonic()+15
    while not gate.exists():
        if time.monotonic() > deadline:
            raise TimeoutError('own controller gate did not arrive')
        time.sleep(.01)
    settings = json.loads(gate.read_text(encoding='utf-8'))
    time.sleep(max(0., settings['start']-time.monotonic()))
    leaves = []
    if args.scenario == 'long-lived':
        burn_cpu(settings['duration'], wall=True)
    else:
        kernel = native()
        for cycle in range(8):
            time.sleep(max(0., settings['start']+cycle*.5+.08-time.monotonic()))
            command = [sys.executable, '-S', '-B', str(Path(__file__).resolve()), '--leaf']
            started = utc()
            child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            code = child.wait(timeout=3)
            measured = process_times(kernel, int(child._handle))
            leaves.append(dict(measured, pid=child.pid, exit_code=code, started_utc=started, finished_utc=utc()))
            child._handle.Close()
    write(directory/f'worker-{args.worker}.json', {'pid': os.getpid(), 'scenario': args.scenario, 'leaves': leaves, 'finished_utc': utc()})
    return 0


def scenario(output, name, guard_module):
    from m2_load_guard import LoadPolicy, M2LoadGuard
    output.mkdir()
    policy = LoadPolicy(max_nonworker_cpu_fraction=.2, interval_seconds=.5,
                        max_sample_gap_seconds=2., host_window_seconds=2.)
    guard, job, roots, observations = None, None, [], []
    result = {'scenario': name, 'started_utc': utc(), 'subject_module_sha256': sha(guard_module),
              'diagnostic_only': True, 'threshold_changed': False, 'policy_failure': None,
              'duration_seconds': 4.0, 'root_count': 4, 'sample_interval_seconds': .5}
    try:
        guard = M2LoadGuard(output/'guard-load.jsonl', policy)
        guard.require_idle(2.1)
        job = Job()
        base_python = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve())
        for index in range(4):
            command = [base_python, '-S', '-B', str(Path(__file__).resolve()), '--worker', str(index),
                       '--scenario', name, '--output', str(output)]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            roots.append(process)
            job.assign(process)
            identity = process_times(job.kernel, int(process._handle))
            guard.attach_worker(process.pid, identity['creation_ticks'])
        ready_deadline = time.monotonic()+5
        while not all((output/f'ready-{index}.json').is_file() for index in range(4)):
            if time.monotonic() > ready_deadline:
                raise TimeoutError('own direct workers did not reach the gate')
            time.sleep(.01)
        for index, process in enumerate(roots):
            if json.loads((output/f'ready-{index}.json').read_text())['pid'] != process.pid:
                raise AssertionError('worker is not the direct registered process')

        def sampled(phase):
            before = job.accounting()
            raw = guard.sample(phase)
            after = job.accounting()
            observations.append({'phase': phase, 'before_job': before, 'after_job': after,
                                 'guard_host': raw.get('host'), 'guard_failure': raw['failure']})
            return raw

        baseline = sampled('gated-baseline')
        start = time.monotonic()+.1
        write(output/'gate.json', {'start': start, 'duration': 4.0})
        counted = []
        for index in range(1, 13):
            time.sleep(max(0., start+index*.5-time.monotonic()))
            raw = sampled('diagnostic-work')
            counted.append(raw)
            if all(process.poll() is not None for process in roots):
                break
        for process in roots:
            process.wait(timeout=3)
        final_raw = sampled('workers-exited')
        counted.append(final_raw)
        for process in roots:
            guard.detach_worker(process.pid)
        if job.accounting()['active_processes'] != 0:
            raise AssertionError('own Job still has active descendants')
        worker_records = [json.loads((output/f'worker-{index}.json').read_text(encoding='utf-8')) for index in range(4)]
        root_records = [dict(process_times(job.kernel, int(process._handle)), pid=process.pid,
                             exit_code=process.returncode) for process in roots]
        if any(row['exit_code'] != 0 for row in root_records):
            raise AssertionError('own workload worker failed')
        known = {(row['pid'], row['creation_ticks']) for raw in counted for row in raw['processes'] if row.get('owned')}
        known |= {(row['pid'], row['creation_ticks']) for raw in counted for row in raw['retained_owned']}
        leaves = [leaf for row in worker_records for leaf in row['leaves']]
        missed = [row for row in leaves if (row['pid'], row['creation_ticks']) not in known]
        guard_ticks = sum(row['host']['owned_delta_ticks'] for row in counted if 'host' in row)
        low = observations[-1]['before_job']['cpu_ticks']-observations[0]['after_job']['cpu_ticks']
        high = observations[-1]['after_job']['cpu_ticks']-observations[0]['before_job']['cpu_ticks']
        result.update(status='DIAGNOSTIC_COMPLETE', roots=root_records, workers=worker_records,
                      observations=observations, leaf_process_count=len(leaves), unobserved_leaf_count=len(missed),
                      unobserved_leaf_cpu_ticks=sum(row['cpu_ticks'] for row in missed),
                      guard_counted_owned_delta_ticks=guard_ticks, job_work_cpu_lower_ticks=low,
                      job_work_cpu_upper_ticks=high, job_minus_guard_lower_ticks=low-guard_ticks,
                      job_minus_guard_upper_ticks=high-guard_ticks,
                      final_job=job.accounting(), measurement_rows=len(counted),
                      accounting_scope='Job query brackets bound non-simultaneous guard process-counter reads; baseline is after all gated roots are registered. Job membership captures their descendants even when absent between snapshots.')
    except Exception as error:
        result.update(status='DIAGNOSTIC_STOPPED', error=f'{type(error).__name__}: {error}', observations=observations)
    finally:
        if job is not None:
            if any(process.poll() is None for process in roots):
                result['owned_job_cleanup_required'] = True
                job.kernel.TerminateJobObject(job.handle, 3)
                for process in roots:
                    process.wait(timeout=3)
            result['job_after_workload_cleanup'] = job.accounting()
            job.close()
        if guard is not None:
            # Final failed state is retained. This diagnostic never relabels a
            # >20% reading as clean or uses these observations as match results.
            guard.close()
            result['guard_summary'] = guard.summary
            result['guard_policy_status'] = 'FAIL' if guard.summary['failure'] else 'PASS'
            result['diagnostic_continued_after_policy_failure'] = bool(guard.summary['failure'] and observations)
        for process in roots:
            process._handle.Close()
        result['finished_utc'] = utc()
        write(output/'receipt.json', result)
    return result


def controller(output):
    if sys.platform != 'win32':
        raise RuntimeError('Windows native Job APIs required')
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(ROOT/'scripts'))
    guard_module = ROOT/'scripts/m2_load_guard.py'
    sources = {str(path): sha(path) for path in (guard_module, ROOT/'scripts/see_clean_guard.py', ROOT/'scripts/compare_engines.py')}
    started, begin = utc(), time.monotonic()
    rows = [scenario(output/name, name, guard_module) for name in ('long-lived', 'short-child-churn')]
    result = {'schema_version': 1, 'status': 'DIAGNOSTIC_COMPLETE', 'started_utc': started,
              'finished_utc': utc(), 'wall_seconds': time.monotonic()-begin,
              'probe_source_sha256': sha(__file__), 'source_before_sha256': sources,
              'source_after_sha256': {path: sha(path) for path in sources},
              'official_references': REFERENCES, 'official_reference_checked_date': '2026-10-21',
              'native_oracle': 'QueryInformationJobObject class 1, TotalUserTime + TotalKernelTime, 100ns ticks, includes active and terminated associated processes.',
              'scenarios': rows, 'policy_max_nonworker_cpu_fraction': .2,
              'engine_games_started': 0, 'user_applications_changed': False, 'source_changes': False,
              'strength_eligible': False, 'promotion_performed': False,
              'scope': 'Bounded synthetic attribution diagnostic. It can demonstrate a measurement failure mode, not retroactively prove that mode caused either stopped match. All original verdicts and policies remain unchanged.'}
    if result['source_before_sha256'] != result['source_after_sha256']:
        raise AssertionError('Repository source changed during probe')
    write(output/'receipt.json', result)
    print(json.dumps({'status': result['status'], 'wall_seconds': result['wall_seconds'],
                      'scenarios': [{key: row.get(key) for key in ('scenario', 'status', 'guard_policy_status',
                                    'leaf_process_count', 'unobserved_leaf_count', 'job_minus_guard_lower_ticks',
                                    'job_minus_guard_upper_ticks')} for row in rows]}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--scenario', choices=('long-lived', 'short-child-churn'))
    parser.add_argument('--worker', type=int)
    parser.add_argument('--leaf', action='store_true')
    args = parser.parse_args()
    if args.leaf:
        burn_cpu(.12)
        return 0
    if args.worker is not None:
        return worker(args)
    return controller(args.output.resolve())


if __name__ == '__main__':
    raise SystemExit(main())
