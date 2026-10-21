"""Independent 96-game audit using the frozen v6 raw/CPU/PGN checks.

Only the input selection, harness count and audited-preflight transfer extend
v6. The 33 original runtime files and v6 independent auditor remain unmodified.
This source copies only the frozen-input validator and phase coverage driver
with explicit 4-family, 34-source, 48-pair/96-game and exposure changes. All
process scope, CPU arithmetic, guard, invocation and per-game checks are reused.
No product runtime/validator is imported and no engine/process is started.
"""
import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
CORE = HERE/'peer_game_audit_engine_cpu_v6.py'
CORE_SHA256 = '64cca8cd6d5f6101341953fb43bcaf92b814b08baaa76b2bd05619785b873450'
assert hashlib.sha256(CORE.read_bytes()).hexdigest() == CORE_SHA256
spec = importlib.util.spec_from_file_location('frozen_independent_v6_core', CORE)
core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)
for name in ('helper', 'check', 'sha', 'read', 'bound_file', 'opening_rows', 'schedule',
             'git_receipts', 'coordinator_cache_scope', 'OWNERSHIP_METHOD', 'REPOSITORY',
             'HELPER_PATH', 'HELPER_SHA256', 'utc', 'guard_records', 'invocation',
             'native_assignment', 'engine_cpu_records', 'io', 'chess'):
    globals()[name] = getattr(core, name)
