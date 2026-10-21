"""Independent stopped-v5 integrity audit; no product imports or engine runs.

Every score remains excluded. Failed global numeric-parent candidates have no
verified creation identity and are recorded as unmeasurable, never owned.
"""
from __future__ import annotations
import argparse
from collections import Counter,deque
import ctypes
from ctypes import wintypes
from datetime import datetime,timezone
import importlib.util
import io
import json
import math
from pathlib import Path
import sys
import chess
import chess.pgn
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
CORE_PATH=HERE/'peer_game_audit_engine_cpu_v5.py'
CORE_SHA256='89e6d1f9c745c05bfd6138597e4e108109118c0382399788078c2bef59030220'
spec=importlib.util.spec_from_file_location('frozen_independent_v5_checks',CORE_PATH)
core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
helper=core.helper
for name in ('check','sha','read','utc','decoded','equal_number','identity','ancestry','invocation',
             'native_frame','native_cpu','native_assignment','midpoint','frozen_inputs','git_receipts','engine_cpu_records'):
    globals()[name]=getattr(core,name)
HELPER_PATH,HELPER_SHA256=core.HELPER_PATH,core.HELPER_SHA256

def observe_cleanup(identities, final_timers, active_roots, details):
    """Read current Windows process objects without starting/killing processes."""
    check(sys.platform == 'win32', 'Live cleanup observation requires Windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)]*4)]
    rows = []
    for pid, created in sorted(identities):
        handle = kernel.OpenProcess(0x101000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            rows.append({'pid': pid, 'creation_ticks': created,
                         'state': 'absent' if error == 87 else 'uninspectable', 'open_error': error})
            continue
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                rows.append({'pid': pid, 'creation_ticks': created, 'state': 'uninspectable', 'timer_error': ctypes.get_last_error()})
                continue
            actual_created = times[0].dwLowDateTime | times[0].dwHighDateTime << 32
            status = kernel.WaitForSingleObject(handle, 0)
            state = ('pid_reused' if actual_created != created else
                     'exited' if status == 0 else 'still_alive' if status == 258 else 'uninspectable')
            rows.append({'pid': pid, 'creation_ticks': created, 'observed_creation_ticks': actual_created,
                         'state': state, 'wait_status': status})
        finally:
            kernel.CloseHandle(handle)
    for row in rows:
        key = row['pid'], row['creation_ticks']
        row.update(details.get(key, {}))
        row['historical_expected_exit_timer_observed'] = key in final_timers
        if key in final_timers:
            row['historical_expected_final_cpu_ticks'] = final_timers[key]
        row['active_at_stop'] = key in active_roots
    counts = dict(Counter(row['state'] for row in rows))
    active = [row for row in rows if row['active_at_stop']]
    check(len(active) == len(active_roots) and all(row['state'] in ('absent', 'pid_reused', 'exited') for row in active),
          'A still-attached stop worker lacks current absent/reused/exited proof')
    historical_stopped = all(row['state'] in ('absent', 'pid_reused', 'exited')
                             or row['historical_expected_exit_timer_observed'] for row in rows)
    return {'observed_utc': datetime.now(timezone.utc).isoformat(), 'states': counts, 'processes': rows,
            'current_pid_observation_status': 'UNKNOWN' if counts.get('uninspectable') else 'LIVE' if counts.get('still_alive') else 'NO_RECORDED_IDENTITY_RUNNING',
            'all_recorded_owned_identities_no_longer_running': not counts.get('still_alive') and not counts.get('uninspectable'),
            'historical_expected_identities_all_have_stopped_evidence': historical_stopped,
            'active_stop_workers_current_absent_reused_or_exited': True,
            'scope': 'Historical exact held final timers with exited=true prove those expected identities exited, even when the current numeric PID cannot be opened or its current creation is unknown. Such current PID states remain UNKNOWN. Active stop workers require current absent/reused/exited evidence. No assertion that every process exited before the last stopped load sample; no engine launched or process terminated by this auditor.'}

def guard_records(target, plan, report):
    """Recalculate identities, deltas and overlapping host windows from raw fields."""
    path = target / 'load.jsonl'
    check(sha(path) == report['guard_log_sha256'], 'Raw guard file hash differs')
    summary = read(target / 'load-summary.json')
    started = read(target / 'run-started.json')
    check(started['pid'] == summary['monitor_pid'] and utc(started['started_utc']) <= utc(report['started_utc']),
          'Guard monitor identity differs from phase invocation')
    check(sha(target / 'load-summary.json') == report['guard_summary_sha256'], 'Guard summary hash differs')
    check(summary['failure']['kind']=='process_access' and summary['failure']['reason']=='cannot measure process PID 34924: OpenProcess error 5' and summary['valid_for_load_check'] is False and summary['preflight_complete'] is True,
          'Stopped worker-error phase must retain active-worker closure invalidity')
    check(summary['platform'] == 'win32' and summary['cpu_ticks_per_second'] == 10000000
          and summary['external_cpu_tolerance_ticks'] == 0, 'Native counter platform/rate differs')
    check(summary['schema_version'] == 3 and summary['residual_cpu_policy'] == 'warning-only' and summary['accounting_method'] == 'windows-job-plus-coordinator-cpu'
          and summary['approximate'] is True, 'Wrong Job accounting schema/scope')
    final_job = read(target/'job-final-accounting.json')
    check(sha(target/'job-final-accounting.json') == report['job_final_accounting_sha256'], 'Final Job hash differs')
    check(final_job['active_processes'] == 0 and final_job['coordinator_in_job'] is False, 'Final Job is not empty/outside coordinator')
    policy = summary['policy']
    check(all(policy[key] == value for key, value in plan['load_policy'].items()), 'Guard policy differs from freeze')
    check(.5 <= policy['interval_seconds'] <= 1 and policy['max_sample_gap_seconds'] == 2
          and policy['host_window_seconds'] == 2 and policy['max_nonworker_cpu_fraction'] == .20, 'Formal CPU policy differs')
    check(summary['completed_idle_seconds'] >= plan['idle_probe_seconds'] >= 2, 'Idle preflight incomplete')
    active, known, attached, detached, observed_owned = {}, {}, {}, {}, {}
    observed_details={}
    retired, final_timers, retirement_counts = {}, {}, Counter()
    external_before, owned_before, previous, previous_end = None, {}, None, None
    end, samples, max_gap, max_duration, maximum_fraction, maximum_ready_fraction = 0., 0, 0., 0., 0., 0.
    intervals, external_identities = deque(), set()
    ready_windows, underflow_windows, idle_start, idle_end = 0, 0, None, None
    timer_failed=0
    unmeasurable=[]
    first_error_sample=None
    warning_windows = warning_episodes = 0
    warning_peak = 0.
    previous_warning = False
    worker_events, phase_events = [], Counter()
    timer_started = timer_stopped = False
    host_progress_end = None
    previous_utc = None
    first_frame = last_frame = None
    job_delta_sum = coordinator_delta_sum = underflow_sum = 0.
    with path.open(encoding='utf-8') as stream:
        for number, text in enumerate(stream, 1):
            check(text.strip(), 'Blank line in native load receipt')
            row = decoded(text)
            instant = utc(row['utc'])
            check(previous_utc is None or instant >= previous_utc, 'Load UTC clock regressed')
            previous_utc = instant
            event = row['event']
            if number == 1:
                check(row['schema_version'] == 3 and row['residual_cpu_policy'] == 'warning-only' and row['accounting_method'] == summary['accounting_method'] and row['approximate'] is True, 'Policy accounting scope differs')
                check(event == 'policy' and row['policy'] == policy and row['monitor_pid'] == summary['monitor_pid']
                      and row['cpu_ticks_per_second'] == 10000000 and row['platform'] == 'win32', 'Missing native policy header')
                continue
            if event=='timer_failed':
                check(first_error_sample is not None and row['failure']==summary['failure'],'Sampler failure lacks preceding raw process-access error')
                timer_failed+=1
                continue
            if event == 'timer_started':
                check(not timer_started and not active and row['interval_seconds'] == policy['interval_seconds']
                      and row['join_bound_seconds'] == policy['max_sample_gap_seconds'], 'Timer startup differs from frozen policy')
                timer_started = True
                continue
            if event == 'timer_stopped':
                check(timer_started and not timer_stopped and row['timer_samples'] == phase_events['timer'], 'Timer stop counter differs')
                timer_stopped = True
                continue
            if event == 'attach':
                root = tuple(row['identity'])
                check(len(root) == 2 and all(type(v) is int and v > 0 for v in root)
                      and root[0] not in active and root not in attached, 'Duplicate/invalid worker attach')
                check(idle_end is not None and idle_end - idle_start >= plan['idle_probe_seconds'] - .01,
                      'Worker attached before complete sampled idle window')
                check(timer_started and not timer_stopped, 'Worker attached without active sampler timer')
                active[root[0]], known[root], attached[root] = root, root, row['utc']
                worker_events.append(('attach', root, row['utc']))
                continue
            if event == 'detach':
                root = tuple(row['identity'])
                check(active.get(root[0]) == root and root not in detached, 'Unmatched worker detach')
                check(root in owned_before and owned_before[root]['exited'] is True, 'Detached worker lacks exited final CPU timer')
                del active[root[0]]
                detached[root] = row['utc']
                worker_events.append(('detach', root, row['utc']))
                continue
            check(event=='sample' and (row['failure'] is None or row['failure']==summary['failure']),
                  'Unexpected guard failure/event outside stopped close')
            if row['failure'] is not None and first_error_sample is None:first_error_sample=row['utc']
            if first_error_sample is not None:check(row['failure']==summary['failure'],'Process failure disappeared after first observation')
            roots = [tuple(value) for value in row['roots']]
            check(len(roots) == len(set(roots)) and set(roots) == set(active.values()), 'Sample roots do not match attaches')
            gap, duration = row['gap_seconds'], row['snapshot_duration_seconds']
            check(type(gap) in (float, int) and type(duration) in (float, int) and 0 <= duration <= gap + 1e-8
                  and max(gap, duration) <= policy['max_sample_gap_seconds'], 'Sample gap/duration exceeds policy')
            end += gap
            max_gap, max_duration = max(max_gap, gap), max(max_duration, duration)
            phase_events[row['phase']] += 1
            if row['phase'] == 'timer':
                check(timer_started and not timer_stopped, 'Timer sample outside active timer lifecycle')
            if row['phase'] == 'idle-start':
                check(idle_start is None and not active, 'Invalid initial idle sample')
                idle_start = end
            if row['phase'] == 'idle':
                check(idle_start is not None and not active, 'Idle sample contains workers')
                idle_end = end
            system = row['system']
            check(all(type(system[key]) is int and system[key] >= 0 for key in ('idle_ticks', 'kernel_ticks', 'user_ticks'))
                  and system['idle_ticks'] <= system['kernel_ticks'] and type(row['wall_ticks']) is int and row['wall_ticks'] > 0,
                  'Invalid raw native system counters')
            check(row['accounting_method'] == summary['accounting_method'] and row['residual_cpu_policy'] == 'warning-only' and row['accounting_baseline_accepted'] is True,
                  'Clean native accounting baseline was not accepted')
            frame = row['native_accounting']
            native_frame(frame, summary['monitor_pid'])
            check(all(system[key] == frame['system'][key] for key in system), 'System frame differs from load counters')
            if first_frame is None:
                first_frame = frame
                check(frame['job_before']['cpu_ticks'] == frame['job_after']['cpu_ticks'] == 0
                      and frame['job_after']['total_processes'] == 0, 'Initial Job was not empty')
            if last_frame is not None:
                for kind in ('job', 'monitor'):
                    check(frame[kind+'_before']['cpu_ticks'] >= last_frame[kind+'_after']['cpu_ticks'], 'Native Job/monitor counter regressed')
                check((frame['monitor_before']['pid'], frame['monitor_before']['creation_ticks']) ==
                      (last_frame['monitor_after']['pid'], last_frame['monitor_after']['creation_ticks']), 'Coordinator identity changed')
            last_frame = frame
            processes = {item['pid']: item for item in row['processes']}
            check(len(processes) == len(row['processes']), 'Duplicate sampled PID')
            owned, external = {}, {}
            for item in row['processes']:
                if item.get('error'):
                    check(row['failure']==summary['failure'] and item['error']=='OpenProcess error 5'
                          and item['creation_ticks'] is None and item['cpu_ticks'] is None
                          and 'owned' not in item and 'worker_root' not in item,
                          'Unmeasurable process was assigned an unproven identity/owner')
                    check((item['pid'],item['parent_pid'],item['name']) in {
                        (34924,3144,'csrss.exe'),(11368,3144,'winlogon.exe'),
                        (4380,11368,'fontdrvhost.exe'),(25884,11368,'dwm.exe')},
                          'Unexpected access-failed numeric-parent candidate')
                    unmeasurable.append(dict(sample_utc=row['utc'],**item))
                    continue
                key = identity(item)
                observed_details[key]={'historical_name':item['name'],'historical_parent_pid':item['parent_pid']}
                check(not item.get('error'), 'Process measurement error')
                owner = known.get(key)
                derived_chain = None
                if owner is None:
                    for root in roots:
                        derived_chain = ancestry(item, processes, root)
                        if derived_chain is not None:
                            owner, known[key] = root, root
                            break
                check(item['owned'] is (owner is not None) and
                      (tuple(item['worker_root']) if item['worker_root'] else None) == owner,
                      'Owned process classification differs from checked ancestry')
                if item.get('ancestry') is not None:
                    check(derived_chain == item['ancestry'], 'Recorded process ancestry differs')
                if owner is not None:
                    owned[key] = item
                    observed_owned.setdefault(key, owner)
                    check(observed_owned[key] == owner, 'Process identity changed worker owner')
                elif item['name'].casefold() in {name.casefold() for name in policy['engine_names']} or key[0] in policy['extra_engine_pids']:
                    check(item['exited'] is False, 'Known external engine exited during snapshot')
                    external[key] = item
                    external_identities.add(key)
            for item in row['retained_owned']:
                key = identity(item)
                check(key in known, 'Retained timer lacks prior checked ownership')
                if key in owned:
                    check(item['cpu_ticks'] >= owned[key]['cpu_ticks'], 'Retained CPU counter precedes live sample')
                owned[key] = item
                observed_owned.setdefault(key, known[key])
                check(known[key] in roots or item['exited'] is True, 'Live owned orphan after detach')
            for root in roots:
                current = processes.get(root[0])
                check((current is not None and identity(current) == root) or
                      (current is None and root in owned and owned[root]['exited'] is True), 'Worker identity missing/reused')
                if current is not None:
                    check(current['parent_pid'] == summary['monitor_pid'], 'Worker is not the recorded monitor direct subprocess')
            for key, item in owned.items():
                if key in final_timers:
                    check(item['exited'] is True and item['cpu_ticks'] == final_timers[key], 'Final accounted process CPU/exit state changed')
                if item['exited'] is True:
                    final_timers[key] = item['cpu_ticks']
            for retirement in row['retirement_events']:
                key = tuple(retirement['identity'])
                check(key in known and key in final_timers and retirement['cpu_ticks'] == final_timers[key],
                      'Retirement event lacks a checked final owned CPU timer')
                action = retirement['event']
                if action == 'retired_timer_held':
                    check(key not in retired, 'Duplicate held retired timer')
                    retired[key] = retirement['cpu_ticks']
                elif action == 'stale_owned_entry':
                    check(retirement['original_error'] == 'OpenProcess error 5'
                          and retirement['proof'] == 'exact-held-exited-process', 'Stale entry lacks declared exact held-exited proof')
                    check(key in retired or key in owned_before, 'Stale entry has no previously retained exact timer')
                elif action == 'retired_timer_closed':
                    check(key in retired and retired.pop(key) == retirement['cpu_ticks']
                          and type(retirement['absent_snapshots']) is int and retirement['absent_snapshots'] == 2,
                          'Closed retired timer lacks prior held identity/two-absence receipt')
                    check(key[0] not in processes, 'Closed retired PID still appears in emitted inventory')
                else:
                    check(False, 'Unknown retirement event')
                retirement_counts[action] += 1
            check(type(row['retired_handle_count']) is int and row['retired_handle_count'] == len(retired),
                  'Retired handle count differs from recorded lifecycle')
            if external_before is not None:
                check(set(external) == set(external_before), 'Known external PID+creation set changed')
                check(all(external[key]['cpu_ticks'] == external_before[key]['cpu_ticks'] for key in external),
                      'Known external engine CPU counter changed (zero-tolerance policy)')
            external_before = external
            if host_progress_end is None:
                host_progress_end = end
            if previous is not None:
                total = system['kernel_ticks'] + system['user_ticks'] - previous['system']['kernel_ticks'] - previous['system']['user_ticks']
                idle = system['idle_ticks'] - previous['system']['idle_ticks']
                check(total >= 0 and 0 <= idle <= total and row['wall_ticks'] >= previous['wall_ticks']
                      and all(system[key] >= previous['system'][key] for key in ('kernel_ticks', 'user_ticks')), 'Native counters regressed')
                if total > 0:
                    host_progress_end = end
                else:
                    check(end-host_progress_end < policy['interval_seconds'], 'Native aggregate CPU counter stalled for a sample interval')
                owned_delta = 0
                for key, item in owned.items():
                    if key in owned_before:
                        difference = item['cpu_ticks'] - owned_before[key]['cpu_ticks']
                        check(difference >= 0, 'Owned native CPU counter regressed')
                        owned_delta += difference
                    elif key[1] >= previous['wall_ticks']:
                        owned_delta += item['cpu_ticks']
                busy = total - idle
                before_frame = previous['native_accounting']
                job_delta = midpoint(frame, 'job')-midpoint(before_frame, 'job')
                coordinator_delta = midpoint(frame, 'monitor')-midpoint(before_frame, 'monitor')
                check(job_delta >= 0 and coordinator_delta >= 0, 'Native Job/coordinator delta regressed')
                nonworker = busy-job_delta-coordinator_delta
                job_delta_sum += job_delta
                coordinator_delta_sum += coordinator_delta
                underflow_sum += max(0, -nonworker)
                # Windows counters are quantized; zero-tick intervals still
                # occupy wall time in the complete two-second window.
                intervals.append((previous_end, end, total, nonworker))
                cutoff = end - policy['host_window_seconds']
                while intervals and intervals[0][1] <= cutoff:
                    intervals.popleft()
                window_total = window_nonworker = seconds = 0.
                for left, right, ticks, other in intervals:
                    overlap = max(0., right - max(left, cutoff))
                    if overlap and right > left:
                        fraction = overlap / (right - left)
                        window_total += ticks * fraction
                        window_nonworker += other * fraction
                        seconds += overlap
                fraction = max(0., window_nonworker) / window_total if window_total > 0 else None
                ready = seconds >= policy['host_window_seconds'] - 1e-6 and window_total > 0
                expected = {'total_delta_ticks': total, 'busy_delta_ticks': busy, 'owned_delta_ticks': job_delta,
                            'experiment_job_delta_ticks': job_delta, 'coordinator_delta_ticks': coordinator_delta,
                            'observed_process_delta_ticks_diagnostic': owned_delta,
                            'residual_signed_delta_ticks': nonworker, 'nonworker_delta_ticks': nonworker,
                            'accounting_underflow_ticks': max(0, -nonworker),
                            'window_seconds': seconds, 'window_total_ticks': window_total,
                            'window_nonworker_ticks': window_nonworker, 'window_fraction': fraction,
                            'window_ready': ready, 'approximate': True,
                            'residual_cpu_policy': 'warning-only',
                            'residual_load_warning_threshold_fraction': policy['max_nonworker_cpu_fraction'],
                            'residual_load_warning': bool(ready and fraction > policy['max_nonworker_cpu_fraction']),
                            'residual_load_warning_episode_started': bool(ready and fraction > policy['max_nonworker_cpu_fraction'] and not previous_warning)}
                check(set(row['host']) == set(expected), 'Host-accounting fields differ')
                for key, value in expected.items():
                    equal_number(row['host'][key], value, 'Host independent recomputation differs: ' + key)
                if fraction is not None:
                    maximum_fraction = max(maximum_fraction, fraction)
                if ready:
                    maximum_ready_fraction = max(maximum_ready_fraction, fraction)
                    ready_windows += 1
                warning = bool(ready and fraction > policy['max_nonworker_cpu_fraction'])
                warning_windows += int(warning)
                warning_episodes += int(warning and not previous_warning)
                if warning:
                    warning_peak = max(warning_peak, fraction)
                previous_warning = warning
                underflow_windows += int(nonworker < 0)
            else:
                check('host' not in row, 'First sample has no previous native counter baseline')
            for key in row.get('retiring_owned', []):
                key = tuple(key)
                check(key in owned and owned[key]['exited'] is True and key not in roots, 'Retired timer is not an exited child')
            previous, previous_end, owned_before = row, end, owned
            samples += 1
    check(len(active)==4 and set(detached)<set(attached), 'Stopped active/detached worker boundary differs')
    check(phase_events['closed'] == 1 and previous['phase'] == 'closed' and phase_events['phase-completed'] == 0,
          'Missing final completion/closed native samples')
    check(ready_windows > 0 and samples == summary['snapshot_count'], 'Sample count/window differs')
    timer = summary['timer']
    check(timer_started and timer['started'] is True and timer['alive'] is False and isinstance(timer['failure'],dict) and timer['failure']['kind']==summary['failure']['kind'] and timer['failure']['reason']==summary['failure']['reason']
          and timer['samples'] == phase_events['timer'] and timer['samples'] > 0
          and timer['join_bound_seconds'] == policy['max_sample_gap_seconds'], 'Sampler final lifecycle/summary differs')
    equal_number(summary['max_sample_gap_seconds'], max_gap, 'Maximum sample gap differs')
    equal_number(summary['max_snapshot_duration_seconds'], max_duration, 'Maximum snapshot duration differs')
    equal_number(summary['max_nonworker_cpu_fraction'], maximum_ready_fraction, 'Maximum ready host nonworker fraction differs')
    equal_number(summary['residual_load_ready_window_count'], ready_windows, 'Ready warning window count differs')
    equal_number(summary['residual_load_warning_window_count'], warning_windows, 'Warning window count differs')
    equal_number(summary['residual_load_warning_episode_count'], warning_episodes, 'Warning episode count differs')
    equal_number(summary['residual_load_warning_peak_fraction'], warning_peak, 'Warning peak differs')
    check(timer_failed==1 and first_error_sample is not None and utc(first_error_sample)<=utc(summary['failure']['utc'])<=previous_utc and len(unmeasurable)==8,'Raw process failure coverage differs')
    check(summary['external_cpu_increase_ticks'] == 0, 'Guard summary external CPU differs')
    equal_number(summary['experiment_job_delta_ticks'], job_delta_sum, 'Summary Job delta differs')
    equal_number(summary['coordinator_delta_ticks'], coordinator_delta_sum, 'Summary coordinator delta differs')
    equal_number(summary['accounting_underflow_ticks'], underflow_sum, 'Summary signed underflow differs')
    equal_number(job_delta_sum, midpoint(last_frame, 'job')-midpoint(first_frame, 'job'), 'Job deltas do not telescope')
    for field in ('user_ticks', 'kernel_ticks', 'cpu_ticks', 'total_processes', 'active_processes'):
        check((final_job[field]>=last_frame['job_after'][field] if field!='active_processes' else final_job[field]==0), 'Final Job receipt differs from closed raw frame: '+field)
    check(final_job['total_processes'] >= len(observed_owned), 'Job process count is smaller than observed owned identities')
    recorded_workers = {(item['pid'], item['creation_ticks']): item for item in summary['workers']}
    check(len(recorded_workers) == len(summary['workers']) == len(attached) and set(recorded_workers) == set(attached),
          'Guard worker summary differs from attach events')
    for key, item in recorded_workers.items():
        check(item['attached_utc'] and utc(item['attached_utc'])<=utc(attached[key]), 'Worker attach timestamp differs')
        if key in detached:
            check(item['detached_utc'] and utc(item['detached_utc'])<=utc(detached[key]) and utc(attached[key])<utc(detached[key]), 'Worker detach timestamp differs')
        else:
            check(item['detached_utc'] is None, 'Active stopped worker falsely detached')
    return {'monitor_pid': summary['monitor_pid'], 'samples': samples, 'ready_host_windows': ready_windows, 'known_external_identities': [list(key) for key in sorted(external_identities)],
            'observed_known_external_cpu_delta_ticks': 0, 'max_sample_gap_seconds': max_gap,
            'max_snapshot_duration_seconds': max_duration, 'max_host_nonworker_fraction_including_partial_windows': maximum_fraction,
            'max_host_nonworker_fraction_ready_windows': maximum_ready_fraction,
            'residual_cpu_policy': 'warning-only', 'residual_load_warning_window_count': warning_windows,
            'residual_load_warning_episode_count': warning_episodes, 'residual_load_warning_peak_fraction': warning_peak,
            'accounting_underflow_intervals': underflow_windows, 'accounting_underflow_ticks': underflow_sum,
            'experiment_job_delta_ticks': job_delta_sum, 'coordinator_delta_ticks': coordinator_delta_sum,
            'final_job': final_job,'last_raw_job':last_frame['job_after'],
            'job_cpu_ticks_after_last_raw_sample':final_job['cpu_ticks']-last_frame['job_after']['cpu_ticks'],
            'unmeasurable_numeric_parent_candidates':unmeasurable,'first_access_failure_utc':summary['failure']['utc'],
            'first_failed_sample_started_utc':first_error_sample,
            'failure':summary['failure'],'valid_for_load_check':False, 'accounting_method': summary['accounting_method'], 'policy': policy,
            'timer': timer, 'timer_stopped_event_recorded': timer_stopped,
            'retirement_events': dict(retirement_counts), 'retired_handles_at_last_sample_before_provider_close': len(retired),
            'scope': 'Job cumulative CPU includes exited descendants; coordinator is independently subtracted. Raw signed residual and two-second interpolation are recomputed without product validators. Process/ancestry and retirement remain sampled provider receipts, not full OS inventory; asynchronous quantized estimates do not certify per-app causes, individual search contention, power, thermals or OS isolation.'}, attached, detached, observed_owned, worker_events, final_timers, observed_details

def inspect_partial(directory,target,phase,plan,frozen,profiles,banks,cells,report,by_launch,attached,detached,observed_owned,lane_by_root):
    completed={row['id'] for row in report['cells']};active={row['id'] for row in report['active_cells_at_stop']}
    remaining=set(by_launch)-completed
    check(len(remaining)==4 and len(active)==4 and active==remaining,
          'Failed/active partial cell boundary differs')
    results=[]
    for identifier in sorted(remaining):
        index=next(i for i,cell in enumerate(cells) if cell['id']==identifier);cell=cells[index];launch=by_launch[identifier]
        cell_dir=target/'cells'/identifier
        invoked=invocation(cell_dir,cell,profiles[cell['candidate']],profiles[cell['opponent']],plan,frozen,launch,phase,index,directory)
        root=(launch['pid'],launch['creation_ticks']);check(root in attached,'Partial worker was never registered')
        check(root not in detached,'Active stopped worker was detached')
        native_assignment(launch['job_assignment']);lane_by_root[root]=cell['lane']
        row=dict(cell,worker_identity=list(root),counted_as_completed=False,scores_excluded=True,
                 artifact_sha256={p.name:sha(p) for p in cell_dir.iterdir() if p.is_file()})
        if (cell_dir/'games.json').exists():
            metadata=read(cell_dir/'games.json');check(metadata['formal_cell']==cell and metadata['formal_git_receipts']==frozen['git_receipts'],'Partial metadata context differs')
            row.update(reported_complete=metadata['completed'],exit_code=metadata['exit_code'],errors=metadata['errors'],searches=sum(len(game['search_moves']) for game in metadata['games']))
            for role,key in (('baseline','opponent'),('candidate','candidate')):
                engine=metadata['engines'][role];profile=profiles[cell[key]]
                check(engine['command']==profile['command'] and engine['sha256']==profile['binary']['sha256'],'Partial declared engine identity differs')
        else:
            row['partial_metadata_missing']=True
        results.append(row)
    return results

def audit(directory, phase):
    directory = Path(directory).resolve()
    sha(__file__)
    # Check the completion marker before loading large records or doing audits.
    report = read(directory / phase / 'report.json')
    check(phase=='main' and report['status']=='STOPPED' and report['valid_for_comparison'] is False,
          'Only stopped invalid v4 main is accepted')
    check(sha(CORE_PATH)==CORE_SHA256,'Frozen complete v5 auditor changed')
    check(sha(HELPER_PATH) == HELPER_SHA256, 'Independent helper changed')
    frozen, profiles, banks, candidates, opponents = frozen_inputs(directory)
    plan, target = frozen['plan'], directory / phase
    git_values = git_receipts(frozen, profiles, candidates)
    check(all(utc(item['verified_utc']) <= utc(report['started_utc']) for item in frozen['git_receipts']),
          'Git verification happened after measured phase started')
    cells = frozen['cells'][phase]
    pairs, games = (12, 24) if phase == 'preflight' else (384, 768)
    completed_pairs=97
    check(len(cells)==pairs==384 and len(report['cells'])==completed_pairs and len(report['launched_cells'])==101, 'Stopped completion boundary differs')
    check(report['phase'] == phase and report['frozen_plan_sha256'] == sha(directory / 'frozen-plan.json'), 'Report phase/freeze differs')
    check(report['scheduled_cells']==pairs and report['scheduled_games']==games==768 and report['completed_games']==194
          and report['uncompleted_scheduled_games']==574 and report['unstarted_pairs']==283,'Stopped phase counters differ')
    check(report['elo_estimate'] is None and report['promotion_performed'] is False and report['stop_reason']=='LoadGuardViolation: cannot measure process PID 34924: OpenProcess error 5', 'Unexpected promotion/error claim')
    check(utc(report['finished_utc']) >= utc(report['started_utc']), 'Phase timestamps regressed')
    guard, attached, detached, observed_owned, worker_events, final_timers, observed_details = guard_records(target, plan, report)
    by_report = {item['id']: item for item in report['cells']}
    by_launch = {item['id']: item for item in report['launched_cells']}
    check(len(by_report)==97 and len(by_launch)==101 and set(by_report)<set(by_launch)<={cell['id'] for cell in cells},'Stopped completed/launched sets differ')
    check(len(attached)==101 and len(detached)==97,'Stopped worker lifecycle differs')
    for lane in range(4):
        expected=[cell['id'] for cell in cells if cell['lane']==lane]
        launched=[cell['id'] for cell in report['launched_cells'] if cell['lane']==lane]
        check(launched==expected[:len(launched)],'Lane abandoned fixed schedule prefix')
    attached_by_pid = {}
    for key in attached:
        attached_by_pid.setdefault(key[0], []).append(key)
    coverage, outcomes, results, roots_used = Counter(), Counter(), [], set()
    cpu_cells = []
    # These event identities also prove that a lane never ran overlapping workers.
    lane_by_root = {}
    for actual in report['cells']:
        index=next(i for i,cell in enumerate(cells) if cell['id']==actual['id']);cell=cells[index]
        launch=by_launch[cell['id']]
        check(all(actual[key] == value == launch[key] for key, value in cell.items()) and actual['status'] == 'COMPLETE', 'Cell schedule differs')
        cell_dir = target / 'cells' / cell['id']
        metadata = read(cell_dir / 'games.json')
        check(sha(cell_dir / 'games.json') == actual['games_json_sha256'] and sha(cell_dir / 'games.pgn') == actual['pgn_sha256'], 'Cell result hashes differ')
        check(metadata['completed'] is True and metadata['errors'] == [] and metadata['exit_code'] == 0,
              'Incomplete/errored match was marked complete')
        check(metadata['formal_git_receipts'] == frozen['git_receipts'], 'Worker Git receipts leave sealed freeze envelope')
        checkout = metadata['harness_checkout']
        check(checkout['commit'] == git_values[('rev-parse', 'HEAD')]
              and checkout['status_porcelain'] == git_values[('status', '--porcelain')] and not checkout.get('error'),
              'Worker checkout context differs from root-verified freeze receipts')
        check(checkout['scope'] == 'Git objects/context verified by real root commands at input freeze and served from sealed exact-query receipts. Worker rechecks file/model/harness hashes at match start; no transient Git helper is launched in a measured worker.',
              'Worker checkout scope misstates freeze-time Git context')
        opening = banks[phase][cell['opening_index']]
        check(metadata['formal_cell'] == cell and metadata['selected_openings'] == [opening]
              and read(cell_dir / 'openings.json') == {'schema_version': 1, 'openings': [opening]}, 'Wrong cell opening')
        check(metadata['opening_file_sha256'] == sha(cell_dir / 'openings.json') and metadata['opening_source'] == str(cell_dir / 'openings.json'), 'Opening binding/hash differs')
        check(metadata['limits'] == {'nodes': None, 'depth': None, 'movetime_ms': cell['movetime_ms']}
              and metadata['max_plies'] == plan['max_plies'] and metadata['draw_policy'] == 'claim', 'Played limits differ')
        check(metadata['games_requested'] == len(metadata['games']) == 2, 'Missing/extra games in pair')
        check(metadata['python_version'] == plan['worker_runtime']['python_version']
              and metadata['chess_version'] == plan['worker_runtime']['chess_version'], 'Actual worker versions differ')
        candidate, opponent = profiles[cell['candidate']], profiles[cell['opponent']]
        invoked = invocation(cell_dir, cell, candidate, opponent, plan, frozen, launch, phase, index, directory)
        matching = [key for key in attached_by_pid.get(launch['pid'], [])
                    if utc(attached[key]) <= utc(invoked['started_utc']) <= utc(detached[key])]
        check(len(matching) == 1, 'Invocation PID does not join exactly one attached creation identity')
        root = matching[0]
        check(root == (launch['pid'], launch['creation_ticks']), 'Job assignment/registration creation identity differs')
        assignment = launch['job_assignment']
        check(launch['creation_ticks'] == assignment['creation_ticks']
              and assignment['pid'] == launch['pid'] and assignment['parent_pid'] == guard['monitor_pid'],
              'Job assignment identity differs from launched direct child')
        native_assignment(assignment)
        check(root not in roots_used, 'Same worker identity reused for multiple pairs')
        roots_used.add(root)
        lane_by_root[root] = cell['lane']
        mask, setting = plan['lane_affinity_masks'][cell['lane']], metadata['affinity']
        check(setting['expected_mask'] == setting['worker_mask'] == mask and len(setting['transports']) == 2, 'Observed worker affinity/transport count differs')
        for transport, profile in zip(setting['transports'], (opponent, candidate), strict=True):
            check(transport['affinity_mask'] == mask and transport['command'] == profile['command'], 'Actual engine affinity/command differs')
            transport_identity = transport['pid'], transport['creation_ticks']
            check(observed_owned.get(transport_identity) == root,
                  'Engine transport PID+creation is not observed under its registered worker identity')
        for role, profile in (('candidate', candidate), ('baseline', opponent)):
            engine = metadata['engines'][role]
            check(engine['command'] == profile['command'] and engine['sha256'] == profile['binary']['sha256'], 'Actual command/executable differs')
            helper.advertised_options(engine, profile['resolved_options'])
            local = profile['kind'] == 'regicide'
            check(engine['source_commit_object_verified'] is local and
                  engine['provided_source_commit'] == (profile['source']['commit'] if local else None), 'Source relationship claim differs')
            if local:
                commit = profile['source']['commit']
                check(git_values[('rev-parse', '--verify', commit+'^{commit}')].lower() == commit.lower(),
                      'Candidate source commit lacks exact root verification receipt')
            if not local:
                external = engine['external_source']
                check(external['manifest_sha256'] == sha(cell_dir / 'external-source.json')
                      and external['binary_sha256'] == profile['binary']['sha256'] and external['args'] == profile['command'][1:], 'External manifest/command changed')
                check(all(external[key] == profile['source'][key] for key in ('name', 'version', 'revision', 'source_url')), 'External source declaration differs')
        cpu_item=engine_cpu_records(metadata,cell,candidate,opponent,plan,root,observed_owned,cell_dir)
        check(actual['engine_job_scope_audits']==cpu_item['job_scope_audits'] and all(item['coordinator_pid']==guard['monitor_pid'] for item in cpu_item['job_scope_audits']),'Cell Job scope differs from independent raw proof')
        check(actual['engine_cpu_summary']==cpu_item['raw_recomputed_summary'], 'Coordinator cell CPU summary differs from independent raw values')
        check(actual['engine_cpu_trace_sha256']==sha(cell_dir/'engine-cpu.jsonl')
              and actual['engine_cpu_trace_audit']=={'status':'PASS','search_receipts':cpu_item['searches'],'processes':2},
              'Coordinator CPU trace receipt differs from raw complete lifecycle')
        cpu_cells.append(dict(cell_id=cell['id'],**cpu_item))
        pgn_stream = io.StringIO((cell_dir / 'games.pgn').read_text(encoding='utf-8'))
        points, pv_before = [], helper.MOVES['legal_pv_plies']
        for number, record in enumerate(metadata['games'], 1):
            check(record['game'] == number and record['pair'] == 1 and record['completed'] is True, 'Pair numbering/completion differs')
            parsed = chess.pgn.read_game(pgn_stream)
            check(parsed is not None, 'Missing PGN game')
            point = helper.game(record, parsed, opening, number == 1, metadata, cell)
            check(point is not None, 'Unfinished game in completed phase')
            points.append(point)
            coverage[(cell['candidate'], cell['opponent'], cell['movetime_ms'], cell['family_id'], number)] += 1
            outcomes[(cell['candidate'], cell['opponent'], cell['movetime_ms'], point)] += 1
        check(chess.pgn.read_game(pgn_stream) is None, 'Extra PGN game')
        counts = {'candidate_wins': points.count(1.), 'candidate_draws': points.count(.5), 'candidate_losses': points.count(0.),
                  'candidate_points': sum(points), 'candidate_score': sum(points)/2,
                  'completed_games': 2, 'incomplete_games': 0, 'unplayed_games': 0, 'attempted_games': 2, 'scheduled_games': 2}
        for summary in (metadata['summary'], actual['summary']):
            check(all(summary[key] == value for key, value in counts.items()), 'Independent WDL/points differs')
            pair = summary['pairs']
            check(len(pair) == 1 and pair[0]['pair'] == 1 and pair[0]['games'] == [1, 2] and pair[0]['completed'] is True
                  and pair[0]['candidate_points'] == sum(points) and pair[0]['candidate_score'] == sum(points)/2
                  and pair[0]['results'] == [game['result'] for game in metadata['games']], 'Paired summary differs')
        check(actual['pv_plies'] == helper.MOVES['legal_pv_plies'] - pv_before, 'Cell PV counter differs')
        results.append(dict(cell, **counts, worker_identity=list(root), invocation_sha256=sha(cell_dir/'invocation.json'),
                            games_json_sha256=actual['games_json_sha256'], pgn_sha256=actual['pgn_sha256']))
    check(roots_used<=set(detached) and len(roots_used)==97,'Completed worker subset differs')
    partial_cells=inspect_partial(directory,target,phase,plan,frozen,profiles,banks,cells,report,by_launch,attached,detached,observed_owned,lane_by_root)
    check(set(lane_by_root)==set(attached),'A launched worker has no completed/partial accounting')
    active_lanes = {}
    for event, root, _time in worker_events:
        lane = lane_by_root[root]
        if event == 'attach':
            check(lane not in active_lanes, 'Two workers overlapped on the same lane')
            active_lanes[lane] = root
        else:
            check(active_lanes.pop(lane, None) == root, 'Lane detach does not match running worker')
    check(set(active_lanes.values())==set(attached)-set(detached),'Stopped active lane registration differs')
    expected_coverage = {(candidate, opponent, milliseconds, opening['family_id'], color)
                         for candidate in candidates for opponent in opponents for milliseconds in plan['movetimes_ms']
                         for opening in banks[phase] for color in (1, 2)}
    check(set(coverage)<expected_coverage and len(coverage)==194 and set(coverage.values())=={1},'Completed partial coverage differs')
    cleanup=observe_cleanup(set(observed_owned)|set(attached),final_timers,set(attached)-set(detached),observed_details)
    strata = []
    for candidate in candidates:
        for opponent in opponents:
            for milliseconds in plan['movetimes_ms']:
                key = (candidate, opponent, milliseconds)
                strata.append({'candidate': candidate, 'opponent': opponent, 'movetime_ms': milliseconds,
                               'wins': outcomes[key+(1.,)], 'draws': outcomes[key+(.5,)], 'losses': outcomes[key+(0.,)]})
    check(all(sha(path) == digest for path, digest in list(helper.FILES.items())), 'Audit inputs changed while checking')
    return {'schema_version': 1, 'status': 'INTEGRITY_PASS_STOPPED_INVALID', 'phase': phase, 'directory': str(directory), 'plan_id': plan['id'],
            'audited_utc': datetime.now(timezone.utc).isoformat(), 'audit_script_sha256': sha(__file__),
            'independent_helper_sha256': sha(HELPER_PATH), 'scheduled_pairs':pairs,'audited_completed_pairs':97,'launched_pairs':101,'active_pairs_at_stop':4,'failed_setup_pairs':0,
            'scheduled_games':games,'audited_report_completed_games':194,'phase_complete':False,'partial_cells':partial_cells,'cleanup':cleanup,
            'all_match_scores_excluded':True,'stop_reason':report['stop_reason'],
            'prior_audit_failure':{'path':'peer-attempt-002-main-stopped.json','sha256':sha(HERE/'peer-attempt-002-main-stopped.json'),
                'correction':'First auditor incorrectly equated sample-begin UTC with failure UTC occurring inside that sample. Original script/output are preserved; this revision checks their chronological order.'},
            'replay': dict(helper.MOVES), 'cells': results, 'strata': strata, 'load': guard,
            'engine_cpu': {'cells':cpu_cells,'searches':sum(row['searches'] for row in cpu_cells),
                           'residual_policy':'warning-only','cpu_or_nps_failure_floor':None},
            'git_scope': 'Exact candidate commit, HEAD and porcelain-status queries succeeded at input freeze and their sealed receipts match every worker metadata copy. The hashed v4 coordinator source wraps its measured phase in the same exact-query cache; this is source/provenance verification, not an exhaustive OS process trace. These are freeze-time context, not fresh per-worker Git commands; file/model/harness identities are separately hash-checked.',
            'input_sha256': dict(sorted(helper.FILES.items())),
            'source_scope': 'File/model/artifact identities, declared local commit objects and native build receipt/source consistency checked. No independent source-to-binary rebuild; external version/revision/URL remain declarations. Direct worker executable and the two declared chess source files are hashed; these are not a complete frozen Python runtime.',
            'configuration_scope': 'Serialized actual command including direct executable/-s, file bindings, model SHA, accepted UCI option types/order, worker and launch-time engine affinity, PID+creation ancestry and raw counters checked. Worker self-reported executable/hash/versions/no_user_site and three environment variables match frozen bindings; no external OS environment readback, raw UCI wire transcript, general option readback or continuous affinity monitoring is claimed.',
            'score_scope': 'Legal mainlines, root/final FEN, rule endings and WDL are independently replayed. Raw PV legality and recorded mover score type/POV checked; search evaluations are not recomputed by another engine.',
            'valid_for_declared_local_comparison':False,'valid_for_comparison':False,'strength_eligible':False,
            'elo_estimate': None, 'promotion_performed': False,
            'claim_scope': 'Integrity audit of a stopped invalid partial run, with every score excluded. No model promotion, world ranking, causal evaluator improvement or complete OS/thermal isolation claim.'}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--phase',choices=('main',),default='main')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    check(not args.output.exists(), 'Audit output must be a new file')
    check(args.output.name.startswith('peer-') and args.output.suffix == '.json', 'Output must be peer-*.json')
    try:
        result = audit(args.directory, args.phase)
        code = 0
    except Exception as error:
        result = {'schema_version': 1, 'status': 'FAIL', 'phase': args.phase,
                  'audited_utc': datetime.now(timezone.utc).isoformat(), 'audit_script_sha256': sha(__file__),
                  'error': f'{type(error).__name__}: {error}', 'input_sha256': dict(sorted(helper.FILES.items())),
                  'strength_eligible': False, 'elo_estimate': None, 'promotion_performed': False}
        code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({key: result[key] for key in ('status','phase','audited_report_completed_games','error','replay') if key in result}, ensure_ascii=False))
    return code

if __name__=='__main__':raise SystemExit(main())
