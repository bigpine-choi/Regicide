"""Independent read-only audit of original GitHub arena evidence and rejection."""
import ast
from collections import Counter
import datetime
import hashlib
import io
import json
import math
from pathlib import Path, PureWindowsPath
import random
import subprocess
import sys
import zipfile

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from collect_learning_data import OPTIONS

DIRECTORY = Path(__file__).with_name('remote-download-study6')
RUN = ROOT / 'build/learning-cycle/runs/000006-39915e221a26'
OUTPUT = Path(__file__).with_name('remote-audit-study6.json')
RESULT_COMMIT = 'c2c1a80eaf3f8b8ee0ecf0a4168740681bf485ba'
PARENT = '31564da7e209a3dcff755953c9a5d436524ef21d'
IMPLEMENTATION = 'fee60f4e23155a0c4e2b15f83a922411d9e62b3a'
BASELINE = '527289e74dbdfb28a98625ef961c7470e9c78ff9'
BINARY = 'e2b218e7b22f3f4f166d999f011ed08cb66120cc6a6f3265448537d72b8254e3'
MODEL = 'f4484072465f1c44dc41c1215704125a9bb61d5cfa3bf76f2496bd4d00af74a7'
checks = Counter()
inputs = {}

def require(value, message):
    checks['conditions'] += 1
    if not value:
        raise AssertionError(message)

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def raw(path):
    path = Path(path).resolve()
    value = path.read_bytes()
    actual = digest(value)
    if path in inputs:
        require(inputs[path] == actual, 'audited file changed during reads')
    inputs[path] = actual
    return value

def document(path):
    return json.loads(raw(path))

def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])

def boundary_lower(pair_scores, total):
    alpha = .05 / (6 * 7)
    return max(0.0, sum(pair_scores) / total - math.sqrt(math.log(1 / alpha) / (2 * total)))

def source_function(source, name):
    text = source.decode('utf-8')
    node = next(node for node in ast.parse(text).body if isinstance(node, ast.FunctionDef) and node.name == name)
    return '\n'.join(text.splitlines()[node.lineno - 1:node.end_lineno]).encode('utf-8')

