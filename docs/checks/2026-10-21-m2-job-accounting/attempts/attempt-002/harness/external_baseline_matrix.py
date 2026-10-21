"""Freeze evaluator/opponent inputs and run a bounded, paired external pilot.

Reuses compare_engines for UCI, move selection and adjudication. A pilot cannot
promote an evaluator or establish Elo. Existing games/processes are never stopped.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time

import chess
import chess.pgn

import compare_engines as comparison
from engine_profile import load_profile, verify_profile, snapshot_profile, compare_arguments

ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    # All published status files are replaced atomically; a stale PASS is never
    # reused for a new run. Source files and match evidence are never overwritten.
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def integer(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{label} must be an integer >= {minimum}')
    return value


def referenced_file(entry, base):
    if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
        raise ValueError('file reference requires path and sha256')
    path = (base / entry['path']).resolve()
    if not path.is_file() or sha256(path) != entry.get('sha256'):
        raise ValueError(f'input file hash mismatch: {path}')
    return path


def load_plan(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    plan = json.loads(raw.decode('utf-8-sig'))
    if type(plan.get('schema_version')) is not int or plan['schema_version'] != 1:
        raise ValueError('unsupported matrix plan schema')
    if plan.get('phase') != 'pilot':
        raise ValueError('this runner currently supports descriptive pilots only')
    if not isinstance(plan.get('id'), str) or not ID.fullmatch(plan['id']):
        raise ValueError('plan id is invalid')
    times = plan.get('movetimes_ms')
    if not isinstance(times, list) or not times or len(set(times)) != len(times):
        raise ValueError('movetimes_ms must be a nonempty unique integer list')
    for value in times:
        integer(value, 'movetime_ms')
    for key in ('max_plies', 'cell_timeout_seconds', 'total_timeout_seconds'):
        integer(plan.get(key), key)
    if plan.get('draw_policy') not in ('claim', 'automatic'):
        raise ValueError('draw_policy must be claim or automatic')
    if plan.get('schedule') != 'rotate-candidates-per-block':
        raise ValueError('declare the supported deterministic schedule')
    profiles = {}
    for role, kind in (('candidates', 'regicide'), ('opponents', 'external')):
        entries = plan.get(role)
        if not isinstance(entries, list) or not entries:
            raise ValueError(f'{role} must be nonempty')
        profiles[role] = []
        for entry in entries:
            manifest = referenced_file(entry, path.parent)
            profile = load_profile(manifest)
            if profile['kind'] != kind:
                raise ValueError(f'{role} has wrong engine kind')
            profiles[role].append(profile)
    ids = [p['id'] for group in profiles.values() for p in group]
    if len(set(ids)) != len(ids):
        raise ValueError('profile ids must be globally unique')
    openings_path = referenced_file(plan['openings'], path.parent)
    _, openings = comparison.load_openings(openings_path)
    if any(len(row['moves']) >= plan['max_plies'] for row in openings):
        raise ValueError('max_plies must leave a searched ply after each opening')
    # Family bootstrap below is only justified by one declared position per
    # family. Do not count correlated variants as independent opening pairs.
    families = [row.get('family_id') for row in openings]
    positions = [row['fen_after_opening'].rsplit(' ', 2)[0] for row in openings]
    if any(not isinstance(f, str) or not f for f in families) or len(set(families)) != len(families):
        raise ValueError('pilot requires one opening per unique family_id')
    if len(set(positions)) != len(positions):
        raise ValueError('duplicate final opening positions')
    return plan, profiles, openings_path, len(openings), hashlib.sha256(raw).hexdigest()


def schedule(candidates, opponents, movetimes):
    cells = []
    for block, (opponent, movetime) in enumerate((o, t) for o in opponents for t in movetimes):
        shift = block % len(candidates)
        order = candidates[shift:] + candidates[:shift]
        for candidate in order:
            cells.append({'id': f'{len(cells)+1:03d}-{candidate}-vs-{opponent}-{movetime}ms',
                          'candidate': candidate, 'opponent': opponent, 'movetime_ms': movetime})
    return cells


def freeze_plan(path, destination):
    plan, profiles, openings_path, count, plan_hash = load_plan(path)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    try:
        frozen_profiles = {}
        for role in ('candidates', 'opponents'):
            for profile in profiles[role]:
                target = destination / 'profiles' / profile['id']
                target.parent.mkdir(exist_ok=True)
                saved = snapshot_profile(profile, target)
                frozen_profiles[profile['id']] = {'path': str(target / 'profile.json'),
                                                  'sha256': sha256(target / 'profile.json')}
                verify_profile(saved)
        (destination / 'openings.json').write_bytes(openings_path.read_bytes())
        if sha256(destination / 'openings.json') != plan['openings']['sha256']:
            raise ValueError('opening input changed during freeze')
        harness = destination / 'harness'
        harness.mkdir()
        harness_hashes = {}
        for source in (Path(__file__), Path(comparison.__file__), ROOT / 'scripts/engine_profile.py', ROOT / 'scripts/see_clean_guard.py'):
            target = harness / source.name
            data = source.read_bytes()
            target.write_bytes(data)
            harness_hashes[str(source.resolve())] = hashlib.sha256(data).hexdigest()
        frozen = {'schema_version': 1, 'frozen_utc': utc_now(), 'plan': plan,
                  'source_plan_sha256': plan_hash, 'source_plan_path': str(Path(path).resolve()),
                  'profiles': frozen_profiles, 'harness_source_sha256': harness_hashes,
                  'openings_sha256': sha256(destination / 'openings.json'),
                  'opening_families': count, 'scheduled_games_per_cell': 2 * count,
                  'cells': schedule([p['id'] for p in profiles['candidates']],
                                    [p['id'] for p in profiles['opponents']], plan['movetimes_ms']),
                  'scope': 'Shared-host descriptive pilot only; no evaluator promotion or Elo claim.'}
        write_json(destination / 'frozen-plan.json', frozen)
        write_json(destination / 'seal.json', {'frozen_plan_sha256': sha256(destination / 'frozen-plan.json')})
        verify_frozen(destination)
        return frozen
    except BaseException as error:
        write_json(destination / 'freeze-failure.json', {'status': 'FAIL', 'error': str(error)})
        raise


def verify_frozen(directory):
    directory = Path(directory).resolve()
    seal = json.loads((directory / 'seal.json').read_text(encoding='utf-8'))
    if sha256(directory / 'frozen-plan.json') != seal['frozen_plan_sha256']:
        raise ValueError('frozen plan changed')
    frozen = json.loads((directory / 'frozen-plan.json').read_text(encoding='utf-8'))
    if sha256(directory / 'openings.json') != frozen['openings_sha256']:
        raise ValueError('frozen openings changed')
    for path, digest in frozen['harness_source_sha256'].items():
        if sha256(path) != digest or sha256(directory / 'harness' / Path(path).name) != digest:
            raise ValueError('matrix harness changed after freeze')
    profiles = {}
    for identifier, entry in frozen['profiles'].items():
        manifest = referenced_file(entry, directory)
        profile = load_profile(manifest)
        if profile['id'] != identifier:
            raise ValueError('frozen profile id changed')
        profiles[identifier] = profile
    return frozen, profiles


def replay_cell(metadata, pgn_path):
    """Independently replay every recorded ply and recheck PGN/results/endings."""
    openings = metadata['selected_openings']
    scheduled = len(openings) * 2
    games = metadata['games']
    if not openings or not games or len(games) > scheduled or metadata['games_requested'] != scheduled:
        raise ValueError('invalid scheduled/recorded game count')
    if metadata['completed'] and (len(games) != scheduled or not all(g['completed'] for g in games)):
        raise ValueError('incomplete records marked completed')
    stream = io.StringIO(Path(pgn_path).read_text(encoding='utf-8'))
    for index, record in enumerate(games):
        opening = openings[index // 2]
        if (record['game'] != index + 1 or record['pair'] != index // 2 + 1
                or record['white'] != ('candidate' if index % 2 == 0 else 'baseline')
                or record['black'] != ('baseline' if index % 2 == 0 else 'candidate')
                or record['opening'] != opening['name'] or record['opening_uci_moves'] != opening['moves']
                or record['uci_moves'][:len(opening['moves'])] != opening['moves']):
            raise ValueError('game schedule or opening differs')
        game = chess.pgn.read_game(stream)
        if game is None or game.errors or game.headers['Result'] != record['result']:
            raise ValueError('PGN game/result mismatch')
        if [move.uci() for move in game.mainline_moves()] != record['uci_moves']:
            raise ValueError('PGN move sequence mismatch')
        board = chess.Board(record['initial_fen'])
        for move in record['uci_moves']:
            board.push_uci(move)
        if board.fen(en_passant='fen') != record['final_fen'] or len(board.move_stack) != record['plies']:
            raise ValueError('recorded final board mismatch')
        outcome = board.outcome(claim_draw=metadata['draw_policy'] == 'claim')
        if record['completed']:
            if outcome is None or outcome.result() != record['result'] or outcome.termination.name.lower() != record['termination']:
                raise ValueError('completed game has no matching chess-rule outcome')
            expected = 0.5 if outcome.winner is None else float((outcome.winner == chess.WHITE) == (record['white'] == 'candidate'))
            if record['candidate_points'] != expected:
                raise ValueError('candidate points mismatch')
        elif record['result'] != '*' or record['candidate_points'] is not None:
            raise ValueError('unfinished game was scored')
    if chess.pgn.read_game(stream) is not None:
        raise ValueError('extra PGN games')
    rebuilt = comparison.summarize(metadata['games'], metadata['selected_openings'])
    if rebuilt != metadata['summary']:
        raise ValueError('stored match summary differs from independent aggregation')
    return rebuilt


def verify_cell_inputs(metadata, directory, frozen, profiles, cell):
    _, expected_openings = comparison.load_openings(directory / 'openings.json')
    if (metadata['limits'] != {'nodes': None, 'depth': None, 'movetime_ms': cell['movetime_ms']}
            or metadata['max_plies'] != frozen['plan']['max_plies']
            or metadata['draw_policy'] != frozen['plan']['draw_policy']
            or metadata['opening_file_sha256'] != frozen['openings_sha256']
            or metadata['selected_openings'] != expected_openings):
        raise ValueError('cell conditions differ from the frozen plan')
    for role, identifier in (('candidate', cell['candidate']), ('baseline', cell['opponent'])):
        profile = profiles[identifier]
        expected = compare_arguments(profile)
        actual = metadata['engines'][role]
        if (actual['command'] != [expected['binary'], *expected['args']]
                or actual['sha256'] != profile['binary']['sha256']
                or actual['provided_source_commit'] != expected['commit']
                or actual['requested_options'] != expected['options']
                or actual['validated_options'] != expected['options']):
            raise ValueError('engine identity/options differ from frozen profile')


def terminate_owned_tree(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=15)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != 'nt':
            os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        process.wait(timeout=5)


class LoadObservation:
    """Sample known engine CPU outside our child; observation is not isolation."""
    def __init__(self):
        self.provider = None
        self.error = None
        if sys.platform == 'win32':
            from see_clean_guard import WindowsProcesses
            self.provider = WindowsProcesses()

    def sample(self, worker_pid):
        row = {'utc': utc_now(), 'worker_pid': worker_pid, 'known_external_engines': []}
        if self.provider is None:
            row['unsupported'] = True
            return row
        try:
            processes = self.provider.snapshot()
            for process in processes.values():
                if not self.provider.is_engine(process):
                    continue
                ancestor, seen = process.pid, set()
                owned = False
                while ancestor in processes and ancestor not in seen:
                    if ancestor == worker_pid:
                        owned = True
                        break
                    seen.add(ancestor)
                    ancestor = processes[ancestor].parent_pid
                if not owned:
                    row['known_external_engines'].append({'pid': process.pid, 'name': process.name,
                        'creation_ticks': process.creation_ticks, 'cpu_ticks': process.cpu_ticks, 'error': process.error})
        except Exception as error:
            row['error'] = str(error)
        return row

    def close(self):
        if self.provider is not None:
            self.provider.close()


def run_cell(directory, frozen, profiles, cell, timeout_seconds):
    target = directory / 'cells' / cell['id']
    target.mkdir(parents=True, exist_ok=False)
    candidate = compare_arguments(profiles[cell['candidate']])
    opponent = compare_arguments(profiles[cell['opponent']])
    source_path = target / 'external-source.json'
    write_json(source_path, opponent['external_source'])
    pgn = target / 'games.pgn'
    command = [sys.executable, str(ROOT / 'scripts/compare_engines.py'),
               opponent['binary'], candidate['binary'], '--baseline-source', str(source_path),
               '--candidate-commit', candidate['commit'], '--baseline-options', json.dumps(opponent['options']),
               '--candidate-options', json.dumps(candidate['options']), '--openings', str(directory / 'openings.json'),
               '--movetime', str(cell['movetime_ms']), '--max-plies', str(frozen['plan']['max_plies']),
               '--draw-policy', frozen['plan']['draw_policy'], '--pgn', str(pgn)]
    write_json(target / 'invocation.json', {'command': command, 'timeout_seconds': timeout_seconds, 'started_utc': utc_now()})
    observer = LoadObservation()
    process = None
    started = time.monotonic()
    timed_out = False
    try:
        with (target / 'worker.log').open('w', encoding='utf-8') as log, (target / 'load.jsonl').open('w', encoding='utf-8') as load:
            load.write(json.dumps(observer.sample(None)) + '\n')
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            while process.poll() is None:
                load.write(json.dumps(observer.sample(process.pid)) + '\n')
                load.flush()
                if time.monotonic() - started > timeout_seconds:
                    timed_out = True
                    terminate_owned_tree(process)
                    break
                time.sleep(1)
            load.write(json.dumps(observer.sample(None)) + '\n')
        result = dict(cell, status='TIMEOUT' if timed_out else 'ERROR', exit_code=process.returncode,
                      elapsed_seconds=round(time.monotonic() - started, 4), summary=None)
        if pgn.with_suffix('.json').is_file():
            metadata = json.loads(pgn.with_suffix('.json').read_text(encoding='utf-8'))
            verify_cell_inputs(metadata, directory, frozen, profiles, cell)
            result['summary'] = replay_cell(metadata, pgn)
            result['games_json_sha256'] = sha256(pgn.with_suffix('.json'))
            result['pgn_sha256'] = sha256(pgn)
            result['status'] = 'COMPLETE' if process.returncode == 0 and metadata['completed'] and not timed_out else 'INCOMPLETE'
        write_json(target / 'result.json', result)
        return result
    finally:
        if process is not None:
            terminate_owned_tree(process)
        observer.close()


def run_matrix(directory):
    directory = Path(directory).resolve()
    frozen, profiles = verify_frozen(directory)
    # Exclusive marker prevents concurrent runs or silently replacing a failure.
    with (directory / 'run-started.json').open('x', encoding='utf-8') as stream:
        json.dump({'started_utc': utc_now(), 'pid': os.getpid()}, stream)
    report = {'status': 'RUNNING', 'started_utc': utc_now(), 'cells': [],
              'scheduled_cells': len(frozen['cells']), 'scheduled_games': len(frozen['cells']) * frozen['scheduled_games_per_cell'],
              'phase': 'pilot', 'strength_eligible': False, 'elo_estimate': None,
              'resource_scope': 'Shared host. Known external engine CPU sampled every ~1s; other load/thermal/power effects are not isolated.',
              'python_version': platform.python_version(), 'python_chess_version': chess.__version__,
              'platform': platform.platform(), 'logical_cpus': os.cpu_count(), 'frozen_plan_sha256': sha256(directory / 'frozen-plan.json')}
    started = time.monotonic()
    write_json(directory / 'report.json', report)
    try:
        for cell in frozen['cells']:
            verify_frozen(directory)
            remaining = frozen['plan']['total_timeout_seconds'] - (time.monotonic() - started)
            if remaining <= 0:
                report['stop_reason'] = 'total_wall_budget_exhausted'
                break
            report['cells'].append(dict(cell, status='RUNNING', summary=None))
            write_json(directory / 'report.json', report)
            result = run_cell(directory, frozen, profiles, cell, min(remaining, frozen['plan']['cell_timeout_seconds']))
            report['cells'][-1] = result
            verify_frozen(directory)
            write_json(directory / 'report.json', report)
            print(f"{len(report['cells'])}/{len(frozen['cells'])} {cell['id']}: {result['status']}", flush=True)
            if result['status'] != 'COMPLETE':
                report['stop_reason'] = 'cell_not_complete'
                break
        report['status'] = 'COMPLETE' if len(report['cells']) == len(frozen['cells']) and all(c['status'] == 'COMPLETE' for c in report['cells']) else 'INCOMPLETE'
    except BaseException as error:
        report['status'] = 'INTERRUPTED' if isinstance(error, KeyboardInterrupt) else 'ERROR'
        report['stop_reason'] = f'{type(error).__name__}: {error}'
        if report['cells'] and report['cells'][-1]['status'] == 'RUNNING':
            report['cells'][-1]['status'] = report['status']
            report['cells'][-1]['error'] = report['stop_reason']
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        report['finished_utc'] = utc_now()
        report['elapsed_seconds'] = round(time.monotonic() - started, 4)
        report['unplayed_cells'] = len(frozen['cells']) - len(report['cells'])
        report['completed_games'] = sum(c['summary']['completed_games'] for c in report['cells'] if c['summary'] is not None)
        report['recorded_incomplete_games'] = sum(c['summary']['incomplete_games'] for c in report['cells'] if c['summary'] is not None)
        report['unstarted_cell_games'] = report['unplayed_cells'] * frozen['scheduled_games_per_cell']
        report['attempted_cells_without_game_records'] = sum(c['summary'] is None for c in report['cells'])
        write_json(directory / 'report.json', report)
    return report


def audit_matrix(directory):
    directory = Path(directory).resolve()
    frozen, profiles = verify_frozen(directory)
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    if report['frozen_plan_sha256'] != sha256(directory / 'frozen-plan.json'):
        raise ValueError('report belongs to a different frozen plan')
    if len(report['cells']) > len(frozen['cells']):
        raise ValueError('extra cells in report')
    for expected, cell in zip(frozen['cells'], report['cells']):
        if any(cell.get(key) != value for key, value in expected.items()):
            raise ValueError('cell schedule changed')
        target = directory / 'cells' / cell['id']
        if cell['status'] == 'COMPLETE' and cell['summary'] is None:
            raise ValueError('complete cell requires durable game evidence')
        if cell['summary'] is not None:
            metadata = json.loads((target / 'games.json').read_text(encoding='utf-8'))
            verify_cell_inputs(metadata, directory, frozen, profiles, cell)
            if sha256(target / 'games.json') != cell['games_json_sha256'] or sha256(target / 'games.pgn') != cell['pgn_sha256']:
                raise ValueError('cell evidence changed')
            if replay_cell(metadata, target / 'games.pgn') != cell['summary']:
                raise ValueError('cell summary changed')
            if cell['status'] == 'COMPLETE' and (not metadata['completed'] or metadata['exit_code'] != 0 or cell['exit_code'] != 0):
                raise ValueError('incomplete cell marked complete')
    if report['status'] == 'COMPLETE' and (len(report['cells']) != len(frozen['cells']) or any(c['status'] != 'COMPLETE' for c in report['cells'])):
        raise ValueError('incomplete matrix marked complete')
    return {'status': 'PASS', 'matrix_status': report['status'], 'audited_cells': len(report['cells']),
            'scheduled_cells': len(frozen['cells']), 'strength_eligible': False,
            'scope': 'Artifact consistency and independent chess replay only; not a strength endorsement.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('--plan', type=Path, required=True)
    freeze.add_argument('--output', type=Path, required=True)
    for name in ('run', 'audit'):
        command = commands.add_parser(name)
        command.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'freeze':
            frozen = freeze_plan(args.plan, args.output)
            print(json.dumps({'status': 'FROZEN', 'cells': len(frozen['cells']), 'directory': str(args.output)}, ensure_ascii=False))
            return 0
        result = run_matrix(args.directory) if args.command == 'run' else audit_matrix(args.directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] in ('COMPLETE', 'PASS') else 2
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
