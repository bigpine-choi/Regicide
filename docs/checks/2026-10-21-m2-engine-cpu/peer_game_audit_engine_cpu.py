"""Independent raw-record audit of a completed per-engine-CPU v4 M2 phase.

No product modules are imported, engines started, or original records changed.
The earlier independent pilot auditor supplies only its file/profile/PGN pure
checks; scheduling, cell invocation and native CPU accounting are checked here.
Run only after the requested phase has completed. Only a new output is written.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, deque
from datetime import datetime, timezone
import importlib.util
import io
import json
import math
from pathlib import Path
import re
import sys

import chess
import chess.pgn

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[2]
HELPER_PATH = HERE.parent / '2026-10-21-m2-foundation' / 'peer_audit.py'
HELPER_SHA256 = 'bc56bd0157b4eb9006276f8ae14d2eebd721646d41774b249839c3f456289feb'
spec = importlib.util.spec_from_file_location('independent_m2_pilot_checks', HELPER_PATH)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
check, sha, read = helper.check, helper.sha, helper.read
resolve, bound_file = helper.resolve, helper.bound_file


def decoded(text):
    return json.loads(text, object_pairs_hook=helper.unique_object, parse_constant=helper.reject_constant)


def equal_number(actual, expected, context):
    if type(expected) is str:
        check(type(actual) is str and actual == expected, context)
    elif expected is None or type(expected) is bool:
        check(actual is expected, context)
    elif type(expected) is int:
        check(type(actual) is int and actual == expected, context)
    else:
        check(type(actual) in (float, int) and math.isfinite(actual)
              and math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-6), context)


def utc(text):
    value = datetime.fromisoformat(text)
    check(value.tzinfo is not None, 'Timestamp lacks timezone')
    return value


def opening_rows(path, count, *, selected_prefix=False):
    document = read(path)
    size = len(document['openings'])
    check(document['schema_version'] == 1 and (size >= count if selected_prefix else size == count), 'Opening count/schema differs')
    if selected_prefix:
        check(document.get('selected_indices_one_based', list(range(1, count+1))) == list(range(1, count+1)),
              'Source bank declares a different fixed prefix')
    rows = []
    for literal in document['openings']:
        row = dict(literal)
        check(row['family_id'] and row['cluster_id'] and row['name'], 'Missing opening identity')
        board = chess.Board()
        for text in row['moves']:
            move = chess.Move.from_uci(text)
            check(board.outcome(claim_draw=True) is None and move and move in board.legal_moves,
                  'Illegal or already terminal opening')
            board.push(move)
        check(board.outcome(claim_draw=True) is None, 'Terminal opening endpoint')
        fen = board.fen(en_passant='fen')
        check(row.get('fen_after_opening', fen) == fen, 'Opening FEN differs from legal moves')
        row['fen_after_opening'] = fen
        rows.append(row)
    check(len({row['family_id'] for row in rows}) == size, 'Duplicate opening family')
    return rows[:count] if selected_prefix else rows


def schedule(candidates, opponents, times, openings, phase):
    rows = []
    for time_index, milliseconds in enumerate(times):
        for family_index, opening in enumerate(openings):
            for opponent_index, opponent in enumerate(opponents):
                rotation = (family_index + opponent_index + time_index) % 3
                for candidate in candidates[rotation:] + candidates[:rotation]:
                    rows.append({'id': f'{phase}-{len(rows)+1:04d}',
                                 'lane': (family_index + 2*time_index + opponent_index) % 4,
                                 'candidate': candidate, 'opponent': opponent, 'movetime_ms': milliseconds,
                                 'opening_index': family_index, 'family_id': opening['family_id'],
                                 'cluster_id': opening['cluster_id']})
    return rows


def git_receipts(frozen, profiles, candidates):
    commits = sorted({profiles[key]['source']['commit'] for key in candidates})
    expected = [('rev-parse', '--verify', commit + '^{commit}') for commit in commits]
    expected += [('rev-parse', 'HEAD'), ('status', '--porcelain')]
    receipts = frozen['git_receipts']
    check(isinstance(receipts, list) and len(receipts) == len(expected), 'Git receipt count differs')
    observed = {}
    for item, query in zip(receipts, expected, strict=True):
        check(item['arguments'] == list(query) and query not in observed, 'Git receipt query/order is not exact')
        check(type(item['exit_code']) is int and item['exit_code'] == 0
              and isinstance(item['stdout'], str) and item['stdout'] == item['stdout'].strip(), 'Git receipt failed/unnormalized')
        utc(item['verified_utc'])  # May follow frozen_utc: commands are verified sequentially during freeze.
        check(item['scope'] == 'Actual root Git command at freeze; stdout uses existing harness strip normalization.',
              'Git receipt scope is not real root verification at freeze')
        if query[:2] == ('rev-parse', '--verify'):
            commit = query[2][:-len('^{commit}')]
            check(re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', commit)
                  and item['stdout'].lower() == commit.lower(), 'Source commit receipt is not its full resolved object ID')
        elif query == ('rev-parse', 'HEAD'):
            check(re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', item['stdout']), 'Frozen checkout HEAD is not a full object ID')
        observed[query] = item['stdout']
    return observed


def coordinator_cache_scope(directory):
    """Inspect the sealed wrapper as source; never import or run the harness."""
    path = directory/'harness'/'m2_formal_v4.py'
    sha(path)
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    wrappers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'run_phase']
    check(len(wrappers) == 1, 'Missing unique v4 coordinator wrapper')
    body = wrappers[0].body
    contexts = [(index, node) for index, node in enumerate(body) if isinstance(node, ast.With)]
    check(len(contexts) == 1, 'Coordinator measured phase lacks a single cache context')
    index, context = contexts[0]
    check(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'verify_frozen'
              for statement in body[:index] for node in ast.walk(statement)), 'No real pre-phase frozen verification')
    lookup = [node for statement in body[:index] for node in ast.walk(statement)
              if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'lookup' for target in node.targets)]
    check(len(lookup) == 1 and isinstance(lookup[0].value, ast.Call)
          and ast.unparse(lookup[0].value) == "git_receipt_cache(frozen['git_receipts'])", 'Coordinator cache leaves sealed Git envelope')
    check(len(context.items) == 1 and ast.unparse(context.items[0].context_expr) ==
          "patch.object(comparison, 'git_command', side_effect=lookup)", 'Coordinator patch scope differs')
    check(len(context.body) == 1 and isinstance(context.body[0], ast.Return)
          and ast.unparse(context.body[0].value) == '_run_phase(directory, phase)', 'Measured phase escapes coordinator cache')
    check(index == len(body)-1, 'Coordinator executes statements after measured cache context')


def frozen_inputs(directory):
    frozen = read(directory / 'frozen-plan.json')
    check(read(directory / 'seal.json')['frozen_plan_sha256'] == sha(directory / 'frozen-plan.json'), 'Broken freeze seal')
    source = Path(frozen['source_plan_path']).resolve()
    check(sha(source) == sha(directory / 'source-plan.json') == frozen['source_plan_sha256'], 'Source plan changed')
    plan = read(source)
    check(plan == read(directory / 'source-plan.json') == frozen['plan'], 'Frozen plan conditions differ')
    check(plan['schema_version'] == 3 and plan['phase'] == 'formal-m2-engine-cpu', 'Wrong formal plan')
    check(plan['movetimes_ms'] == [100, 1000] and plan['draw_policy'] == 'claim', 'Wrong times/draw policy')
    check(plan['retry_policy'] == 'no-result-based-retries-stop-on-first-failure', 'Adaptive/unknown retry policy')
    check(plan['move_deadline'] == {'multiplier': 2.0, 'slack_seconds': 0.25}, 'Deadline policy differs')
    check(type(plan['max_plies']) is int and plan['max_plies'] >= 360, 'Ply cap differs')
    check(plan['preflight_opening_count'] == 1, 'Preflight count differs')
    check(plan['accounting_policy']['mode'] == 'windows-job-plus-coordinator-cpu'
          and plan['accounting_policy']['approximate'] is True
          and plan['accounting_policy']['raw_signed_residual_required'] is True
          and plan['accounting_policy']['individual_search_contention_proven'] is False,
          'Job accounting scope differs')
    check(plan['exposure_policy'] == {
        'previously_exposed_fixed_baseline': True, 'fresh_holdout': False,
        'result_adaptive_reselection': False, 'prior_failed_attempts_preserved': True,
        'new_full_main_games': 768, 'new_preflight_games': 24,
        'previous_partial_games_count_toward_new_completion': False}, 'Exposure/reuse contract differs')
    previous_plan = read(bound_file(plan['source_plan'], source.parent))
    for key in ('candidates', 'opponents', 'openings', 'preflight_openings', 'preflight_opening_count',
                'movetimes_ms', 'max_plies', 'draw_policy', 'lane_affinity_masks', 'topology',
                'load_policy', 'idle_probe_seconds', 'move_deadline', 'cell_timeout_seconds',
                'preflight_timeout_seconds', 'main_timeout_seconds', 'retry_policy', 'statistics', 'worker_runtime'):
        check(plan[key] == previous_plan[key], 'Numerical/input conditions changed: '+key)
    parent = read(bound_file(plan['parent_plan'], source.parent))
    for key in ('candidates', 'opponents', 'openings', 'preflight_openings', 'preflight_opening_count',
                'movetimes_ms', 'max_plies', 'draw_policy', 'lane_affinity_masks', 'topology',
                'load_policy', 'idle_probe_seconds', 'move_deadline', 'cell_timeout_seconds',
                'preflight_timeout_seconds', 'main_timeout_seconds', 'retry_policy', 'statistics',
                'worker_runtime', 'accounting_policy'):
        check(plan[key] == parent[key], 'Input/numerical parent condition changed: '+key)
    check(plan['residual_load_policy']['enforcement'] == 'warning-only'
          and plan['residual_load_policy']['stops_phase'] is False
          and plan['residual_load_policy']['affects_comparison_validity'] is False,
          'Residual observations must be warning-only')
    cpu_policy = plan['engine_cpu_policy']
    check(cpu_policy['mode'] == 'held-getprocesstimes-per-search'
          and cpu_policy['every_actual_search_required'] is True
          and cpu_policy['minimum_cpu_fraction'] is None and cpu_policy['nps_floor'] is None
          and cpu_policy['cpu_wall_difference_is_os_waiting_time'] is False,
          'CPU policy invents a failure floor or overstates attribution')
    runtime = plan['worker_runtime']
    bound_file(runtime['executable'], source.parent)
    check(runtime['python_version'] == '3.13.16' and runtime['chess_version'] == '1.11.2', 'Worker runtime versions differ')
    package_files = [bound_file(entry, source.parent) for entry in runtime['package_files']]
    pythonpath = Path(runtime['pythonpath']).resolve()
    check(len(package_files) == 2 and set(package_files) ==
          {pythonpath/'chess'/'__init__.py', pythonpath/'chess'/'engine.py'}, 'Worker chess source bindings differ')
    expected_harness = {'m2_formal.py', 'm2_load_guard.py', 'compare_engines.py', 'external_baseline_matrix.py',
                        'engine_profile.py', 'see_clean_guard.py', 'm2_formal_summary.py',
                        'm2_formal_v2.py', 'm2_job_load_guard.py', 'm2_job_accounting.py',
                        'm2_job_operations.py', 'm2_job_receipts.py', 'm2_formal_summary_v2.py',
                        'm2_formal_v3.py', 'm2_formal_summary_v3.py', 'm2_formal_v4.py', 'm2_formal_summary_v4.py',
                        'm2_warning_load_guard.py', 'm2_warning_receipts.py', 'm2_engine_cpu.py', 'm2_engine_receipts.py',
                        'm2_engine_identity.py', 'm2_engine_tracking.py'}
    check({Path(path).name for path in frozen['harness_source_sha256']} == expected_harness, 'Harness source set differs')
    for original, expected in frozen['harness_source_sha256'].items():
        check(sha(original) == sha(directory / 'harness' / Path(original).name) == expected, 'Live/frozen harness changed')
    coordinator_cache_scope(directory)
    originals = [bound_file(entry, source.parent) for entry in plan['preregistration_artifacts']]
    copies = [bound_file(entry, directory) for entry in frozen['preregistration_artifacts']]
    check([sha(path) for path in originals] == [sha(path) for path in copies], 'Preregistration copy changed')
    profiles = {identifier: helper.profile(bound_file(entry, directory)) for identifier, entry in frozen['profiles'].items()}
    candidates, opponents = [], []
    for role, identifiers, kind in (('candidates', candidates, 'regicide'), ('opponents', opponents, 'external')):
        check(len(plan[role]) == (3 if role == 'candidates' else 2), 'Wrong profile count')
        for entry in plan[role]:
            original = helper.profile(bound_file(entry, source.parent))
            identifiers.append(original['id'])
            check(original['kind'] == kind, 'Profile kind differs')
            helper.same_identity(original, profiles[original['id']])
    check(len(set(candidates + opponents)) == 5 and set(profiles) == set(candidates + opponents), 'Profile IDs differ')
    check({profiles[key]['evaluation'] for key in candidates} == {'classical', 'residual', 'nnue'}, 'Missing evaluator')
    git_receipts(frozen, profiles, candidates)
    mode_keys = {'UseNeuralEvaluation', 'UseNNUEEvaluation', 'EvalFile', 'NeuralModelFile'}
    common = [{key: value for key, value in profiles[name]['resolved_options'].items() if key not in mode_keys}
              for name in candidates]
    check(common[0] == common[1] == common[2], 'Candidate search configuration differs')
    check(len({profiles[name]['binary']['sha256'] for name in candidates}) == 1, 'Candidate binaries differ')
    banks = {}
    for phase, key, count in (('preflight', 'preflight_openings', 1), ('main', 'openings', 32)):
        original = opening_rows(bound_file(plan[key], source.parent), count, selected_prefix=phase == 'preflight')
        copied = opening_rows(bound_file(frozen['banks'][phase], directory), count)
        check(original == copied, 'Frozen opening bank changed')
        banks[phase] = copied
        check(frozen['cells'][phase] == schedule(candidates, opponents, plan['movetimes_ms'], copied, phase),
              'Schedule is not the declared full rotated factorial')
    check(len({row['family_id'] for rows in banks.values() for row in rows}) == 33, 'Preflight/main family overlap')
    masks, cores = plan['lane_affinity_masks'], plan['topology']['physical_core_masks']
    check(len(masks) == 4 and len(cores) == 8, 'Expected four lanes/eight physical cores')
    for collection in (masks, cores):
        combined = 0
        for mask in collection:
            check(type(mask) is int and mask > 0 and not combined & mask, 'Affinity masks overlap/invalid')
            combined |= mask
    check(sum(masks) == sum(cores), 'Lanes do not cover declared cores')
    for mask in masks:
        check(sum(bool(mask & core) for core in cores) == 2 and all(mask & core in (0, core) for core in cores),
              'Lane splits a core or lacks two complete physical cores')
    balance = Counter((row['candidate'], row['opponent'], row['movetime_ms'], row['lane']) for row in frozen['cells']['main'])
    check(len(balance) == 48 and set(balance.values()) == {8}, 'Main lane balance differs from eight pairs per stratum/lane')
    return frozen, profiles, banks, candidates, opponents


def invocation(cell_dir, cell, candidate, opponent, plan, frozen, launched, phase, index, directory):
    item = read(cell_dir / 'invocation.json')
    check(item['cell'] == cell and item['worker_pid'] == launched['pid'], 'Worker invocation is not launched cell')
    mask = plan['lane_affinity_masks'][cell['lane']]
    check(item['affinity_mask'] == mask, 'Worker affinity differs')
    gate_path = directory / phase / 'start-gates' / (cell['id'] + '.json')
    gate = read(gate_path)
    check(gate == {'worker_pid': launched['pid'], 'frozen_plan_sha256': sha(directory / 'frozen-plan.json')}, 'Startup gate differs')
    worker = launched['command']
    runtime = plan['worker_runtime']
    check(len(worker) == 12 and Path(worker[0]).resolve() == Path(runtime['executable']['path']).resolve()
          and worker[1] == '-s' and Path(worker[2]).resolve() == REPOSITORY / 'scripts' / 'm2_formal_v4.py'
          and worker[3:] == ['worker', '--directory', str(directory), '--phase', phase, '--index', str(index),
                             '--start-gate', str(gate_path)], 'Worker command differs from scheduled cell')
    check(sha(worker[0]) == runtime['executable']['sha256'], 'Actual worker executable leaves frozen runtime binding')
    check(Path(item['runtime_executable']).resolve() == Path(runtime['executable']['path']).resolve()
          and item['runtime_sha256'] == runtime['executable']['sha256']
          and item['python_version'] == runtime['python_version'] and item['chess_version'] == runtime['chess_version']
          and type(item['no_user_site']) is int and item['no_user_site'] == 1,
          'Worker runtime self-report differs from frozen executable/version/flags')
    check(item['environment'] == {'PYTHONPATH': runtime['pythonpath'], 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
          'Worker environment self-report differs from frozen bindings')
    command = item['comparison_arguments']
    check(command[:2] == [opponent['command'][0], candidate['command'][0]] and len(command) == 20, 'Comparison command differs')
    arguments = dict(zip(command[2::2], command[3::2], strict=True))
    check(set(arguments) == {'--baseline-source', '--candidate-commit', '--baseline-options', '--candidate-options',
                            '--openings', '--movetime', '--max-plies', '--draw-policy', '--pgn'}, 'Comparison flags differ')
    check(arguments['--candidate-commit'] == candidate['source']['commit'], 'Command source differs')
    check(arguments['--movetime'] == str(cell['movetime_ms']) and arguments['--max-plies'] == str(plan['max_plies'])
          and arguments['--draw-policy'] == 'claim', 'Comparison limits differ')
    for flag, name in (('--openings', 'openings.json'), ('--pgn', 'games.pgn'), ('--baseline-source', 'external-source.json')):
        check(Path(arguments[flag]).resolve() == cell_dir / name, 'Comparison file binding differs')
    for role, profile in (('baseline', opponent), ('candidate', candidate)):
        options = decoded(arguments['--' + role + '-options'])
        check(options == profile['resolved_options'], 'Command options leave frozen binding')
        for active, binding in (('UseNeuralEvaluation', 'EvalFile'), ('UseNNUEEvaluation', 'NeuralModelFile')):
            if options.get(active) is True:
                check(list(options).index(binding) < list(options).index(active), 'Model activation precedes file binding')
    external = read(cell_dir / 'external-source.json')
    for key in ('name', 'version', 'source_url', 'revision'):
        check(external[key] == opponent['source'][key], 'External identity declaration differs')
    check(external['binary_sha256'] == opponent['binary']['sha256'] and external['args'] == opponent['command'][1:],
          'External executable/argument declaration differs')
    return item


def identity(item):
    key = item['pid'], item['creation_ticks']
    check(all(type(value) is int and value > 0 for value in key), 'Invalid process identity')
    check(type(item['cpu_ticks']) is int and item['cpu_ticks'] >= 0, 'Invalid process CPU counter')
    return key


def ancestry(item, processes, root):
    current, chain, seen = item, [item['pid']], {item['pid']}
    while current['pid'] != root[0]:
        parent = processes.get(current['parent_pid'])
        if parent is None or parent['pid'] in seen or parent['creation_ticks'] > current['creation_ticks']:
            return None
        chain.append(parent['pid'])
        seen.add(parent['pid'])
        current = parent
    return chain if (current['pid'], current['creation_ticks']) == root else None


def midpoint(frame, kind):
    return (frame[kind+'_before']['cpu_ticks']+frame[kind+'_after']['cpu_ticks'])/2


def native_cpu(row, *, job=False):
    check(all(type(row[key]) is int and row[key] >= 0 for key in ('user_ticks', 'kernel_ticks', 'cpu_ticks'))
          and row['cpu_ticks'] == row['user_ticks']+row['kernel_ticks'], 'Raw native CPU normalization differs')
    check(type(row['read_started_monotonic']) in (float, int) and type(row['read_finished_monotonic']) in (float, int)
          and math.isfinite(row['read_started_monotonic']) and math.isfinite(row['read_finished_monotonic'])
          and row['read_started_monotonic'] <= row['read_finished_monotonic'], 'Raw native read bracket invalid')
    if job:
        check(row['coordinator_in_job'] is False, 'Coordinator was included in worker Job')
        for key in ('total_processes', 'active_processes', 'terminated_processes_by_limit',
                    'pre_assignment_cpu_lower_ticks_total', 'pre_assignment_cpu_upper_ticks_total'):
            check(type(row[key]) is int and row[key] >= 0, 'Invalid Job raw field '+key)
        check(row['active_processes'] <= row['total_processes']
              and row['terminated_processes'] == row['terminated_processes_by_limit']
              and row['pre_assignment_cpu_lower_ticks_total'] <= row['pre_assignment_cpu_upper_ticks_total'],
              'Invalid Job process counts/boundary diagnostics')


def native_frame(frame, monitor_pid):
    check(set(frame) == {'job_before', 'monitor_before', 'system', 'monitor_after', 'job_after'}, 'Native frame keys differ')
    order = ('job_before', 'monitor_before', 'system', 'monitor_after', 'job_after')
    for key in ('job_before', 'job_after', 'monitor_before', 'monitor_after'):
        native_cpu(frame[key], job=key.startswith('job'))
    for a, b in zip(order, order[1:]):
        check(frame[a]['read_finished_monotonic'] <= frame[b]['read_started_monotonic'], 'Native API read sequence overlaps/regresses')
    check((frame['monitor_before']['pid'], frame['monitor_before']['creation_ticks']) ==
          (frame['monitor_after']['pid'], frame['monitor_after']['creation_ticks'])
          and frame['monitor_before']['pid'] == monitor_pid, 'Wrong native coordinator identity')
    check(frame['monitor_before']['creation_ticks'] > 0, 'Coordinator has no creation identity')
    for kind in ('job', 'monitor'):
        for field in ('user_ticks', 'kernel_ticks', 'cpu_ticks'):
            check(frame[kind+'_before'][field] <= frame[kind+'_after'][field], 'Within-frame CPU regressed')
    system = frame['system']
    check(all(type(system[key]) in (float, int) and math.isfinite(system[key])
              for key in ('read_started_monotonic', 'read_finished_monotonic'))
          and system['read_started_monotonic'] <= system['read_finished_monotonic'], 'Invalid system read bracket')
    check(system['total_ticks'] == system['kernel_ticks']+system['user_ticks']
          and system['busy_ticks'] == system['total_ticks']-system['idle_ticks'], 'Native system normalization differs')


def native_assignment(row):
    for key in ('process_before', 'process_after'):
        native_cpu(row[key])
        check(row[key]['pid'] == row['pid'] and row[key]['creation_ticks'] == row['creation_ticks'], 'Assignment exact process identity differs')
    for key in ('job_before', 'job_after'):
        native_cpu(row[key], job=True)
    check(row['process_before']['cpu_ticks'] == row['pre_assignment_cpu_lower_ticks']
          and row['process_after']['cpu_ticks'] == row['pre_assignment_cpu_upper_ticks']
          and row['pre_assignment_cpu_lower_ticks'] <= row['pre_assignment_cpu_upper_ticks'], 'Assignment boundary diagnostics differ')
    check(row['job_after']['total_processes'] >= row['job_before']['total_processes']+1, 'Assignment did not add its root to Job')


def guard_records(target, plan, report):
    """Recalculate identities, deltas and overlapping host windows from raw fields."""
    path = target / 'load.jsonl'
    check(sha(path) == report['guard_log_sha256'], 'Raw guard file hash differs')
    summary = read(target / 'load-summary.json')
    started = read(target / 'run-started.json')
    check(started['pid'] == summary['monitor_pid'] and utc(started['started_utc']) <= utc(report['started_utc']),
          'Guard monitor identity differs from phase invocation')
    check(sha(target / 'load-summary.json') == report['guard_summary_sha256'], 'Guard summary hash differs')
    check(summary['failure'] is None and summary['valid_for_load_check'] is True and summary['preflight_complete'] is True,
          'Guard did not finish cleanly')
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
    retired, final_timers, retirement_counts = {}, {}, Counter()
    external_before, owned_before, previous, previous_end = None, {}, None, None
    end, samples, max_gap, max_duration, maximum_fraction, maximum_ready_fraction = 0., 0, 0., 0., 0., 0.
    intervals, external_identities = deque(), set()
    ready_windows, underflow_windows, idle_start, idle_end = 0, 0, None, None
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
            check(event == 'sample' and row['failure'] is None, 'Guard error/unknown event')
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
                key = identity(item)
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
    check(not active and set(attached) == set(detached), 'Workers still attached at guard close')
    check(phase_events['closed'] == 1 and previous['phase'] == 'closed' and phase_events['phase-completed'] == 1,
          'Missing final completion/closed native samples')
    check(ready_windows > 0 and samples == summary['snapshot_count'], 'Sample count/window differs')
    timer = summary['timer']
    check(timer_started and timer['started'] is True and timer['alive'] is False and timer['failure'] is None
          and timer['samples'] == phase_events['timer'] and timer['samples'] > 0
          and timer['join_bound_seconds'] == policy['max_sample_gap_seconds'], 'Sampler final lifecycle/summary differs')
    equal_number(summary['max_sample_gap_seconds'], max_gap, 'Maximum sample gap differs')
    equal_number(summary['max_snapshot_duration_seconds'], max_duration, 'Maximum snapshot duration differs')
    equal_number(summary['max_nonworker_cpu_fraction'], maximum_ready_fraction, 'Maximum ready host nonworker fraction differs')
    equal_number(summary['residual_load_ready_window_count'], ready_windows, 'Ready warning window count differs')
    equal_number(summary['residual_load_warning_window_count'], warning_windows, 'Warning window count differs')
    equal_number(summary['residual_load_warning_episode_count'], warning_episodes, 'Warning episode count differs')
    equal_number(summary['residual_load_warning_peak_fraction'], warning_peak, 'Warning peak differs')
    check(summary['external_cpu_increase_ticks'] == 0, 'Guard summary external CPU differs')
    equal_number(summary['experiment_job_delta_ticks'], job_delta_sum, 'Summary Job delta differs')
    equal_number(summary['coordinator_delta_ticks'], coordinator_delta_sum, 'Summary coordinator delta differs')
    equal_number(summary['accounting_underflow_ticks'], underflow_sum, 'Summary signed underflow differs')
    equal_number(job_delta_sum, midpoint(last_frame, 'job')-midpoint(first_frame, 'job'), 'Job deltas do not telescope')
    for field in ('user_ticks', 'kernel_ticks', 'cpu_ticks', 'total_processes', 'active_processes'):
        check(last_frame['job_after'][field] == final_job[field], 'Final Job receipt differs from closed raw frame: '+field)
    check(final_job['total_processes'] >= len(observed_owned), 'Job process count is smaller than observed owned identities')
    recorded_workers = {(item['pid'], item['creation_ticks']): item for item in summary['workers']}
    check(len(recorded_workers) == len(summary['workers']) == len(attached) and set(recorded_workers) == set(attached),
          'Guard worker summary differs from attach events')
    for key, item in recorded_workers.items():
        check(item['attached_utc'] and item['detached_utc'] and utc(item['attached_utc']) <= utc(attached[key])
              and utc(item['detached_utc']) <= utc(detached[key]) and utc(attached[key]) < utc(detached[key]), 'Worker lifecycle timestamp differs')
    return {'monitor_pid': summary['monitor_pid'], 'samples': samples, 'ready_host_windows': ready_windows, 'known_external_identities': [list(key) for key in sorted(external_identities)],
            'observed_known_external_cpu_delta_ticks': 0, 'max_sample_gap_seconds': max_gap,
            'max_snapshot_duration_seconds': max_duration, 'max_host_nonworker_fraction_including_partial_windows': maximum_fraction,
            'max_host_nonworker_fraction_ready_windows': maximum_ready_fraction,
            'residual_cpu_policy': 'warning-only', 'residual_load_warning_window_count': warning_windows,
            'residual_load_warning_episode_count': warning_episodes, 'residual_load_warning_peak_fraction': warning_peak,
            'accounting_underflow_intervals': underflow_windows, 'accounting_underflow_ticks': underflow_sum,
            'experiment_job_delta_ticks': job_delta_sum, 'coordinator_delta_ticks': coordinator_delta_sum,
            'final_job': final_job, 'accounting_method': summary['accounting_method'], 'policy': policy,
            'timer': timer, 'timer_stopped_event_recorded': timer_stopped,
            'retirement_events': dict(retirement_counts), 'retired_handles_at_last_sample_before_provider_close': len(retired),
            'scope': 'Job cumulative CPU includes exited descendants; coordinator is independently subtracted. Raw signed residual and two-second interpolation are recomputed without product validators. Process/ancestry and retirement remain sampled provider receipts, not full OS inventory; asynchronous quantized estimates do not certify per-app causes, individual search contention, power, thermals or OS isolation.'}, attached, detached, observed_owned, worker_events


def process_cpu_receipt(receipt, expected_identity, previous=None):
    """Recompute one actual process receipt without importing a product validator.

    No utilization floor or upper bound is imposed. A zero native CPU delta is
    legitimate; raw CPU query brackets are wider than callable wall duration.
    """
    check(receipt['schema_version'] == 1 and receipt['status'] == 'OK'
          and receipt['outcome'] == 'complete', 'Incomplete or unknown engine CPU receipt')
    check(receipt['scope'] == 'actual UCI transport process, all threads; descendants excluded'
          and receipt['ratio_is_diagnostic_only'] is True,
          'CPU process/ratio scope differs')
    before, after = receipt['before'], receipt['after']
    for row in (before, after):
        native_cpu(row)
        check((row['pid'], row['creation_ticks']) == expected_identity,
              'Engine CPU receipt leaves fixed process PID+creation identity')
    for key in ('user_ticks', 'kernel_ticks', 'cpu_ticks'):
        check(after[key] >= before[key], 'Engine CPU counter regressed within search')
    if previous is not None:
        check(receipt['sequence'] == previous['sequence']+1, 'Engine CPU per-process search sequence has a gap')
        for key in ('user_ticks', 'kernel_ticks', 'cpu_ticks'):
            check(before[key] >= previous['after'][key], 'Engine CPU counter regressed across searches')
        check(before['read_started_monotonic'] >= previous['after']['read_finished_monotonic'],
              'Engine CPU reads overlap or regress across searches')
    else:
        check(type(receipt['sequence']) is int and receipt['sequence'] == 1,
              'Engine CPU process trace does not start with search one')
    begin, end = receipt['call_started_monotonic'], receipt['call_finished_monotonic']
    check(all(type(value) in (int, float) and math.isfinite(value) for value in (begin, end))
          and before['read_finished_monotonic'] <= begin <= end <= after['read_started_monotonic'],
          'Engine callable interval leaves its CPU query bracket')
    elapsed = end-begin
    equal_number(receipt['response_elapsed_seconds'], elapsed, 'Engine response wall time differs')
    for name in ('user', 'kernel', 'cpu'):
        ticks = after[name+'_ticks']-before[name+'_ticks']
        equal_number(receipt[name+'_delta_ticks'], ticks, 'Engine CPU tick delta differs: '+name)
        equal_number(receipt[name+'_seconds'], ticks/10_000_000, 'Engine CPU seconds differ: '+name)
    check(receipt['cpu_delta_ticks'] == receipt['user_delta_ticks']+receipt['kernel_delta_ticks'],
          'Engine CPU delta components do not add')
    ratio = receipt['cpu_seconds']/elapsed if elapsed > 0 else None
    equal_number(receipt['cpu_per_wall_ratio'], ratio, 'Engine CPU/wall ratio differs')
    check(not any(key in receipt for key in ('error_type', 'error_message', 'call_error_type')),
          'Completed engine search retains an error')
    return {'cpu_seconds': receipt['cpu_seconds'], 'response_elapsed_seconds': elapsed,
            'zero_cpu': receipt['cpu_delta_ticks'] == 0, 'identity': list(expected_identity)}


def identity_image(row, expected_path, expected_sha256, context):
    check(type(row['pid']) is int and row['pid'] > 0
          and type(row['creation_ticks']) is int and row['creation_ticks'] > 0
          and type(row['parent_pid']) is int and row['parent_pid'] > 0,
          context+' has invalid native identity')
    check(Path(row['image_path']).resolve() == Path(expected_path).resolve()
          and row['image_sha256'] == expected_sha256 == sha(expected_path),
          context+' image differs from frozen executable')
    return row['pid'], row['creation_ticks']


def engine_cpu_records(metadata, cell, candidate, opponent, plan, root, observed_owned, cell_dir):
    """Join all raw CPU samples to actual transports and every legal search."""
    processes = metadata['engine_cpu_processes']
    trace = metadata['engine_cpu_trace']
    trace_path=cell_dir/'engine-cpu.jsonl';declared=metadata['engine_cpu_trace_file']
    check(Path(declared['path']).resolve()==trace_path.resolve() and sha(trace_path)==declared['sha256'],
          'Raw CPU trace path/hash differs from match metadata')
    trace_rows=[decoded(line) for line in trace_path.read_text(encoding='utf-8').splitlines()]
    check(trace_rows and trace_rows[0]=={'event':'policy','record':{
        'schema_version':1,'cell_id':cell['id'],
        'scope':'Actual verified UCI endpoint, all threads; launcher/bootstrap CPU remains in the full Job.',
        'cpu_ratio_is_diagnostic_only':True}}, 'Raw CPU trace initial policy differs')
    check(all(row['event'] in ('policy','endpoint','search','process_final') for row in trace_rows)
          and sum(row['event']=='policy' for row in trace_rows)==1, 'Raw CPU trace has failure/unknown/repeated policy')
    check([row['record'] for row in trace_rows if row['event']=='search']==trace,
          'Raw CPU search stream differs from every-search metadata trace')
    finals=[row['record'] for row in trace_rows if row['event']=='process_final']
    setups=[row['record'] for row in trace_rows if row['event']=='endpoint']
    check(finals==processes and len(setups)==len(finals)==2,
          'Raw CPU process final/setup coverage differs')
    check([row['event'] for row in trace_rows]==['policy','endpoint','endpoint']+['search']*len(trace)+['process_final','process_final'],
          'Raw CPU events leave startup/search/final lifecycle order')
    for initial,final in zip(setups,finals,strict=True):
        check(initial['final'] is None and initial['meter_closed'] is False,
              'Raw CPU setup already claims a finalized meter')
        check(all(initial[key]==final[key] for key in ('role','transport_index','transport_pid',
              'transport_creation_ticks','endpoint_resolution','baseline')),
              'Raw CPU initial endpoint differs from final process identity')
    check(type(processes) is list and len(processes) == 2 and type(trace) is list,
          'Missing engine CPU process/trace collection')
    by_role, previous = {}, {}
    for index, (entry, role, profile) in enumerate(zip(processes, ('baseline','candidate'), (opponent,candidate), strict=True)):
        check(entry['role'] == role and entry['transport_index'] == index and entry['meter_closed'] is True,
              'CPU process order/lifecycle differs from transports')
        transport = metadata['affinity']['transports'][index]
        launched = entry['transport_pid'], entry['transport_creation_ticks']
        check(launched == (transport['pid'],transport['creation_ticks']), 'CPU transport leaves affinity PID+creation')
        resolution = entry['endpoint_resolution']
        observed_transport = identity_image(resolution['transport'], profile['command'][0], profile['binary']['sha256'], 'Transport')
        check(observed_transport == launched, 'Endpoint resolution belongs to a different transport')
        endpoint = resolution['endpoint']
        mode = resolution['mode']
        if mode == 'native-direct':
            actual = identity_image(endpoint, profile['command'][0], profile['binary']['sha256'], 'Direct endpoint')
            check(actual == launched, 'Direct native engine endpoint is not its transport')
        else:
            check(mode == 'sunfish-venv-child' and profile['id'] == 'sunfish-e396948-cpython313',
                  'Unknown or unregistered child endpoint resolution')
            runtime = plan['worker_runtime']['executable']
            actual = identity_image(endpoint, runtime['path'], runtime['sha256'], 'Sunfish actual interpreter endpoint')
            check(endpoint['parent_pid'] == launched[0] and actual != launched
                  and endpoint['creation_ticks'] >= launched[1], 'Sunfish endpoint is not a later direct transport child')
        check(resolution['schema_version'] == 1
              and resolution['method'] == 'frozen-image-and-held-creation-with-toolhelp-parent'
              and resolution['profile_id'] == profile['id'], 'Endpoint proof schema/method/profile differs')
        check(resolution['transport']['parent_pid'] == root[0], 'Transport endpoint proof does not name its worker parent')
        bound_binary=resolution['frozen_binary']
        check(Path(bound_binary['path']).resolve()==Path(profile['command'][0]).resolve()
              and bound_binary['sha256']==profile['binary']['sha256'], 'Endpoint frozen binary binding differs')
        if profile['id']=='sunfish-e396948-cpython313':
            script=resolution['frozen_script'];runtime=resolution['expected_base_runtime']
            check(Path(script['path']).resolve()==Path(profile['command'][2]).resolve()
                  and script['sha256']==sha(profile['command'][2]), 'Sunfish frozen script binding differs')
            check(Path(runtime['path']).resolve()==Path(plan['worker_runtime']['executable']['path']).resolve()
                  and runtime['sha256']==plan['worker_runtime']['executable']['sha256'], 'Sunfish expected base runtime differs')
        else:
            check(resolution['frozen_script'] is None and resolution['expected_base_runtime'] is None,
                  'Native profile invents a script/base-runtime binding')
        check(resolution['observed_direct_children']==([] if actual==launched else [actual[0]]),
              'Endpoint is not native-direct or its unique observed computational child')
        check(resolution['expected_console_helper'] is None and resolution['console_helpers']==[]
              and resolution['console_helpers_after']==[] and resolution['query_access_mask']==0x101000,
              'Unregistered console helper or unexpected process access scope')
        inventories=resolution['matching_inventories']
        check(type(inventories) is list and len(inventories)==2 and inventories[0]==inventories[1],
              'Endpoint two parent inventories differ')
        inventory={int(key):value for key,value in inventories[0].items()}
        check(len(inventory)==len(inventories[0]) and set(inventory)=={launched[0],actual[0]},
              'Endpoint inventory contains a missing or unexpected descendant')
        check(inventory[launched[0]]=={'pid':launched[0],'parent_pid':root[0]}, 'Transport sampled parent proof differs')
        if actual!=launched:
            check(inventory[actual[0]]=={'pid':actual[0],'parent_pid':launched[0]}, 'Actual child sampled parent proof differs')
        for label, row, expected_identity, expected_image, expected_hash in (
                ('transport_after',resolution['transport_after'],launched,profile['command'][0],profile['binary']['sha256']),
                ('endpoint_after',resolution['endpoint_after'],actual,endpoint['image_path'],endpoint['image_sha256'])):
            check((row['pid'],row['creation_ticks'])==expected_identity
                  and Path(row['image_path']).resolve()==Path(expected_image).resolve()
                  and row['image_sha256']==expected_hash, 'Held endpoint recheck identity/image differs: '+label)
        for row in (resolution['transport'],endpoint,resolution['transport_after'],resolution['endpoint_after']):
            check(row['wait_status']==258 and row['liveness_method']=='nonsignaled-held-process-object',
                  'Endpoint proof lacks held nonsignaled-process liveness')
            check(all(type(row[key]) in (int,float) and math.isfinite(row[key]) for key in
                      ('read_started_monotonic','read_finished_monotonic'))
                  and row['read_finished_monotonic']>=row['read_started_monotonic'], 'Endpoint proof read bracket regressed')
        check(resolution['transport_after']['read_started_monotonic']>=resolution['transport']['read_finished_monotonic']
              and resolution['endpoint_after']['read_started_monotonic']>=endpoint['read_finished_monotonic'],
              'Held endpoint recheck clocks regressed')
        check(observed_owned.get(actual) == root, 'Actual CPU endpoint is not observed under registered worker')
        baseline, final = entry['baseline'], entry['final']
        proof_start,proof_end=resolution['read_started_monotonic'],resolution['read_finished_monotonic']
        check(all(type(value) in (int,float) and math.isfinite(value) for value in (proof_start,proof_end))
              and proof_start<=proof_end<=baseline['read_started_monotonic'],
              'Held endpoint proof clock does not precede its CPU baseline')
        for row in (resolution['transport'],endpoint,resolution['transport_after'],resolution['endpoint_after']):
            check(proof_start<=row['read_started_monotonic']<=row['read_finished_monotonic']<=proof_end,
                  'Held endpoint query leaves its recorded resolution envelope')
        for row in (baseline,final):
            native_cpu(row)
            check((row['pid'],row['creation_ticks']) == actual, 'CPU process baseline/final identity differs')
        for key in ('user_ticks','kernel_ticks','cpu_ticks'):
            check(final[key] >= baseline[key], 'CPU final process counter regressed below baseline')
        check(final['read_started_monotonic'] >= baseline['read_finished_monotonic'],
              'CPU process final clock precedes baseline')
        by_role[role] = dict(identity=actual,entry=entry,index=index,profile=profile)
    flattened = [(game_index,move) for game_index,game in enumerate(metadata['games']) for move in game['search_moves']]
    check(len(trace) == len(flattened) and bool(trace), 'CPU raw trace does not cover every actual search')
    stats = {role: {'engine': item['profile']['id'], 'searches':0, 'cpu_seconds':0.,
                   'response_elapsed_seconds':0., 'envelope_elapsed_seconds':0., 'zero_cpu_searches':0,
                   'response_over_requested_searches':0, 'envelope_over_requested_searches':0,
                   'maximum_response_seconds':0., 'maximum_envelope_seconds':0.,
                   'maximum_cpu_per_wall_ratio':None}
             for role,item in by_role.items()}
    previous_envelope_end = None
    for ordinal, (receipt, (game_index, move)) in enumerate(zip(trace, flattened, strict=True), 1):
        role = move['role']; item = by_role[role]; identity = item['identity']; entry = item['entry']
        check(move['engine_cpu'] == receipt, 'Search CPU copy differs from raw execution trace')
        check(receipt['ordinal'] == ordinal and receipt['game_index'] == game_index
              and receipt['ply'] == move['ply'] and receipt['role'] == role
              and receipt['transport_index'] == item['index']
              and (receipt['transport_pid'],receipt['transport_creation_ticks']) ==
              (entry['transport_pid'],entry['transport_creation_ticks'])
              and (receipt['pid'],receipt['creation_ticks']) == identity
              and receipt['fen_before'] == move['fen_before'] and receipt['move'] == move['move'],
              'CPU raw trace leaves executed search/transport/game/ply')
        raw_stats = process_cpu_receipt(receipt, identity, previous.get(identity))
        old = previous.get(identity)
        if old is None:
            for key in ('user_ticks','kernel_ticks','cpu_ticks'):
                check(receipt['before'][key] >= entry['baseline'][key], 'First CPU search precedes meter baseline')
            check(receipt['before']['read_started_monotonic'] >= entry['baseline']['read_finished_monotonic'],
                  'First CPU search clock precedes baseline')
        previous[identity] = receipt
        requested = cell['movetime_ms']/1000
        deadline = requested*plan['move_deadline']['multiplier']+plan['move_deadline']['slack_seconds']
        equal_number(receipt['requested_seconds'], requested, 'CPU trace requested time differs')
        equal_number(receipt['response_deadline_seconds'], deadline, 'CPU trace deadline differs')
        begin, end = receipt['envelope_started_monotonic'],receipt['envelope_finished_monotonic']
        check(all(type(value) in (int,float) and math.isfinite(value) for value in (begin,end))
              and begin <= receipt['before']['read_started_monotonic']
              and end >= receipt['after']['read_finished_monotonic'],
              'CPU envelope does not include native reads and callable')
        check(previous_envelope_end is None or begin >= previous_envelope_end,
              'Sequential engine calls overlap or regress across the ordered trace')
        previous_envelope_end=end
        elapsed=end-begin
        equal_number(receipt['envelope_elapsed_seconds'],elapsed,'CPU envelope elapsed differs')
        check(receipt['deadline_exceeded'] is False and elapsed <= deadline
              and raw_stats['response_elapsed_seconds'] <= deadline,
              'Engine exceeded unchanged response deadline')
        check(move['elapsed_seconds']+1e-6 >= elapsed, 'Search elapsed time is smaller than measured transport envelope')
        row=stats[role];row['searches']+=1;row['cpu_seconds']+=raw_stats['cpu_seconds']
        row['response_elapsed_seconds']+=raw_stats['response_elapsed_seconds'];row['envelope_elapsed_seconds']+=elapsed
        row['zero_cpu_searches']+=int(raw_stats['zero_cpu'])
        row['response_over_requested_searches']+=int(raw_stats['response_elapsed_seconds']>requested)
        row['envelope_over_requested_searches']+=int(elapsed>requested)
        row['maximum_response_seconds']=max(row['maximum_response_seconds'],raw_stats['response_elapsed_seconds'])
        row['maximum_envelope_seconds']=max(row['maximum_envelope_seconds'],elapsed)
        ratio=receipt['cpu_per_wall_ratio']
        if ratio is not None:
            row['maximum_cpu_per_wall_ratio']=ratio if row['maximum_cpu_per_wall_ratio'] is None else max(row['maximum_cpu_per_wall_ratio'],ratio)
    for role,item in by_role.items():
        last=previous.get(item['identity']);check(last is not None,'Engine has no measured search')
        final=item['entry']['final']
        check(final['read_started_monotonic']>=last['after']['read_finished_monotonic'], 'CPU process final clock precedes final search')
        for key in ('user_ticks','kernel_ticks','cpu_ticks'):
            check(final[key]>=last['after'][key], 'Final CPU process counter precedes final search')
    def distribution(values):
        ordered=sorted(values)
        def percentile(p):
            if not ordered:return None
            position=(len(ordered)-1)*p;low=math.floor(position);high=math.ceil(position)
            return ordered[low]+(ordered[high]-ordered[low])*(position-low)
        return {'p50':percentile(.5),'p95':percentile(.95),'max':max(ordered) if ordered else None,'total':math.fsum(ordered)}
    raw_roles={}
    for role,item in by_role.items():
        rows=[row for row in trace if row['role']==role]
        raw_cpu=sum(row['after']['cpu_ticks']-row['before']['cpu_ticks'] for row in rows)
        raw_kernel=sum(row['after']['kernel_ticks']-row['before']['kernel_ticks'] for row in rows)
        raw_user=sum(row['after']['user_ticks']-row['before']['user_ticks'] for row in rows)
        response=[row['call_finished_monotonic']-row['call_started_monotonic'] for row in rows]
        outer=[move['elapsed_seconds'] for _game_index,move in flattened if move['role']==role]
        envelope=[row['envelope_finished_monotonic']-row['envelope_started_monotonic'] for row in rows]
        ratios=[(row['after']['cpu_ticks']-row['before']['cpu_ticks'])/10_000_000/elapsed
                for row,elapsed in zip(rows,response,strict=True) if elapsed>0]
        ratio_distribution=distribution(ratios);ratio_distribution.pop('total');ratio_distribution['count_none']=len(rows)-len(ratios)
        lifetime=item['entry']['final']['cpu_ticks']-item['entry']['baseline']['cpu_ticks']
        check(raw_cpu<=lifetime,'Sum of endpoint search CPU exceeds independently bracketed process lifetime')
        raw_roles[role]={'searched_moves':len(rows),'cpu_ticks':raw_cpu,'cpu_seconds':raw_cpu/10_000_000,
            'kernel_ticks':raw_kernel,'user_ticks':raw_user,'response_seconds':distribution(response),
            'envelope_seconds':distribution(envelope),'comparison_outer_seconds':distribution(outer),
            'outer_minus_envelope_seconds':distribution([value-internal for value,internal in zip(outer,envelope,strict=True)]),
            'requested_exceeded_count':sum(value>requested for value in response),
            'envelope_requested_exceeded_count':sum(value>requested for value in envelope),
            'zero_cpu_delta_count':sum(row['after']['cpu_ticks']==row['before']['cpu_ticks'] for row in rows),
            'cpu_per_wall_ratio':ratio_distribution,'lifetime_cpu_seconds':lifetime/10_000_000}
    raw_summary={'schema_version':1,'valid':True,'searched_moves':len(trace),
        'total_cpu_ticks':sum(row['cpu_ticks'] for row in raw_roles.values()),
        'total_cpu_seconds':sum(row['cpu_ticks'] for row in raw_roles.values())/10_000_000,'roles':raw_roles,
        'percentile_method':'sorted linear interpolation at (n-1)*p; Hyndman-Fan type 7',
        'scope':'actual UCI transport process, all threads; descendants excluded',
        'cpu_wall_ratio_is_diagnostic_only':True,'comparison_outer_elapsed_is_diagnostic_only':True,
        'requested_seconds':requested,'response_deadline_seconds':deadline}
    check(metadata['engine_cpu_summary']==raw_summary,'Stored CPU summary differs from independent raw sums/percentiles')
    return {'searches':len(trace),'roles':stats,'raw_recomputed_summary':raw_summary,'processes':[
        {'role':role,'identity':list(item['identity']),'transport_identity':[item['entry']['transport_pid'],item['entry']['transport_creation_ticks']],
         'endpoint_mode':item['entry']['endpoint_resolution']['mode']}
        for role,item in by_role.items()],
        'scope':'Independent raw GetProcessTimes/callable/envelope and every-search join. CPU delta query interval is wider than the callable; quantization allows zero CPU and ratios above one. No CPU/NPS floor or OS-wait inference. Sunfish verified direct or actual interpreter child endpoint is measured; any separate launcher CPU remains in Job aggregate.'}


def audit(directory, phase):
    directory = Path(directory).resolve()
    sha(__file__)
    # Check the completion marker before loading large records or doing audits.
    report = read(directory / phase / 'report.json')
    check(report['status'] == 'COMPLETE' and report['valid_for_comparison'] is True,
          'Requested phase is not complete/valid; do not audit running phases')
    check(sha(HELPER_PATH) == HELPER_SHA256, 'Independent helper changed')
    frozen, profiles, banks, candidates, opponents = frozen_inputs(directory)
    plan, target = frozen['plan'], directory / phase
    git_values = git_receipts(frozen, profiles, candidates)
    check(all(utc(item['verified_utc']) <= utc(report['started_utc']) for item in frozen['git_receipts']),
          'Git verification happened after measured phase started')
    cells = frozen['cells'][phase]
    pairs, games = (12, 24) if phase == 'preflight' else (384, 768)
    check(len(cells) == pairs and len(report['cells']) == pairs and len(report['launched_cells']) == pairs, 'Missing/extra phase pairs')
    check(report['phase'] == phase and report['frozen_plan_sha256'] == sha(directory / 'frozen-plan.json'), 'Report phase/freeze differs')
    check(report['scheduled_cells'] == pairs and report['scheduled_games'] == report['completed_games'] == games
          and report['uncompleted_scheduled_games'] == report['unstarted_pairs'] == 0, 'Phase completion counters differ')
    check(report['elo_estimate'] is None and report['promotion_performed'] is False and not report.get('stop_reason'), 'Unexpected promotion/error claim')
    check(utc(report['finished_utc']) >= utc(report['started_utc']), 'Phase timestamps regressed')
    guard, attached, detached, observed_owned, worker_events = guard_records(target, plan, report)
    by_report = {item['id']: item for item in report['cells']}
    by_launch = {item['id']: item for item in report['launched_cells']}
    check(len(by_report) == len(by_launch) == pairs and set(by_report) == set(by_launch) == {cell['id'] for cell in cells}, 'Duplicate/changed cell IDs')
    check(len(attached) == pairs, 'Worker attach count differs from scheduled cells')
    attached_by_pid = {}
    for key in attached:
        attached_by_pid.setdefault(key[0], []).append(key)
    coverage, outcomes, results, roots_used = Counter(), Counter(), [], set()
    cpu_cells = []
    # These event identities also prove that a lane never ran overlapping workers.
    lane_by_root = {}
    for index, cell in enumerate(cells):
        actual, launch = by_report[cell['id']], by_launch[cell['id']]
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
    check(roots_used == set(attached), 'Unmatched registered worker')
    active_lanes = {}
    for event, root, _time in worker_events:
        lane = lane_by_root[root]
        if event == 'attach':
            check(lane not in active_lanes, 'Two workers overlapped on the same lane')
            active_lanes[lane] = root
        else:
            check(active_lanes.pop(lane, None) == root, 'Lane detach does not match running worker')
    check(not active_lanes, 'Lane still active after completion')
    expected_coverage = {(candidate, opponent, milliseconds, opening['family_id'], color)
                         for candidate in candidates for opponent in opponents for milliseconds in plan['movetimes_ms']
                         for opening in banks[phase] for color in (1, 2)}
    check(set(coverage) == expected_coverage and len(coverage) == games and set(coverage.values()) == {1}, 'Full factorial/color coverage differs')
    strata = []
    for candidate in candidates:
        for opponent in opponents:
            for milliseconds in plan['movetimes_ms']:
                key = (candidate, opponent, milliseconds)
                strata.append({'candidate': candidate, 'opponent': opponent, 'movetime_ms': milliseconds,
                               'wins': outcomes[key+(1.,)], 'draws': outcomes[key+(.5,)], 'losses': outcomes[key+(0.,)]})
    check(all(sha(path) == digest for path, digest in list(helper.FILES.items())), 'Audit inputs changed while checking')
    return {'schema_version': 1, 'status': 'PASS', 'phase': phase, 'directory': str(directory), 'plan_id': plan['id'],
            'audited_utc': datetime.now(timezone.utc).isoformat(), 'audit_script_sha256': sha(__file__),
            'independent_helper_sha256': sha(HELPER_PATH), 'scheduled_pairs': pairs, 'audited_pairs': pairs,
            'scheduled_games': games, 'completed_games': games, 'incomplete_games': 0, 'errors': 0,
            'replay': dict(helper.MOVES), 'cells': results, 'strata': strata, 'load': guard,
            'engine_cpu': {'cells':cpu_cells,'searches':sum(row['searches'] for row in cpu_cells),
                           'residual_policy':'warning-only','cpu_or_nps_failure_floor':None},
            'git_scope': 'Exact candidate commit, HEAD and porcelain-status queries succeeded at input freeze and their sealed receipts match every worker metadata copy. The hashed v4 coordinator source wraps its measured phase in the same exact-query cache; this is source/provenance verification, not an exhaustive OS process trace. These are freeze-time context, not fresh per-worker Git commands; file/model/harness identities are separately hash-checked.',
            'input_sha256': dict(sorted(helper.FILES.items())),
            'source_scope': 'File/model/artifact identities, declared local commit objects and native build receipt/source consistency checked. No independent source-to-binary rebuild; external version/revision/URL remain declarations. Direct worker executable and the two declared chess source files are hashed; these are not a complete frozen Python runtime.',
            'configuration_scope': 'Serialized actual command including direct executable/-s, file bindings, model SHA, accepted UCI option types/order, worker and launch-time engine affinity, PID+creation ancestry and raw counters checked. Worker self-reported executable/hash/versions/no_user_site and three environment variables match frozen bindings; no external OS environment readback, raw UCI wire transcript, general option readback or continuous affinity monitoring is claimed.',
            'score_scope': 'Legal mainlines, root/final FEN, rule endings and WDL are independently replayed. Raw PV legality and recorded mover score type/POV checked; search evaluations are not recomputed by another engine.',
            'valid_for_declared_local_comparison': True, 'strength_eligible': False,
            'elo_estimate': None, 'promotion_performed': False,
            'claim_scope': 'Completed preregistered local external baseline record audit. No model promotion, world ranking, causal evaluator improvement or complete OS/thermal isolation claim.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--phase', choices=('preflight', 'main'), required=True)
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
    print(json.dumps({key: result[key] for key in ('status', 'phase', 'completed_games', 'error', 'replay') if key in result}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
