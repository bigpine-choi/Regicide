"""Independent, read-only integrity audit of stopped preflight attempt 005.

Reuses the immutable independent raw guard/profile/PGN checks, without any
product import, engine launch, process termination or application change.
Completed pairs and stopped active artifacts are separate; every score stays
excluded. Current PID unknowns remain separate from historical exit proofs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys

import chess
import chess.pgn

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
STOPPED_PATH = HERE/'peer_stopped_audit.py'
STOPPED_SHA256 = '028b3d3db08ef937f24b727e1a4473731ab531c4cb867fd60359dfa4b1ebf06d'
spec = importlib.util.spec_from_file_location('independent_stopped_main_checks', STOPPED_PATH)
stopped = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stopped)
core, helper = stopped.core, stopped.helper
CORE_PATH, CORE_SHA256 = stopped.CORE_PATH, stopped.CORE_SHA256
HELPER_PATH, HELPER_SHA256 = stopped.HELPER_PATH, stopped.HELPER_SHA256
check, sha, read, utc = stopped.check, stopped.sha, stopped.read, stopped.utc
invocation, guard_records = stopped.invocation, stopped.guard_records


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


def audit(directory, phase):
    directory = Path(directory).resolve()
    sha(__file__)
    # Check the completion marker before loading large records or doing audits.
    report = read(directory / phase / 'report.json')
    check(phase == 'preflight' and report['status'] == 'STOPPED' and report['valid_for_comparison'] is False,
          'Requested preflight must be stopped and invalid; running/valid results are rejected')
    check(sha(CORE_PATH) == CORE_SHA256, 'Complete-only independent auditor changed')
    check(sha(STOPPED_PATH) == STOPPED_SHA256, 'Stopped-main independent auditor changed')
    check(sha(HELPER_PATH) == HELPER_SHA256, 'Independent helper changed')
    frozen, profiles, banks, candidates, opponents = core.frozen_inputs(directory)
    plan, target = frozen['plan'], directory / phase
    git_values = core.git_receipts(frozen, profiles, candidates)
    check(all(utc(item['verified_utc']) <= utc(report['started_utc']) for item in frozen['git_receipts']),
          'Git verification happened after measured phase started')
    cells = frozen['cells'][phase]
    pairs, games = 12, 24
    check(len(cells) == pairs and len(report['cells']) == 6 and len(report['launched_cells']) == 8, 'Stopped preflight 005 pair boundary differs')
    check(report['phase'] == phase and report['frozen_plan_sha256'] == sha(directory / 'frozen-plan.json'), 'Report phase/freeze differs')
    check(report['scheduled_cells'] == pairs and report['scheduled_games'] == games and report['completed_games'] == 12
          and report['uncompleted_scheduled_games'] == 12 and report['unstarted_pairs'] == 4, 'Stopped phase counters differ')
    check(report['elo_estimate'] is None and report['promotion_performed'] is False and report['stop_reason'] == 'LoadGuardViolation: rolling nonworker host CPU fraction exceeded the declared threshold', 'Unexpected promotion/error claim')
    check(utc(report['finished_utc']) >= utc(report['started_utc']), 'Phase timestamps regressed')
    guard, attached, detached, observed_owned, worker_events, final_timers, observed_details = guard_records(target, plan, report)
    by_report = {item['id']: item for item in report['cells']}
    by_launch = {item['id']: item for item in report['launched_cells']}
    expected_cells = {cell['id']: cell for cell in cells}
    check(len(by_report) == 6 and len(by_launch) == 8 and set(by_report) < set(by_launch) <= set(expected_cells), 'Duplicate/changed stopped cell IDs')
    check(len(attached) == 8 and len(detached) == 6, 'Worker lifecycle count differs from launched/completed cells')
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
    check(set(coverage) < expected_coverage and len(coverage) == 12 and set(coverage.values()) == {1}, 'Completed subset/color coverage differs')
    cleanup = observe_cleanup(set(observed_owned) | set(attached), final_timers, set(attached)-set(detached), observed_details)
    check(all(sha(path) == digest for path, digest in list(helper.FILES.items())), 'Audit inputs changed while checking')
    return {'schema_version': 1, 'status': 'INTEGRITY_PASS_STOPPED_INVALID', 'integrity_replay_status': 'PASS',
            'current_pid_cleanup_observation': cleanup['current_pid_observation_status'],
            'phase': phase, 'directory': str(directory),
            'audited_utc': datetime.now(timezone.utc).isoformat(), 'audit_script_sha256': sha(__file__),
            'independent_helper_sha256': sha(HELPER_PATH), 'complete_auditor_sha256': sha(CORE_PATH), 'stopped_main_auditor_sha256': sha(STOPPED_PATH),
            'scheduled_pairs': pairs, 'audited_completed_pairs': 6, 'launched_pairs': 8,
            'scheduled_games': games, 'audited_report_completed_games': 12, 'uncompleted_scheduled_games': 12,
            'active_pairs_at_stop': 2, 'unstarted_pairs': 4, 'phase_complete': False,
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

