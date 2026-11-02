"""Compare boss and normal modes through two production HumanGame hosts.

Both modes retain the app's classical evaluation, opening variety and dynamic
policy. Equal configured move times do not imply equal extra search or CPU use.
Only legal outcomes count; caps and failures remain unfinished. No user game or
process is reused. Shards derive seeds from global pair IDs, never from results.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import time

import chess
import chess.pgn

import boss_play
import dynamic_play
from boss_strength_match import (SOURCES as BASE_SOURCES, DisplayOnlyPeer,
                                 RecordedEngine, counts, end_state, native_engine,
                                 sha, utc, write_json)
from play_game import source_version
from play_human import HumanGame
from runtime_engine import resolve_runtime_engine

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (*BASE_SOURCES, 'scripts/boss_mode_match.py', 'scripts/dynamic_play.py',
           'scripts/opening_guide.py', 'scripts/opening_names.py', 'scripts/opening_sides.py',
           'scripts/play_game.py')
CATALOG = tuple('data/openings/' + name for name in
                ('manifest.json', 'a.tsv', 'b.tsv', 'c.tsv', 'd.tsv', 'e.tsv', 'LICENSE'))


def schedule(pairs, pair_start, seed):
    if any(type(value) is not int or value < 1 for value in (pairs, pair_start)):
        raise ValueError('pairs and pair-start must be positive integers')
    entries = []
    for pair in range(pair_start, pair_start + pairs):
        seeds = {mode + '_seed': hashlib.sha256(json.dumps(
            [str(seed), 'boss-vs-normal', mode, pair]).encode()).hexdigest()[:32]
                 for mode in ('boss', 'normal')}
        for offset, color in enumerate(('white', 'black')):
            entries.append({'game': 2 * (pair - 1) + offset + 1, 'pair': pair,
                            'boss_color': color,
                            'normal_color': 'black' if color == 'white' else 'white', **seeds})
    return entries


def same_board(actual, expected):
    return (actual.root().fen(en_passant='fen') == expected.root().fen(en_passant='fen')
            and actual.fen(en_passant='fen') == expected.fen(en_passant='fen')
            and actual.move_stack == expected.move_stack)


def match_pgn(board, record, config):
    game = chess.pgn.Game.from_board(board)
    game.headers.update(Event='Regicide boss vs normal mode', Site='local isolated match',
                        Round=str(record['game']), Pair=str(record['pair']), Result=record['result'],
                        White='Regicide ' + ('Boss' if record['boss_color'] == 'white' else 'Normal'),
                        Black='Regicide ' + ('Boss' if record['boss_color'] == 'black' else 'Normal'),
                        BossSeed=record['boss_seed'], NormalSeed=record['normal_seed'],
                        BossMoveTimeMS=str(config['boss_ms']), NormalMoveTimeMS=str(config['normal_ms']),
                        Termination='normal' if record['legal_result'] else 'unterminated',
                        TerminationReason=record['termination'],
                        MatchPurpose='Direct mode comparison; equal settings, not equal CPU use')
    return str(game) + '\n\n'


def play_one(config, entry, output, *, factories, on_progress=None):
    record = {**entry, 'started_at': utc(), 'result': '*', 'termination': 'not_started',
              'completed': False, 'legal_result': False, 'capped': False, 'error': None,
              'initial_fen': chess.STARTING_FEN, 'final_fen': chess.STARTING_FEN,
              'uci_moves': [], 'moves': [], 'plies': 0, 'hosts': {},
              'record_file': f"game-{entry['game']:03d}.json",
              'pgn_file': f"game-{entry['game']:03d}.pgn"}
    board, hosts, owned = chess.Board(), {}, []
    started = time.monotonic()

    def failed(message, termination='engine_error'):
        record.update(error=(record['error'] + '; ' if record['error'] else '') + message,
                      result='*', completed=False, legal_result=False, termination=termination)

    try:
        for mode in ('boss', 'normal'):
            details = {'engines': [], 'save_directory': f"host/game-{entry['game']:03d}/{mode}"}
            record['hosts'][mode] = details

            def start_engine(path, mode=mode, details=details):
                options = {}
                details['engines'].append(options)
                engine = RecordedEngine(factories[mode](path), options)
                owned.append(engine)
                if not {'Hash', 'UsePieceActivity'}.issubset(engine.options):
                    raise ValueError('Mode comparison requires Hash and UsePieceActivity options')
                settings = {'Hash': config['hash_mb']}
                if 'Threads' in engine.options:
                    settings['Threads'] = config['threads']
                engine.configure(settings)
                return engine

            host = HumanGame(Path(config['binary']), output / details['save_directory'],
                             engine_factory=start_engine,
                             hint_engine_factory=lambda _: DisplayOnlyPeer(),
                             evaluation_engine_factory=lambda _: DisplayOnlyPeer(),
                             neural_model=None, opening_variety_enabled=True, dynamic_play_enabled=True)
            hosts[mode] = host
            human_color = 'black' if entry[mode + '_color'] == 'white' else 'white'
            host.new_game(human_color, config[mode + '_ms'], host.revision, mode == 'boss')
            host._opening_seed = entry[mode + '_seed']
            host._save()
            details.update(game_id=host.game_id, evaluation=host.classical_evaluation_mode,
                           model_sha256=host.neural_sha256)
        trace = output / f"game-{entry['game']:03d}.moves.jsonl"
        with trace.open('x', encoding='utf-8') as stream:
            while True:
                if not all(same_board(host.board, board) for host in hosts.values()):
                    raise ValueError('Mode hosts diverged before a move')
                ending = end_state(board, config['max_plies'])
                if ending:
                    record.update(ending)
                    break
                mode = 'boss' if board.turn == (entry['boss_color'] == 'white') else 'normal'
                other = 'normal' if mode == 'boss' else 'boss'
                actor, receiver = hosts[mode], hosts[other]
                before = board.copy(stack=True)
                budget = actor.state()['engine']
                record['failed_turn'] = {'ply': len(board.move_stack) + 1, 'actor': mode,
                                         'before_fen': before.fen(en_passant='fen')}
                move_started = time.monotonic()
                state = actor.engine_move(actor.revision)
                elapsed = (time.monotonic() - move_started) * 1000
                if state['error']:
                    raise RuntimeError(mode + ' host error: ' + state['error'])
                if len(actor.board.move_stack) != len(board.move_stack) + 1:
                    raise ValueError(mode + ' host did not append exactly one move')
                move = actor.board.peek()
                record['failed_turn']['uci'] = move.uci()
                if move not in board.legal_moves:
                    raise ValueError(mode + ' host returned an illegal move')
                expected = board.copy(stack=True)
                expected.push(move)
                if not same_board(actor.board, expected):
                    raise ValueError(mode + ' history differs from independent replay')
                relay_started = time.monotonic()
                receiver.human_move(move.uci(), receiver.revision)
                if not same_board(receiver.board, expected):
                    raise ValueError(other + ' host diverged after receiving opponent move')
                relay_elapsed = (time.monotonic() - relay_started) * 1000
                board = expected
                turn = {'ply': len(board.move_stack), 'actor': mode, 'uci': move.uci(),
                        'san': before.san(move), 'before_fen': before.fen(en_passant='fen'),
                        'after_fen': board.fen(en_passant='fen'), 'elapsed_ms': round(elapsed, 3),
                        'relay_elapsed_ms': round(relay_elapsed, 3),
                        'baseline_budget_ms': budget['movetime_ms'],
                        'advertised_extra_budget_ms': budget['extra_budget_ms'],
                        'boss_decision': copy.deepcopy(actor._boss_decisions.get(len(board.move_stack))),
                        'dynamic_decision': copy.deepcopy(actor._dynamic_decisions.get(len(board.move_stack))),
                        'opening_decision': copy.deepcopy(actor._opening_decisions.get(len(board.move_stack)))}
                record['moves'].append(turn)
                record['uci_moves'].append(move.uci())
                record.update(plies=turn['ply'], final_fen=turn['after_fen'])
                record.pop('failed_turn')
                stream.write(json.dumps(turn, ensure_ascii=False, allow_nan=False) + '\n')
                stream.flush()
                if on_progress:
                    on_progress(record, turn)
    except BaseException as error:
        failed(f'{type(error).__name__}: {error}',
               'interrupted' if isinstance(error, KeyboardInterrupt) else 'engine_error')
    finally:
        for mode, host in hosts.items():
            try:
                record['hosts'][mode].update(final_fen=host.board.fen(en_passant='fen'),
                                             uci_moves=[move.uci() for move in host.board.move_stack],
                                             boss_play=host._boss_metadata(), dynamic_play=host._dynamic_metadata(),
                                             opening_variety=host._opening_metadata())
            except Exception as error:
                failed(f'{mode} metadata: {error}')
            try:
                host.close()
            except Exception as error:
                failed(f'{mode} cleanup: {error}')
        for engine in owned:
            try:
                engine.close()
            except Exception as error:
                failed(f'engine cleanup: {error}')
        try:
            pgn = match_pgn(board, record, config)
        except Exception as error:
            failed(f'PGN export: {error}')
            fallback = chess.pgn.Game.from_board(board)
            fallback.headers['Result'] = '*'
            pgn = str(fallback) + '\n\n'
        record.update(finished_at=utc(), elapsed_seconds=round(time.monotonic() - started, 3))
        (output / record['pgn_file']).write_text(pgn, encoding='utf-8')
        write_json(output / record['record_file'], record)
    return record, pgn


def run_match(config, *, boss_factory=None, normal_factory=None):
    config = {**config, 'binary': str(Path(config['binary']).resolve(strict=True)),
              'output_dir': str(Path(config['output_dir']).resolve()),
              'threads': 1, 'hash_mb': 16, 'cap_policy': 'incomplete', 'classical': True}
    entries = schedule(config['pairs'], config['pair_start'], config['seed'])
    for key in ('boss_ms', 'normal_ms'):
        if type(config[key]) is not int or not 100 <= config[key] <= 5000:
            raise ValueError(key + ' must be 100..5000')
    if type(config['max_plies']) is not int or config['max_plies'] < 1:
        raise ValueError('max-plies must be positive')
    output = Path(config['output_dir'])
    output.mkdir(parents=True, exist_ok=False)
    metadata = {'schema': 'regicide.boss-mode-match.v1', 'started_at': utc(), 'config': config,
                'source': source_version(), 'source_sha256': {path: sha(ROOT / path) for path in SOURCES},
                'catalog_sha256': {path: sha(ROOT / path) for path in CATALOG},
                'binary_sha256': sha(config['binary']), 'python': platform.python_version(),
                'python_chess': chess.__version__, 'platform': platform.platform(), 'logical_cpus': os.cpu_count(),
                'policies': {'boss': boss_play.POLICY, 'normal': dynamic_play.POLICY},
                'protocol': {'start': 'standard position; no forced opening',
                             'paths': 'Two HumanGame hosts, dynamic_play_enabled=True; boss_mode=True/False',
                             'evaluation': 'Classical, UsePieceActivity=true; no adopted or experimental neural model',
                             'draws': 'Always claim available threefold/50-move draws',
                             'adjudication': 'Legal outcomes only; caps and failures never enter W/D/L',
                             'timing': 'Configured baseline plus each production policy extra; not equal CPU use',
                             'elapsed_ms': 'engine_move wall time, including its own persistence; relay measured separately',
                             'pairing': 'Global pair IDs; independent mode seeds, repeated with colors swapped',
                             'display_helpers': 'No-search peers; no UI helper engine compute', 'ponder': False},
                'schedule': entries, 'games': [], 'status': 'running', 'errors': []}
    write_json(output / 'match.json', metadata)
    factories = {'boss': boss_factory or (lambda path: native_engine(path, boss=True)),
                 'normal': normal_factory or (lambda path: native_engine(path, boss=True))}
    pgns = []
    def progress(record, turn):
        if turn['ply'] % 10 == 0:
            print(json.dumps({'game': record['game'], 'ply': turn['ply'], 'actor': turn['actor'],
                              'move': turn['uci']}, ensure_ascii=False), flush=True)
    for entry in entries:
        metadata['active_game'] = entry['game']
        write_json(output / 'match.json', metadata)
        record, pgn = play_one(config, entry, output, factories=factories, on_progress=progress)
        metadata['games'].append({key: value for key, value in record.items()
                                  if key not in ('moves', 'hosts', 'uci_moves')})
        pgns.append(pgn)
        (output / 'match.pgn').write_text(''.join(pgns), encoding='utf-8')
        metadata.update(summary=counts(metadata['games'], legal_only=True))
        if record['error']:
            metadata['errors'].append(record['error'])
        write_json(output / 'match.json', metadata)
        print(json.dumps({'game': entry['game'], 'result': record['result'], 'termination': record['termination'],
                          'plies': record['plies'], 'error': record['error']}, ensure_ascii=False), flush=True)
        if record['error']:
            break
    sources_unchanged = all(sha(ROOT / path) == digest for path, digest in metadata['source_sha256'].items())
    catalog_unchanged = all(sha(ROOT / path) == digest for path, digest in metadata['catalog_sha256'].items())
    binary_unchanged = sha(config['binary']) == metadata['binary_sha256']
    metadata.update(finished_at=utc(), active_game=None, sources_unchanged=sources_unchanged,
                    catalog_unchanged=catalog_unchanged, binary_unchanged=binary_unchanged,
                    status='completed' if len(metadata['games']) == len(entries) and not metadata['errors']
                    and sources_unchanged and catalog_unchanged and binary_unchanged else 'failed')
    write_json(output / 'match.json', metadata)
    return metadata


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pairs', type=int, default=8)
    parser.add_argument('--pair-start', type=int, default=1)
    parser.add_argument('--boss-ms', type=int, default=1000)
    parser.add_argument('--normal-ms', type=int, default=1000)
    parser.add_argument('--seed', required=True)
    parser.add_argument('--max-plies', type=int, default=300)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--binary', type=Path)
    args = vars(parser.parse_args(argv))
    try:
        args['binary'] = (args['binary'] or resolve_runtime_engine(ROOT)).expanduser().resolve(strict=True)
        if not args['binary'].is_file():
            raise ValueError('binary must be a file')
        schedule(args['pairs'], args['pair_start'], args['seed'])
        if any(not 100 <= args[key] <= 5000 for key in ('boss_ms', 'normal_ms')) or args['max_plies'] < 1:
            raise ValueError('times must be 100..5000ms and max-plies must be positive')
        if args['output_dir'].exists():
            raise ValueError('output-dir must be new')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return {key: str(value) if isinstance(value, Path) else value for key, value in args.items()}


def main(argv=None):
    return 0 if run_match(arguments(argv))['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
