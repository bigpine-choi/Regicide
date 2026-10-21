"""Independent, read-only audit of the completed 48-game M2 pilot.

Does not import the matrix/profile/comparison harness or start any engine.
Only the requested new output JSON is written. Source, binaries, games and
the frozen directory are never changed. This checks records, not strength.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import sys

import chess
import chess.pgn

REPOSITORY = Path(__file__).resolve().parents[3]
FILES = {}
MOVES = Counter()


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        result = hashlib.file_digest(stream, 'sha256').hexdigest()
    previous = FILES.setdefault(str(path), result)
    check(previous == result, f'Input changed while auditing: {path}')
    return result


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        check(key not in result, f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def reject_constant(value):
    raise AssertionError(f'Nonfinite JSON constant: {value}')


def read(path):
    path = Path(path).resolve()
    sha(path)
    value = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique_object,
                       parse_constant=reject_constant)
    check(isinstance(value, dict), f'Expected JSON object: {path}')
    return value


def resolve(path, base):
    path = Path(path)
    return (path if path.is_absolute() else base / path).resolve()


def bound_file(entry, base):
    path = resolve(entry['path'], base)
    check(sha(path) == entry['sha256'], f'File SHA256 differs: {path}')
    return path


def profile(path):
    """Interpret file bindings directly, independently of engine_profile.py."""
    path = Path(path).resolve()
    literal = read(path)
    result = deepcopy(literal)
    binary = bound_file(literal['binary'], path.parent)
    files = {'binary': str(binary)}
    options = dict(literal['options'])
    check(len({key.casefold() for key in options}) == len(options), 'Ambiguous UCI option names')
    models = literal.get('models', {})
    for option, model in models.items():
        target = bound_file(model, path.parent)
        check(resolve(options[option], path.parent) == target, 'Model option leaves its frozen file binding')
        options[option] = str(target)
        files['model:' + option] = str(target)
        if literal['kind'] == 'regicide':
            with target.open('rb') as stream:
                magic = stream.read(256).split()
            check((magic[0] in (b'REGICIDE_NN_V1', b'REGICIDE_NN_V2') if option == 'EvalFile'
                   else magic[:2] in ([b'REGICIDE_NNUE', b'1'], [b'REGICIDE_NNUE', b'2'])),
                  'Frozen model has the wrong native model family')
    artifacts = {}
    for number, artifact in enumerate(literal.get('artifacts', [])):
        target = bound_file(artifact, path.parent)
        artifacts[str(target)] = artifact
        files[f'artifact:{number}:{artifact["role"]}'] = str(target)
    args = []
    for arg in literal['args']:
        candidate = str(resolve(arg, path.parent)) if arg and not arg.startswith('-') else None
        args.append(candidate if candidate in artifacts else arg)
    if literal['kind'] == 'regicide':
        check(args == ['uci'], 'Regicide command is not the frozen UCI command')
        check(options['UseNeuralEvaluation'] == (literal['evaluation'] == 'residual'), 'Residual activation differs')
        check(options['UseNNUEEvaluation'] == (literal['evaluation'] == 'nnue'), 'NNUE activation differs')
        required = {'residual': 'EvalFile', 'nnue': 'NeuralModelFile'}.get(literal['evaluation'])
        check(not required or required in models, 'Missing declared model')
        for name, sentinel in (('EvalFile', '<empty>'), ('NeuralModelFile', '<builtin>')):
            check(name in models or options.get(name, sentinel) == sentinel, 'Undeclared native model option')
        commit = literal['source']['commit']
        kind = subprocess.run(['git', '-C', str(REPOSITORY), 'cat-file', '-t', commit],
                              capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        check(kind == 'commit', 'Declared Regicide source object is not a commit')
        for artifact in literal.get('artifacts', []):
            if artifact['role'] != 'native-build-receipt':
                continue
            receipt = read(resolve(artifact['path'], path.parent))
            check(receipt['windows_binary_sha256'] == literal['binary']['sha256'], 'Build receipt binary differs')
            for relative, expected in receipt['native_source_sha256'].items():
                source = REPOSITORY / relative
                check(sha(source) == expected, 'Current native source differs from copied build receipt')
                committed = subprocess.run(['git', '-C', str(REPOSITORY), 'show', commit + ':' + relative],
                                           capture_output=True, check=True, timeout=10).stdout
                check(source.read_bytes().replace(b'\r\n', b'\n') == committed.replace(b'\r\n', b'\n'),
                      'Native source differs from declared Git commit')
    result.update(command=[str(binary), *args], resolved_options=options, resolved_files=files)
    return result


def same_identity(original, frozen):
    for key in ('id', 'kind', 'evaluation', 'source'):
        check(original[key] == frozen[key], f'Profile snapshot changed {key}')
    for key in ('binary',):
        check(original[key]['sha256'] == frozen[key]['sha256'], 'Snapshot executable differs')
    check({key: value['sha256'] for key, value in original['models'].items()} ==
          {key: value['sha256'] for key, value in frozen['models'].items()}, 'Snapshot model identities differ')
    check([(a['role'], a['sha256']) for a in original['artifacts']] ==
          [(a['role'], a['sha256']) for a in frozen['artifacts']], 'Snapshot artifacts differ')
    old, new = dict(original['resolved_options']), dict(frozen['resolved_options'])
    for key in original['models']:
        old[key] = new[key] = '<same verified model SHA256>'
    check(old == new, 'Snapshot options changed beyond model-path relocation')


def advertised_options(actual, expected):
    check(actual['requested_options'] == expected and actual['validated_options'] == expected,
          'Requested/validated options differ from frozen profile')
    advertised = actual['advertised_options']
    for name, value in expected.items():
        check(name in advertised, f'Unadvertised option: {name}')
        option = advertised[name]
        if option['type'] == 'check':
            check(type(value) is bool, 'Check option is not boolean')
        elif option['type'] == 'spin':
            check(type(value) is int and option['min'] <= value <= option['max'], 'Spin option outside range')
        elif option['type'] == 'combo':
            check(value in option['var'], 'Combo option outside advertised choices')
    check('no general option readback' in actual['configuration_note'], 'Configuration claim omits readback limit')


def invocation(cell_dir, cell, candidate, opponent, plan, frozen):
    document = read(cell_dir / 'invocation.json')
    command = document['command']
    check(len(command) >= 4 and (len(command) - 4) % 2 == 0, 'Malformed match invocation')
    check(command[2:4] == [opponent['command'][0], candidate['command'][0]], 'Actual match executable paths differ')
    live = next(Path(path) for path in frozen['harness_source_sha256'] if Path(path).name == 'compare_engines.py')
    check(Path(command[1]).resolve() == live.resolve(), 'Invocation uses a different comparison script')
    sha(command[0])  # Interpreter identity is reported, not a fully frozen environment.
    arguments = dict(zip(command[4::2], command[5::2]))
    check(len(arguments) == (len(command) - 4) // 2, 'Duplicate invocation flags')
    check(set(arguments) == {'--baseline-source', '--candidate-commit', '--baseline-options', '--candidate-options',
                              '--openings', '--movetime', '--max-plies', '--draw-policy', '--pgn'}, 'Unexpected invocation flags')
    check(arguments['--candidate-commit'] == candidate['source']['commit'], 'Invocation source commit differs')
    check(arguments['--movetime'] == str(cell['movetime_ms']) and arguments['--max-plies'] == str(plan['max_plies'])
          and arguments['--draw-policy'] == plan['draw_policy'], 'Invocation search/adjudication limits differ')
    check(Path(arguments['--openings']).resolve() == cell_dir.parent.parent / 'openings.json', 'Wrong opening input')
    check(Path(arguments['--pgn']).resolve() == cell_dir / 'games.pgn', 'Wrong PGN output')
    check(Path(arguments['--baseline-source']).resolve() == cell_dir / 'external-source.json', 'Wrong external source input')
    for role, expected in (('baseline', opponent), ('candidate', candidate)):
        options = json.loads(arguments[f'--{role}-options'], object_pairs_hook=unique_object,
                             parse_constant=reject_constant)
        check(options == expected['resolved_options'], 'Invocation option values differ from frozen binding')
        for active, binding in (('UseNeuralEvaluation', 'EvalFile'), ('UseNNUEEvaluation', 'NeuralModelFile')):
            if options.get(active) is True:
                check(list(options).index(binding) < list(options).index(active), 'Actual options activate before file load')
    external = read(cell_dir / 'external-source.json')
    for key in ('name', 'version', 'source_url', 'revision'):
        check(external[key] == opponent['source'][key], 'External declaration differs from frozen profile')
    check(external['binary_sha256'] == opponent['binary']['sha256'] and external['args'] == opponent['command'][1:],
          'External command/hash declaration differs')
    return {'invocation_sha256': sha(cell_dir / 'invocation.json'), 'command': command}


def game(record, pgn, opening, candidate_white, metadata, cell):
    check(record['initial_fen'] == chess.STARTING_FEN, 'JSON root differs from standard chess start')
    check(pgn.board().fen(en_passant='fen') == chess.STARTING_FEN, 'PGN root differs from JSON/standard start')
    check(not pgn.errors and type(pgn.board()) is chess.Board and not pgn.board().chess960, 'Malformed/nonstandard PGN')
    check(all(len(node.variations) <= 1 for node in (pgn, *pgn.mainline())), 'Unexpected side variation in generated PGN')
    check(record['white'] == ('candidate' if candidate_white else 'baseline') and
          record['black'] == ('baseline' if candidate_white else 'candidate'), 'Color assignment differs')
    check(record['opening'] == opening['name'] and record['opening_uci_moves'] == opening['moves'], 'Wrong opening line')
    check(record['uci_moves'][:len(opening['moves'])] == opening['moves'], 'Opening prefix differs')
    check([move.uci() for move in pgn.mainline_moves()] == record['uci_moves'], 'PGN/JSON moves differ')
    check(record['plies'] == len(record['uci_moves']) and record['plies'] <= metadata['max_plies'], 'Ply count differs')
    check(record['error'] is None, 'Game contains an error')
    check(len(record['search_moves']) == len(record['uci_moves']) - len(opening['moves']), 'Missing/extra search receipts')
    board = chess.Board()
    previous = {chess.WHITE: None, chess.BLACK: None}
    backtracks = {role: {'count': 0, 'opportunities': 0} for role in ('candidate', 'baseline')}
    for offset, text in enumerate(record['uci_moves']):
        check(board.outcome(claim_draw=metadata['draw_policy'] == 'claim') is None, 'Move played after rule ending')
        move = chess.Move.from_uci(text)
        check(bool(move) and move in board.legal_moves, 'Illegal/null recorded move')
        if offset >= len(opening['moves']):
            receipt = record['search_moves'][offset - len(opening['moves'])]
            role = record['white' if board.turn else 'black']
            check(receipt['ply'] == offset + 1 and receipt['role'] == role and receipt['move'] == text,
                  'Search receipt move/role/ply differs')
            check(receipt['color'] == ('white' if board.turn else 'black'), 'Search receipt mover color differs')
            check(receipt['fen_before'] == board.fen(en_passant='fen'), 'Search receipt pre-move FEN differs')
            score = receipt['score']
            if score is None:
                MOVES['missing_score_reports'] += 1
            else:
                check(score['pov'] == 'mover' and (score['cp'] is None) != (score['mate'] is None), 'Ambiguous score POV/type')
                check(type(score['cp'] if score['cp'] is not None else score['mate']) is int, 'Noninteger search score')
                check(type(score['lowerbound']) is bool and type(score['upperbound']) is bool, 'Invalid score bounds')
            probe = board.copy(stack=True)
            for pv_text in receipt['pv']:
                pv_move = chess.Move.from_uci(pv_text)
                check(bool(pv_move) and pv_move in probe.legal_moves, 'Illegal reported PV')
                probe.push(pv_move)
                MOVES['legal_pv_plies'] += 1
            piece = board.piece_type_at(move.from_square)
            eligible = piece != chess.PAWN and not board.is_capture(move) and not board.is_check()
            prior = previous[board.turn]
            opportunity = bool(eligible and prior and prior['eligible'])
            reversal = bool(opportunity and prior['piece'] == piece and prior['from'] == move.to_square and prior['to'] == move.from_square)
            check((receipt['quiet_nonpawn_not_in_check'], receipt['backtrack_opportunity'], receipt['immediate_backtrack']) ==
                  (eligible, opportunity, reversal), 'Backtrack receipt differs from independent replay')
            backtracks[role]['opportunities'] += int(opportunity)
            backtracks[role]['count'] += int(reversal)
            previous[board.turn] = {'eligible': eligible, 'piece': piece, 'from': move.from_square, 'to': move.to_square}
            for key in ('nodes', 'depth', 'seldepth'):
                check(receipt[key] is None or type(receipt[key]) is int and receipt[key] >= 0, 'Invalid reported search counter')
            check(isinstance(receipt['elapsed_seconds'], (int, float)) and receipt['elapsed_seconds'] >= 0, 'Invalid move time')
            MOVES['searched_plies'] += 1
        board.push(move)
        MOVES['legal_mainline_plies'] += 1
        if offset + 1 == len(opening['moves']):
            check(record['opening_fen'] == board.fen(en_passant='fen'), 'Opening FEN differs')
    check(record['final_fen'] == board.fen(en_passant='fen'), 'Final FEN differs')
    check(record['backtracks'] == backtracks, 'Game backtrack totals differ')
    outcome = board.outcome(claim_draw=metadata['draw_policy'] == 'claim')
    if record['completed']:
        check(outcome is not None and record['result'] == outcome.result() and
              record['termination'] == outcome.termination.name.lower(), 'Completed result/termination differs from chess rules')
        points = .5 if outcome.winner is None else float(outcome.winner == (chess.WHITE if candidate_white else chess.BLACK))
        check(record['candidate_points'] == points, 'Candidate points differ')
    else:
        check(record['result'] == '*' and record['candidate_points'] is None, 'Unfinished game was scored')
        check(outcome is None, 'Terminal game incorrectly marked unfinished')
        points = None
    headers = {'Result': record['result'], 'Opening': opening['name'], 'Pair': str(record['pair']),
               'Round': str(record['game']), 'OpeningPlies': str(len(opening['moves'])),
               'DrawPolicy': metadata['draw_policy'], 'TerminationReason': record['termination'],
               'Termination': 'normal' if record['completed'] else 'unterminated',
               'RequestedMoveTimeMS': str(cell['movetime_ms']), 'RequestedNodes': '-', 'RequestedDepth': '-'}
    for key, expected in headers.items():
        check(pgn.headers.get(key) == expected, f'PGN header differs: {key}')
    for color in ('White', 'Black'):
        role = record[color.lower()]
        engine = metadata['engines'][role]
        check(pgn.headers[color] == role + ': ' + engine['engine_id'].get('name', 'unknown'), 'PGN player label differs')
        check(pgn.headers[color + 'SHA256'] == engine['sha256'], 'PGN executable SHA differs')
        check(pgn.headers[color + 'SourceCommit'] == (engine['provided_source_commit'] or 'external: see JSON'), 'PGN source commit differs')
    return points


def audit(directory):
    directory = Path(directory).resolve()
    frozen = read(directory / 'frozen-plan.json')
    check(read(directory / 'seal.json')['frozen_plan_sha256'] == sha(directory / 'frozen-plan.json'), 'Broken frozen-plan seal')
    check(sha(frozen['source_plan_path']) == frozen['source_plan_sha256'], 'Original plan changed')
    for original, expected in frozen['harness_source_sha256'].items():
        check(sha(original) == expected and sha(directory / 'harness' / Path(original).name) == expected, 'Harness source/copy changed')
    plan = frozen['plan']
    check(plan['phase'] == 'pilot' and plan['draw_policy'] == 'claim', 'Unexpected pilot phase/policy')
    check(len(plan['candidates']) == 3 and len(plan['opponents']) == 2 and sorted(plan['movetimes_ms']) == [50, 200], 'Unexpected 3x2x2 plan')
    profiles = {identifier: profile(bound_file(entry, directory)) for identifier, entry in frozen['profiles'].items()}
    candidates, opponents = [], []
    plan_base = Path(frozen['source_plan_path']).parent
    for role, identifiers in (('candidates', candidates), ('opponents', opponents)):
        for entry in plan[role]:
            original = profile(bound_file(entry, plan_base))
            identifiers.append(original['id'])
            same_identity(original, profiles[original['id']])
    check({profiles[value]['evaluation'] for value in candidates} == {'classical', 'residual', 'nnue'}, 'Missing evaluator family')
    check(len(set(candidates + opponents)) == 5, 'Duplicate/missing profile identities')
    check(all(profiles[value]['kind'] == 'external' for value in opponents), 'Opponent is not external')
    mode_keys = {'UseNeuralEvaluation', 'UseNNUEEvaluation', 'EvalFile', 'NeuralModelFile'}
    common = [{key: value for key, value in profiles[identifier]['resolved_options'].items() if key not in mode_keys}
              for identifier in candidates]
    check(common[0] == common[1] == common[2], 'Candidate search conditions differ beyond evaluation/model selection')
    check(len({profiles[value]['binary']['sha256'] for value in candidates}) == 1, 'Candidate executable hashes differ')
    openings = read(directory / 'openings.json')['openings']
    check(sha(directory / 'openings.json') == frozen['openings_sha256'] == plan['openings']['sha256'], 'Opening file changed')
    check(len(openings) == frozen['opening_families'] == 2 and len({row['family_id'] for row in openings}) == 2, 'Expected two distinct families')
    for opening in openings:
        board = chess.Board()
        for text in opening['moves']:
            move = chess.Move.from_uci(text)
            check(board.outcome(claim_draw=True) is None and bool(move) and move in board.legal_moves, 'Illegal/terminal frozen opening')
            board.push(move)
        check(board.outcome(claim_draw=True) is None, 'Frozen opening already ended')
        opening['fen_after_opening'] = board.fen(en_passant='fen')
    expected_cells = []
    for block, (opponent, milliseconds) in enumerate((opponent, milliseconds) for opponent in opponents for milliseconds in plan['movetimes_ms']):
        order = candidates[block % 3:] + candidates[:block % 3]
        for candidate in order:
            expected_cells.append({'id': f'{len(expected_cells)+1:03d}-{candidate}-vs-{opponent}-{milliseconds}ms',
                                   'candidate': candidate, 'opponent': opponent, 'movetime_ms': milliseconds})
    check(frozen['cells'] == expected_cells and len(expected_cells) == 12 and frozen['scheduled_games_per_cell'] == 4, 'Frozen cell schedule differs')
    report = read(directory / 'report.json')
    check(report['status'] == 'COMPLETE' and report['phase'] == 'pilot', 'Pilot is not complete; do not audit a running/unfinished pilot as complete')
    check(report['scheduled_cells'] == 12 and report['scheduled_games'] == 48 and len(report['cells']) == 12, 'Expected 12 cells/48 games')
    check(report['frozen_plan_sha256'] == sha(directory / 'frozen-plan.json'), 'Report belongs to another plan')
    check(report['strength_eligible'] is False and report['elo_estimate'] is None, 'Pilot incorrectly claims strength eligibility/Elo')
    results, observations, coverage = [], [], Counter()
    for expected, cell in zip(expected_cells, report['cells'], strict=True):
        check(all(cell[key] == value for key, value in expected.items()), 'Report changed cell schedule/order')
        cell_dir = directory / 'cells' / cell['id']
        check(read(cell_dir / 'result.json') == cell, 'Report/result.json cell differs')
        check(cell['status'] == 'COMPLETE' and cell['exit_code'] == 0, 'Cell failed/incomplete')
        metadata = read(cell_dir / 'games.json')
        check(sha(cell_dir / 'games.json') == cell['games_json_sha256'] and sha(cell_dir / 'games.pgn') == cell['pgn_sha256'], 'Game output hash differs')
        check(metadata['errors'] == [] and metadata['completed'] is True and metadata['exit_code'] == 0, 'Game harness errors/incompletion')
        check(metadata['limits'] == {'nodes': None, 'depth': None, 'movetime_ms': cell['movetime_ms']} and
              metadata['draw_policy'] == plan['draw_policy'] and metadata['max_plies'] == plan['max_plies'], 'Game conditions differ')
        check(metadata['selected_openings'] == openings and metadata['opening_file_sha256'] == frozen['openings_sha256'], 'Selected openings differ')
        check(metadata['games_requested'] == len(metadata['games']) == 4, 'Cell has missing/extra games')
        candidate, opponent = profiles[cell['candidate']], profiles[cell['opponent']]
        command_receipt = invocation(cell_dir, cell, candidate, opponent, plan, frozen)
        for role, expected_profile in (('candidate', candidate), ('baseline', opponent)):
            actual = metadata['engines'][role]
            check(actual['command'] == expected_profile['command'] and actual['sha256'] == expected_profile['binary']['sha256'], 'Recorded command/executable hash differs')
            advertised_options(actual, expected_profile['resolved_options'])
            is_local = expected_profile['kind'] == 'regicide'
            check(actual['source_commit_object_verified'] is is_local and
                  actual['provided_source_commit'] == (expected_profile['source']['commit'] if is_local else None), 'Source identity claim differs')
            if not is_local:
                external = actual['external_source']
                for key in ('name', 'version', 'source_url', 'revision'):
                    check(external[key] == expected_profile['source'][key], 'Recorded external identity differs')
                check(external['binary_sha256'] == actual['sha256'] and external['args'] == actual['command'][1:], 'Recorded external launch differs')
                check(external['manifest_sha256'] == sha(cell_dir / 'external-source.json'), 'External source input hash differs')
        stream = io.StringIO((cell_dir / 'games.pgn').read_text(encoding='utf-8'))
        points = []
        for number, record in enumerate(metadata['games'], 1):
            opening = openings[(number - 1) // 2]
            check(record['game'] == number and record['pair'] == (number + 1) // 2, 'Game/pair numbering differs')
            parsed = chess.pgn.read_game(stream)
            check(parsed is not None, 'Missing PGN game')
            point = game(record, parsed, opening, number % 2 == 1, metadata, cell)
            check(point is not None and record['completed'] is True, 'Unfinished game within completed pilot')
            points.append(point)
            coverage[(cell['candidate'], cell['opponent'], cell['movetime_ms'], opening['family_id'], number % 2)] += 1
        check(chess.pgn.read_game(stream) is None, 'Extra PGN game')
        counts = {'candidate_wins': points.count(1.), 'candidate_draws': points.count(.5), 'candidate_losses': points.count(0.),
                  'candidate_points': sum(points), 'candidate_score': sum(points) / 4,
                  'completed_games': 4, 'incomplete_games': 0, 'unplayed_games': 0, 'attempted_games': 4, 'scheduled_games': 4}
        for summary in (metadata['summary'], cell['summary']):
            check(all(summary[key] == value for key, value in counts.items()), 'Cell WDL/points summary differs')
            for pair, first in enumerate((0, 2), 1):
                item = summary['pairs'][pair - 1]
                check(item['pair'] == pair and item['games'] == [first + 1, first + 2] and item['completed'] is True and
                      item['candidate_points'] == sum(points[first:first+2]) and item['candidate_score'] == sum(points[first:first+2]) / 2,
                      'Paired score differs')
        load_path = cell_dir / 'load.jsonl'
        sha(load_path)
        rows = [json.loads(line) for line in load_path.read_text(encoding='utf-8').splitlines() if line]
        check(len(rows) >= 2, 'Missing load observation receipts')
        observations.extend((cell['id'], row) for row in rows)
        results.append(dict(expected, **counts, **command_receipt, games_json_sha256=cell['games_json_sha256'], pgn_sha256=cell['pgn_sha256']))
    expected_coverage = {(c, o, t, opening['family_id'], color) for c in candidates for o in opponents
                         for t in plan['movetimes_ms'] for opening in openings for color in (0, 1)}
    check(set(coverage) == expected_coverage and len(coverage) == 48 and set(coverage.values()) == {1}, '48-game factorial/color coverage differs')
    check(report['completed_games'] == 48 and report['recorded_incomplete_games'] == report['unplayed_cells'] ==
          report['unstarted_cell_games'] == report['attempted_cells_without_game_records'] == 0, 'Incomplete/unplayed totals differ')
    counters, last_times, cpu_increases, load_errors, max_gap = {}, {}, 0, [], 0.
    for cell_id, row in observations:
        instant = datetime.fromisoformat(row['utc'])
        if cell_id in last_times:
            max_gap = max(max_gap, (instant - last_times[cell_id]).total_seconds())
        last_times[cell_id] = instant
        if row.get('error') or row.get('unsupported'):
            load_errors.append({'cell': cell_id, 'utc': row['utc'], 'error': row.get('error'), 'unsupported': row.get('unsupported', False)})
        for process in row['known_external_engines']:
            identity = (process['pid'], process['creation_ticks'])
            ticks = process['cpu_ticks']
            if process.get('error') or ticks is None:
                load_errors.append({'cell': cell_id, 'utc': row['utc'], 'process': process})
                continue
            check(type(ticks) is int and ticks >= 0, 'Invalid sampled CPU counter')
            if identity in counters:
                cpu_increases += max(0, ticks - counters[identity])
            counters[identity] = ticks
    check(all(sha(path) == expected for path, expected in list(FILES.items())), 'Audit inputs changed')
    return {'schema_version': 1, 'status': 'PASS', 'audited_utc': datetime.now(timezone.utc).isoformat(),
            'audit_script_sha256': sha(__file__), 'pilot_directory': str(directory), 'scheduled_games': 48,
            'completed_games': 48, 'cells': results, 'coverage': '3 evaluators x 2 opponents x 2 times x 2 families x both colors, once each',
            'replay': dict(MOVES), 'input_sha256': dict(sorted(FILES.items())),
            'source_scope': 'File identities, declared source fields, local commit objects and receipt/source consistency verified. No independent source-to-binary rebuild was performed; external revision/URL remain declarations.',
            'configuration_scope': 'Actual command, frozen model paths/SHA and serialized option values/order checked. No raw UCI transcript or general engine option readback exists for these pilot matches.',
            'score_scope': 'Outcomes/points independently derived from rules; recorded search-score POV/type checked, not independently recomputed by an engine.',
            'external_cpu_observation': {'samples': len(observations), 'process_identities': len(counters),
                'positive_observed_cpu_tick_deltas': cpu_increases, 'max_within_cell_sample_gap_seconds': max_gap,
                'observation_errors': load_errors, 'scope': 'Sampled unowned known engine processes only; first observations have no prior delta. Other host load, gaps, thermal/power effects and interpreter dependencies are not isolated.'},
            'strength_eligible': False, 'elo_estimate': None, 'promotion_performed': False,
            'claim_scope': 'Independent record/integrity audit of a shared-host descriptive pilot. No strength improvement, model promotion, full M2 completion or world ranking claim.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    check(not args.output.exists(), 'Audit output must be a new file')
    try:
        result = audit(args.directory)
        code = 0
    except Exception as error:
        result = {'status': 'FAIL', 'error': f'{type(error).__name__}: {error}',
                  'audited_utc': datetime.now(timezone.utc).isoformat(), 'input_sha256': FILES,
                  'strength_eligible': False, 'elo_estimate': None}
        code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({key: result[key] for key in ('status', 'completed_games', 'error', 'replay') if key in result}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
