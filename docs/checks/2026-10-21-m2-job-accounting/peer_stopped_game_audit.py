"""Independent raw audit of STOPPED/INVALID Job-accounting preflight 001.

No product imports, engines or original record changes. Completed game replay,
policy invalidity and post-stop Job/process cleanup are separate claims.
"""
from __future__ import annotations
import argparse
from collections import Counter, deque
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import importlib.util
import io
import json
import math
from pathlib import Path
import sys
import chess
import chess.pgn
sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
CORE_PATH = HERE/'peer_game_audit.py'
CORE_SHA256 = '1886134e6e406b310c1946362317d628429371f0ed52eaa7828f9402ec3a52a5'
STOPPED_PATH = HERE.parent/'2026-10-21-m2-formal'/'peer_preflight_stopped_audit.py'
STOPPED_SHA256 = 'f3b9fd45d037fbb45674b830558f6d50076eb7dfd945d36f977af3ce42414758'
spec = importlib.util.spec_from_file_location('independent_job_game_checks', CORE_PATH)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
helper = core.helper
check, sha, read, utc = core.check, core.sha, core.read, core.utc
decoded, equal_number = core.decoded, core.equal_number
identity, ancestry, invocation = core.identity, core.ancestry, core.invocation
native_frame, native_cpu, native_assignment, midpoint = core.native_frame, core.native_cpu, core.native_assignment, core.midpoint
HELPER_PATH, HELPER_SHA256 = core.HELPER_PATH, core.HELPER_SHA256


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

def inspect_partial_cells(directory, frozen, profiles, banks, cells, report, by_launch,
                          attached, detached, observed_owned, lane_by_root):
    rows = []
    phase = report["phase"]
    expected_cells = {cell['id']: cell for cell in cells}
    active_ids = {cell['id'] for cell in report['active_cells_at_stop']}
    completed_ids = {cell['id'] for cell in report['cells']}
    check(len(active_ids) == len(report["active_cells_at_stop"]) > 0 and active_ids == set(by_launch)-completed_ids, 'Stopped active cells differ from launched remainder')
    for stopped in report['active_cells_at_stop']:
        cell = expected_cells[stopped['id']]
        launch = by_launch[cell['id']]
        check(all(stopped[key] == value == launch[key] for key, value in cell.items())
              and stopped['pid'] == launch['pid'], 'Active cell schedule/PID differs')
        index = next(index for index, value in enumerate(cells) if value['id'] == cell['id'])
        cell_dir = directory/phase/'cells'/cell['id']
        invoked = invocation(cell_dir, cell, profiles[cell['candidate']], profiles[cell['opponent']],
                             frozen['plan'], frozen, launch, phase, index, directory)
        roots = [key for key in attached if key[0] == launch['pid'] and key not in detached
                 and utc(attached[key]) <= utc(invoked['started_utc'])]
        check(len(roots) == 1, 'Active invocation does not join one still-attached exact worker')
        root = roots[0]
        check(root == (launch['pid'], launch['creation_ticks']), 'Active launch creation differs')
        check(launch['job_assignment']['parent_pid'] == report['runtime_monitor_pid'], 'Active Job parent differs')
        native_assignment(launch['job_assignment'])
        lane_by_root[root] = cell['lane']
        hashes = {path.name: sha(path) for path in cell_dir.iterdir() if path.is_file()}
        check(read(cell_dir/'openings.json') == {'schema_version': 1, 'openings': [banks[phase][cell['opening_index']]]},
              'Active cell opening differs')
        item = dict(cell, worker_identity=list(root), artifact_sha256=hashes,
                    counted_in_report_completed_games=False, scores_excluded=True,
                    replay_scope='No partial/active game is included in the completed-game replay or comparison.')
        if (cell_dir/'games.json').is_file():
            metadata = read(cell_dir/'games.json')
            check(metadata['formal_cell'] == cell and metadata['formal_git_receipts'] == frozen['git_receipts'],
                  'Partial artifact context differs')
            for role, profile in (('candidate', profiles[cell['candidate']]), ('baseline', profiles[cell['opponent']])):
                actual = metadata['engines'][role]
                check(actual['command'] == profile['command'] and actual['sha256'] == profile['binary']['sha256'],
                      'Partial artifact engine identity differs')
                helper.advertised_options(actual, profile['resolved_options'])
            for transport in metadata['affinity']['transports']:
                check(transport['affinity_mask'] == frozen['plan']['lane_affinity_masks'][cell['lane']]
                      and observed_owned.get((transport['pid'], transport['creation_ticks'])) == root,
                      'Partial artifact transport lacks observed exact ownership/affinity')
            item.update(artifact_match_completed=metadata['completed'], artifact_exit_code=metadata['exit_code'],
                        artifact_errors=metadata['errors'], artifact_started_utc=metadata['started_at'],
                        artifact_reported_elapsed_seconds=metadata['total_elapsed_seconds'],
                        artifact_reported_game_count=len(metadata['games']),
                        artifact_reported_completed_games=metadata['summary']['completed_games'],
                        artifact_reported_incomplete_games=metadata['summary']['incomplete_games'])
        else:
            item.update(artifact_match_completed=None, artifact_reported_game_count=0,
                        artifact_note='Invocation/input records only; no games JSON was persisted.')
        rows.append(item)
    return rows