UNCHANGED_FIELDS = ('candidates','opponents','openings','preflight_openings','preflight_opening_count',
    'movetimes_ms','max_plies','draw_policy','lane_affinity_masks','topology','load_policy','idle_probe_seconds',
    'move_deadline','cell_timeout_seconds','preflight_timeout_seconds','main_timeout_seconds','retry_policy',
    'statistics','worker_runtime','accounting_policy')


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def baseline_selection_and_transfer(directory, frozen, profiles, banks, candidates, opponents):
    plan = frozen['plan']; source = Path(frozen['source_plan_path']).resolve()
    check(plan['baseline96_selection'] == {'main_opening_count':4,'main_games':96,'selection':'first4bybankorder'},
          '96-game subset declaration differs')
    selection = plan['main_selection_policy']; cells = schedule(candidates, opponents, plan['movetimes_ms'], banks['main'], 'main')
    check(selection['selected_indices_zero_based'] == [0,1,2,3]
          and selection['selected_indices_one_based'] == [1,2,3,4]
          and selection['selected_family_ids'] == [row['family_id'] for row in banks['main']]
          and selection['selected_cluster_ids'] == [row['cluster_id'] for row in banks['main']]
          and selection['selected_cell_ids'] == [row['id'] for row in cells]
          and selection['selected_cells_canonical_sha256'] == canonical(cells)
          and len(cells) == selection['expected_main_pairs'] == 48 and selection['expected_main_games'] == 96
          and selection['result_adaptive_selection'] is False, '96-game fixed selection differs')
    full_bank = opening_rows(bound_file(plan['openings'], source.parent), 32)
    check(full_bank[:4] == banks['main'] and selection['source_bank'] == plan['openings']
          and selection['copied_main_bank_family_count'] == 4, 'Copied bank is not the fixed source prefix')
    transfer = plan['preflight_transfer']; rule = plan['preflight_transfer_policy']
    source_directory = Path(transfer['directory']).resolve()
    source_frozen_path = bound_file(rule['source_frozen_plan'], source.parent)
    source_frozen = read(source_frozen_path)
    check(source_frozen_path == source_directory/'frozen-plan.json'
          and read(bound_file(rule['source_seal'], source.parent))['frozen_plan_sha256'] == sha(source_frozen_path),
          'Inherited preflight seal/directory differs')
    source_plan_path = bound_file(rule['source_plan'], source.parent)
    source_plan = read(source_plan_path)
    check(source_frozen['plan'] == source_plan and source_frozen['source_plan_sha256'] == sha(source_plan_path)
          and all(plan[key] == source_plan[key] for key in UNCHANGED_FIELDS), 'Preflight conditions differ')
    expected_sources = source_frozen['harness_source_sha256']
    check(len(expected_sources) == 33 and expected_sources == rule['source_frozen_runtime_sha256']
          and len(frozen['harness_source_sha256']) == 34
          and all(frozen['harness_source_sha256'].get(path) == digest == sha(path)
                  == sha(source_directory/'harness'/Path(path).name) for path, digest in expected_sources.items()),
          'Inherited 33 runtime sources differ')
    check(source_frozen['cells']['preflight'] == frozen['cells']['preflight']
          and canonical(source_frozen['cells']['preflight']) == rule['source_preflight_cells_canonical_sha256'],
          'Inherited preflight cells differ')
    source_pre_bank = bound_file(source_frozen['banks']['preflight'], source_directory)
    new_pre_bank = bound_file(frozen['banks']['preflight'], directory)
    check(sha(source_pre_bank) == sha(new_pre_bank) and read(source_pre_bank) == read(new_pre_bank),
          'Inherited preflight bank differs')
    for identifier, entry in source_frozen['profiles'].items():
        helper.same_identity(helper.profile(bound_file(entry, source_directory)), profiles[identifier])
    peer_path = bound_file(transfer['peer_audit'], source.parent)
    peer = read(peer_path)
    check(peer['status'] == 'PASS' and peer['phase'] == 'preflight'
          and Path(peer['directory']).resolve() == source_directory and peer['plan_id'] == source_plan['id']
          and peer['audit_script_sha256'] == sha(CORE) == CORE_SHA256
          and peer['scheduled_pairs'] == peer['audited_pairs'] == 12
          and peer['scheduled_games'] == peer['completed_games'] == 24
          and peer['incomplete_games'] == peer['errors'] == 0
          and peer['valid_for_declared_local_comparison'] is True, 'Inherited independent preflight did not pass')
    check(peer['input_sha256'].get(str(source_frozen_path)) == sha(source_frozen_path),
          'Independent preflight receipt does not bind the source freeze')
    for path, digest in peer['input_sha256'].items():
        check(sha(path) == digest, 'Inherited independent audit input changed: '+path)
    product = read(bound_file(transfer['product_audit'], source.parent))
    check(product['status'] == 'PASS' and product['phase'] == 'preflight' and product['phase_status'] == 'COMPLETE'
          and product['valid_for_comparison'] is True and product['audited_pairs'] == product['scheduled_pairs'] == 12,
          'Inherited product preflight did not pass')
    report = read(source_directory/'preflight/report.json')
    check(report['status'] == 'COMPLETE' and report['valid_for_comparison'] is True
          and report['scheduled_games'] == report['completed_games'] == 24
          and report['frozen_plan_sha256'] == sha(source_frozen_path), 'Inherited preflight report differs')


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
        'new_full_main_games': 96, 'new_preflight_games': 0, 'inherited_preflight_games_required': 24,
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
          and cpu_policy['cpu_wall_difference_is_os_waiting_time'] is False
          and cpu_policy['actual_endpoint_resolution']['ownership_scope']=='calling-worker-current-job-with-no-breakaway-and-creation-order',
          'CPU policy invents a failure floor or overstates attribution')
    check(plan['process_discovery_policy']['mode'] == OWNERSHIP_METHOD, 'Provider discovery scope differs')
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
                        'm2_engine_identity.py', 'm2_engine_tracking.py', 'm2_formal_v5.py', 'm2_formal_summary_v5.py',
                        'm2_engine_identity_v2.py', 'm2_engine_tracking_v2.py', 'm2_engine_receipts_v2.py',
                        'm2_formal_v6.py', 'm2_formal_summary_v6.py', 'm2_job_process_scope.py',
                        'm2_warning_load_guard_v2.py', 'm2_warning_receipts_v2.py', 'm2_baseline96.py'}
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
    for phase, key, count in (('preflight', 'preflight_openings', 1), ('main', 'openings', 4)):
        original = opening_rows(bound_file(plan[key], source.parent), count, selected_prefix=True)
        copied = opening_rows(bound_file(frozen['banks'][phase], directory), count)
        check(original == copied, 'Frozen opening bank changed')
        banks[phase] = copied
        check(frozen['cells'][phase] == schedule(candidates, opponents, plan['movetimes_ms'], copied, phase),
              'Schedule is not the declared full rotated factorial')
    check(len({row['family_id'] for rows in banks.values() for row in rows}) == 5, 'Preflight/main family overlap')
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
    check(len(balance) == 48 and set(balance.values()) == {1}, '96-game lane balance differs from one pair per stratum/lane')
    baseline_selection_and_transfer(directory, frozen, profiles, banks, candidates, opponents)
    return frozen, profiles, banks, candidates, opponents


