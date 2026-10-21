"""Read-only endpoint identities bounded by the caller's immediate Job.

Global Toolhelp parent PIDs can refer to an earlier process with a reused PID.
Only members of the verified non-breakaway calling Job become candidates; a
same-Job candidate older than its held parent is excluded by positive birth
evidence before inspecting its image. No access rights are escalated.

https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_process_id_list
https://devblogs.microsoft.com/oldnewthing/20150403-00/?p=44313
"""
from __future__ import annotations

from copy import deepcopy
import ctypes as C
import os
from pathlib import Path
import time

import m2_engine_identity as previous
from m2_job_accounting import AccountingError, _ExtendedLimits, _FileTime, _ticks, _bracket

EngineIdentityError = previous.EngineIdentityError
_inventory = previous._inventory
_profile, _file, _equal_path = previous._profile, previous._file, previous._equal_path
_read_live = previous._read_live
METHOD = 'frozen-image-and-held-creation-with-job-scoped-parent'
SCOPE_METHOD = 'current-immediate-job-and-held-creation'
_BREAKAWAY = 0x1800
_MAX_PIDS, _MAX_ATTEMPTS = 4096, 8


def _last_error():
    return getattr(C, 'get_last_error', lambda: 0)()


def _error(operation):
    return EngineIdentityError(f'{operation} failed (Windows error {_last_error()})')