def main():
    result = document(DIRECTORY / 'remote-result.json')
    arena = document(DIRECTORY / 'arena/arena.json')
    request_bytes = raw(DIRECTORY / 'request.json')
    request = json.loads(request_bytes)
    curve = document(RUN / 'curve/report.json')
    require(result['schema'] == 'regicide.learning.remote-result.v1' and result['status'] == 'complete' and
            result['errors'] == [] and result['promotion_performed'] is False, 'remote result is incomplete or claims promotion')
    require(result['implementation_commit'] == IMPLEMENTATION == request['implementation_commit'] and
            result['checked_out_commit'] == PARENT and result['prepared_run'] == request['prepared_run'] == RUN.relative_to(ROOT / 'build/learning-cycle').as_posix(),
            'remote source, checkout or prepared run identity differs')
    require(git('show', '-s', '--format=%P', RESULT_COMMIT).decode().strip().split() == [PARENT], 'result commit parent differs')
    git('merge-base', '--is-ancestor', IMPLEMENTATION, PARENT)
    require(git('show', PARENT + ':' + result['request_path']) == request_bytes == raw(ROOT / result['request_path']),
            'original request Git/local/export bytes differ')
    require(digest(request_bytes) == result['request_sha256'] and request['attempt_index'] == result['attempt_index'] == 6 and
            request['pairs'] == 32 and request['movetime_ms'] == 100 and request['source_hash_basis'] == 'git_blob_bytes',
            'request limits, attempt or hash basis changed')
    require(request['binary_sha256'] == BINARY and request['candidate_sha256'] == MODEL and
            request['champion_sha256'] is None and request['champion_model'] is None and
            curve['candidate']['model_sha256'] == MODEL and curve['candidate']['added_train'] == 1024,
            'fixed model or native binary identity differs')
    require(result['result_branch'] == f"validation/endgame-6-{result['request_sha256'][:12]}" and
            result['result_path'] == request['output_prefix'] + '/' + result['request_sha256'][:12], 'remote result namespace differs')
    host = result['host']['github']
    require(host['GITHUB_REPOSITORY'] == 'bigpine-choi/Regicide' and host['GITHUB_SHA'] == PARENT and
            host['RUNNER_OS'] == 'Windows' and host['GITHUB_RUN_ATTEMPT'] == '1' and host['GITHUB_RUN_ID'] == '39152559775',
            'remote execution host provenance differs')
    for relative, expected in request['source_files_sha256'].items():
        require(digest(git('show', IMPLEMENTATION + ':' + relative)) == expected == digest(git('show', PARENT + ':' + relative)),
                'implementation/request Git source blobs differ')
    archive = ROOT / request['binary_zip']
    require(digest(raw(archive)) == request['binary_zip_sha256'] == digest(git('show', PARENT + ':' + request['binary_zip'])),
            'native ZIP source bytes differ')
    with zipfile.ZipFile(archive) as stream:
        require(stream.namelist() == ['regicide.exe'], 'ZIP payload has unexpected members')
        executable = stream.read('regicide.exe')
    require(executable.startswith(b'MZ') and digest(executable) == BINARY == digest(raw(RUN / 'regicide.exe')),
            'transferred and original study executable bytes differ')
    require(digest(raw(ROOT / request['candidate_model'])) == MODEL == digest(raw(DIRECTORY / 'arena/candidate.nn')) ==
            digest(raw(RUN / 'curve' / curve['candidate']['model_path'])), 'remote/local candidate bytes differ')

    content = document(DIRECTORY / 'content-manifest.json')
    expected_content = {row['path']: (row['sha256'], row['bytes']) for row in content['files']}
    actual_content = {path.relative_to(DIRECTORY).as_posix(): (digest(raw(path)), path.stat().st_size)
                      for path in DIRECTORY.rglob('*') if path.is_file() and path.name != 'content-manifest.json'}
    require(actual_content == expected_content, 'result content manifest differs from original files')
    prefix = result['result_path'] + '/'
    tree = {}
    for entry in git('ls-tree', '-r', '-z', RESULT_COMMIT, '--', result['result_path']).split(b'\0'):
        if not entry:
            continue
        header, name = entry.split(b'\t', 1)
        mode, kind, blob = header.decode().split()
        name = name.decode()
        require(mode in ('100644', '100755') and kind == 'blob' and name.startswith(prefix), 'result tree has nonregular entry')
        tree[name[len(prefix):]] = blob
    require(set(tree) == {path.relative_to(DIRECTORY).as_posix() for path in DIRECTORY.rglob('*') if path.is_file()},
            'supplied raw files differ from result commit tree')
    for relative, blob in tree.items():
        require(git('cat-file', 'blob', blob) == raw(DIRECTORY / relative), 'original remote Git blob changed')

    require(arena['status'] == result['arena_status'] == 'rejected_early_futile' and
            arena['schema'] == 'regicide.learning.arena.v1' and arena['attempt_index'] == 6 and
            arena['requested_pairs'] == 32 and arena['movetime_ms'] == 100 and arena['hash_mb'] == 16 and arena['threads'] == 1 and
            arena['binary_sha256'] == BINARY and arena['candidate_sha256'] == MODEL and arena['champion_sha256'] is None and
            arena['completed'] is False and arena['valid_for_rejection'] is True and arena['stop_when_impossible'] is True and
            arena['eligible'] is False and arena['promotion_passed'] is False and arena['errors'] == [] and arena['draw_policy'] == 'claim',
            'early-rejection gate, engine or resource identity differs')
    require(arena['move_response_allowance_seconds'] == .2, 'equal-time response allowance changed')
    pool = document(ROOT / 'tests/data/learning_arena_openings_v1.json')
    selector = random.Random(request['seed'])
    expected_starts = []
    for index in range(32):
        seed = selector.choice(pool['seeds'])
        generator = random.Random(seed)
        moves = list(generator.choice(pool['prefixes']))
        board = chess.Board()
        for uci in moves:
            board.push_uci(uci)
        while len(moves) < 12:
            move = generator.choice(sorted(board.legal_moves, key=lambda move: move.uci()))
            moves.append(move.uci())
            board.push(move)
        expected_starts.append({'name': f'reserved-{index + 1:04d}-seed-{seed}', 'seed': seed, 'moves': moves,
                                'fen_after_opening': board.fen(en_passant='fen')})
    require(arena['opening_reserve']['openings'] == expected_starts and arena['opening_reserve']['seed'] == request['seed'],
            'all 32 reserved paired starts differ from the fixed random request')
    require(set(arena['engines']) == {'baseline', 'candidate'}, 'wrong role set')
    for role, neural in (('baseline', False), ('candidate', True)):
        engine = arena['engines'][role]
        options = dict(engine['options'])
        if neural:
            evalfile = options.pop('EvalFile')
            require(PureWindowsPath(evalfile) == PureWindowsPath(arena['artifact_directory']) / 'candidate.nn', 'remote EvalFile snapshot path differs')
        require(options == dict(OPTIONS, UseNeuralEvaluation=neural) and engine['binary_sha256'] == BINARY and
                engine['model_sha256'] == (MODEL if neural else None), 'role options or exact engine/model identity changed')
    require(arena['native_model_validation']['candidate']['model_sha256'] == MODEL and
            type(arena['native_model_validation']['candidate']['score_cp']) is int, 'native model parser evidence missing')
    pgn_path = DIRECTORY / 'arena/games.pgn'
    require(digest(raw(pgn_path)) == arena['pgn_sha256'], 'original remote PGN hash differs')
    stream = io.StringIO(pgn_path.read_text(encoding='utf-8'))
    scores, game_points = [], []
    require(len(arena['games']) == 16, 'expected eight complete observed pairs')
    for index, game in enumerate(arena['games']):
        opening = expected_starts[index // 2]
        candidate_white = index % 2 == 0
        white, black = ('candidate', 'baseline') if candidate_white else ('baseline', 'candidate')
        require(game['game'] == index + 1 and game['pair'] == index // 2 + 1 and game['white'] == white and game['black'] == black and
                game['initial_fen'] == chess.STARTING_FEN and game['opening_uci_moves'] == opening['moves'] and
                game['opening'] == opening['name'] and game['opening_fen'] == opening['fen_after_opening'] and
                game['uci_moves'][:12] == opening['moves'] and game['completed'] is True and game['error'] is None,
                'observed schedule, actual history or paired colors differ')
        require(len(game['search_moves']) == len(game['uci_moves']) - 12, 'observed per-move resource evidence missing')
        board = chess.Board()
        for ply, uci in enumerate(game['uci_moves']):
            require(board.outcome(claim_draw=True) is None, 'observed game continues after rule ending')
            if ply >= 12:
                item = game['search_moves'][ply - 12]
                require(item['role'] == (white if board.turn else black) and item['move'] == uci and
                        item['fen_before'] == board.fen(en_passant='fen') and
                        type(item['elapsed_seconds']) in (int, float) and math.isfinite(item['elapsed_seconds']) and
                        0 <= item['elapsed_seconds'] <= .2, 'observed position, role or 100ms response evidence differs')
                require(type(item['nodes']) is int and item['nodes'] >= 1, 'observed node telemetry missing')
                require(item['pv'] and item['pv'][0] == uci, 'observed bestmove differs from its PV')
                probe = board.copy(stack=True)
                for text in item['pv']:
                    require(probe.outcome(claim_draw=False) is None, 'PV continues past automatic end')
                    move = chess.Move.from_uci(text)
                    require(move in probe.legal_moves, 'illegal original remote PV')
                    probe.push(move)
                    checks['PV_plies'] += 1
            move = chess.Move.from_uci(uci)
            require(move in board.legal_moves, 'illegal actual remote move')
            board.push(move)
            checks['actual_game_plies'] += 1
        outcome = board.outcome(claim_draw=True)
        require(outcome is not None and outcome.result() == game['result'] and outcome.termination.name.lower() == game['termination'] and
                board.fen(en_passant='fen') == game['final_fen'] and len(game['uci_moves']) == game['plies'], 'actual remote result or final FEN differs')
        score = .5 if outcome.winner is None else float(outcome.winner == candidate_white)
        require(score == game['candidate_points'], 'candidate points differ from actual result')
        game_points.append(score)
        for role in ('baseline', 'candidate'):
            items = [item for item in game['search_moves'] if item['role'] == role]
            require(game['resources'][role] == {'searched_moves': len(items), 'reported_nodes': sum(item['nodes'] for item in items),
                    'moves_without_node_report': 0, 'elapsed_seconds': round(sum(item['elapsed_seconds'] for item in items), 6)},
                    'remote aggregate role resource evidence differs from actual per-move records')
        parsed = chess.pgn.read_game(stream)
        require(parsed is not None and not parsed.errors and parsed.board().fen(en_passant='fen') == game['initial_fen'] and
                [move.uci() for move in parsed.mainline_moves()] == game['uci_moves'] and parsed.headers['Result'] == game['result'] and
                parsed.headers['White'] == white and parsed.headers['Black'] == black, 'original remote PGN differs from complete recorded history')
    require(chess.pgn.read_game(stream) is None, 'PGN invents an unplayed game')
    scores = [(game_points[i] + game_points[i + 1]) / 2 for i in range(0, 16, 2)]
    decision = arena['decision']
    require(decision == result['decision'] and decision['attempt_alpha'] == .05 / (6 * 7) and
            decision['completed_pairs'] == 8 and decision['candidate_pair_score'] == sum(scores) / 8 and
            decision['one_sided_lower_bound'] == boundary_lower(scores, 8) and decision['eligible'] is False and decision['passed'] is False,
            'actual incomplete full-gate decision differs')
    certificate = arena['futility_certificate']
    optimistic_lower = boundary_lower(scores + [1] * 24, 32)
    require(optimistic_lower <= .5 and certificate['policy'] == 'fixed-final-gate-impossibility-v1' and
            certificate['planned_pairs'] == 32 and certificate['observed_pairs'] == 8 and certificate['unplayed_pairs'] == 24 and
            certificate['observed_pair_scores'] == scores and certificate['cannot_pass_final_gate'] is True and certificate['stop_early'] is True,
            'mathematical futility certificate differs from actual paired scores')
    optimistic = certificate['optimistic_final_decision']
    require(optimistic['candidate_pair_score'] == (sum(scores) + 24) / 32 and
            optimistic['one_sided_lower_bound'] == optimistic_lower and optimistic['eligible'] is True and optimistic['passed'] is False,
            'unplayed-win bound differs from the unchanged final 32-pair gate')
    require(all(boundary_lower(scores[:count] + [1] * (32 - count), 32) > .5 for count in range(1, 8)),
            'recording did not stop at the first mathematically futile complete pair')

    samples_path = DIRECTORY / 'arena/resource-samples.jsonl'
    require(digest(raw(samples_path)) == arena['resource_samples_sha256'], 'resource sample hash differs')
    samples = [json.loads(line) for line in samples_path.read_text().splitlines()]
    guard = arena['resource_guard']
    require(len(samples) == guard['snapshot_count'] == 343 and guard['contaminated'] is False and guard['failure'] is None and
            guard['external_cpu_increase_ticks'] == 0 and guard['external_cpu_tolerance_ticks'] == 0 and
            guard['observed_external_engine_cpu_increase'] is False and guard['external_engine_identities'] == [] and
            guard['valid_for_comparison'] is False, 'resource summary is dirty or makes a complete-match claim')
    transports = guard['transport_engines']
    owned = {item['pid']: item for item in guard['owned_engines']}
    require(set(transports) == {'baseline', 'candidate'} and len(owned) == 2 and
            set(owned) == {item['pid'] for item in transports.values()}, 'actual native transport ownership differs')
    cpu = {}
    for sample in samples:
        require(sample['contaminated'] is False and sample['external_cpu_delta_ticks'] == 0 and
                0 <= sample['gap_seconds'] <= 2 and 0 <= sample['snapshot_duration_seconds'] <= 2,
                'raw resource sample exceeds unchanged zero-external-CPU/2s policy')
        for process in sample['processes']:
            identity = owned.get(process['pid'])
            require(identity is not None and process['owned'] is True and process['name'] == 'regicide.exe' and
                    process['creation_ticks'] == identity['creation_ticks'] and process['parent_pid'] == guard['monitor_root_pid'],
                    'resource sample has an unknown or aliased process identity')
            if process['cpu_ticks'] is not None:
                require(process['cpu_ticks'] >= cpu.get(process['pid'], 0), 'native CPU ticks decreased for one identity')
                cpu[process['pid']] = process['cpu_ticks']
    require(max(row['gap_seconds'] for row in samples) == guard['max_snapshot_gap_seconds'] and
            max(row['snapshot_duration_seconds'] for row in samples) == guard['max_snapshot_duration_seconds'], 'resource sample maxima differ')
    preservation = document(Path(__file__).with_name('criterion-preservation.json'))
    require(preservation['status'] == 'passed' and preservation['baseline_commit'] == BASELINE and
            preservation['current_source_commit'] == IMPLEMENTATION, 'criterion preservation source identity differs')
    old_arena = git('show', BASELINE + ':scripts/learning_arena.py')
    current_arena = git('show', IMPLEMENTATION + ':scripts/learning_arena.py')
    for name in ('promotion_decision', 'verify_match_records'):
        old, new = source_function(old_arena, name), source_function(current_arena, name)
        require(old == new and digest(old) == preservation['functions'][name]['baseline_sha256'] ==
                preservation['functions'][name]['current_sha256'], 'full acceptance formula or rule verifier changed')
    require(git('show', BASELINE + ':scripts/learning_registry.py') == git('show', IMPLEMENTATION + ':scripts/learning_registry.py'),
            'whole atomic champion registry changed')
    for path, expected in inputs.items():
        require(digest(path.read_bytes()) == expected, 'original audited evidence changed during audit')
    counts = (game_points.count(1), game_points.count(.5), game_points.count(0))
    require(sum(counts) == 16, 'observed game classification is incomplete')
    report = {'status': 'passed', 'finished_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scope': 'Independent replay of authenticated original Git blobs and factual completed prefix. Early rejection only; 16 observed games are not a full 64-game strength or Elo estimate. No engine launched, source edited or model changed.',
              'result_commit': RESULT_COMMIT, 'request_parent': PARENT, 'implementation_commit': IMPLEMENTATION,
              'workflow_run_id': host['GITHUB_RUN_ID'], 'attempt_index': 6, 'binary_sha256': BINARY, 'candidate_sha256': MODEL,
              'actual_completed_pairs': 8, 'actual_games': 16, 'wins': counts[0], 'draws': counts[1], 'losses': counts[2],
              'observed_prefix_score': sum(game_points) / 16, 'unplayed_pairs': 24, 'unplayed_games': 48,
              'hypothetical_all_unplayed_wins_final_score': (sum(scores) + 24) / 32,
              'hypothetical_all_unplayed_wins_final_lower_bound': optimistic_lower,
              'unchanged_final_gate_pass_possible': False, 'full_promotion_functions_identical': True, 'whole_registry_identical': True,
              'resource_samples': len(samples), 'max_sample_gap_seconds': guard['max_snapshot_gap_seconds'],
              'observed_external_cpu_ticks': 0, 'checks': dict(checks), 'input_files': len(inputs),
              'input_sha256': {path.relative_to(ROOT).as_posix(): value for path, value in inputs.items()}}
    OUTPUT.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('status', 'actual_completed_pairs', 'actual_games', 'wins', 'draws', 'losses',
              'hypothetical_all_unplayed_wins_final_lower_bound', 'resource_samples', 'checks')}))

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        OUTPUT.write_text(json.dumps({'status': 'failed', 'error_type': type(error).__name__, 'error': str(error),
                                     'checks': dict(checks)}, indent=2) + '\n', encoding='utf-8')
        raise