def guard_records(target, plan, report):
    """Recalculate identities, deltas and overlapping host windows from raw fields."""
    path = target / 'load.jsonl'
    check(sha(path) == report['guard_log_sha256'], 'Raw guard file hash differs')
    summary = read(target / 'load-summary.json')
    started = read(target / 'run-started.json')
    check(started['pid'] == summary['monitor_pid'] and utc(started['started_utc']) <= utc(report['started_utc']),
          'Guard monitor identity differs from phase invocation')
    check(sha(target / 'load-summary.json') == report['guard_summary_sha256'], 'Guard summary hash differs')
    check(summary['failure']['kind'] == 'host_nonworker_cpu' and summary['valid_for_load_check'] is False
          and summary['preflight_complete'] is True, 'Stopped guard must record the expected invalid host-load boundary')
    check(summary['platform'] == 'win32' and summary['cpu_ticks_per_second'] == 10000000
          and summary['external_cpu_tolerance_ticks'] == 0, 'Native counter platform/rate differs')
    check(summary['schema_version'] == 2 and summary['accounting_method'] == 'windows-job-plus-coordinator-cpu'
          and summary['approximate'] is True, 'Wrong Job accounting schema/scope')
    final_job = read(target/'job-final-accounting.json')
    check(sha(target/'job-final-accounting.json') == report['job_final_accounting_sha256'], 'Final Job hash differs')
    native_cpu(final_job, job=True)
    check(final_job['active_processes'] == 0 and final_job['total_processes'] == 35, 'Final Job cleanup/count differs')
    policy = summary['policy']
    check(all(policy[key] == value for key, value in plan['load_policy'].items()), 'Guard policy differs from freeze')
    check(.5 <= policy['interval_seconds'] <= 1 and policy['max_sample_gap_seconds'] == 2
          and policy['host_window_seconds'] == 2 and policy['max_nonworker_cpu_fraction'] == .20, 'Formal CPU policy differs')
    check(summary['completed_idle_seconds'] >= plan['idle_probe_seconds'] >= 2, 'Idle preflight incomplete')
    active, known, attached, detached, observed_owned = {}, {}, {}, {}, {}
    observed_details = {}
    retired, final_timers, retirement_counts = {}, {}, Counter()
    external_before, owned_before, previous, previous_end = None, {}, None, None
    end, samples, max_gap, max_duration, maximum_fraction, maximum_ready_fraction = 0., 0, 0., 0., 0., 0.
    intervals, external_identities = deque(), set()
    ready_windows, underflow_windows, idle_start, idle_end = 0, 0, None, None
    worker_events, phase_events = [], Counter()
    timer_started = timer_stopped = False
    first_failure, exceeded_windows, timer_failed_events = None, [], 0
    host_progress_end = None
    previous_utc = None
    first_frame = last_frame = maximum_window = None
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
                check(row['schema_version'] == 2 and row['accounting_method'] == summary['accounting_method'] and row['approximate'] is True, 'Policy accounting scope differs')
                check(event == 'policy' and row['policy'] == policy and row['monitor_pid'] == summary['monitor_pid']
                      and row['cpu_ticks_per_second'] == 10000000 and row['platform'] == 'win32', 'Missing native policy header')
                continue
            if event == 'timer_failed':
                check(first_failure is not None and row['failure'] == summary['failure'], 'Timer failure does not follow raw host-policy breach')
                timer_failed_events += 1
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
            if event in ('attach', 'detach'):
                check(first_failure is None, 'Worker identity lifecycle changed after first policy breach')
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
            check(event == 'sample', 'Unexpected guard event')
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
            check(row['accounting_method'] == summary['accounting_method'] and row['accounting_baseline_accepted'] is True,
                  'Measured native baseline was not accepted')
            frame = row['native_accounting']
            native_frame(frame, summary['monitor_pid'])
            check(all(system[key] == frame['system'][key] for key in system), 'System frame differs from sampled counters')
            if first_frame is None:
                first_frame = frame
                check(frame['job_before']['cpu_ticks'] == frame['job_after']['cpu_ticks'] == 0
                      and frame['job_after']['total_processes'] == 0, 'Initial Job was not empty')
            if last_frame is not None:
                for kind in ('job', 'monitor'):
                    for field in ('user_ticks', 'kernel_ticks', 'cpu_ticks'):
                        check(frame[kind+'_before'][field] >= last_frame[kind+'_after'][field], 'Native cumulative counter regressed')
                check((frame['monitor_before']['pid'], frame['monitor_before']['creation_ticks']) ==
                      (last_frame['monitor_after']['pid'], last_frame['monitor_after']['creation_ticks']), 'Coordinator identity changed')
            last_frame = frame
            processes = {item['pid']: item for item in row['processes']}
            check(len(processes) == len(row['processes']), 'Duplicate sampled PID')
            owned, external = {}, {}
            for item in row['processes']:
                key = identity(item)
                observed_details[key] = {'historical_name': item['name'], 'historical_parent_pid': item['parent_pid']}
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
                check(job_delta >= 0 and coordinator_delta >= 0, 'Job/coordinator cumulative delta regressed')
                nonworker = busy-job_delta-coordinator_delta
                job_delta_sum += job_delta
                coordinator_delta_sum += coordinator_delta
                underflow_sum += max(0, -nonworker)
                # Windows counters are quantized; zero-tick intervals still
                # occupy wall time in the complete two-second window.
                intervals.append((previous_end, end, total, nonworker, busy, job_delta, coordinator_delta))
                cutoff = end - policy['host_window_seconds']
                while intervals and intervals[0][1] <= cutoff:
                    intervals.popleft()
                window_total = window_nonworker = seconds = window_busy = window_job = window_coordinator = 0.
                for left, right, ticks, other, busy_ticks, job_ticks, coordinator_ticks in intervals:
                    overlap = max(0., right - max(left, cutoff))
                    if overlap and right > left:
                        fraction = overlap / (right - left)
                        window_total += ticks * fraction
                        window_nonworker += other * fraction
                        window_busy += busy_ticks*fraction
                        window_job += job_ticks*fraction
                        window_coordinator += coordinator_ticks*fraction
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
                            'window_ready': ready, 'approximate': True}
                check(set(row['host']) == set(expected), 'Host-accounting fields differ')
                for key, value in expected.items():
                    equal_number(row['host'][key], value, 'Host independent recomputation differs: ' + key)
                if fraction is not None:
                    maximum_fraction = max(maximum_fraction, fraction)
                if ready:
                    parts = {'system_busy': window_busy, 'job': window_job, 'coordinator': window_coordinator,
                             'residual_signed': window_nonworker}
                    equal_number(window_busy-window_job-window_coordinator, window_nonworker, 'Rolling partition differs')
                    boundary = {'sample': samples+1, 'utc': row['utc'], 'phase': row['phase'], 'fraction': fraction,
                                'window_seconds': seconds, 'window_total_ticks': window_total,
                                'window_component_ticks': parts,
                                'window_component_percent': {key: value/window_total*100 for key,value in parts.items()}}
                    if maximum_window is None or fraction > maximum_window['fraction']:
                        maximum_window = boundary
                    if fraction > policy['max_nonworker_cpu_fraction']:
                        exceeded_windows.append(boundary)
                        if first_failure is None:
                            first_failure = boundary
                            check(utc(row['utc']) <= utc(summary['failure']['utc']), 'Host breach timestamp precedes raw sample')
                    maximum_ready_fraction = max(maximum_ready_fraction, fraction)
                    ready_windows += 1
                underflow_windows += int(nonworker < 0)
            else:
                check('host' not in row, 'First sample has no previous native counter baseline')
            check(row['failure'] == (summary['failure'] if first_failure is not None else None),
                  'Raw failure field differs from independently found first host-policy boundary')
            for key in row.get('retiring_owned', []):
                key = tuple(key)
                check(key in owned and owned[key]['exited'] is True and key not in roots, 'Retired timer is not an exited child')
            previous, previous_end, owned_before = row, end, owned
            samples += 1
    check(set(detached) <= set(attached) and set(active.values()) == set(attached)-set(detached), 'Stopped worker lifecycle differs')
    check(first_failure is not None and timer_failed_events == 1, 'Missing unique policy-failure boundary')
    check({key[0] for key in active.values()} == {item['pid'] for item in report['active_cells_at_stop']}, 'Active workers differ from stop report')
    check(phase_events['closed'] == 1 and previous['phase'] == 'closed' and phase_events['phase-completed'] == 0,
          'Missing final completion/closed native samples')
    check(ready_windows > 0 and samples == summary['snapshot_count'], 'Sample count/window differs')
    timer = summary['timer']
    check(timer_started and timer['started'] is True and timer['alive'] is False and timer['failure']['kind'] == summary['failure']['kind']
          and timer['samples'] == phase_events['timer'] and timer['samples'] > 0
          and timer['join_bound_seconds'] == policy['max_sample_gap_seconds'], 'Sampler final lifecycle/summary differs')
    equal_number(summary['max_sample_gap_seconds'], max_gap, 'Maximum sample gap differs')
    equal_number(summary['max_snapshot_duration_seconds'], max_duration, 'Maximum snapshot duration differs')
    equal_number(summary['max_nonworker_cpu_fraction'], maximum_ready_fraction, 'Maximum ready host nonworker fraction differs')
    check(summary['external_cpu_increase_ticks'] == 0, 'Guard summary external CPU differs')
    equal_number(summary['experiment_job_delta_ticks'], job_delta_sum, 'Summary Job delta differs')
    equal_number(summary['coordinator_delta_ticks'], coordinator_delta_sum, 'Summary coordinator delta differs')
    equal_number(summary['accounting_underflow_ticks'], underflow_sum, 'Summary signed underflow differs')
    equal_number(job_delta_sum, midpoint(last_frame, 'job')-midpoint(first_frame, 'job'), 'Job deltas do not telescope')
    for field in ('user_ticks', 'kernel_ticks', 'cpu_ticks', 'total_processes'):
        check(final_job[field] >= last_frame['job_after'][field], 'Final Job counter precedes last raw sample: '+field)
    check(final_job['read_started_monotonic'] >= last_frame['job_after']['read_finished_monotonic'], 'Final Job cleanup predates last raw read')
    check(final_job['total_processes'] >= len(observed_owned), 'Job cumulative process count below observed identities')
    final_delta = final_job['cpu_ticks']-last_frame['job_after']['cpu_ticks']
    recorded_workers = {(item['pid'], item['creation_ticks']): item for item in summary['workers']}
    check(len(recorded_workers) == len(summary['workers']) == len(attached) and set(recorded_workers) == set(attached),
          'Guard worker summary differs from attach events')
    for key, item in recorded_workers.items():
        check(item['attached_utc'] and utc(item['attached_utc']) <= utc(attached[key]), 'Worker attachment timestamp differs')
        if key in detached:
            check(item['detached_utc'] and utc(item['detached_utc']) <= utc(detached[key])
                  and utc(attached[key]) < utc(detached[key]), 'Worker detach timestamp differs')
        else:
            check(item['detached_utc'] is None, 'Stopped active worker falsely marked detached')
    return {'monitor_pid': summary['monitor_pid'], 'samples': samples, 'ready_host_windows': ready_windows, 'known_external_identities': [list(key) for key in sorted(external_identities)],
            'observed_known_external_cpu_delta_ticks': 0, 'max_sample_gap_seconds': max_gap,
            'max_snapshot_duration_seconds': max_duration, 'max_host_nonworker_fraction_including_partial_windows': maximum_fraction,
            'max_host_nonworker_fraction_ready_windows': maximum_ready_fraction,
            'accounting_underflow_intervals': underflow_windows, 'accounting_underflow_ticks': underflow_sum,
            'experiment_job_delta_ticks': job_delta_sum, 'coordinator_delta_ticks': coordinator_delta_sum,
            'final_job': final_job, 'last_raw_job': last_frame['job_after'],
            'job_cpu_ticks_after_last_raw_sample': final_delta, 'policy': policy,
            'valid_for_load_check': False, 'failure': summary['failure'], 'first_exceeded_window': first_failure,
            'maximum_ready_window': maximum_window, 'accounting_method': summary['accounting_method'],
            'exceeded_windows': exceeded_windows, 'still_attached_at_stop': [list(key) for key in active.values()],
            'timer': timer, 'timer_stopped_event_recorded': timer_stopped,
            'retirement_events': dict(retirement_counts), 'retired_handles_at_last_sample_before_provider_close': len(retired),
            'scope': 'Raw Job and coordinator deltas, signed rolling residual, sampled ancestry and exact held timer lifecycle independently checked without product validators. Final Job CPU may increase after the failed timer closes until owned cleanup; that difference is recorded. Residual is asynchronous/quantized whole-system background, not a per-app cause or OS isolation claim.'}, attached, detached, observed_owned, worker_events, final_timers, observed_details

