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
OWNERSHIP_METHOD = 'explicit-owned-job-membership-before-process-query'


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


def _engine(process, policy):
    name = process['name'].casefold()
    return (name in {item.casefold() for item in policy.engine_names}
        or process['pid'] in policy.extra_engine_pids
        or bool(policy.engine_names) and name.endswith('.exe') and name.startswith(('stockfish', 'regicide')))


def _processes(values, label, *, inventory=False):
    _require(isinstance(values, list), label + ' is not a list')
    result = {}
    fields = {'pid', 'parent_pid', 'name', 'creation_ticks', 'cpu_ticks', 'error', 'exited_during_snapshot'}
    for row in values:
        _require(isinstance(row, dict) and set(row) == fields, label + ' ProcessInfo fields differ')
        pid = _number(row['pid'], label + ' PID', minimum=0 if inventory else 1, integer=True)
        _number(row['parent_pid'], label + ' parent PID', minimum=0, integer=True)
        _require(isinstance(row['name'], str) and bool(row['name']), label + ' process name missing')
        _require(pid not in result, label + ' duplicate PID')
        _require(row['error'] is None and type(row['exited_during_snapshot']) is bool,
                 label + ' access error or nonboolean exit state')
        if inventory:
            _require(row['creation_ticks'] is None and row['cpu_ticks'] is None
                     and row['exited_during_snapshot'] is False, label + ' contains queried CPU/identity')
        else:
            _number(row['creation_ticks'], label + ' creation', minimum=1, integer=True)
            _number(row['cpu_ticks'], label + ' CPU', minimum=0, integer=True)
        result[pid] = row
    return result


def _job_membership(row, monitor_pid, label):
    _require(isinstance(row, dict) and row.get('job_handle_kind') == 'explicit-owned-job-handle',
             label + ' Job handle kind differs')
    _require(row['coordinator_pid'] == monitor_pid and row['coordinator_in_job'] is False,
             label + ' coordinator Job scope differs')
    flags = _number(row['limit_flags'], label + ' limit flags', minimum=0, integer=True)
    _require(not flags & 0x1800, label + ' permits Job breakaway')
    pointer = _number(row['pointer_bytes'], label + ' pointer bytes', minimum=4, integer=True)
    _require(pointer in (4, 8), label + ' pointer width differs')
    _same(row['limits_returned_bytes'], 144 if pointer == 8 else 112, label + ' limit query length')
    _bracket(row, label)
    attempts = row['query_attempts']
    _require(isinstance(attempts, list) and 1 <= len(attempts) <= 8, label + ' query attempts missing/unbounded')
    previous_capacity = 0
    for index, attempt in enumerate(attempts):
        cap = _number(attempt['buffer_capacity'], label + ' capacity', minimum=1, integer=True)
        _require(previous_capacity < cap <= 4096, label + ' capacity growth differs')
        previous_capacity = cap
        _same(attempt['buffer_bytes'], 8 + cap * pointer, label + ' buffer bytes')
        assigned = _number(attempt['assigned_processes'], label + ' assigned processes', minimum=0, integer=True)
        listed = _number(attempt['listed_processes'], label + ' listed processes', minimum=0, integer=True)
        _require(listed <= min(assigned, cap) and assigned <= 4096, label + ' PID counts exceed bounds')
        pids = attempt['pids']
        _require(isinstance(pids, list) and len(pids) == listed and len(set(pids)) == listed,
                 label + ' duplicate/missing PID list')
        for pid in pids:
            _number(pid, label + ' PID', minimum=1, integer=True)
            _require(pid <= 0xffffffff and pid != monitor_pid, label + ' PID out of bounds/coordinator')
        _number(attempt['returned_bytes'], label + ' returned bytes', minimum=0, integer=True)
        _require(type(attempt['success']) is bool, label + ' query success flag missing')
        error = _number(attempt['windows_error'], label + ' query error', minimum=0, integer=True)
        _require(error == 0 if attempt['success'] else error in (122, 234), label + ' query failed')
        complete = attempt['success'] and assigned == listed
        _require(complete == (index == len(attempts)-1), label + ' final query is not complete')
    final = attempts[-1]
    for field in ('assigned_processes', 'listed_processes', 'returned_bytes', 'buffer_capacity'):
        _same(row[field], final[field], label + ' final ' + field)
    _require(row['pids'] == sorted(final['pids']), label + ' final PID projection differs')
    _require(8 + len(row['pids']) * pointer <= row['returned_bytes'] <= 8 + row['buffer_capacity'] * pointer,
             label + ' final list is truncated')
    return set(row['pids'])