def audit_selected_phase(directory, phase):
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
    pairs, games = (12, 24) if phase == 'preflight' else (48, 96)
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
        check(actual['engine_job_scope_audits']==cpu_item['job_scope_audits']
              and all(row['coordinator_pid']==guard['monitor_pid'] for row in cpu_item['job_scope_audits']),
              'Coordinator cell ownership scope differs from independent raw proof')
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
            'git_scope': 'Exact candidate commit, HEAD and porcelain-status queries succeeded at input freeze and their sealed receipts match every worker metadata copy. The hashed v5 coordinator source wraps its measured phase in the same exact-query cache; this is source/provenance verification, not an exhaustive OS process trace. These are freeze-time context, not fresh per-worker Git commands; file/model/harness identities are separately hash-checked.',
            'input_sha256': dict(sorted(helper.FILES.items())),
            'source_scope': 'File/model/artifact identities, declared local commit objects and native build receipt/source consistency checked. No independent source-to-binary rebuild; external version/revision/URL remain declarations. Direct worker executable and the two declared chess source files are hashed; these are not a complete frozen Python runtime.',
            'configuration_scope': 'Serialized actual command including direct executable/-s, file bindings, model SHA, accepted UCI option types/order, worker and launch-time engine affinity, PID+creation ancestry and raw counters checked. Worker self-reported executable/hash/versions/no_user_site and three environment variables match frozen bindings; no external OS environment readback, raw UCI wire transcript, general option readback or continuous affinity monitoring is claimed.',
            'score_scope': 'Legal mainlines, root/final FEN, rule endings and WDL are independently replayed. Raw PV legality and recorded mover score type/POV checked; search evaluations are not recomputed by another engine.',
            'valid_for_declared_local_comparison': True, 'strength_eligible': False,
            'elo_estimate': None, 'promotion_performed': False,
            'claim_scope': 'Completed preregistered local external baseline record audit. No model promotion, world ranking, causal evaluator improvement or complete OS/thermal isolation claim.'}

def audit(directory):
    check(sha(CORE) == CORE_SHA256, 'Independent v6 core changed')
    result = audit_selected_phase(Path(directory).resolve(), 'main')
    check(result['scheduled_games'] == result['completed_games'] == 96
          and result['scheduled_pairs'] == result['audited_pairs'] == 48, '96-game complete coverage differs')
    result.update(audit_script_sha256=sha(__file__), reused_v6_audit_sha256=sha(CORE),
        claim_scope='Completed 96-game fixed-prefix descriptive baseline only. Narrow exposed opening sample; '
                    'no 768-game completion, Elo, evaluator promotion or broad strength conclusion.',
        preflight_scope='Immutable prior 24-game v6 preflight inherited only after source33, all20 conditions, '
                        'profiles/bank/cells, product PASS and independent PASS/input hashes are rechecked.')
    result['input_sha256'] = dict(sorted(helper.FILES.items()))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--phase', choices=('main',), default='main')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    check(not args.output.exists() and args.output.name.startswith('peer-') and args.output.suffix == '.json',
          'Output must be a new peer-*.json')
    try:
        result = audit(args.directory); code = 0
    except Exception as error:
        result = dict(schema_version=1,status='FAIL',phase='main',audited_utc=datetime.now(timezone.utc).isoformat(),
            audit_script_sha256=sha(__file__),error=f'{type(error).__name__}: {error}',input_sha256=dict(helper.FILES)); code=1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False); stream.write('\n')
    print(json.dumps({key:result[key] for key in ('status','phase','error','scheduled_games','completed_games') if key in result}))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