def audit(directory, phase):
    directory = Path(directory).resolve()
    sha(__file__)
    # Check the completion marker before loading large records or doing audits.
    report = read(directory / phase / 'report.json')
    check(phase == 'preflight' and report['status'] == 'STOPPED' and report['valid_for_comparison'] is False,
          'Requested preflight must be stopped and invalid; running/valid results are rejected')
    check(sha(CORE_PATH) == CORE_SHA256, 'Complete-only independent auditor changed')
    check(sha(STOPPED_PATH) == STOPPED_SHA256, 'Earlier stopped-preflight independent auditor changed')
    check(sha(HELPER_PATH) == HELPER_SHA256, 'Independent helper changed')
    frozen, profiles, banks, candidates, opponents = core.frozen_inputs(directory)
    plan, target = frozen['plan'], directory / phase
    git_values = core.git_receipts(frozen, profiles, candidates)
    check(all(utc(item['verified_utc']) <= utc(report['started_utc']) for item in frozen['git_receipts']),
          'Git verification happened after measured phase started')
    cells = frozen['cells'][phase]
    pairs, games = 12, 24
    check(len(cells) == pairs and len(report['cells']) == 8 and len(report['launched_cells']) == 10, 'Stopped Job preflight 001 pair boundary differs')
    check(report['phase'] == phase and report['frozen_plan_sha256'] == sha(directory / 'frozen-plan.json'), 'Report phase/freeze differs')
    check(report['scheduled_cells'] == pairs and report['scheduled_games'] == games and report['completed_games'] == 16
          and report['uncompleted_scheduled_games'] == 8 and report['unstarted_pairs'] == 2, 'Stopped phase counters differ')
    check(report['elo_estimate'] is None and report['promotion_performed'] is False and report['stop_reason'] == 'LoadGuardViolation: rolling residual system/background CPU estimate exceeded the declared threshold', 'Unexpected promotion/error claim')
    check(utc(report['finished_utc']) >= utc(report['started_utc']), 'Phase timestamps regressed')
    guard, attached, detached, observed_owned, worker_events, final_timers, observed_details = guard_records(target, plan, report)
    report['runtime_monitor_pid'] = guard['monitor_pid']  # In-memory context only; raw report is never rewritten.
    check(not (directory/'main'/'run-started.json').exists(), 'Main unexpectedly started')
    by_report = {item['id']: item for item in report['cells']}
    by_launch = {item['id']: item for item in report['launched_cells']}
    expected_cells = {cell['id']: cell for cell in cells}
    check(len(by_report) == 8 and len(by_launch) == 10 and set(by_report) < set(by_launch) <= set(expected_cells), 'Duplicate/changed stopped cell IDs')
    check(len(attached) == 10 and len(detached) == 8, 'Worker lifecycle count differs from launched/completed cells')
    for lane in range(4):
        scheduled_ids = [cell['id'] for cell in cells if cell['lane'] == lane]
        launched_ids = [cell['id'] for cell in report['launched_cells'] if cell['lane'] == lane]
        check(launched_ids == scheduled_ids[:len(launched_ids)], 'Lane did not execute its fixed nonadaptive prefix')
    attached_by_pid = {}
    for key in attached:
        attached_by_pid.setdefault(key[0], []).append(key)
    coverage, outcomes, results, roots_used = Counter(), Counter(), [], set()
    # These event identities also prove that a lane never ran overlapping workers.
    lane_by_root = {}
    for actual in report['cells']:
        cell = expected_cells[actual['id']]
        index = next(i for i, value in enumerate(cells) if value['id'] == cell['id'])
        launch = by_launch[cell['id']]
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
        check(root == (launch['pid'], launch['creation_ticks']), 'Launch registration creation differs')
        assignment = launch['job_assignment']
        native_assignment(assignment)
        check(assignment['pid'] == root[0] and assignment['creation_ticks'] == root[1]
              and assignment['parent_pid'] == guard['monitor_pid'], 'Job assignment differs from direct root')
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
        pgn_stream = io.StringIO((cell_dir / 'games.pgn').read_text(encoding='utf-8'))
        points, pv_before = [], helper.MOVES['legal_pv_plies']
        for number, record in enumerate(metadata['games'], 1):
            check(record['game'] == number and record['pair'] == 1 and record['completed'] is True, 'Pair numbering/completion differs')
            parsed = chess.pgn.read_game(pgn_stream)
            check(parsed is not None, 'Missing PGN game')
            point = helper.game(record, parsed, opening, number == 1, metadata, cell)
            check(point is not None, 'Unfinished game in completed phase')
            check(all(move['elapsed_seconds'] <= cell['movetime_ms']/1000*2 + .25 + 1e-6 for move in record['search_moves']), 'Move exceeded declared response deadline')
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
    check(roots_used == set(detached), 'Completed cell roots differ from detached workers')
    partial_cells = inspect_partial_cells(directory, frozen, profiles, banks, cells, report, by_launch, attached, detached, observed_owned, lane_by_root)
    check(set(lane_by_root) == set(attached), 'Registered worker not accounted for as completed/active')
    active_lanes = {}
    for event, root, _time in worker_events:
        lane = lane_by_root[root]
        if event == 'attach':
            check(lane not in active_lanes, 'Two workers overlapped on the same lane')
            active_lanes[lane] = root
        else:
            check(active_lanes.pop(lane, None) == root, 'Lane detach does not match running worker')
    check(set(active_lanes.values()) == set(attached)-set(detached), 'Stopped lanes differ from four active roots')
    expected_coverage = {(candidate, opponent, milliseconds, opening['family_id'], color)
                         for candidate in candidates for opponent in opponents for milliseconds in plan['movetimes_ms']
                         for opening in banks[phase] for color in (1, 2)}
    check(set(coverage) < expected_coverage and len(coverage) == 16 and set(coverage.values()) == {1}, 'Completed subset/color coverage differs')
    cleanup = observe_cleanup(set(observed_owned) | set(attached), final_timers, set(attached)-set(detached), observed_details)
    check(all(sha(path) == digest for path, digest in list(helper.FILES.items())), 'Audit inputs changed while checking')
    return {'schema_version': 1, 'status': 'INTEGRITY_PASS_STOPPED_INVALID', 'integrity_replay_status': 'PASS',
            'current_pid_cleanup_observation': cleanup['current_pid_observation_status'],
            'phase': phase, 'directory': str(directory),
            'audited_utc': datetime.now(timezone.utc).isoformat(), 'audit_script_sha256': sha(__file__),
            'independent_helper_sha256': sha(HELPER_PATH), 'complete_auditor_sha256': sha(CORE_PATH), 'prior_stopped_auditor_sha256': sha(STOPPED_PATH),
            'scheduled_pairs': pairs, 'audited_completed_pairs': 8, 'launched_pairs': 10,
            'scheduled_games': games, 'audited_report_completed_games': 16, 'uncompleted_scheduled_games': 8,
            'active_pairs_at_stop': 2, 'unstarted_pairs': 2, 'main_started': False, 'phase_complete': False,
            'replay': dict(helper.MOVES), 'completed_cells': results, 'partial_cells': partial_cells,
            'load': guard, 'cleanup': cleanup,
            'git_scope': 'Exact candidate commit, HEAD and porcelain-status queries succeeded at input freeze and their sealed receipts match every worker metadata copy. These are freeze-time context, not fresh per-worker Git commands; file/model/harness identities are separately hash-checked.',
            'input_sha256': dict(sorted(helper.FILES.items())),
            'source_scope': 'File/model/artifact identities, declared local commit objects and native build receipt/source consistency checked. No independent source-to-binary rebuild; external version/revision/URL remain declarations. Direct worker executable and the two declared chess source files are hashed; these are not a complete frozen Python runtime.',
            'configuration_scope': 'Serialized actual command including direct executable/-s, file bindings, model SHA, accepted UCI option types/order, worker and launch-time engine affinity, PID+creation ancestry and raw counters checked. Worker self-reported executable/hash/versions/no_user_site and three environment variables match frozen bindings; no external OS environment readback, raw UCI wire transcript, general option readback or continuous affinity monitoring is claimed.',
            'score_scope': 'Legal mainlines, root/final FEN, rule endings and WDL are independently replayed. Raw PV legality and recorded mover score type/POV checked; search evaluations are not recomputed by another engine.',
            'valid_for_declared_local_comparison': False, 'valid_for_comparison': False,
            'all_match_scores_excluded': True, 'strength_eligible': False,
            'elo_estimate': None, 'promotion_performed': False,
            'claim_scope': 'Integrity and policy-stop audit of an INVALID partial run. All partial-run scores are excluded from comparison/strength inference. No preflight/main completion, model promotion, world ranking or complete OS/thermal isolation claim.'}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    check(not args.output.exists(), 'Preserve every earlier audit output')
    check(args.output.name.startswith('peer-') and args.output.suffix == '.json', 'Expected new peer-*.json')
    try:
        result = audit(args.directory, 'preflight')
        code = 0
    except Exception as error:
        result = {'schema_version': 1, 'status': 'AUDIT_FAIL', 'phase_status': 'STOPPED',
                  'audited_utc': datetime.now(timezone.utc).isoformat(), 'audit_script_sha256': sha(__file__),
                  'error': f'{type(error).__name__}: {error}', 'input_sha256': dict(sorted(helper.FILES.items())),
                  'valid_for_comparison': False, 'strength_eligible': False, 'all_match_scores_excluded': True,
                  'elo_estimate': None, 'promotion_performed': False}
        code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({key: result[key] for key in ('status', 'audited_report_completed_games', 'error', 'replay') if key in result}))
    return code

if __name__ == '__main__':
    raise SystemExit(main())