def _api(injected):
    api = previous._api(injected)
    signatures = {
        'GetCurrentProcess': ([], C.c_void_p),
        'IsProcessInJob': ([C.c_void_p, C.c_void_p, C.POINTER(C.c_int32)], C.c_int32),
        'QueryInformationJobObject': ([C.c_void_p, C.c_int32, C.c_void_p, C.c_uint32,
                                       C.POINTER(C.c_uint32)], C.c_int32),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def _job_snapshot(api, worker_pid, coordinator_pid, clock, *, receipt=None):
    started = clock()
    receipt={} if receipt is None else receipt
    receipt.update(query_job_handle='NULL-current-immediate-job',worker_pid=worker_pid,
                   coordinator_pid=coordinator_pid,read_started_monotonic=started)
    current = api.GetCurrentProcess()
    membership = C.c_int32()
    if not api.IsProcessInJob(current, None, C.byref(membership)):
        raise _error('IsProcessInJob current caller')
    if membership.value != 1:
        raise EngineIdentityError('current caller has no verified Job')
    limits, limit_length = _ExtendedLimits(), C.c_uint32()
    if not api.QueryInformationJobObject(None, 9, C.byref(limits), C.sizeof(limits), C.byref(limit_length)):
        raise _error('QueryInformationJobObject current limits')
    if limit_length.value != C.sizeof(limits):
        raise EngineIdentityError('current Job limits query length differs')
    flags = int(limits.basic.flags)
    receipt.update(caller_in_job=True,limit_flags=flags,limits_returned_bytes=int(limit_length.value))
    if flags & _BREAKAWAY:
        raise EngineIdentityError('current Job permits breakaway; endpoint scope is unproven')
    capacity, attempts = 64, []
    receipt['query_attempts']=attempts
    for _ in range(_MAX_ATTEMPTS):
        size = 8 + capacity * C.sizeof(C.c_size_t)
        buffer, returned = C.create_string_buffer(size), C.c_uint32()
        success = bool(api.QueryInformationJobObject(None, 3, buffer, size, C.byref(returned)))
        error = 0 if success else _last_error()
        header = (C.c_uint32 * 2).from_buffer(buffer)
        assigned, listed = map(int, header)
        pids = list((C.c_size_t * listed).from_buffer(buffer, 8)) if listed <= capacity else []
        attempt = {'success': success, 'windows_error': error, 'buffer_capacity': capacity,
                   'buffer_bytes': size, 'returned_bytes': int(returned.value),
                   'assigned_processes': assigned, 'listed_processes': listed, 'pids': pids}
        attempts.append(attempt)
        if listed > capacity or listed > assigned or assigned > _MAX_PIDS:
            raise EngineIdentityError('current Job PID list counts are malformed or exceed bound')
        if not success and error not in (122, 234):
            raise _error('QueryInformationJobObject current PID list')
        if success and assigned == listed:
            needed = 8 + listed * C.sizeof(C.c_size_t)
            if not needed <= returned.value <= size:
                raise EngineIdentityError('current Job PID list returned length is truncated or oversized')
            if any(type(pid) is not int or not 0 < pid <= 0xffffffff for pid in pids) or len(set(pids)) != len(pids):
                raise EngineIdentityError('current Job PID list contains invalid or duplicate PIDs')
            if worker_pid not in pids or coordinator_pid in pids:
                raise EngineIdentityError('calling Job worker/coordinator membership differs')
            receipt.update({'query_job_handle': 'NULL-current-immediate-job', 'worker_pid': worker_pid,
                    'coordinator_pid': coordinator_pid, 'caller_in_job': True,
                    'limit_flags': flags, 'limits_returned_bytes': int(limit_length.value),
                    'assigned_processes': assigned, 'listed_processes': listed,
                    'returned_bytes': int(returned.value), 'buffer_capacity': capacity,
                    'pointer_bytes': C.sizeof(C.c_size_t), 'pids': sorted(pids),
                    'query_attempts': attempts, **_bracket(started, clock())})
            return receipt
        capacity = max(capacity * 2, assigned, (int(returned.value) + C.sizeof(C.c_size_t) - 1) // C.sizeof(C.c_size_t))
        if capacity > _MAX_PIDS:
            break
    raise EngineIdentityError('current Job PID list did not become complete within bounded queries')


def _birth(api, handle, pid, expected_creation, clock):
    started = clock()
    if not handle or api.GetProcessId(handle) != pid:
        raise EngineIdentityError('held candidate handle PID differs')
    creation, exited, kernel, user = (_FileTime() for _ in range(4))
    if not api.GetProcessTimes(handle, C.byref(creation), C.byref(exited), C.byref(kernel), C.byref(user)):
        raise _error('GetProcessTimes candidate creation')
    created = _ticks(creation)
    if created <= 0 or expected_creation is not None and created != expected_creation:
        raise EngineIdentityError('held candidate creation identity differs')
    status = api.WaitForSingleObject(handle, 0)
    if status == 0:
        raise EngineIdentityError('held candidate process has exited')
    if status != 258:
        raise _error('WaitForSingleObject candidate identity')
    member = C.c_int32()
    if not api.IsProcessInJob(handle, None, C.byref(member)):
        raise _error('IsProcessInJob held candidate')
    if member.value != 1:
        raise EngineIdentityError('held candidate no longer belongs to a Job')
    return {'pid': pid, 'creation_ticks': created, 'wait_status': int(status),
            'in_any_job': True, **_bracket(started, clock())}


def resolve_engine_endpoint(transport_pid, transport_creation, profile, expected_base_runtime,
                            *, expected_console_helper=None, _kernel=None, _clock=time.monotonic):
    """Same caller API as v1; current-immediate-Job scope is mandatory.

    On failure, exception.identity_receipt retains the phase, offending PID and
    available raw scope evidence. Only newly opened query/wait handles close.
    """
    if type(transport_pid) is not int or not 0 < transport_pid <= 0xffffffff:
        raise EngineIdentityError('transport PID must be a positive Windows DWORD')
    if type(transport_creation) is not int or transport_creation <= 0:
        raise EngineIdentityError('transport creation ticks must be a positive integer')
    mode, binary, script, runtime = _profile(profile, expected_base_runtime)
    helper_file = None if expected_console_helper is None else _file(expected_console_helper, 'expected console helper')
    if helper_file is not None and Path(helper_file['path']).name.casefold() != 'conhost.exe':
        raise EngineIdentityError('only an explicitly frozen conhost.exe helper is allowed')
    api, started = _api(_kernel), _clock()
    worker_pid, coordinator_pid = os.getpid(), os.getppid()
    scope = {'schema_version': 1, 'method': SCOPE_METHOD, 'worker_pid': worker_pid,
             'coordinator_pid': coordinator_pid, 'job_snapshots': [], 'global_inventories': [],
             'membership_candidates': [], 'membership_candidates_after': [],
             'outside_job_edges': [], 'stale_job_edges': [], 'local_subtree_pids': []}
    forensic = {'schema_version': 1, 'method': METHOD, 'phase': 'job-before',
                'transport_pid': transport_pid, 'offending_pid': transport_pid,
                'endpoint_job_scope': scope}
    handles, candidate_handles, identities, actual = [], {}, {}, {}
    failure = None
    try:
        before_job={}
        scope['job_snapshots'].append(before_job)
        _job_snapshot(api, worker_pid, coordinator_pid, _clock,receipt=before_job)
        first = _inventory(api)
        scope['global_inventories'].append(list(first.values()))
        members = set(before_job['pids'])
        if transport_pid not in members or worker_pid not in first or transport_pid not in first:
            raise EngineIdentityError('worker/transport is missing from current Job or inventory')
        if first[worker_pid]['parent_pid'] != coordinator_pid or first[transport_pid]['parent_pid'] != worker_pid:
            raise EngineIdentityError('worker/coordinator/transport parent identity differs')
        worker = _birth(api, api.GetCurrentProcess(), worker_pid, None, _clock)
        scope['worker_identity'] = worker
        def held(pid, parent, parent_creation):
            forensic.update(phase='candidate-open', offending_pid=pid)
            handle = api.OpenProcess(0x101000, False, pid)
            if not handle:
                raise _error(f'OpenProcess query/wait-only candidate PID {pid}')
            handles.append(handle)
            candidate_handles[pid] = handle
            row = _birth(api, handle, pid, transport_creation if pid == transport_pid else None, _clock)
            row.update(parent_pid=parent, parent_creation_ticks=parent_creation)
            identities[pid] = row
            scope['membership_candidates'].append(row)
            return row, handle
        root, transport_handle = held(transport_pid, worker_pid, worker['creation_ticks'])
        if root['creation_ticks'] < worker['creation_ticks']:
            raise EngineIdentityError('transport predates its held worker')
        transport = _read_live(api, transport_handle, transport_pid, transport_creation, binary, _clock)
        transport['parent_pid'] = worker_pid
        actual[transport_pid] = transport
        queue, children, child_endpoints, helpers = [transport_pid], [], [], []
        while queue:
            parent_pid = queue.pop(0)
            parent_creation = identities[parent_pid]['creation_ticks']
            for pid, row in sorted(first.items()):
                if row['parent_pid'] != parent_pid:
                    continue
                if pid not in members:
                    continue  # Raw outside edges are derived from both inventories below.
                if pid in identities:
                    raise EngineIdentityError('current Job parent graph cycles or repeats a candidate')
                child_birth, child_handle = held(pid, parent_pid, parent_creation)
                if child_birth['creation_ticks'] < parent_creation:
                    scope['stale_job_edges'].append({'pid': pid, 'parent_pid': parent_pid,
                        'parent_creation_ticks': parent_creation, 'child_identity': child_birth,
                        'reason': 'creation-precedes-held-parent'})
                    continue
                if parent_pid != transport_pid:
                    raise EngineIdentityError('engine child has an unexpected current-Job descendant process')
                allowed = [item for item in (runtime, helper_file) if item is not None]
                child = _read_live(api, child_handle, pid, child_birth['creation_ticks'], allowed, _clock)
                child['parent_pid'] = transport_pid
                actual[pid] = child
                children.append(pid)
                queue.append(pid)
                if helper_file is not None and _equal_path(Path(child['image_path']), Path(helper_file['path'])):
                    helpers.append((child, child_handle))
                elif runtime is not None and _equal_path(Path(child['image_path']), Path(runtime['path'])):
                    child_endpoints.append((child, child_handle))
                else:
                    raise EngineIdentityError('current Job child is outside endpoint/helper scope')
        if len(helpers) > 1:
            raise EngineIdentityError('engine has multiple console helpers')
        if mode == 'native-direct' and child_endpoints:
            raise EngineIdentityError('native engine has an unexpected computational child process')
        endpoint, endpoint_handle = dict(transport), transport_handle
        if mode == 'sunfish-venv-child':
            if len(child_endpoints) > 1:
                raise EngineIdentityError('Sunfish has multiple base-Python endpoints')
            if child_endpoints:
                endpoint, endpoint_handle = child_endpoints[0]
            else:
                mode = 'native-direct'
        forensic.update(phase='inventory-after', offending_pid=transport_pid)
        second = _inventory(api)
        scope['global_inventories'].append(list(second.values()))
        after_job={}
        scope['job_snapshots'].append(after_job)
        _job_snapshot(api, worker_pid, coordinator_pid, _clock,receipt=after_job)
        scope['local_subtree_pids'] = sorted(actual)
        scope['worker_identity_after'] = _birth(api, api.GetCurrentProcess(), worker_pid, worker['creation_ticks'], _clock)
        for pid, row in identities.items():
            forensic.update(phase='candidate-after', offending_pid=pid)
            after = _birth(api, candidate_handles[pid], pid, row['creation_ticks'], _clock)
            after.update(parent_pid=row['parent_pid'], parent_creation_ticks=row['parent_creation_ticks'])
            scope['membership_candidates_after'].append(after)
        for edge in scope['stale_job_edges']:
            edge['child_identity_after'] = next(row for row in scope['membership_candidates_after'] if row['pid'] == edge['pid'])
        relevant = set(actual)
        edges = {(row['pid'], row['parent_pid']) for inventory in (first, second)
                 for row in inventory.values() if row['parent_pid'] in relevant}
        later_members = set(after_job['pids'])
        for pid, parent in sorted(edges):
            if pid not in members and pid not in later_members:
                scope['outside_job_edges'].append({'pid': pid, 'parent_pid': parent,
                    'in_job_before': False, 'in_job_after': False,
                    'observed_in_first': first.get(pid, {}).get('parent_pid') == parent,
                    'observed_in_second': second.get(pid, {}).get('parent_pid') == parent,
                    'reason': 'outside-current-immediate-job'})
        matching = [{pid: row for pid, row in inventory.items() if pid in actual} for inventory in (first, second)]
        transport_after = _read_live(api, transport_handle, transport_pid, transport_creation, binary, _clock)
        endpoint_after = (dict(transport_after) if mode == 'native-direct' else
            _read_live(api, endpoint_handle, endpoint['pid'], endpoint['creation_ticks'], runtime, _clock))
        helpers_after = [_read_live(api, handle, row['pid'], row['creation_ticks'], helper_file, _clock)
                         for row, handle in helpers]
        result = {'schema_version': 1, 'method': METHOD, 'profile_id': profile.get('id'), 'mode': mode,
            'transport': transport, 'endpoint': endpoint, 'transport_after': transport_after,
            'endpoint_after': endpoint_after, 'frozen_binary': binary, 'frozen_script': script,
            'expected_base_runtime': runtime, 'observed_direct_children': children,
            'expected_console_helper': helper_file, 'console_helpers': [row for row, _ in helpers],
            'console_helpers_after': helpers_after, 'matching_inventories': matching,
            'query_access_mask': 0x101000, 'endpoint_job_scope': scope,
            'allowed_scope': 'Frozen endpoint and optional single frozen conhost; only verified current-Job descendants with nondecreasing held creation identities.',
            'scope': 'Two sampled Job/global inventories plus held PID/creation proof. Outside-Job parent matches never opened; stale same-Job edges excluded by positive creation inversion. No future-descendant guarantee.',
            **_bracket(started, _clock())}
        _validate_scope(result, worker_pid)
        return result
    except BaseException as issue:
        failure = issue
        forensic['error'] = f'{type(issue).__name__}: {issue}'
        issue.identity_receipt = forensic
        raise
    finally:
        errors = []
        for handle in reversed(handles):
            if not api.CloseHandle(handle):
                errors.append(int(handle))
        if errors:
            forensic['close_failed_handles'] = errors
            if failure is None:
                issue = _error('CloseHandle engine identity query')
                issue.identity_receipt = forensic
                raise issue


def _require(condition, message):
    if not condition:
        raise EngineIdentityError('Job endpoint scope: ' + message)


def _identity(row, label):
    _require(isinstance(row, dict), label + ' missing')
    _require(type(row.get('pid')) is int and 0 < row['pid'] <= 0xffffffff
             and type(row.get('creation_ticks')) is int and row['creation_ticks'] > 0, label + ' identity invalid')
    _require(row.get('wait_status') == 258, label + ' is not a live held process')
    start, finish = row.get('read_started_monotonic'), row.get('read_finished_monotonic')
    _bracket(start, finish)
    return row['pid'], row['creation_ticks']


def validate_resolution_scope(resolution, *, expected_worker_pid=None):
    """Pure raw-proof validation; does not inspect a process or file."""
    try:
        return _validate_scope(resolution, expected_worker_pid)
    except (AccountingError, KeyError, TypeError, IndexError, AttributeError) as issue:
        raise ValueError('invalid Job endpoint scope: ' + str(issue)) from issue


def _validate_scope(resolution, expected_worker_pid):
    _require(resolution.get('method') == METHOD, 'new method missing')
    scope = resolution['endpoint_job_scope']
    _require(scope.get('schema_version') == 1 and scope.get('method') == SCOPE_METHOD, 'scope schema/method differs')
    worker, coordinator = scope['worker_pid'], scope['coordinator_pid']
    _require(type(worker) is int and worker > 0 and type(coordinator) is int and coordinator > 0
             and worker != coordinator and (expected_worker_pid is None or worker == expected_worker_pid), 'worker/coordinator differs')
    worker_identity = _identity(scope['worker_identity'], 'worker')
    _require(worker_identity[0] == worker and _identity(scope['worker_identity_after'], 'worker after') == worker_identity,
             'worker held identity changed')
    snapshots, inventories = scope['job_snapshots'], scope['global_inventories']
    _require(len(snapshots) == len(inventories) == 2, 'two complete Job/global snapshots required')
    sets, maps = [], []
    for snapshot, inventory in zip(snapshots, inventories):
        _require(snapshot['query_job_handle'] == 'NULL-current-immediate-job' and snapshot['caller_in_job'] is True,
                 'calling immediate Job proof missing')
        flags = snapshot['limit_flags']
        _require(type(flags) is int and flags >= 0 and flags & _BREAKAWAY == 0, 'Job permits breakaway')
        _require(snapshot['limits_returned_bytes'] == C.sizeof(_ExtendedLimits), 'Job limits length differs')
        pids = snapshot['pids']
        _require(isinstance(pids, list) and all(type(pid) is int and 0 < pid <= 0xffffffff for pid in pids)
                 and len(set(pids)) == len(pids), 'Job PID list invalid or duplicate')
        count = len(pids)
        _require(snapshot['assigned_processes'] == snapshot['listed_processes'] == count
                 and count <= snapshot['buffer_capacity'] <= _MAX_PIDS, 'Job PID list incomplete')
        _require(snapshot['pointer_bytes'] == C.sizeof(C.c_size_t)
                 and 8 + count * snapshot['pointer_bytes'] <= snapshot['returned_bytes']
                 <= 8 + snapshot['buffer_capacity'] * snapshot['pointer_bytes'], 'Job PID bytes truncated')
        attempts=snapshot['query_attempts']
        _require(isinstance(attempts,list) and 1 <= len(attempts) <= _MAX_ATTEMPTS, 'Job query attempts missing or unbounded')
        for index,attempt in enumerate(attempts):
            capacity=attempt['buffer_capacity']
            _require(type(capacity) is int and 0 < capacity <= _MAX_PIDS
                     and attempt['buffer_bytes']==8+capacity*snapshot['pointer_bytes'], 'Job attempt buffer malformed')
            _require(type(attempt['success']) is bool and type(attempt['windows_error']) is int
                     and (attempt['windows_error']==0 if attempt['success'] else attempt['windows_error'] in (122,234)),
                     'Job attempt API result invalid')
            _require(type(attempt['assigned_processes']) is int and type(attempt['listed_processes']) is int
                     and 0 <= attempt['listed_processes'] <= min(attempt['assigned_processes'],capacity)
                     and attempt['assigned_processes'] <= _MAX_PIDS
                     and len(attempt['pids'])==attempt['listed_processes'], 'Job attempt PID counts malformed')
            if index:
                _require(capacity>attempts[index-1]['buffer_capacity'], 'Job query buffer did not grow')
        final_attempt=attempts[-1]
        _require(final_attempt['success'] is True and final_attempt['windows_error']==0
                 and sorted(final_attempt['pids'])==sorted(pids)
                 and all(final_attempt[key]==snapshot[key] for key in
                         ('buffer_capacity','returned_bytes','assigned_processes','listed_processes')),
                 'final Job query attempt differs from complete snapshot')
        _bracket(snapshot['read_started_monotonic'], snapshot['read_finished_monotonic'])
        _require(snapshot['worker_pid'] == worker and snapshot['coordinator_pid'] == coordinator
                 and worker in pids and coordinator not in pids, 'Job worker/coordinator membership differs')
        _require(isinstance(inventory, list), 'global inventory missing')
        table = {}
        for row in inventory:
            _require(isinstance(row, dict) and type(row.get('pid')) is int and row['pid'] >= 0
                     and type(row.get('parent_pid')) is int and row['parent_pid'] >= 0
                     and row['pid'] not in table, 'global inventory malformed or duplicate')
            table[row['pid']] = row
        _require(worker in table and table[worker]['parent_pid'] == coordinator, 'worker global parent differs')
        sets.append(set(pids)); maps.append(table)
    before, after = {}, {}
    for target, rows in ((before, scope['membership_candidates']), (after, scope['membership_candidates_after'])):
        for row in rows:
            pid, _ = _identity(row, 'held candidate')
            _require(pid not in target and row.get('in_any_job') is True, 'candidate duplicate or Job proof missing')
            target[pid] = row
    _require(set(before) == set(after), 'held candidate set changed')
    for pid, row in before.items():
        _require(_identity(after[pid], 'candidate after') == (pid, row['creation_ticks']), 'held candidate identity changed')
        _require(pid in sets[0] & sets[1] and pid in maps[0] and pid in maps[1], 'candidate left Job or inventory')
        _require(row['parent_pid'] == after[pid]['parent_pid'] == maps[0][pid]['parent_pid'] == maps[1][pid]['parent_pid'],
                 'candidate parent inventory changed')
    transport = resolution['transport']
    transport_identity = _identity(transport, 'transport')
    tid = transport_identity[0]
    _require(tid in before and (tid, before[tid]['creation_ticks']) == transport_identity
             and transport['parent_pid'] == worker and before[tid]['parent_pid'] == worker
             and before[tid]['parent_creation_ticks'] == worker_identity[1]
             and transport_identity[1] >= worker_identity[1], 'transport worker/creation differs')
    _require(_identity(resolution['transport_after'], 'transport after') == transport_identity, 'transport changed')
    local = scope['local_subtree_pids']
    _require(isinstance(local, list) and len(set(local)) == len(local) and tid in local, 'local subtree invalid')
    local = set(local)
    endpoint_identity = _identity(resolution['endpoint'], 'endpoint')
    _require(endpoint_identity[0] in local and _identity(resolution['endpoint_after'], 'endpoint after') == endpoint_identity,
             'endpoint held identity differs')
    helpers = resolution.get('console_helpers', [])
    expected_local = {tid, endpoint_identity[0], *(_identity(row, 'helper')[0] for row in helpers)}
    _require(local == expected_local and len(helpers) <= 1, 'local subtree has unknown members')
    _require(len(resolution.get('console_helpers_after', [])) == len(helpers), 'helper final identities missing')
    for first, second in zip(helpers, resolution.get('console_helpers_after', [])):
        _require(_identity(first, 'helper') == _identity(second, 'helper after'), 'helper held identity changed')
    for pid in local:
        _require(pid in before, 'local process held proof missing')
        if pid != tid:
            _require(before[pid]['parent_pid'] == tid and before[pid]['parent_creation_ticks'] == transport_identity[1]
                     and before[pid]['creation_ticks'] >= transport_identity[1], 'current local descendant invalid')
    _require(sorted(resolution['observed_direct_children']) == sorted(local - {tid}), 'observed current children differ')
    stale = {}
    for edge in scope['stale_job_edges']:
        pid, parent = edge['pid'], edge['parent_pid']
        _require(pid not in stale and pid not in local and parent in local, 'stale edge duplicate or parent invalid')
        identity = _identity(edge['child_identity'], 'stale child')
        _require(_identity(edge['child_identity_after'], 'stale child after') == identity
                 and pid in before and identity == (pid, before[pid]['creation_ticks'])
                 and edge['child_identity'] == before[pid] and edge['child_identity_after'] == after[pid],
                 'stale held creation proof differs')
        parent_creation = before[parent]['creation_ticks']
        _require(identity[0] == pid and edge['parent_creation_ticks'] == parent_creation
                 and before[pid]['parent_pid'] == parent and identity[1] < parent_creation
                 and edge['reason'] == 'creation-precedes-held-parent', 'stale creation inversion is unproven')
        stale[pid] = edge
    _require(set(before) == local | set(stale), 'unknown held candidate remains')
    outside = {}
    for edge in scope['outside_job_edges']:
        key = edge['pid'], edge['parent_pid']
        _require(key not in outside and key[1] in local and edge['in_job_before'] is False
                 and edge['in_job_after'] is False and edge['reason'] == 'outside-current-immediate-job'
                 and key[0] not in sets[0] | sets[1] and key[0] not in before,
                 'outside edge exclusion lacks Job proof')
        _require(edge['observed_in_first'] is (maps[0].get(key[0], {}).get('parent_pid') == key[1])
                 and edge['observed_in_second'] is (maps[1].get(key[0], {}).get('parent_pid') == key[1]),
                 'outside edge raw inventory differs')
        outside[key] = edge
    for index, table in enumerate(maps):
        for pid, row in table.items():
            if row['parent_pid'] not in local:
                continue
            if pid in sets[index]:
                _require(pid in local or pid in stale, 'new or unknown actual Job descendant')
            else:
                _require((pid, row['parent_pid']) in outside, 'outside edge is not explicitly recorded')
    expected_matching = [{str(pid): maps[index][pid] for pid in sorted(local)} for index in (0, 1)]
    actual_matching = [{str(pid): row for pid, row in table.items()} for table in resolution['matching_inventories']]
    _require(actual_matching == expected_matching and actual_matching[0] == actual_matching[1], 'scoped inventory changed')
    return {'status': 'PASS', 'worker_pid': worker, 'coordinator_pid': coordinator,
            'job_scope_method': SCOPE_METHOD, 'local_processes': len(local),
            'excluded_outside_job_edges': len(outside), 'excluded_stale_job_edges': len(stale)}
