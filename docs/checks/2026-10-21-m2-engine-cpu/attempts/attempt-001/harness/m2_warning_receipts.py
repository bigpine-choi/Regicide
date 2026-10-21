"""Independently replay warning-policy Job/coordinator receipts without native calls.

Only arithmetic and recorded evidence are audited. These asynchronous counters
remain estimates, not proof of application attribution or move-level isolation.
Malformed/mismatched evidence raises ValueError. An explicitly failed, incomplete
measurement returns INVALID with its missing coverage listed; it never becomes a
clean comparison merely because the available intervals can be recomputed.
"""
from collections import deque
from dataclasses import asdict
import json
import math
from pathlib import Path

from m2_load_guard import LoadPolicy

METHOD = 'windows-job-plus-coordinator-cpu'


def _require(condition, message):
    if not condition:
        raise ValueError('Job receipt: ' + message)


def _number(value, label, *, minimum=None, integer=False):
    _require(type(value) is int if integer else type(value) in (int, float), label + ' is not numeric')
    _require(math.isfinite(value), label + ' is not finite')
    _require(minimum is None or value >= minimum, label + ' is below its minimum')
    return value


def _same(actual, expected, label, *, approximate=False):
    if expected is None or isinstance(expected, bool):
        _require(actual is expected, label + ' differs')
    else:
        _number(actual, label)
        # Integer and half-tick midpoint sums are exact at this run's scale.
        # Only window interpolation needs tolerance for the relative endpoint
        # reconstruction versus the producer's large absolute monotonic clock.
        equal = (math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-7)
                 if approximate else actual == expected)
        _require(equal, label + ' differs')


def _bracket(row, label):
    begin = _number(row['read_started_monotonic'], label + ' read start', minimum=0)
    end = _number(row['read_finished_monotonic'], label + ' read finish', minimum=0)
    _require(end >= begin, label + ' read clock regressed')
    return begin, end


def _cpu(row, label, *, job=False):
    for key in ('user_ticks', 'kernel_ticks', 'cpu_ticks'):
        _number(row[key], label + ' ' + key, minimum=0, integer=True)
    _require(row['cpu_ticks'] == row['user_ticks'] + row['kernel_ticks'], label + ' CPU sum differs')
    _bracket(row, label)
    if job:
        _require(row['coordinator_in_job'] is False, 'coordinator belongs to Job')
        for key in ('total_processes', 'active_processes', 'terminated_processes_by_limit'):
            _number(row[key], label + ' ' + key, minimum=0, integer=True)
        _require(max(row['active_processes'], row['terminated_processes_by_limit']) <= row['total_processes'],
                 label + ' process counters are inconsistent')
    else:
        for key in ('pid', 'creation_ticks'):
            _number(row[key], label + ' ' + key, minimum=1, integer=True)


def _frame(frame, monitor_pid):
    _require(isinstance(frame, dict), 'native accounting frame is missing')
    for key in ('job_before', 'job_after', 'monitor_before', 'monitor_after'):
        _cpu(frame[key], key, job=key.startswith('job'))
    system = frame['system']
    for key in ('idle_ticks', 'kernel_ticks', 'user_ticks', 'total_ticks', 'busy_ticks'):
        _number(system[key], 'system ' + key, minimum=0, integer=True)
    _require(system['idle_ticks'] <= system['kernel_ticks'], 'system idle exceeds kernel')
    _require(system['total_ticks'] == system['kernel_ticks'] + system['user_ticks'], 'system total differs')
    _require(system['busy_ticks'] == system['total_ticks'] - system['idle_ticks'], 'system busy differs')
    _bracket(system, 'system')
    order = ('job_before', 'monitor_before', 'system', 'monitor_after', 'job_after')
    for left, right in zip(order, order[1:]):
        _require(frame[left]['read_finished_monotonic'] <= frame[right]['read_started_monotonic'],
                 'native accounting bracket order differs')
    for kind in ('job', 'monitor'):
        a, b = frame[kind + '_before'], frame[kind + '_after']
        for key in ('user_ticks', 'kernel_ticks'):
            _require(b[key] >= a[key], kind + ' CPU regressed within query')
    a, b = frame['monitor_before'], frame['monitor_after']
    identity = a['pid'], a['creation_ticks']
    _require(identity == (b['pid'], b['creation_ticks']) and identity[0] == monitor_pid,
             'coordinator identity differs within query/policy')
    return identity


def _point(frame, kind):
    return (frame[kind + '_before']['cpu_ticks'] + frame[kind + '_after']['cpu_ticks']) / 2


