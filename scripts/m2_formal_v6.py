"""Preregistered M2: fixed affinities, guarded lanes, preflight then full pairs.

Keeps the earlier pilot harness and its receipts unchanged. This is a bounded
local external baseline, not a model-promotion test or a universal Elo estimate.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from unittest.mock import patch

import chess
import chess.pgn

import compare_engines as comparison
import external_baseline_matrix as pilot
from engine_profile import load_profile, snapshot_profile, compare_arguments
from m2_job_accounting import OwnedJob
from m2_job_operations import bounded_job_call
from m2_warning_receipts_v2 import validate_job_receipts
from m2_engine_tracking_v2 import EngineCpuTrace, validate_trace_file
from m2_engine_receipts_v2 import validate_cell_cpu, validate_job_scopes

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ('m2_formal_v6.py', 'm2_formal_summary_v6.py', 'm2_job_process_scope.py', 'm2_warning_load_guard_v2.py', 'm2_warning_receipts_v2.py', 'm2_formal_v5.py', 'm2_formal_summary_v5.py', 'm2_engine_identity_v2.py', 'm2_engine_tracking_v2.py', 'm2_engine_receipts_v2.py', 'm2_formal_v4.py', 'm2_formal_summary_v4.py', 'm2_warning_load_guard.py', 'm2_warning_receipts.py', 'm2_engine_cpu.py', 'm2_engine_receipts.py', 'm2_engine_identity.py', 'm2_engine_tracking.py', 'm2_formal_v3.py', 'm2_formal_summary_v3.py', 'm2_formal_v2.py', 'm2_job_load_guard.py', 'm2_job_accounting.py', 'm2_job_operations.py', 'm2_job_receipts.py', 'm2_formal_summary_v2.py', 'm2_formal.py', 'm2_load_guard.py', 'compare_engines.py',
           'external_baseline_matrix.py', 'engine_profile.py', 'see_clean_guard.py', 'm2_formal_summary.py')
sha256, write_json, utc_now = pilot.sha256, pilot.write_json, pilot.utc_now


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def topology():
    """Read physical-core masks, without assuming logical CPU numbering."""
    if sys.platform != 'win32':
        raise ValueError('formal affinity execution currently requires Windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class Cache(ctypes.Structure):
        _fields_ = [('level', ctypes.c_ubyte), ('associativity', ctypes.c_ubyte),
                    ('line_size', wintypes.WORD), ('size', wintypes.DWORD), ('type', ctypes.c_int)]
    class Detail(ctypes.Union):
        _fields_ = [('flags', ctypes.c_ubyte), ('node', wintypes.DWORD),
                    ('cache', Cache), ('reserved', ctypes.c_ulonglong * 2)]
    class Information(ctypes.Structure):
        _fields_ = [('mask', ctypes.c_size_t), ('relationship', ctypes.c_int), ('detail', Detail)]
    function = kernel.GetLogicalProcessorInformation
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    function.restype = wintypes.BOOL
    size = wintypes.DWORD()
    function(None, ctypes.byref(size))
    if not size.value:
        raise OSError(ctypes.get_last_error(), 'CPU topology size query failed')
    buffer = ctypes.create_string_buffer(size.value)
    if not function(buffer, ctypes.byref(size)):
        raise OSError(ctypes.get_last_error(), 'CPU topology query failed')
    rows = (Information * (size.value // ctypes.sizeof(Information))).from_buffer(buffer)
    cores = sorted(int(row.mask) for row in rows if row.relationship == 0)
    return {'physical_core_masks': cores, 'logical_cpus': os.cpu_count(),
            'scope': 'Single Windows processor group, physical-core affinity masks.'}


def affinity(pid=None, mask=None):
    if sys.platform != 'win32':
        raise ValueError('Windows affinity is required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    kernel.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    handle = kernel.OpenProcess(0x1200, False, pid or os.getpid())
    if not handle:
        raise OSError(ctypes.get_last_error(), 'Cannot inspect owned process affinity')
    try:
        if mask is not None and not kernel.SetProcessAffinityMask(handle, mask):
            raise OSError(ctypes.get_last_error(), 'Cannot apply frozen affinity')
        actual, system = ctypes.c_size_t(), ctypes.c_size_t()
        if not kernel.GetProcessAffinityMask(handle, ctypes.byref(actual), ctypes.byref(system)):
            raise OSError(ctypes.get_last_error(), 'Cannot verify affinity')
        if mask is not None and actual.value != mask:
            raise ValueError('Affinity did not match requested mask')
        return actual.value
    finally:
        kernel.CloseHandle(handle)


def validate_resources(plan):
    masks = plan['lane_affinity_masks']
    if not isinstance(masks, list) or len(masks) != 4:
        raise ValueError('exactly four preregistered lanes are required')
    combined = 0
    for mask in masks:
        if type(mask) is not int or mask <= 0 or combined & mask:
            raise ValueError('lane masks must be positive and disjoint')
        combined |= mask
    cores = plan['topology']['physical_core_masks']
    if len(cores) != 8 or any(sum(bool(mask & core) for core in cores) != 2 for mask in masks):
        raise ValueError('each lane must have exactly two physical cores')
    for mask in masks:
        if any(mask & core not in (0, core) for core in cores):
            raise ValueError('a physical core cannot be split between lanes')
    if combined != sum(cores):
        raise ValueError('lane masks must cover the declared topology')


def cell_schedule(candidates, opponents, times, openings, phase):
    cells = []
    for time_index, movetime in enumerate(times):
        for family_index, opening in enumerate(openings):
            for opponent_index, opponent in enumerate(opponents):
                shift = (family_index + opponent_index + time_index) % len(candidates)
                order = candidates[shift:] + candidates[:shift]
                for candidate in order:
                    index = len(cells)
                    cells.append({'id': f'{phase}-{index+1:04d}',
                                  'lane': (family_index + 2*time_index + opponent_index) % 4,
                                  'candidate': candidate, 'opponent': opponent, 'movetime_ms': movetime,
                                  'opening_index': family_index, 'family_id': opening['family_id'],
                                  'cluster_id': opening['cluster_id']})
    return cells


def load_plan(path):
    path = Path(path).resolve()
    plan = read_json(path)
    if plan.get('schema_version') != 3 or plan.get('phase') != 'formal-m2-engine-cpu':
        raise ValueError('expected formal-m2-engine-cpu schema version 3')
    if (plan.get('accounting_policy', {}).get('mode') != 'windows-job-plus-coordinator-cpu'
            or plan.get('load_policy', {}).get('max_nonworker_cpu_fraction') != .2):
        raise ValueError('Job/coordinator accounting and unchanged 20 percent warning threshold required')
    if (plan.get('residual_load_policy', {}).get('enforcement') != 'warning-only'
            or plan['residual_load_policy'].get('stops_phase') is not False):
        raise ValueError('residual CPU must be warning-only')
    if plan.get('engine_cpu_policy', {}).get('actual_endpoint_resolution', {}).get('ownership_scope') != 'calling-worker-current-job-with-no-breakaway-and-creation-order':
        raise ValueError('exact current worker Job endpoint scope required')
    if plan.get('process_discovery_policy', {}).get('mode') != 'explicit-owned-job-membership-before-process-query':
        raise ValueError('runtime process discovery must verify explicit owned Job membership before CPU queries')
    if plan.get('movetimes_ms') != [100, 1000] or plan.get('draw_policy') != 'claim':
        raise ValueError('formal M2 requires 100/1000ms and the declared claim policy')
    if plan.get('retry_policy') != 'no-result-based-retries-stop-on-first-failure':
        raise ValueError('explicit nonadaptive stop policy required')
    for key in ('max_plies', 'cell_timeout_seconds', 'preflight_timeout_seconds', 'main_timeout_seconds'):
        pilot.integer(plan[key], key)
    if plan['max_plies'] < 360 or plan.get('preflight_opening_count') != 1:
        raise ValueError('formal ply cap/preflight count differs from contract')
    from m2_load_guard import LoadPolicy
    LoadPolicy(**plan['load_policy'])
    if plan['idle_probe_seconds'] < plan['load_policy']['host_window_seconds']:
        raise ValueError('idle probe must include a complete host CPU window')
    if plan.get('move_deadline') != {'multiplier': 2.0, 'slack_seconds': 0.25}:
        raise ValueError('explicit UCI deadline policy required')
    validate_resources(plan)
    pilot.referenced_file(plan['worker_runtime']['executable'], path.parent)
    for entry in plan['worker_runtime']['package_files']:
        pilot.referenced_file(entry,path.parent)
    profiles = {}
    for role, kind, count in [('candidates', 'regicide', 3), ('opponents', 'external', 2)]:
        if len(plan[role]) != count:
            raise ValueError('expected three candidates and two opponents')
        profiles[role] = [load_profile(pilot.referenced_file(entry, path.parent)) for entry in plan[role]]
        if any(p['kind'] != kind for p in profiles[role]):
            raise ValueError('profile kind mismatch')
    identifiers = [p['id'] for group in profiles.values() for p in group]
    if len(set(identifiers)) != 5 or {p['evaluation'] for p in profiles['candidates']} != {'classical', 'residual', 'nnue'}:
        raise ValueError('three distinct evaluation paths must be preserved')
    banks = {}
    for phase, key in [('preflight', 'preflight_openings'), ('main', 'openings')]:
        bank = pilot.referenced_file(plan[key], path.parent)
        _, rows = comparison.load_openings(bank)
        rows = rows[:1] if phase == 'preflight' else rows
        if len(rows) != (1 if phase == 'preflight' else 32):
            raise ValueError('wrong opening-family count')
        if len({r['family_id'] for r in rows}) != len(rows) or any(not r.get('cluster_id') for r in rows):
            raise ValueError('family and correlation cluster declarations required')
        banks[phase] = (bank, rows)
    if len({r['family_id'] for _, rows in banks.values() for r in rows}) != 33:
        raise ValueError('preflight/main families overlap')
    for entry in plan['preregistration_artifacts']:
        pilot.referenced_file(entry, path.parent)
    return plan, profiles, banks


def freeze_plan(path, destination):
    path, destination = Path(path).resolve(), Path(destination).resolve()
    plan, profiles, banks = load_plan(path)
    if topology() != plan['topology']:
        raise ValueError('physical CPU topology changed')
    destination.mkdir(parents=True, exist_ok=False)
    frozen_profiles = {}
    for group in profiles.values():
        for profile in group:
            target = destination/'profiles'/profile['id']
            target.parent.mkdir(exist_ok=True)
            snapshot_profile(profile, target)
            frozen_profiles[profile['id']] = {'path': str(target/'profile.json'), 'sha256': sha256(target/'profile.json')}
    frozen = {'schema_version': 1, 'frozen_utc': utc_now(), 'plan': plan,
              'source_plan_sha256': sha256(path), 'source_plan_path': str(path), 'profiles': frozen_profiles,
              'harness_source_sha256': {}, 'banks': {}, 'cells': {}, 'preregistration_artifacts': []}
    # Verify real Git objects here, before measured workers. Short-lived Git
    # helpers can exit between process enumeration and a native timer query.
    commits=sorted({p['source']['commit'] for p in profiles['candidates']})
    queries=[['rev-parse','--verify',commit+'^{commit}'] for commit in commits]
    queries += [['rev-parse','HEAD'],['status','--porcelain']]
    frozen['git_receipts']=[{'arguments':query,'stdout':comparison.git_command(*query),'exit_code':0,
        'verified_utc':utc_now(),'scope':'Actual root Git command at freeze; stdout uses existing harness strip normalization.'} for query in queries]
    (destination/'harness').mkdir()
    (destination/'source-plan.json').write_bytes(path.read_bytes())
    for name in HARNESS:
        source = ROOT/'scripts'/name
        (destination/'harness'/name).write_bytes(source.read_bytes())
        frozen['harness_source_sha256'][str(source)] = sha256(source)
    for index, entry in enumerate(plan['preregistration_artifacts']):
        source = pilot.referenced_file(entry, path.parent)
        target = destination/f'preregistration-{index:02d}-{source.name}'
        target.write_bytes(source.read_bytes())
        frozen['preregistration_artifacts'].append({'path': str(target), 'sha256': sha256(target)})
    for phase, (source, rows) in banks.items():
        target = destination/(phase+'-openings.json')
        write_json(target, {'schema_version': 1, 'openings': rows})
        frozen['banks'][phase] = {'path': str(target), 'sha256': sha256(target)}
        frozen['cells'][phase] = cell_schedule([p['id'] for p in profiles['candidates']],
            [p['id'] for p in profiles['opponents']], plan['movetimes_ms'], rows, phase)
    write_json(destination/'frozen-plan.json', frozen)
    write_json(destination/'seal.json', {'frozen_plan_sha256': sha256(destination/'frozen-plan.json')})
    verify_frozen(destination)
    return frozen


def verify_frozen(directory):
    directory = Path(directory).resolve()
    seal, frozen = read_json(directory/'seal.json'), read_json(directory/'frozen-plan.json')
    if sha256(directory/'frozen-plan.json') != seal['frozen_plan_sha256']:
        raise ValueError('frozen formal plan changed')
    if sha256(directory/'source-plan.json') != frozen['source_plan_sha256']:
        raise ValueError('preregistered plan copy changed')
    if read_json(directory/'source-plan.json') != frozen['plan']:
        raise ValueError('frozen conditions differ from preregistered plan copy')
    for absolute, digest in frozen['harness_source_sha256'].items():
        if sha256(absolute) != digest or sha256(directory/'harness'/Path(absolute).name) != digest:
            raise ValueError('formal harness changed after freeze')
    for entry in frozen['preregistration_artifacts']:
        pilot.referenced_file(entry, directory)
    pilot.referenced_file(frozen['plan']['worker_runtime']['executable'],Path(frozen['source_plan_path']).parent)
    for entry in frozen['plan']['worker_runtime']['package_files']:
        pilot.referenced_file(entry,Path(frozen['source_plan_path']).parent)
    profiles = {key: load_profile(pilot.referenced_file(entry, directory)) for key, entry in frozen['profiles'].items()}
    for phase, entry in frozen['banks'].items():
        bank = pilot.referenced_file(entry, directory)
        _, rows = comparison.load_openings(bank)
        candidate_ids = [p['id'] for p in profiles.values() if p['kind']=='regicide']
        opponent_ids = [p['id'] for p in profiles.values() if p['kind']=='external']
        if cell_schedule(candidate_ids, opponent_ids, frozen['plan']['movetimes_ms'], rows, phase) != frozen['cells'][phase]:
            raise ValueError('schedule does not match full preregistered Cartesian product')
    return frozen, profiles


def cell_inputs(directory, frozen, profiles, cell):
    target = Path(directory)/cell['id'].split('-')[0]/'cells'/cell['id']
    phase = cell['id'].split('-')[0]
    _, rows = comparison.load_openings(Path(frozen['banks'][phase]['path']))
    target.mkdir(parents=True, exist_ok=False)
    write_json(target/'openings.json', {'schema_version': 1, 'openings': [rows[cell['opening_index']]]})
    opponent, candidate = (compare_arguments(profiles[cell[key]]) for key in ('opponent','candidate'))
    write_json(target/'external-source.json', opponent['external_source'])
    command = [opponent['binary'], candidate['binary'], '--baseline-source', str(target/'external-source.json'),
        '--candidate-commit', candidate['commit'], '--baseline-options', json.dumps(opponent['options']),
        '--candidate-options', json.dumps(candidate['options']), '--openings', str(target/'openings.json'),
        '--movetime', str(cell['movetime_ms']), '--max-plies', str(frozen['plan']['max_plies']),
        '--draw-policy', 'claim', '--pgn', str(target/'games.pgn')]
    return target, command


def git_receipt_cache(receipts):
    cache={}
    for entry in receipts:
        key=tuple(entry['arguments'])
        if key in cache or entry['exit_code']!=0 or not isinstance(entry['stdout'],str):
            raise ValueError('invalid sealed Git receipt')
        cache[key]=entry['stdout']
    def lookup(*arguments):
        key=tuple(arguments)
        if key not in cache:
            raise ValueError('worker requested a Git query not verified at input freeze')
        return cache[key]
    return lookup


def execute_worker(directory, phase, index, start_gate):
    directory=Path(directory).resolve()
    if sha256(directory/'frozen-plan.json')!=read_json(directory/'seal.json')['frozen_plan_sha256']:
        raise ValueError('Git receipt envelope changed')
    frozen=read_json(directory/'frozen-plan.json')
    # The existing full commit validator still runs. Only its exact previously
    # verified Git queries are served from the sealed root receipts.
    with patch.object(comparison,'git_command',side_effect=git_receipt_cache(frozen['git_receipts'])):
        return _execute_worker(directory,phase,index,start_gate)


def _execute_worker(directory, phase, index, start_gate):
    directory = Path(directory).resolve()
    frozen, profiles = verify_frozen(directory)
    cell = frozen['cells'][phase][index]
    runtime=frozen['plan']['worker_runtime']
    environment={key:os.environ.get(key) for key in ('PYTHONPATH','PYTHONNOUSERSITE','PYTHONDONTWRITEBYTECODE')}
    if (Path(sys.executable).resolve()!=Path(runtime['executable']['path']).resolve()
            or platform.python_version()!=runtime['python_version'] or chess.__version__!=runtime['chess_version']
            or environment!={'PYTHONPATH':runtime['pythonpath'],'PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1'}
            or not sys.flags.no_user_site):
        raise ValueError('worker runtime/environment differs from frozen direct interpreter')
    deadline=time.monotonic()+30
    while not Path(start_gate).is_file():
        if time.monotonic()>deadline: raise TimeoutError('parent did not authorize owned worker startup')
        time.sleep(.05)
    gate=read_json(start_gate)
    if gate!={'worker_pid':os.getpid(),'frozen_plan_sha256':sha256(directory/'frozen-plan.json')}:
        raise ValueError('worker startup gate differs from parent registration')
    mask = frozen['plan']['lane_affinity_masks'][cell['lane']]
    affinity(mask=mask)
    target, command = cell_inputs(directory, frozen, profiles, cell)
    args = comparison.arguments(command)
    original = comparison.chess.engine.SimpleEngine.popen_uci
    transports = []
    def launch(*values, **kwargs):
        engine = original(*values, **kwargs)
        try:
            pid = engine.transport.get_pid()
            observed = affinity(pid)
            if observed != mask:
                raise ValueError('engine did not inherit its lane affinity')
            from see_clean_guard import ProcessInfo, WindowsProcesses
            provider=WindowsProcesses()
            try:
                identity=provider.times(ProcessInfo(pid,os.getpid(),Path(values[0][0]).name))
                if identity.error or identity.creation_ticks is None:
                    raise ValueError('cannot establish actual UCI transport creation identity')
            finally:
                provider.close()
            transports.append({'pid': pid, 'creation_ticks':identity.creation_ticks,
                               'affinity_mask': observed, 'command': list(values[0])})
            index=len(transports)-1
            profile=profiles[cell['opponent' if index==0 else 'candidate']]
            return cpu_tracker.wrap(engine, transports[-1], index, profile)
        except BaseException:
            engine.quit()
            engine.close()
            raise
    write_json(target/'invocation.json', {'comparison_arguments': command, 'cell': cell,
        'worker_pid': os.getpid(), 'affinity_mask': mask, 'started_utc': utc_now(),
        'runtime_executable':sys.executable,'runtime_sha256':sha256(sys.executable),
        'python_version':platform.python_version(),'chess_version':chess.__version__,
        'environment':environment,'no_user_site':sys.flags.no_user_site})
    with EngineCpuTrace(target, cell, frozen['plan']['worker_runtime']['executable']) as cpu_tracker:
        with patch.object(comparison.chess.engine.SimpleEngine, 'popen_uci', side_effect=launch):
            metadata, code = comparison.run_match(args)
    metadata['formal_cell'] = cell
    metadata['formal_git_receipts']=frozen['git_receipts']
    metadata['harness_checkout']['scope']='Git objects/context verified by real root commands at input freeze and served from sealed exact-query receipts. Worker rechecks file/model/harness hashes at match start; no transient Git helper is launched in a measured worker.'
    metadata['affinity'] = {'worker_pid': os.getpid(), 'worker_mask': affinity(), 'expected_mask': mask, 'transports': transports}
    metadata, code = cpu_tracker.attach(metadata, code)
    comparison.save_outputs(args, metadata)
    print(json.dumps({'cell': cell['id'], 'completed': metadata['completed'], 'exit_code': code}), flush=True)
    return code


def audit_cell(directory, frozen, profiles, cell):
    phase = cell['id'].split('-')[0]
    target = Path(directory)/phase/'cells'/cell['id']
    metadata = read_json(target/'games.json')
    if metadata.get('formal_git_receipts') != frozen['git_receipts']:
        raise ValueError('worker Git provenance differs from root-verified sealed receipts')
    cached_git=git_receipt_cache(frozen['git_receipts'])
    if (metadata['harness_checkout']['commit']!=cached_git('rev-parse','HEAD')
            or metadata['harness_checkout']['status_porcelain']!=cached_git('status','--porcelain')):
        raise ValueError('worker checkout provenance differs from input freeze')
    _, rows = comparison.load_openings(Path(frozen['banks'][phase]['path']))
    expected_opening = rows[cell['opening_index']]
    if (metadata['formal_cell'] != cell or metadata['selected_openings'] != [expected_opening]
            or metadata['opening_file_sha256'] != sha256(target/'openings.json')
            or metadata['limits'] != {'nodes': None, 'depth': None, 'movetime_ms': cell['movetime_ms']}
            or metadata['max_plies'] != frozen['plan']['max_plies'] or metadata['draw_policy']!='claim'):
        raise ValueError('played conditions differ from frozen plan')
    for role, key in [('candidate','candidate'), ('baseline','opponent')]:
        profile = profiles[cell[key]]
        expected, actual = compare_arguments(profile), metadata['engines'][role]
        if (actual['command'] != [expected['binary'], *expected['args']]
                or actual['sha256'] != profile['binary']['sha256']
                or actual['provided_source_commit'] != expected['commit']
                or actual['requested_options'] != expected['options'] or actual['validated_options'] != expected['options']):
            raise ValueError('engine/model/option identity mismatch')
    mask = frozen['plan']['lane_affinity_masks'][cell['lane']]
    setting = metadata['affinity']
    if (setting['worker_mask'] != mask or setting['expected_mask'] != mask
            or len(setting['transports']) != 2 or any(row['affinity_mask']!=mask for row in setting['transports'])):
        raise ValueError('engine affinity record mismatch')
    if [row['command'] for row in setting['transports']] != [metadata['engines'][role]['command'] for role in ('baseline','candidate')]:
        raise ValueError('transport commands differ from actual declared engines')
    if (len({row['pid'] for row in setting['transports']})!=2
            or any(type(row.get('creation_ticks')) is not int or row['creation_ticks']<=0 for row in setting['transports'])):
        raise ValueError('transport process identities are missing or duplicated')
    invocation=read_json(target/'invocation.json')
    gate=read_json(Path(directory)/phase/'start-gates'/(cell['id']+'.json'))
    coordinator=read_json(Path(directory)/phase/'run-started.json')['pid']
    worker=setting.get('worker_pid')
    if (type(worker) is not int or worker<=0 or invocation.get('worker_pid')!=worker
            or gate!={'worker_pid':worker,'frozen_plan_sha256':sha256(Path(directory)/'frozen-plan.json')}):
        raise ValueError('CPU endpoint scope worker differs from invocation/start gate')
    job_scope_audits=validate_job_scopes(metadata)
    if any(p['endpoint_resolution']['endpoint_job_scope']['coordinator_pid']!=coordinator
           for p in metadata['engine_cpu_processes']):
        raise ValueError('CPU endpoint scope coordinator differs from phase owner')
    cpu_summary=validate_cell_cpu(metadata,cell)
    if metadata.get('engine_cpu_summary') != cpu_summary:
        raise ValueError('engine CPU summary changed')
    cpu_trace_audit=validate_trace_file(metadata,target)
    summary = pilot.replay_cell(metadata, target/'games.pgn')
    pgn = io.StringIO((target/'games.pgn').read_text(encoding='utf-8'))
    pv_plies = 0
    for record in metadata['games']:
        game = chess.pgn.read_game(pgn)
        if record['initial_fen'] != chess.STARTING_FEN or game.board().fen()!=chess.STARTING_FEN:
            raise ValueError('nonstandard initial board')
        board = chess.Board()
        for move in record['opening_uci_moves']:
            board.push_uci(move)
        if len(record['search_moves']) != len(record['uci_moves'])-len(record['opening_uci_moves']):
            raise ValueError('missing search receipts')
        for offset, move_record in enumerate(record['search_moves'], len(record['opening_uci_moves'])):
            if move_record['fen_before'] != board.fen(en_passant='fen'):
                raise ValueError('search record does not match pre-move position')
            if (move_record['move'] != record['uci_moves'][offset] or move_record['ply'] != offset+1
                    or move_record['role'] != record['white' if board.turn else 'black']
                    or move_record['color'] != ('white' if board.turn else 'black')):
                raise ValueError('search receipt role/move/ply mismatch')
            pv_board = board.copy()
            for move in move_record.get('pv', []):
                pv_board.push_uci(move)
                pv_plies += 1
            board.push_uci(move_record['move'])
    return {'summary': summary, 'games_json_sha256': sha256(target/'games.json'),
            'pgn_sha256': sha256(target/'games.pgn'), 'pv_plies': pv_plies,
            'engine_job_scope_audits': job_scope_audits,
            'engine_cpu_summary': cpu_summary, 'engine_cpu_trace_audit': cpu_trace_audit,
            'engine_cpu_trace_sha256':sha256(target/'engine-cpu.jsonl')}


def run_phase(directory, phase):
    # Check real Git objects before the measured interval. During the entire
    # measured phase, coordinator and workers may only reuse exact queries
    # already verified and sealed at input freeze; no helper Git process is
    # allowed to escape Job plus coordinator-process CPU accounting.
    directory = Path(directory).resolve()
    frozen, _ = verify_frozen(directory)
    lookup = git_receipt_cache(frozen['git_receipts'])
    with patch.object(comparison, 'git_command', side_effect=lookup):
        return _run_phase(directory, phase)


def _run_phase(directory, phase):
    from m2_warning_load_guard_v2 import LoadPolicy, WarningGuardMonitor as GuardMonitor
    directory = Path(directory).resolve()
    frozen, profiles = verify_frozen(directory)
    if topology() != frozen['plan']['topology']:
        raise ValueError('topology changed since preregistration')
    if phase == 'main':
        preflight = audit_phase(directory, 'preflight')
        if preflight['status'] != 'PASS' or not preflight['valid_for_comparison']:
            raise ValueError('main requires a clean, complete audited preflight')
    target = directory/phase
    target.mkdir(exist_ok=True)
    with (target/'run-started.json').open('x', encoding='utf-8') as stream:
        json.dump({'started_utc': utc_now(), 'pid': os.getpid()}, stream)
    report = {'status': 'RUNNING', 'phase': phase, 'started_utc': utc_now(),
        'frozen_plan_sha256': sha256(directory/'frozen-plan.json'), 'cells': [],
        'scheduled_cells': len(frozen['cells'][phase]), 'scheduled_games': len(frozen['cells'][phase])*2,
        'valid_for_comparison': False, 'elo_estimate': None, 'promotion_performed': False,
        'launched_cells': [],
        'runtime': {'python': platform.python_version(), 'chess': chess.__version__, 'platform': platform.platform()}}
    write_json(target/'report.json', report)
    policy = LoadPolicy(**frozen['plan']['load_policy'])
    guard, job = None, None
    active, lane_progress = {}, [0]*4
    queues = [[(i,c) for i,c in enumerate(frozen['cells'][phase]) if c['lane']==lane] for lane in range(4)]
    started, next_verify = time.monotonic(), time.monotonic()
    budget = frozen['plan'][phase+'_timeout_seconds']
    try:
        job=OwnedJob()
        guard = GuardMonitor(target/'load.jsonl', policy, job)
        guard.require_idle(frozen['plan']['idle_probe_seconds'])
        guard.start_timer()
        while any(lane_progress[lane] < len(queues[lane]) for lane in range(4)) or active:
            if time.monotonic()-started > budget:
                raise TimeoutError('preregistered phase wall budget exhausted')
            for lane, item in list(active.items()):
                process, index, cell, launch_time, log = item
                code = process.poll()
                if code is None:
                    if time.monotonic()-launch_time > frozen['plan']['cell_timeout_seconds']:
                        raise TimeoutError('preregistered pair wall budget exhausted')
                    continue
                log.close()
                guard.sample('worker-completed')
                guard.assert_clean()
                guard.detach_worker(process.pid)
                del active[lane]
                if code != 0:
                    raise ValueError(f"worker failed: {cell['id']} exit={code}")
                checked = audit_cell(directory, frozen, profiles, cell)
                if checked['summary']['completed_games'] != 2 or checked['summary']['incomplete_games']:
                    raise ValueError('incomplete pair; no truncation is scored as draw')
                report['cells'].append(dict(cell, status='COMPLETE', **checked))
                write_json(target/'report.json', report)
                print(f"{phase} {len(report['cells'])}/{len(frozen['cells'][phase])} pairs complete", flush=True)
            for lane in range(4):
                if lane in active or lane_progress[lane] >= len(queues[lane]):
                    continue
                index, cell = queues[lane][lane_progress[lane]]
                lane_progress[lane] += 1
                log_path = target/(cell['id']+'-worker.log')
                log = log_path.open('x', encoding='utf-8')
                start_gate=target/'start-gates'/(cell['id']+'.json')
                start_gate.parent.mkdir(exist_ok=True)
                runtime=frozen['plan']['worker_runtime']
                command = [runtime['executable']['path'], '-s', str(ROOT/'scripts/m2_formal_v6.py'), 'worker', '--directory', str(directory),
                           '--phase', phase, '--index', str(index), '--start-gate',str(start_gate)]
                environment=dict(os.environ,PYTHONPATH=runtime['pythonpath'],PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
                process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                    env=environment,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
                active[lane] = (process, index, cell, time.monotonic(), log)
                assignment = bounded_job_call(job.assign, process, timeout_seconds=policy.max_sample_gap_seconds)
                report['launched_cells'].append(dict(cell, pid=process.pid, command=command, job_assignment=assignment))
                identity=guard.identify_worker(process.pid)
                report['launched_cells'][-1]['creation_ticks']=identity[1]
                guard.attach_worker(*identity)
                write_json(start_gate,{'worker_pid':process.pid,'frozen_plan_sha256':sha256(directory/'frozen-plan.json')})
            guard.sample('running')
            guard.assert_clean()
            if time.monotonic() >= next_verify:
                verify_frozen(directory)
                next_verify = time.monotonic()+60
            time.sleep(policy.interval_seconds)
        guard.sample('phase-completed')
        guard.assert_clean()
        verify_frozen(directory)
        report['status'] = 'COMPLETE'
        report['valid_for_comparison'] = True
    except BaseException as error:
        report['status'] = 'INTERRUPTED' if isinstance(error, KeyboardInterrupt) else 'STOPPED'
        report['stop_reason'] = f'{type(error).__name__}: {error}'
        report['active_cells_at_stop'] = [dict(item[2], pid=item[0].pid) for item in active.values()]
    finally:
        if job is not None:
            try:
                final_job = bounded_job_call(job.terminate_and_snapshot, 5., timeout_seconds=7.)
                write_json(target/'job-final-accounting.json', final_job)
                report['job_final_accounting_sha256'] = sha256(target/'job-final-accounting.json')
            except Exception as error:
                report['status'], report['valid_for_comparison'] = 'STOPPED', False
                report.setdefault('stop_reason', f'Owned Job cleanup failed: {error}')
        for process, _, _, _, log in active.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # A worker whose Job assignment failed remains gated and may
                # not belong to the Job. Terminate only its held Popen handle.
                try:
                    bounded_job_call(process.kill, timeout_seconds=policy.max_sample_gap_seconds)
                    process.wait(timeout=2)
                except Exception as error:
                    report['cleanup_unknown'] = f'own worker cleanup failed: {error}'
                report['status'], report['valid_for_comparison'] = 'STOPPED', False
                report.setdefault('stop_reason', 'owned worker did not stop after Job termination')
            log.close()
        if guard is not None:
            guard.close()
            try:
                guard.assert_clean()
            except Exception as error:
                report['status'], report['valid_for_comparison'] = 'STOPPED', False
                report.setdefault('stop_reason', f'Final guard check failed: {error}')
        if job is not None:
            try:
                bounded_job_call(job.close, timeout_seconds=policy.max_sample_gap_seconds)
            except Exception as error:
                report['status'], report['valid_for_comparison'] = 'STOPPED', False
                report.setdefault('stop_reason', f'Owned Job close failed: {error}')
        write_json(target/'load-summary.json', guard.summary if guard is not None else
            {'valid_for_load_check':False,'failure':{'kind':'initialization','reason':report.get('stop_reason')}})
        report['guard_summary_sha256'] = sha256(target/'load-summary.json')
        report['guard_log_sha256'] = sha256(target/'load.jsonl') if (target/'load.jsonl').exists() else None
        report['finished_utc'] = utc_now()
        report['elapsed_seconds'] = round(time.monotonic()-started, 3)
        report['completed_games'] = sum(c['summary']['completed_games'] for c in report['cells'])
        report['uncompleted_scheduled_games'] = report['scheduled_games']-report['completed_games']
        report['unstarted_pairs'] = report['scheduled_cells']-len(report['launched_cells'])
        report['scope'] = 'Core-disjoint four-lane local baseline. Explicit owned-Job process discovery, owned Job CPU plus separate coordinator CPU; residual system/background is an asynchronous quantized warning only; actual endpoint CPU and per-search responses audited. Not full OS/thermal isolation or model promotion.'
        write_json(target/'report.json', report)
    return report


def audit_phase(directory, phase):
    directory = Path(directory).resolve()
    frozen, profiles = verify_frozen(directory)
    target = directory/phase
    report, guard = read_json(target/'report.json'), read_json(target/'load-summary.json')
    if report['frozen_plan_sha256'] != sha256(directory/'frozen-plan.json'):
        raise ValueError('report belongs to another frozen plan')
    observed_log=sha256(target/'load.jsonl') if (target/'load.jsonl').is_file() else None
    if (sha256(target/'load-summary.json') != report['guard_summary_sha256']
            or observed_log != report['guard_log_sha256']):
        raise ValueError('guard evidence changed')
    expected = {c['id']: c for c in frozen['cells'][phase]}
    seen = set()
    for cell in report['cells']:
        identifier = cell['id']
        if identifier not in expected or identifier in seen or any(cell.get(k)!=v for k,v in expected[identifier].items()):
            raise ValueError('extra, duplicated or changed pair')
        seen.add(identifier)
        checked = audit_cell(directory, frozen, profiles, expected[identifier])
        if any(cell[k]!=v for k,v in checked.items()):
            raise ValueError('pair evidence changed after completion')
    final_job=read_json(target/'job-final-accounting.json')
    if sha256(target/'job-final-accounting.json') != report.get('job_final_accounting_sha256'):
        raise ValueError('final Job accounting changed')
    raw_audit=validate_job_receipts(target/'load.jsonl',guard,final_job,
        dict(frozen['plan']['load_policy'],idle_probe_seconds=frozen['plan']['idle_probe_seconds']))
    guard_clean=bool(raw_audit['valid_for_load_check'] and final_job['active_processes']==0 and final_job['coordinator_in_job'] is False
        and guard.get('residual_cpu_policy')=='warning-only'
        and guard.get('schema_version')==4
        and guard.get('process_ownership_method')=='explicit-owned-job-membership-before-process-query'
        and guard.get('accounting_method')=='windows-job-plus-coordinator-cpu' and guard.get('valid_for_load_check') and guard.get('preflight_complete')
        and guard.get('failure') is None and guard.get('external_cpu_increase_ticks')==0
        and guard.get('completed_idle_seconds',0)>=frozen['plan']['idle_probe_seconds'])
    valid = bool(report['status']=='COMPLETE' and report['valid_for_comparison'] and seen==set(expected) and guard_clean)
    if (report['scheduled_cells']!=len(expected) or report['scheduled_games']!=len(expected)*2
            or report['completed_games']!=sum(c['summary']['completed_games'] for c in report['cells'])):
        raise ValueError('phase counters differ from durable evidence')
    if report['valid_for_comparison'] and not valid:
        raise ValueError('incomplete phase marked valid')
    return {'status': 'PASS', 'phase_status': report['status'], 'phase': phase,
            'audited_pairs': len(seen), 'scheduled_pairs': len(expected), 'valid_for_comparison': valid,
            'elo_estimate': None, 'promotion_performed': False,
            'guard_summary': guard, 'raw_cpu_audit': raw_audit}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('topology','freeze','run','worker','audit'))
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--phase', choices=('preflight','main'))
    parser.add_argument('--index', type=int)
    parser.add_argument('--start-gate',type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command=='topology':
            result=topology()
        elif args.command=='freeze':
            result=freeze_plan(args.plan,args.directory)
            result={'status':'FROZEN','scheduled_main_games':len(result['cells']['main'])*2}
        elif args.command=='worker':
            return execute_worker(args.directory,args.phase,args.index,args.start_gate)
        elif args.command=='run':
            result=run_phase(args.directory,args.phase)
        else:
            result=audit_phase(args.directory,args.phase)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result.get('status') in (None,'FROZEN','COMPLETE','PASS') else 2
    except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError) as error:
        print(f'error: {error}',file=sys.stderr)
        return 2


if __name__=='__main__':
    sys.exit(main())
