"""Isolated CPU-attribution diagnosis, explicitly NOT formal load validation.

The subject guard source is unchanged. Only this synthetic probe uses a 1.0
load threshold so ambient service CPU cannot block measurement of missing
short-child CPU. The original formal .20 rule and all failed verdicts remain.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import probe

HERE, ROOT = probe.HERE, probe.ROOT
Job, sha, utc, write = probe.Job, probe.sha, probe.utc, probe.write
process_times = probe.process_times
PROBE_SOURCE_SHA256 = 'aead8cb9e5e39c7a03fabc27850f2797455874933dde726efa23a19f844854c8'


def scenario(output, name, guard_module):
    from m2_load_guard import LoadPolicy, M2LoadGuard
    output.mkdir()
    policy = LoadPolicy(max_nonworker_cpu_fraction=1.0, interval_seconds=.5,
                        max_sample_gap_seconds=2., host_window_seconds=2.)
    guard, job, roots, observations = None, None, [], []
    result = {'scenario': name, 'started_utc': utc(), 'subject_module_sha256': sha(guard_module),
              'diagnostic_only': True, 'threshold_changed': True, 'policy_failure': None,
              'formal_load_validation': False, 'diagnostic_threshold': 1.0, 'unchanged_formal_threshold': .20,
              'duration_seconds': 4.0, 'root_count': 4, 'sample_interval_seconds': .5}
    try:
        guard = M2LoadGuard(output/'guard-load.jsonl', policy)
        guard.require_idle(2.1)
        job = Job()
        base_python = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve())
        for index in range(4):
            command = [base_python, '-S', '-B', str(Path(probe.__file__).resolve()), '--worker', str(index),
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
            # >20% reading as formal-clean or uses these observations as match results.
            guard.close()
            result['guard_summary'] = guard.summary
            result['diagnostic_policy_status'] = 'FAIL' if guard.summary['failure'] else 'PASS'
            result['would_violate_formal20'] = guard.summary['max_nonworker_cpu_fraction'] > .20
            result['formal_load_validation'] = False
            result['diagnostic_continued_after_policy_failure'] = bool(guard.summary['failure'] and observations)
        for process in roots:
            process._handle.Close()
        result['finished_utc'] = utc()
        write(output/'receipt.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if sha(probe.__file__) != PROBE_SOURCE_SHA256:
        raise AssertionError('Original strict20 diagnostic source changed')
    sys.path.insert(0, str(ROOT/'scripts'))
    files = [ROOT/'scripts/m2_load_guard.py', ROOT/'scripts/see_clean_guard.py', ROOT/'scripts/compare_engines.py']
    before = {str(path): sha(path) for path in files}
    started = time.monotonic()
    result = scenario(output/'short-child-churn', 'short-child-churn', files[0])
    after = {str(path): sha(path) for path in files}
    leaves = [leaf for worker in result.get('workers', []) for leaf in worker['leaves']]
    complete = (result['status'] == 'DIAGNOSTIC_COMPLETE' and len(leaves) == 32
                and all(leaf['exit_code'] == 0 for leaf in leaves)
                and result['final_job']['total_processes'] == 36 and result['final_job']['active_processes'] == 0)
    receipt = {'schema_version': 1, 'status': 'ATTRIBUTION_MEASUREMENT_COMPLETE' if complete else 'MEASUREMENT_INCOMPLETE',
               'formal_load_validation': False, 'valid_for_formal_comparison': False,
               'diagnostic_threshold': 1.0, 'unchanged_formal_threshold': .2,
               'would_violate_formal20': result.get('would_violate_formal20'),
               'wall_seconds': time.monotonic()-started, 'probe_source_sha256': sha(__file__),
               'strict20_probe_source_sha256': sha(probe.__file__),
               'source_before_sha256': before, 'source_after_sha256': after,
               'original_sources_unchanged': before == after, 'scenario': result,
               'official_references': probe.REFERENCES, 'official_reference_checked_date': '2026-10-21',
               'all_leaf_cpu_ticks_from_held_exited_handles': sum(leaf['cpu_ticks'] for leaf in leaves),
               'engine_games_started': 0, 'user_applications_changed': False,
               'strength_eligible': False, 'promotion_performed': False,
               'scope': 'Synthetic CPU attribution only. A diagnostic guard PASS under threshold 1.0 is not formal20 validation. Measured short-child under-accounting cannot retrospectively identify the cause of stopped formal matches; original policy/failure records stay unchanged.'}
    if before != after:
        raise AssertionError('Subject source changed during diagnostic')
    write(output/'receipt.json', receipt)
    print(json.dumps({'status': receipt['status'], 'formal_load_validation': False,
                      'would_violate_formal20': receipt['would_violate_formal20'],
                      'wall_seconds': receipt['wall_seconds'], 'unobserved_leaf_count': result.get('unobserved_leaf_count'),
                      'unobserved_leaf_cpu_ticks': result.get('unobserved_leaf_cpu_ticks'),
                      'job_minus_guard_lower_ticks': result.get('job_minus_guard_lower_ticks'),
                      'job_minus_guard_upper_ticks': result.get('job_minus_guard_upper_ticks')}))
    return 0 if complete else 2


if __name__ == '__main__':
    raise SystemExit(main())