def _chain(process, processes, root):
    """Independent checked ancestry; numeric parent links alone confer no ownership."""
    result, current, seen = [process['pid']], process, {process['pid']}
    while current['pid'] != root[0]:
        parent = processes.get(current['parent_pid'])
        if (parent is None or parent['pid'] in seen
                or parent['creation_ticks'] > current['creation_ticks']):
            return None
        result.append(parent['pid']); seen.add(parent['pid']); current = parent
    return result if (current['pid'], current['creation_ticks']) == root else None


def _ownership_scope(row, policy, monitor_pid, attached, known_owned):
    """Replay raw Job lists, discovery selection and guard ownership separately.

    These asynchronous observations describe the explicit Job cohort. They do
    not assert that Toolhelp, Job queries and process reads were one atomic view.
    """
    label = 'process ownership scope'
    invalid = []
    scope = row['process_ownership_scope']
    _require(isinstance(scope, dict) and scope.get('schema_version') == 1
        and scope.get('method') == OWNERSHIP_METHOD and scope.get('status') == 'COMPLETE', label + ' missing/incomplete')
    _require(scope['coordinator_pid'] == monitor_pid, label + ' coordinator differs')
    roots = [tuple(value) for value in row['roots']]
    _require(set(roots) == attached and len(roots) == len(attached), label + ' active roots differ from attach events')
    _require(scope['roots'] == row['roots'], label + ' raw roots differ')
    before = _job_membership(scope['job_before'], monitor_pid, label + ' before')
    after = _job_membership(scope['job_after'], monitor_pid, label + ' after')
    _require(scope['job_before']['read_finished_monotonic'] <= scope['job_after']['read_started_monotonic'],
             label + ' Job query clock order differs')
    _require(scope['job_after']['read_finished_monotonic'] <= row['native_accounting']['job_before']['read_started_monotonic'],
             label + ' process discovery follows native accounting')
    attempts = scope['global_inventory_attempts']
    _require(isinstance(attempts, list) and 1 <= len(attempts) <= 2, label + ' inventory attempts differ')
    raw = {}
    for attempt in attempts:
        inventory = _processes(attempt, label + ' inventory attempt', inventory=True)
        raw.update(inventory)
    declared = _processes(scope['global_inventory'], label + ' global inventory', inventory=True)
    _require(declared == raw, label + ' inventory union/projection differs')
    selected = _processes(scope['selected_processes'], label + ' selected processes')
    for pid, process in selected.items():
        _require(pid in raw and all(process[field] == raw[pid][field] for field in ('parent_pid', 'name')),
                 label + ' selected process absent/differs from inventory')
    recovered = {}
    for item in scope['held_exit_recoveries']:
        identity = tuple(item['identity'])
        final = _processes([item['final']], label + ' held final')[identity[0]]
        queried_member = any(query.get('in_owned_job_at_read') is True
            and (query.get('pid'), query.get('result', {}).get('creation_ticks')) == identity
            for query in scope['queries'])
        _require(identity == (final['pid'], final['creation_ticks']) and (identity in known_owned or queried_member)
                 and final['exited_during_snapshot'] is True
                 and item['proof'] == 'exact-held-exited-process', label + ' unproven held final')
        if identity in recovered:
            _require(recovered[identity] == final, label + ' inconsistent held final')
        recovered[identity] = final
    queries, proven = {}, set(recovered)
    for query in scope['queries']:
        pid, reason = query['pid'], query['reason']
        process = _processes([query['result']], label + ' CPU query')[pid]
        _require(pid in raw and all(process[field] == raw[pid][field] for field in ('parent_pid', 'name')),
                 label + ' CPU query differs from inventory')
        _require(reason in ('current-job-member', 'declared-external-engine'), label + ' query reason differs')
        _require((reason == 'current-job-member' and pid in before | after)
                 or (reason == 'declared-external-engine' and _engine(raw[pid], policy)),
                 label + ' CPU queried an excluded unrelated process')
        included = query['in_owned_job_at_read']
        _require(type(included) is bool, label + ' native process Job-membership flag missing')
        identity = pid, process['creation_ticks']
        if included:
            proven.add(identity)
        elif reason == 'current-job-member':
            _require(identity in recovered, label + ' member left Job without held final')
        queries[pid] = query
    reasons = scope['selection_reasons']
    _require(isinstance(reasons, list) and len(reasons) == len(selected), label + ' selection reason coverage differs')
    reason_by_pid = {}
    for item in reasons:
        pid = item['pid']
        _require(pid not in reason_by_pid and pid in selected, label + ' selection reason PID differs')
        reason_by_pid[pid] = item['reason']
        process = selected[pid]; identity = pid, process['creation_ticks']
        _same(item['creation_ticks'], identity[1], label + ' selected creation')
        reason = ('known-held-owned-exited' if identity in recovered and process['exited_during_snapshot']
                  else 'current-job-member' if identity in proven else 'declared-external-engine')
        # A current membership read may also report an exited object before
        # Job-after drops it; producer retains current reason in that case.
        allowed = {reason}
        if identity in recovered and pid in queries and queries[pid]['in_owned_job_at_read']:
            allowed.add('current-job-member')
        _require(item['reason'] in allowed, label + ' selection reason differs')
        candidates = [queries[pid]['result']] if pid in queries else []
        if identity in recovered:candidates.append(recovered[identity])
        _require(process in candidates, label + ' selected CPU differs from query/held final')
        if identity not in proven:
            _require(_engine(raw[pid], policy) and pid in queries
                     and queries[pid]['in_owned_job_at_read'] is False, label + ' external selection lacks native proof')
    missing = {}
    for item in scope['missing_job_pids_with_held_final']:
        pid = item['pid']; identity = tuple(item['identity'])
        _require(pid not in missing and pid in before | after and pid not in raw
                 and identity in recovered and identity[0] == pid, label + ' missing Job PID held proof differs')
        missing[pid] = item
    unobserved = {}
    for item in scope['unobserved_after_only_job_pids']:
        pid = item['pid']
        _require(pid not in unobserved and pid in after-before and pid not in raw and len(attempts) == 2
                 and item['in_job_before'] is False and item['in_job_after'] is True
                 and item['reason'] == 'born-after-inventory-identity-not-adopted', label + ' unobserved after-only PID differs')
        unobserved[pid] = item
    for item in scope['unobserved_before_only_job_pids']:
        pid = item['pid']
        _require(pid not in unobserved and pid in before-after and pid not in raw and len(attempts) == 2
                 and item['in_job_before'] is True and item['in_job_after'] is False
                 and item['reason'] == 'ended-between-inventories-identity-not-adopted'
                 and pid not in {identity[0] for identity in roots}, label + ' unobserved before-only PID differs')
        unobserved[pid] = item
    _require((before | after)-set(raw) == set(missing) | set(unobserved), label + ' missing Job PID coverage differs')
    _require((before | after) & set(raw) <= set(selected), label + ' observed Job member omitted')
    _require({pid for pid, process in raw.items() if _engine(process, policy)} <= set(selected),
             label + ' declared engine omitted')
    excluded = sorted(pid for pid, process in raw.items()
                      if pid not in selected and pid not in before | after and not _engine(process, policy))
    _require(scope['excluded_nonengine_pids'] == excluded, label + ' excluded nonengine projection differs')
    possible = {identity[0] for identity in roots}
    for _ in raw:
        added = {pid for pid, process in raw.items() if process['parent_pid'] in possible}-possible
        if not added:break
        possible.update(added)
    matches = [dict(pid=pid, parent_pid=raw[pid]['parent_pid'], name=raw[pid]['name'],
                    reason='outside-owned-job-nonengine-parent-match') for pid in sorted(possible-set(selected))
               if pid in raw and pid not in before | after and not _engine(raw[pid], policy)]
    _require(scope['outside_parent_matches'] == matches, label + ' outside parent-match diagnostic differs')
    retained = _processes(scope['retained_owned'], label + ' retained processes')
    projection = [{'pid': p['pid'], 'creation_ticks': p['creation_ticks'], 'cpu_ticks': p['cpu_ticks'],
                   'exited': p['exited_during_snapshot']} for p in retained.values()]
    _require(row['retained_owned'] == projection, label + ' retained guard projection differs')
    possible = {identity[0] for identity in roots}
    for _ in selected:
        added = {pid for pid, p in selected.items() if p['parent_pid'] in possible
                 and (pid, p['creation_ticks']) in proven}-possible
        if not added:break
        possible.update(added)
    records = []
    for pid, process in selected.items():
        identity = pid, process['creation_ticks']; member = identity in proven
        if not (pid in possible and member) and not _engine(process, policy):continue
        root, chain = known_owned.get(identity) if member else None, None
        if root is None and member:
            for candidate in roots:
                chain = _chain(process, selected, candidate)
                if chain is not None:
                    root = candidate; known_owned[identity] = root; break
        records.append(dict(pid=pid, parent_pid=process['parent_pid'], name=process['name'],
            creation_ticks=identity[1], cpu_ticks=process['cpu_ticks'], owned=root is not None,
            job_scope_member=member, worker_root=None if root is None else list(root), ancestry=chain,
            exited=process['exited_during_snapshot']))
        if root is None and process['exited_during_snapshot']:
            invalid.append('external engine exited during process snapshot')
    _require(row['processes'] == records, label + ' guard ownership classification/projection differs')
    for process in retained.values():
        identity = process['pid'], process['creation_ticks']
        _require(identity in known_owned, label + ' retained timer has no known ownership')
        if known_owned[identity] not in attached and not process['exited_during_snapshot']:
            invalid.append('known owned process is alive after worker detach')
    for identity in roots:
        current, final = selected.get(identity[0]), retained.get(identity[0])
        if current is not None and (current['pid'], current['creation_ticks']) != identity:
            invalid.append('active worker PID identity changed')
        elif current is None and (final is None or (final['pid'], final['creation_ticks']) != identity
                                   or not final['exited_during_snapshot']):
            invalid.append('active worker disappeared without exact retained final')
    return invalid


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
        _require(record.get('schema_version') == 4 and record.get('accounting_method') == METHOD
                 and record.get('approximate') is True, 'accounting method/schema differs')
        _require(record.get('residual_cpu_policy') == 'warning-only', 'residual CPU policy differs')
        _require(record.get('process_ownership_method') == OWNERSHIP_METHOD, 'process ownership method differs')
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
    known_owned = {}
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
                known_owned[item] = item
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
        _require(row.get('process_ownership_method') == OWNERSHIP_METHOD, 'sample process ownership method differs')
        if not accepted:
            _require(row.get('process_ownership_scope_index') is None, 'unaccepted process scope index differs')
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
        _number(row.get('process_ownership_scope_index'), 'process ownership scope index', minimum=1, integer=True)
        _same(row.get('process_ownership_scope_index'), accepted_count, 'process ownership scope index')
        for issue in _ownership_scope(row, policy, monitor_pid, attached, known_owned):
            mark(issue)
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
    _number(summary.get('process_ownership_scope_count'), 'summary process_ownership_scope_count', minimum=0, integer=True)
    for key, value in dict(process_ownership_scope_count=accepted_count, snapshot_count=accepted_count, max_sample_gap_seconds=max_gap,
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
        'counts': {'records': len(rows), 'samples': sample_count, 'accepted_samples': accepted_count, 'process_ownership_scopes': accepted_count,
                   'recomputed_intervals': max(0, accepted_count-1), 'ready_windows': window_count,
                   'failure_observations': len(failures), 'warning_windows': warning_count,
                   'warning_episodes': warning_episodes},
        'recomputed_metrics': {'experiment_job_delta_ticks': job_sum, 'coordinator_delta_ticks': monitor_sum,
            'accounting_underflow_ticks': underflow, 'external_cpu_increase_ticks': external_sum,
            'max_nonworker_cpu_fraction': max_fraction, 'max_sample_gap_seconds': max_gap,
            'max_snapshot_duration_seconds': max_duration, 'idle_sample_span_seconds': idle_span,
            'idle_write_reconciliation_slack_seconds': .05},
        'residual_cpu_policy': 'warning-only', 'process_ownership_method': OWNERSHIP_METHOD,
        'warnings': {'threshold_fraction': policy.max_nonworker_cpu_fraction,
                     'window_count': warning_count, 'episode_count': warning_episodes, 'peak_fraction': warning_peak},
        'invalid_errors': invalid, 'scope': 'Independent raw-counter arithmetic and recorded-coverage audit; asynchronous CPU estimates, not move-level isolation.'}