def validate_job_receipts(log_path, summary, final_job, policy_dict):
    """Replay raw frames, signed rolling windows, summary and final Job state.

    policy_dict contains LoadPolicy fields and optionally idle_probe_seconds.
    Sample endpoints are reconstructed by summing recorded gap_seconds because
    the original guard records no absolute monotonic sample-end field.
    """
    try:
        return _validate(Path(log_path), summary, final_job, policy_dict)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('Job receipt is malformed: ' + str(error)) from error


def _validate(log_path, summary, final_job, policy_dict):
    settings = dict(policy_dict)
    idle_required = settings.pop('idle_probe_seconds', settings.get('host_window_seconds', 2.))
    _number(idle_required, 'required idle seconds', minimum=0)
    policy = LoadPolicy(**settings)
    expected_policy = json.loads(json.dumps(asdict(policy)))
    lines = log_path.read_text(encoding='utf-8').splitlines()
    _require(bool(lines) and all(line.strip() for line in lines), 'empty log or blank record')
    rows = [json.loads(line) for line in lines]
    _require(all(isinstance(row, dict) for row in rows), 'non-object record')
    header = rows[0]
    _require(header.get('event') == 'policy' and sum(r.get('event') == 'policy' for r in rows) == 1,
             'exactly one leading policy record required')
    for record in (header, summary):
        _require(record.get('schema_version') == 3 and record.get('accounting_method') == METHOD
                 and record.get('approximate') is True, 'accounting method/schema differs')
        _require(record.get('residual_cpu_policy') == 'warning-only', 'residual CPU policy differs')
        _require(record.get('policy') == expected_policy, 'frozen policy differs')
        _require(record.get('cpu_ticks_per_second') == 10_000_000 and record.get('platform') == 'win32',
                 'platform/counter units differ')
    monitor_pid = _number(header['monitor_pid'], 'monitor PID', minimum=1, integer=True)
    _require(summary['monitor_pid'] == monitor_pid, 'summary monitor PID differs')
    _require(summary.get('external_cpu_tolerance_ticks') == 0, 'external CPU tolerance is not zero')
    _require(type(summary['valid_for_load_check']) is bool and type(summary['preflight_complete']) is bool,
             'summary validity flags are not booleans')
    clean = summary['valid_for_load_check']
    invalid, failures = [], []

    def mark(reason):
        if reason not in invalid:
            invalid.append(reason)

    def failure(value, where):
        if value is not None:
            _require(isinstance(value, dict) and isinstance(value.get('kind'), str)
                     and isinstance(value.get('reason'), str), where + ' failure is malformed')
            failures.append(value)
            mark(where + ': ' + value['kind'])

    failure(summary.get('failure'), 'summary')
    intervals = deque()
    previous = None
    identity = None
    previous_external = None
    end = max_gap = max_duration = job_sum = monitor_sum = underflow = max_fraction = external_sum = 0.
    accepted_count = sample_count = window_count = timer_samples = 0
    warning_count = warning_episodes = 0
    warning_peak = 0.
    warning_active = False
    idle_start = idle_end = None
    max_job = None
    attached = set()
    timer_started = timer_stopped = 0
    closed = 0
    for index, row in enumerate(rows[1:], 2):
        event = row.get('event')
        _require(isinstance(event, str), 'record event missing')
        if event != 'sample':
            failure(row.get('failure'), 'event ' + event)
            if event == 'attach':
                item = tuple(row['identity'])
                _require(len(item) == 2 and item not in attached, 'duplicate/invalid worker attach')
                attached.add(item)
            elif event == 'detach':
                item = tuple(row['identity'])
                _require(item in attached, 'worker detached without attach')
                attached.remove(item)
            elif event == 'timer_started':
                timer_started += 1
                _same(row['interval_seconds'], policy.interval_seconds, 'timer interval')
                _same(row['join_bound_seconds'], policy.max_sample_gap_seconds, 'timer wait bound')
            elif event == 'timer_stopped':
                timer_stopped += 1
                _same(row['timer_samples'], timer_samples, 'timer sample count')
            continue

        sample_count += 1
        _require(not closed, 'sample appears after closed sample')
        failure(row.get('failure'), 'sample')
        if row.get('phase') == 'timer':
            timer_samples += 1
        if row.get('phase') == 'closed':
            closed += 1
        _require(row.get('accounting_method') == METHOD, 'sample accounting method differs')
        _require(row.get('residual_cpu_policy') == 'warning-only', 'sample residual CPU policy differs')
        gap = _number(row['gap_seconds'], 'sample gap', minimum=0)
        duration = _number(row['snapshot_duration_seconds'], 'snapshot duration', minimum=0)
        end += gap
        max_gap, max_duration = max(max_gap, gap), max(max_duration, duration)
        if max(gap, duration) > policy.max_sample_gap_seconds:
            mark('sample gap/duration exceeds policy')
        accepted = row.get('accounting_baseline_accepted')
        _require(type(accepted) is bool, 'accounting baseline acceptance flag missing')
        frame = row.get('native_accounting')
        if not accepted:
            _require(row.get('failure') is not None and 'host' not in row,
                     'unaccepted sample has no failure or still claims a host interval')
            mark('unaccepted native sample at record ' + str(index))
            # Failed snapshots never advance the accepted CPU baseline. Keep
            # their declared failure visible rather than inventing coverage.
            if frame is not None:
                try:
                    _frame(frame, monitor_pid)
                except (ValueError, KeyError, TypeError) as error:
                    mark('rejected native frame: ' + str(error))
            continue
        _require(frame is not None, 'accepted sample has no native frame')
        current_identity = _frame(frame, monitor_pid)
        if identity is None:
            identity = current_identity
        _require(current_identity == identity, 'coordinator identity changed between samples')
        accepted_count += 1
        system = frame['system']
        _require(row['system'] == {key: system[key] for key in ('idle_ticks', 'kernel_ticks', 'user_ticks')},
                 'sample system differs from native frame')
        _number(row['wall_ticks'], 'creation-clock timestamp', minimum=1, integer=True)
        max_job = frame['job_after']

        external = {}
        for process in row['processes']:
            if process.get('owned') is False:
                _require(process.get('error') is None, 'external process access error')
                key = process['pid'], process['creation_ticks']
                _require(key not in external, 'duplicate external process identity')
                external[key] = _number(process['cpu_ticks'], 'external process CPU', minimum=0, integer=True)
        if previous_external is not None:
            if set(external) != set(previous_external):
                mark('known external identity set changed')
            for key in set(external) & set(previous_external):
                delta = external[key] - previous_external[key]
                _require(delta >= 0, 'external CPU counter regressed')
                external_sum += delta
        previous_external = external

        if previous is not None:
            old_frame, old_wall = previous
            for kind in ('job', 'monitor'):
                before, after = frame[kind + '_before'], old_frame[kind + '_after']
                for key in ('user_ticks', 'kernel_ticks'):
                    _require(before[key] >= after[key], kind + ' CPU counter regressed between samples')
            old_system = old_frame['system']
            for key in ('idle_ticks', 'kernel_ticks', 'user_ticks'):
                _require(system[key] >= old_system[key], 'system counter regressed')
            _require(row['wall_ticks'] >= old_wall, 'creation clock regressed')
            total = system['total_ticks'] - old_system['total_ticks']
            busy = system['busy_ticks'] - old_system['busy_ticks']
            _require(0 <= busy <= total, 'invalid system CPU delta')
            job_delta = _point(frame, 'job') - _point(old_frame, 'job')
            monitor_delta = _point(frame, 'monitor') - _point(old_frame, 'monitor')
            residual = busy - job_delta - monitor_delta
            job_sum += job_delta
            monitor_sum += monitor_delta
            underflow += max(0, -residual)
            intervals.append((end-gap, end, total, residual))
            cutoff = end - policy.host_window_seconds
            while intervals and intervals[0][1] <= cutoff:
                intervals.popleft()
            seconds = capacity = signed = 0.
            for left, right, count, other in intervals:
                overlap = max(0., right - max(left, cutoff))
                if overlap and right > left:
                    weight = overlap/(right-left)
                    seconds += overlap
                    capacity += count*weight
                    signed += other*weight
            ready = seconds >= policy.host_window_seconds - 1e-6 and capacity > 0
            fraction = max(0., signed)/capacity if capacity > 0 else None
            if ready:
                window_count += 1
                max_fraction = max(max_fraction, fraction)
            warning = ready and fraction > policy.max_nonworker_cpu_fraction
            warning_started = warning and not warning_active
            if warning:
                warning_count += 1
                warning_episodes += int(warning_started)
                warning_peak = max(warning_peak, fraction)
            warning_active = warning
            expected = dict(total_delta_ticks=total, busy_delta_ticks=busy, owned_delta_ticks=job_delta,
                experiment_job_delta_ticks=job_delta, coordinator_delta_ticks=monitor_delta,
                residual_signed_delta_ticks=residual, nonworker_delta_ticks=residual,
                accounting_underflow_ticks=max(0, -residual), window_seconds=seconds,
                window_total_ticks=capacity, window_nonworker_ticks=signed,
                window_fraction=fraction, window_ready=ready, approximate=True)
            host = row['host']
            _require(host.get('residual_cpu_policy') == 'warning-only', 'host residual CPU policy differs')
            expected.update(residual_load_warning=warning, residual_load_warning_episode_started=warning_started,
                residual_load_warning_threshold_fraction=policy.max_nonworker_cpu_fraction)
            for key, value in expected.items():
                _same(host[key], value, 'record ' + str(index) + ' host ' + key,
                      approximate=key.startswith('window_'))
        else:
            _require('host' not in row, 'first native baseline already claims a CPU interval')
        previous = frame, row['wall_ticks']
        if row.get('phase') == 'idle-start':
            _require(idle_start is None and not attached, 'repeated/occupied idle start')
            idle_start = end
        elif row.get('phase') == 'idle':
            _require(idle_start is not None and not attached, 'idle sample has no empty-worker baseline')
            idle_end = end

    _require(sample_count > 0, 'no raw samples')
    for key, value in dict(snapshot_count=accepted_count, max_sample_gap_seconds=max_gap,
            max_snapshot_duration_seconds=max_duration, experiment_job_delta_ticks=job_sum,
            coordinator_delta_ticks=monitor_sum, accounting_underflow_ticks=underflow,
            max_nonworker_cpu_fraction=max_fraction, external_cpu_increase_ticks=external_sum).items():
        _same(summary[key], value, 'summary ' + key,
              approximate=key == 'max_nonworker_cpu_fraction')
    for key, value in dict(residual_load_ready_window_count=window_count,
            residual_load_warning_window_count=warning_count, residual_load_warning_episode_count=warning_episodes,
            residual_load_warning_peak_fraction=warning_peak).items():
        _same(summary[key], value, 'summary ' + key,
              approximate=key == 'residual_load_warning_peak_fraction')
    if external_sum:
        mark('known external CPU increased')
    idle_span = 0. if idle_start is None or idle_end is None else idle_end-idle_start
    if summary['preflight_complete']:
        _number(summary['completed_idle_seconds'], 'completed idle', minimum=idle_required)
        # The require_idle timer starts after the first receipt write and ends
        # after the last write. Those write costs are outside sample endpoints;
        # permit only a small explicitly reported timing reconciliation slack.
        slack = .05
        _require(idle_span + slack >= idle_required and abs(summary['completed_idle_seconds']-idle_span) <= slack,
                 'claimed idle duration lacks raw coverage')
        _require(window_count > 0, 'completed idle has no ready CPU window')
    else:
        mark('idle preflight incomplete')
    if summary.get('timer') is not None:
        timer = summary['timer']
        _same(timer['samples'], timer_samples, 'summary timer samples')
        _same(timer['join_bound_seconds'], policy.max_sample_gap_seconds, 'summary timer wait bound')
        if timer.get('started') is not True or timer.get('alive') is not False or timer.get('failure') is not None:
            mark('sampler has not closed cleanly')
    else:
        mark('sampler summary missing')
    if timer_started != 1 or timer_stopped != 1 or closed != 1:
        mark('timer start/stop or final closed sample missing')
    if attached:
        mark('worker identities remain attached')
    if final_job is None:
        mark('final Job accounting missing')
    else:
        _cpu(final_job, 'final Job', job=True)
        if final_job['active_processes'] != 0:
            mark('final Job still has active processes')
        if max_job is not None:
            for key in ('user_ticks', 'kernel_ticks', 'cpu_ticks', 'total_processes'):
                _require(final_job[key] >= max_job[key], 'final Job is below last raw ' + key)
    if not clean:
        mark('summary explicitly invalid')
    if clean:
        _require(not invalid and summary.get('failure') is None, 'clean summary contradicts raw failure/coverage: ' + '; '.join(invalid))
    return {'status': 'INVALID' if invalid else 'PASS', 'valid_for_load_check': clean and not invalid,
        'counts': {'records': len(rows), 'samples': sample_count, 'accepted_samples': accepted_count,
                   'recomputed_intervals': max(0, accepted_count-1), 'ready_windows': window_count,
                   'failure_observations': len(failures), 'warning_windows': warning_count,
                   'warning_episodes': warning_episodes},
        'recomputed_metrics': {'experiment_job_delta_ticks': job_sum, 'coordinator_delta_ticks': monitor_sum,
            'accounting_underflow_ticks': underflow, 'external_cpu_increase_ticks': external_sum,
            'max_nonworker_cpu_fraction': max_fraction, 'max_sample_gap_seconds': max_gap,
            'max_snapshot_duration_seconds': max_duration, 'idle_sample_span_seconds': idle_span,
            'idle_write_reconciliation_slack_seconds': .05},
        'residual_cpu_policy': 'warning-only',
        'warnings': {'threshold_fraction': policy.max_nonworker_cpu_fraction,
                     'window_count': warning_count, 'episode_count': warning_episodes, 'peak_fraction': warning_peak},
        'invalid_errors': invalid, 'scope': 'Independent raw-counter arithmetic and recorded-coverage audit; asynchronous CPU estimates, not move-level isolation.'}
