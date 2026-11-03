"""Play the production Python boss against a UCI-limited Stockfish opponent.

HumanGame owns every boss move, full history and within-game adaptation. Only
non-playing display helpers are replaced. Scores never adjudicate a game. A
Stockfish UCI_Elo setting is an experimental opponent setting, not a platform
or FIDE rating calibration. All files go into a new, isolated output folder.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import chess
import chess.engine
import chess.pgn

import boss_openings
import boss_opponent
import boss_play
from play_game import source_version
from play_human import HumanGame, runtime_evaluation
from runtime_engine import resolve_runtime_engine

ROOT = Path(__file__).resolve().parents[1]
# Snapshot the complete local Python host graph, including optional policy
# modules such as gambit_initiative. A hand-maintained shortlist can silently
# omit a newly imported selector or time-control dependency.
SOURCES = tuple(path.relative_to(ROOT).as_posix()
                for path in sorted(ROOT.joinpath('scripts').glob('*.py')))


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def schedule(games, seed, elo):
    """Pair colors with the same predeclared boss seed, never select by outcome."""
    if type(games) is not int or games < 2 or games % 2:
        raise ValueError('games must be a positive even number')
    return [{'game': index + 1, 'pair': index // 2 + 1,
             'boss_color': 'white' if index % 2 == 0 else 'black', 'stockfish_elo': elo,
             'boss_seed': hashlib.sha256(json.dumps([str(seed), elo, index // 2 + 1]).encode()).hexdigest()[:32]}
            for index in range(games)]


class DisplayOnlyPeer:
    """No process/search/score; this result is never used to play a game move."""
    id = {'name': 'Disabled match display helper'}
    options = {}

    def play(self, board, limit, **kwargs):
        return chess.engine.PlayResult(next(iter(board.legal_moves), None), None, {})

    def quit(self):
        pass

    def close(self):
        pass


class RecordedEngine:
    def __init__(self, engine, record):
        self.engine = engine
        self.record = record
        record.update(id=dict(engine.id), configured_options={},
                      advertised_options={name: {'type': option.type, 'default': option.default,
                                                 'min': option.min, 'max': option.max, 'var': option.var}
                                          for name, option in engine.options.items()})

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def configure(self, values):
        self.engine.configure(values)
        self.record['configured_options'].update(values)


def stockfish_options(engine, elo, threads, hash_mb):
    for name in ('UCI_LimitStrength', 'UCI_Elo', 'Threads', 'Hash'):
        if name not in engine.options:
            raise ValueError('Stockfish did not advertise ' + name)
    if engine.options['UCI_LimitStrength'].type != 'check' or engine.options['UCI_Elo'].type != 'spin':
        raise ValueError('Unexpected Stockfish strength option types')
    requested = {'UCI_LimitStrength': True, 'UCI_Elo': elo, 'Threads': threads, 'Hash': hash_mb}
    for name, value in requested.items():
        engine.options[name].parse(value)
    if 'OwnBook' in engine.options:
        requested['OwnBook'] = False
    engine.configure(requested)
    return requested


def native_engine(binary, *, boss):
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    command = [str(binary), 'uci'] if boss else [str(binary)]
    return chess.engine.SimpleEngine.popen_uci(command, timeout=10, **flags)


def end_state(board, max_plies, cap_policy='incomplete'):
    outcome = board.outcome(claim_draw=True)
    if outcome is not None:
        return {'result': outcome.result(), 'termination': outcome.termination.name.lower(),
                'completed': True, 'legal_result': True, 'capped': False}
    if len(board.move_stack) >= max_plies:
        return {'result': '1/2-1/2' if cap_policy == 'draw' else '*', 'termination': 'ply_cap',
                'completed': cap_policy == 'draw', 'legal_result': False, 'capped': True}
    return None


def counts(records, *, legal_only=False):
    values = {'wins': 0, 'draws': 0, 'losses': 0, 'capped': 0, 'failed': 0, 'unfinished': 0}
    for row in records:
        values['capped'] += bool(row.get('capped'))
        values['failed'] += row.get('error') is not None
        if row.get('error') or not row.get('completed') or (legal_only and not row.get('legal_result')):
            values['unfinished'] += 1
            continue
        if row['result'] == '1/2-1/2':
            values['draws'] += 1
        elif row['result'] == ('1-0' if row['boss_color'] == 'white' else '0-1'):
            values['wins'] += 1
        else:
            values['losses'] += 1
    played = values['wins'] + values['draws'] + values['losses']
    return {**values, 'scored_games': played,
            'score': (values['wins'] + values['draws'] / 2) / played if played else None}


def match_pgn(game, record, config):
    if game is None:
        value = chess.pgn.Game()
    else:
        value = chess.pgn.read_game(io.StringIO(game.pgn()))
        if value is None or value.errors:
            raise ValueError('Production PGN could not be replayed')
    boss_name = (game.engine_name if game else 'Regicide') + ' Boss'
    opponent_name = record.get('stockfish_engine', {}).get('id', {}).get('name', 'Stockfish')
    opponent_name += ' UCI_Elo ' + str(config['stockfish_elo'])
    value.headers.update(Event='Regicide boss vs limited Stockfish diagnostic', Site='local isolated match',
                         Round=str(record['game']), Result=record['result'],
                         White=boss_name if record['boss_color'] == 'white' else opponent_name,
                         Black=opponent_name if record['boss_color'] == 'white' else boss_name,
                         Termination='normal' if record['legal_result'] else 'unterminated',
                         TerminationReason=record['termination'], BossSeed=record['boss_seed'],
                         BossMoveTimeMS=str(config['boss_ms']), StockfishMoveTimeMS=str(config['stockfish_ms']),
                         StockfishUCIElo=str(config['stockfish_elo']), CapPolicy=config['cap_policy'],
                         MatchPurpose='External opponent diagnostic; not a calibrated human rating')
    return str(value) + '\n\n'


def play_one(config, entry, output, evaluation, *, boss_factory, stockfish_factory, on_progress=None):
    record = {**entry, 'started_at': utc(), 'result': '*', 'termination': 'not_started',
              'completed': False, 'legal_result': False, 'capped': False, 'error': None,
              'initial_fen': chess.STARTING_FEN, 'final_fen': chess.STARTING_FEN,
              'uci_moves': [], 'moves': [], 'plies': 0, 'boss_engines': [], 'stockfish_engine': {},
              'record_file': f"game-{entry['game']:03d}.json", 'pgn_file': f"game-{entry['game']:03d}.pgn"}
    host = opponent = None
    started = time.monotonic()
    try:
        opponent = RecordedEngine(stockfish_factory(Path(config['stockfish'])), record['stockfish_engine'])
        stockfish_options(opponent, config['stockfish_elo'], config['threads'], config['hash_mb'])
        def start_boss(path):
            details = {}
            record['boss_engines'].append(details)
            engine = RecordedEngine(boss_factory(path), details)
            options = {name: value for name, value in [('Threads', config['threads']), ('Hash', config['hash_mb'])]
                       if name in engine.options}
            try:
                if options:
                    engine.configure(options)
                return engine
            except BaseException:
                engine.close()
                raise
        host = HumanGame(Path(config['boss_binary']), output / 'host' / f"game-{entry['game']:03d}",
                         engine_factory=start_boss, hint_engine_factory=lambda _: DisplayOnlyPeer(),
                         evaluation_engine_factory=lambda _: DisplayOnlyPeer(),
                         neural_model=evaluation.get('model'), neural_adopted_generation=evaluation.get('generation'))
        human_color = 'black' if entry['boss_color'] == 'white' else 'white'
        host.new_game(human_color, config['boss_ms'], host.revision, True)
        host._opening_seed = entry['boss_seed']
        host._save()
        record.update(host_game_id=host.game_id, evaluation=host.classical_evaluation_mode if not host.neural_model else host.neural_evaluation_mode,
                      model_sha256=host.neural_sha256)
        opponent_token = object()
        trace = output / f"game-{entry['game']:03d}.moves.jsonl"
        with trace.open('x', encoding='utf-8') as stream:
            while True:
                ending = end_state(host.board, config['max_plies'], config['cap_policy'])
                if ending:
                    record.update(ending)
                    break
                before = host.board.copy(stack=True)
                move_started = time.monotonic()
                is_boss = before.turn != host.human_color
                if is_boss:
                    state = host.engine_move(host.revision)
                    if state['error']:
                        raise RuntimeError('Boss host error: ' + state['error'])
                else:
                    reply = opponent.play(before.copy(stack=True), chess.engine.Limit(time=config['stockfish_ms'] / 1000),
                                          game=opponent_token, info=chess.engine.INFO_NONE, ponder=False)
                    if reply.move is None or reply.move not in before.legal_moves:
                        raise ValueError('Stockfish returned an illegal or missing move')
                    host.human_move(reply.move.uci(), host.revision)
                if len(host.board.move_stack) != len(before.move_stack) + 1:
                    raise ValueError('Host did not append exactly one move')
                move = host.board.peek()
                if move not in before.legal_moves:
                    raise ValueError('Host returned an illegal move')
                expected = before.copy(stack=True)
                expected.push(move)
                if expected.fen(en_passant='fen') != host.board.fen(en_passant='fen') or expected.move_stack != host.board.move_stack:
                    raise ValueError('Host history diverged from independently replayed move')
                turn = {'ply': len(host.board.move_stack), 'actor': 'boss' if is_boss else 'stockfish',
                        'uci': move.uci(), 'san': before.san(move), 'elapsed_ms': round((time.monotonic() - move_started) * 1000, 3),
                        'before_fen': before.fen(en_passant='fen'), 'after_fen': host.board.fen(en_passant='fen')}
                if is_boss:
                    turn['boss_decision'] = copy.deepcopy(host._boss_decisions.get(turn['ply']))
                record['moves'].append(turn)
                record['uci_moves'].append(move.uci())
                record.update(plies=turn['ply'], final_fen=turn['after_fen'])
                stream.write(json.dumps(turn, ensure_ascii=False, allow_nan=False) + '\n')
                stream.flush()
                if on_progress:
                    on_progress(record, turn)
        record['boss_play'] = host._boss_metadata()
    except BaseException as error:
        record.update(error=f'{type(error).__name__}: {error}',
                      termination='interrupted' if isinstance(error, KeyboardInterrupt) else 'engine_error')
    finally:
        try:
            if host is not None:
                record['boss_play'] = host._boss_metadata()
            pgn = match_pgn(host, record, config)
        except Exception as error:
            record['error'] = (record['error'] + '; ' if record['error'] else '') + f'Artifact error: {type(error).__name__}: {error}'
            fallback = chess.pgn.Game.from_board(host.board) if host is not None else chess.pgn.Game()
            fallback.headers.update(Event='Match export failed; see JSON evidence', Result=record['result'])
            pgn = str(fallback) + '\n\n'
        for label, engine in [('boss_host', host), ('stockfish', opponent)]:
            if engine is None:
                continue
            try:
                engine.close() if label == 'boss_host' else engine.quit()
            except Exception as error:
                record.setdefault('cleanup_errors', []).append(f'{label}: {type(error).__name__}: {error}')
            finally:
                if label == 'stockfish':
                    try:
                        engine.close()
                    except Exception as error:
                        record.setdefault('cleanup_errors', []).append(f'{label} close: {type(error).__name__}: {error}')
        record.update(finished_at=utc(), elapsed_seconds=round(time.monotonic() - started, 3))
        (output / record['pgn_file']).write_text(pgn, encoding='utf-8')
        write_json(output / record['record_file'], record)
    return record, pgn


def run_match(config, *, boss_factory=None, stockfish_factory=None, evaluation=None):
    output = Path(config['output_dir']).resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = {**config, 'output_dir': str(output),
              'boss_binary': str(Path(config['boss_binary']).resolve(strict=True)),
              'stockfish': str(Path(config['stockfish']).resolve(strict=True))}
    entries = schedule(config['games'], config['seed'], config['stockfish_elo'])
    evaluation = evaluation if evaluation is not None else runtime_evaluation(
        config['boss_binary'], config.get('neural_model'), config.get('learning_state'), config.get('classical', False))
    metadata = {'schema': 'regicide.boss-strength-match.v1', 'started_at': utc(), 'config': config,
                'purpose': 'External opponent diagnostic, not calibrated FIDE or online-platform Elo',
                'source': source_version(), 'source_sha256': {path: sha(ROOT / path) for path in SOURCES},
                'binary_sha256': {'boss': sha(config['boss_binary']), 'stockfish': sha(config['stockfish'])},
                'python': platform.python_version(), 'python_chess': chess.__version__, 'platform': platform.platform(),
                'logical_cpus': os.cpu_count(), 'policy': boss_play.POLICY, 'opponent_policy': boss_opponent.POLICY,
                'repertoire_policy': boss_openings.POLICY, 'evaluation_selection': {**evaluation, 'model': str(evaluation['model']) if evaluation.get('model') else None},
                'protocol': {'start': 'standard starting position; no forced opening moves',
                             'draws': 'Always claim available threefold/50-move draws', 'adjudication': 'Legal chess outcomes only; no score adjudication',
                             'cap': config['cap_policy'], 'pairing': 'Alternating colors, same boss seed per pair',
                             'ponder': False, 'display_helpers': 'Non-playing no-search peers; no UI engine compute',
                             'stockfish_randomness': 'No externally controllable seed; exact moves are preserved',
                             'budget': 'Boss full configured baseline plus optional at most 1000ms extra; opponent fixed movetime'},
                'schedule': entries, 'games': [], 'status': 'running', 'errors': []}
    write_json(output / 'match.json', metadata)
    pgns = []
    def progress(record, turn):
        if turn['ply'] % 10 == 0:
            print(json.dumps({'game': record['game'], 'ply': turn['ply'], 'actor': turn['actor'], 'move': turn['uci']}, ensure_ascii=False), flush=True)
    for entry in entries:
        metadata['active_game'] = entry['game']
        write_json(output / 'match.json', metadata)
        record, pgn = play_one(config, entry, output, evaluation,
                               boss_factory=boss_factory or (lambda path: native_engine(path, boss=True)),
                               stockfish_factory=stockfish_factory or (lambda path: native_engine(path, boss=False)),
                               on_progress=progress)
        metadata['games'].append({key: value for key, value in record.items()
                                  if key not in ('moves', 'boss_play', 'uci_moves')})
        pgns.append(pgn)
        (output / 'match.pgn').write_text(''.join(pgns), encoding='utf-8')
        metadata.update(summary=counts(metadata['games']), legal_results_only=counts(metadata['games'], legal_only=True))
        if record['error']:
            metadata['errors'].append(record['error'])
        write_json(output / 'match.json', metadata)
        print(json.dumps({'game': entry['game'], 'result': record['result'], 'termination': record['termination'],
                          'plies': record['plies'], 'error': record['error']}, ensure_ascii=False), flush=True)
        if record['error']:
            break
    unchanged = all(sha(ROOT / path) == digest for path, digest in metadata['source_sha256'].items())
    binaries_unchanged = (sha(config['boss_binary']) == metadata['binary_sha256']['boss']
                          and sha(config['stockfish']) == metadata['binary_sha256']['stockfish'])
    metadata.update(finished_at=utc(), active_game=None, sources_unchanged=unchanged,
                    binaries_unchanged=binaries_unchanged,
                    status='completed' if len(metadata['games']) == len(entries) and not metadata['errors'] and unchanged and binaries_unchanged else 'failed')
    write_json(output / 'match.json', metadata)
    return metadata


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--boss-binary', type=Path)
    parser.add_argument('--stockfish', type=Path, required=True)
    parser.add_argument('--elo', dest='stockfish_elo', type=int, required=True)
    parser.add_argument('--games', type=int, default=4)
    parser.add_argument('--boss-ms', type=int, default=1000)
    parser.add_argument('--stockfish-ms', type=int, default=2000)
    parser.add_argument('--threads', type=int, default=1)
    parser.add_argument('--hash-mb', type=int, default=16)
    parser.add_argument('--max-plies', type=int, default=300)
    parser.add_argument('--cap-policy', choices=('incomplete', 'draw'), default='incomplete')
    parser.add_argument('--seed', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--classical', action='store_true')
    parser.add_argument('--neural-model', type=Path)
    parser.add_argument('--learning-state', type=Path)
    args = vars(parser.parse_args(argv))
    try:
        args['boss_binary'] = args['boss_binary'] or resolve_runtime_engine(ROOT)
        schedule(args['games'], args['seed'], args['stockfish_elo'])
        if not 500 <= args['boss_ms'] <= 5000:
            raise ValueError('boss-ms must be 500..5000 so the boss policy can run')
        if any(args[key] <= 0 for key in ('stockfish_ms', 'threads', 'hash_mb', 'max_plies')):
            raise ValueError('time, threads, hash and ply cap must be positive')
        for name in ('boss_binary', 'stockfish'):
            args[name] = args[name].expanduser().resolve(strict=True)
            if not args[name].is_file():
                raise ValueError(name + ' must be a file')
        if args['output_dir'].exists():
            raise ValueError('output-dir must be new; existing runs are never overwritten')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return {key: str(value) if isinstance(value, Path) else value for key, value in args.items()}


def main(argv=None):
    report = run_match(arguments(argv))
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
