"""Independent raw-record audit of a completed M2 formal phase.

No product modules are imported, engines started, or original records changed.
The earlier independent pilot auditor supplies only its file/profile/PGN pure
checks; scheduling, cell invocation and native CPU accounting are checked here.
Run only after the requested phase has completed. Only a new output is written.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
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
    if expected is None or type(expected) is bool:
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


def opening_rows(path, count):
    document = read(path)
    check(document['schema_version'] == 1 and len(document['openings']) == count, 'Opening count/schema differs')
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
    check(len({row['family_id'] for row in rows}) == count, 'Duplicate opening family')
    return rows


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


def frozen_inputs(directory):
    frozen = read(directory / 'frozen-plan.json')
    check(read(directory / 'seal.json')['frozen_plan_sha256'] == sha(directory / 'frozen-plan.json'), 'Broken freeze seal')
    source = Path(frozen['source_plan_path']).resolve()
    check(sha(source) == sha(directory / 'source-plan.json') == frozen['source_plan_sha256'], 'Source plan changed')
    plan = read(source)
    check(plan == read(directory / 'source-plan.json') == frozen['plan'], 'Frozen plan conditions differ')
    check(plan['schema_version'] == 1 and plan['phase'] == 'formal-m2', 'Wrong formal plan')
    check(plan['movetimes_ms'] == [100, 1000] and plan['draw_policy'] == 'claim', 'Wrong times/draw policy')
    check(plan['retry_policy'] == 'no-result-based-retries-stop-on-first-failure', 'Adaptive/unknown retry policy')
    check(plan['move_deadline'] == {'multiplier': 2.0, 'slack_seconds': 0.25}, 'Deadline policy differs')
    check(type(plan['max_plies']) is int and plan['max_plies'] >= 360, 'Ply cap differs')
    check(plan['preflight_opening_count'] == 1, 'Preflight count differs')
    runtime = plan['worker_runtime']
    bound_file(runtime['executable'], source.parent)
    check(runtime['python_version'] == '3.13.16' and runtime['chess_version'] == '1.11.2', 'Worker runtime versions differ')
    package_files = [bound_file(entry, source.parent) for entry in runtime['package_files']]
    pythonpath = Path(runtime['pythonpath']).resolve()
    check(len(package_files) == 2 and set(package_files) ==
          {pythonpath/'chess'/'__init__.py', pythonpath/'chess'/'engine.py'}, 'Worker chess source bindings differ')
    expected_harness = {'m2_formal.py', 'm2_load_guard.py', 'compare_engines.py', 'external_baseline_matrix.py',
                        'engine_profile.py', 'see_clean_guard.py', 'm2_formal_summary.py'}
    check({Path(path).name for path in frozen['harness_source_sha256']} == expected_harness, 'Harness source set differs')
    for original, expected in frozen['harness_source_sha256'].items():
        check(sha(original) == sha(directory / 'harness' / Path(original).name) == expected, 'Live/frozen harness changed')
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
    mode_keys = {'UseNeuralEvaluation', 'UseNNUEEvaluation', 'EvalFile', 'NeuralModelFile'}
    common = [{key: value for key, value in profiles[name]['resolved_options'].items() if key not in mode_keys}
              for name in candidates]
    check(common[0] == common[1] == common[2], 'Candidate search configuration differs')
    check(len({profiles[name]['binary']['sha256'] for name in candidates}) == 1, 'Candidate binaries differ')
    banks = {}
    for phase, key, count in (('preflight', 'preflight_openings', 1), ('main', 'openings', 32)):
        original = opening_rows(bound_file(plan[key], source.parent), count)
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
          and worker[1] == '-s' and Path(worker[2]).resolve() == REPOSITORY / 'scripts' / 'm2_formal.py'
          and worker[3:] == ['worker', '--directory', str(directory), '--phase', phase, '--index', str(index),
                             '--start-gate', str(gate_path)], 'Worker command differs from scheduled cell')
    check(sha(worker[0]) == runtime['executable']['sha256'], 'Actual worker executable leaves frozen runtime binding')
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
    policy = summary['policy']
    check(all(policy[key] == value for key, value in plan['load_policy'].items()), 'Guard policy differs from freeze')
    check(.5 <= policy['interval_seconds'] <= 1 and policy['max_sample_gap_seconds'] == 2
          and policy['host_window_seconds'] == 2 and policy['max_nonworker_cpu_fraction'] == .20, 'Formal CPU policy differs')
    check(summary['completed_idle_seconds'] >= plan['idle_probe_seconds'] >= 2, 'Idle preflight incomplete')
    active, known, attached, detached, observed_owned = {}, {}, {}, {}, {}
    external_before, owned_before, previous, previous_end = None, {}, None, None
    end, samples, max_gap, max_duration, maximum_fraction, maximum_ready_fraction = 0., 0, 0., 0., 0., 0.
    intervals, external_identities = deque(), set()
    ready_windows, underflow_windows, idle_start, idle_end = 0, 0, None, None
    worker_events, phase_events = [], Counter()
    previous_utc = None
    with path.open(encoding='utf-8') as stream:
        for number, text in enumerate(stream, 1):
            check(text.strip(), 'Blank line in native load receipt')
            row = decoded(text)
            instant = utc(row['utc'])
            check(previous_utc is None or instant >= previous_utc, 'Load UTC clock regressed')
            previous_utc = instant
            event = row['event']
            if number == 1:
                check(event == 'policy' and row['policy'] == policy and row['monitor_pid'] == summary['monitor_pid']
                      and row['cpu_ticks_per_second'] == 10000000 and row['platform'] == 'win32', 'Missing native policy header')
                continue
            if event == 'attach':
                root = tuple(row['identity'])
                check(len(root) == 2 and all(type(v) is int and v > 0 for v in root)
                      and root[0] not in active and root not in attached, 'Duplicate/invalid worker attach')
                check(idle_end is not None and idle_end - idle_start >= plan['idle_probe_seconds'] - .01,
                      'Worker attached before complete sampled idle window')
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
            if external_before is not None:
                check(set(external) == set(external_before), 'Known external PID+creation set changed')
                check(all(external[key]['cpu_ticks'] == external_before[key]['cpu_ticks'] for key in external),
                      'Known external engine CPU counter changed (zero-tolerance policy)')
            external_before = external
            if previous is not None:
                total = system['kernel_ticks'] + system['user_ticks'] - previous['system']['kernel_ticks'] - previous['system']['user_ticks']
                idle = system['idle_ticks'] - previous['system']['idle_ticks']
                check(total >= 0 and 0 <= idle <= total and row['wall_ticks'] >= previous['wall_ticks']
                      and all(system[key] >= previous['system'][key] for key in ('kernel_ticks', 'user_ticks')), 'Native counters regressed')
                owned_delta = 0
                for key, item in owned.items():
                    if key in owned_before:
                        difference = item['cpu_ticks'] - owned_before[key]['cpu_ticks']
                        check(difference >= 0, 'Owned native CPU counter regressed')
                        owned_delta += difference
                    elif key[1] >= previous['wall_ticks']:
                        owned_delta += item['cpu_ticks']
                busy = total - idle
                nonworker = max(0, busy - owned_delta)
                if total > 0:
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
                fraction = window_nonworker / window_total if window_total > 0 else None
                ready = seconds >= policy['host_window_seconds'] - 1e-6 and window_total > 0
                expected = {'total_delta_ticks': total, 'busy_delta_ticks': busy, 'owned_delta_ticks': owned_delta,
                            'nonworker_delta_ticks': nonworker, 'accounting_underflow_ticks': max(0, owned_delta-busy),
                            'window_seconds': seconds, 'window_total_ticks': window_total,
                            'window_nonworker_ticks': window_nonworker, 'window_fraction': fraction, 'window_ready': ready}
                check(set(row['host']) == set(expected), 'Host-accounting fields differ')
                for key, value in expected.items():
                    equal_number(row['host'][key], value, 'Host independent recomputation differs: ' + key)
                if fraction is not None:
                    maximum_fraction = max(maximum_fraction, fraction)
                if ready:
                    check(fraction <= policy['max_nonworker_cpu_fraction'], 'Native rolling host nonworker CPU exceeds threshold')
                    maximum_ready_fraction = max(maximum_ready_fraction, fraction)
                    ready_windows += 1
                underflow_windows += int(owned_delta > busy)
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
    equal_number(summary['max_sample_gap_seconds'], max_gap, 'Maximum sample gap differs')
    equal_number(summary['max_snapshot_duration_seconds'], max_duration, 'Maximum snapshot duration differs')
    equal_number(summary['max_nonworker_cpu_fraction'], maximum_ready_fraction, 'Maximum ready host nonworker fraction differs')
    check(summary['external_cpu_increase_ticks'] == 0, 'Guard summary external CPU differs')
    recorded_workers = {(item['pid'], item['creation_ticks']): item for item in summary['workers']}
    check(len(recorded_workers) == len(summary['workers']) == len(attached) and set(recorded_workers) == set(attached),
          'Guard worker summary differs from attach events')
    for key, item in recorded_workers.items():
        check(item['attached_utc'] and item['detached_utc'] and utc(item['attached_utc']) <= utc(attached[key])
              and utc(item['detached_utc']) <= utc(detached[key]) and utc(attached[key]) < utc(detached[key]), 'Worker lifecycle timestamp differs')
    return {'samples': samples, 'ready_host_windows': ready_windows, 'known_external_identities': [list(key) for key in sorted(external_identities)],
            'observed_known_external_cpu_delta_ticks': 0, 'max_sample_gap_seconds': max_gap,
            'max_snapshot_duration_seconds': max_duration, 'max_host_nonworker_fraction_including_partial_windows': maximum_fraction,
            'max_host_nonworker_fraction_ready_windows': maximum_ready_fraction,
            'accounting_underflow_intervals': underflow_windows, 'policy': policy,
            'scope': 'Raw native cumulative counters and retained final process timers independently recomputed. Rolling nonworker CPU is an estimate with proportional partial-interval weighting; unknown/transient work, power, thermals and complete OS isolation are not certified.'}, attached, detached, observed_owned, worker_events


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
    return {'schema_version': 1, 'status': 'PASS', 'phase': phase, 'directory': str(directory),
            'audited_utc': datetime.now(timezone.utc).isoformat(), 'audit_script_sha256': sha(__file__),
            'independent_helper_sha256': sha(HELPER_PATH), 'scheduled_pairs': pairs, 'audited_pairs': pairs,
            'scheduled_games': games, 'completed_games': games, 'incomplete_games': 0, 'errors': 0,
            'replay': dict(helper.MOVES), 'cells': results, 'strata': strata, 'load': guard,
            'input_sha256': dict(sorted(helper.FILES.items())),
            'source_scope': 'File/model/artifact identities, declared local commit objects and native build receipt/source consistency checked. No independent source-to-binary rebuild; external version/revision/URL remain declarations. Direct worker executable and the two declared chess source files are hashed; these are not a complete frozen Python runtime.',
            'configuration_scope': 'Serialized actual command including direct executable/-s, file bindings, model SHA, accepted UCI option types/order, worker and launch-time engine affinity, PID+creation ancestry and raw counters checked. PYTHONPATH/PYTHONNOUSERSITE/PYTHONDONTWRITEBYTECODE assignments are in the hash-verified harness; no per-process environment readback, raw UCI wire transcript, general option readback or continuous affinity monitoring is claimed.',
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
