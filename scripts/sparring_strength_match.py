"""Play production sparring mode against four limited Stockfish settings.

One color pair per UCI_Elo setting is a functional playing diagnostic, not a
rating estimate. Regicide retains its full baseline plus the production extra
budget; equal configured move times therefore do not mean equal CPU use.
Only legal outcomes count, and the ply cap remains an unfinished '*'.
"""
import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import time
from unittest.mock import patch

import chess
import chess.engine
import chess.pgn

from boss_strength_match import (DisplayOnlyPeer, RecordedEngine, counts as _counts,
                                 end_state, native_engine, sha, stockfish_options, utc, write_json)
import play_human
from play_game import source_version
from runtime_engine import resolve_runtime_engine
import sparring_play

ROOT = Path(__file__).resolve().parents[1]
RATINGS = (1320, 1700, 2100, 2500)
SOURCES = tuple('scripts/' + name for name in (
    'sparring_strength_match.py', 'boss_strength_match.py', 'play_human.py', 'sparring_play.py',
    'evaluation_stability.py', 'boss_play.py', 'boss_opponent.py', 'boss_openings.py',
    'poisoned_piece.py', 'opening_variety.py', 'dynamic_play.py', 'runtime_engine.py',
    'play_game.py', 'move_hints.py', 'opening_guide.py', 'opening_names.py', 'opening_sides.py',
    'analysis_session.py', 'coaching_session.py', 'game_archive.py', 'summarize_sparring_match.py',
    'sparring_pacing.py', 'sparring_rating.py'))
CATALOG = tuple('data/openings/' + name for name in
                ('manifest.json', 'a.tsv', 'b.tsv', 'c.tsv', 'd.tsv', 'e.tsv', 'LICENSE'))


class QuietHints:
    """Disable screen-only searches without replacing the playing engine."""
    def __init__(self, *_args, **_kwargs):
        pass

    def snapshot(self):
        return {'enabled': False, 'status': 'disabled', 'candidates': []}

    def snapshot_with_evaluation(self):
        return self.snapshot(), {}

    def set_enabled(self, _enabled):
        return self.snapshot()

    def update_position(self, **_kwargs):
        pass

    def close(self):
        pass


def schedule(ratings, seed, colors=('white', 'black')):
    """Global IDs and one seed per rating pair are invariant across shards."""
    if (not isinstance(ratings, (tuple, list)) or not ratings or len(set(ratings)) != len(ratings)
            or any(type(value) is not int or value not in RATINGS for value in ratings)):
        raise ValueError('ratings must be a nonempty unique subset of 1320 1700 2100 2500')
    if not colors or len(set(colors)) != len(colors) or any(value not in ('white', 'black') for value in colors):
        raise ValueError('colors must be a nonempty unique subset of white black')
    entries = []
    for rating in RATINGS:
        if rating not in ratings:
            continue
        pair = RATINGS.index(rating) + 1
        seed_value = hashlib.sha256(json.dumps([str(seed), 'sparring-vs-limited-stockfish', rating]).encode()).hexdigest()[:32]
        for offset, color in enumerate(('white', 'black')):
            if color in colors:
                entries.append({'game': 2 * (pair - 1) + offset + 1, 'pair': pair,
                                'stockfish_elo': rating, 'sparring_color': color,
                                'stockfish_color': 'black' if color == 'white' else 'white',
                                'sparring_seed': seed_value})
    return entries


def stockfish_score_packet(board, info, selected_move, configured_options):
    """Supplementary root evaluation, never the limited-strength chosen move's score."""
    info = info if isinstance(info, dict) else {}
    score = info.get('score')
    valid_pov = isinstance(score, chess.engine.PovScore) and score.turn == board.turn
    white = score.pov(chess.WHITE) if valid_pov else None
    pv = info.get('pv')
    pv_legal = isinstance(pv, (tuple, list)) and 0 < len(pv) <= 128
    replay, moves = board.copy(stack=True), []
    if pv_legal:
        for move in pv:
            if not isinstance(move, chess.Move) or move not in replay.legal_moves:
                pv_legal = False
                break
            moves.append(move.uci())
            replay.push(move)
    lower, upper = bool(info.get('lowerbound')), bool(info.get('upperbound'))
    if valid_pov and score.turn == chess.BLACK:
        lower, upper = upper, lower
    cp, mate = (white.score(), white.mate()) if white is not None else (None, None)
    return {'source': 'play_merged_info', 'before_fen': board.fen(en_passant='fen'),
            'score_pov': 'white', 'score_pov_valid': valid_pov,
            'reported_score_turn': ('white' if score.turn else 'black') if isinstance(score, chess.engine.PovScore) else None,
            'cp_white': cp if type(cp) is int else None, 'mate_white': mate if type(mate) is int else None,
            'winner': ('white' if white.score(mate_score=30000) > 0 else 'black') if mate is not None else None,
            'depth': info.get('depth') if type(info.get('depth')) is int else None,
            'bound': 'conflict' if lower and upper else 'lower' if lower else 'upper' if upper else 'exact',
            'pv_uci': moves, 'pv_legal': bool(pv_legal), 'selected_uci': selected_move.uci(),
            'selected_matches_pv': bool(moves and moves[0] == selected_move.uci()),
            'is_selected_move_score': False, 'iteration_identity': 'not_guaranteed_merged_play_info',
            'configured_options': copy.deepcopy(configured_options),
            'search_counters': {key: info[key] for key in ('nodes', 'nps', 'time', 'seldepth', 'hashfull', 'tbhits', 'multipv')
                                if type(info.get(key)) in (int, float)},
            'scope': 'Stockfish search of this pre-move root; not an evaluation of its weakened selected move; play info can merge iterations'}


def counts(records):
    return _counts([{**row, 'boss_color': row['sparring_color']} for row in records], legal_only=True)


def match_pgn(host, record, config):
    game = chess.pgn.read_game(io.StringIO(host.pgn())) if host is not None else chess.pgn.Game()
    if game is None or game.errors:
        raise ValueError('Production PGN could not be replayed')
    own = (host.engine_name if host is not None else 'Regicide') + ' Sparring'
    opponent = record['stockfish_engine'].get('id', {}).get('name', 'Stockfish') + ' UCI_Elo ' + str(record['stockfish_elo'])
    game.headers.update(Event='Regicide sparring vs limited Stockfish diagnostic', Site='local isolated match',
                        Round=str(record['game']), Pair=str(record['pair']), Result=record['result'],
                        White=own if record['sparring_color'] == 'white' else opponent,
                        Black=own if record['sparring_color'] == 'black' else opponent,
                        SparringMode='1', SparringPolicy=sparring_play.POLICY, SparringSeed=record['sparring_seed'],
                        SparringMoveTimeMS=str(config['sparring_ms']), SparringMaxExtraMS=str(sparring_play.MAX_EXTRA_MS),
                        StockfishMoveTimeMS=str(config['stockfish_ms']), StockfishUCIElo=str(record['stockfish_elo']),
                        Termination='normal' if record['legal_result'] else 'unterminated',
                        TerminationReason=record['termination'], CapPolicy='incomplete',
                        MatchPurpose='Practice-mode diagnostic; unequal extra budgets; no human rating claim')
    return str(game) + '\n\n'


def validate_record(record, pgn):
    """Independent replay and receipt checks; corrupted evidence cannot score a win."""
    if record.get('initial_fen') != chess.STARTING_FEN:
        raise ValueError('Nonstandard initial position')
    board, ucis, decisions = chess.Board(), [], []
    for ply, row in enumerate(record['moves'], 1):
        expected_actor = 'sparring' if (board.turn == chess.WHITE) == (record['sparring_color'] == 'white') else 'stockfish'
        if row['ply'] != ply or row['actor'] != expected_actor or row['before_fen'] != board.fen(en_passant='fen'):
            raise ValueError('Move identity or actor mismatch')
        move = chess.Move.from_uci(row['uci'])
        if move not in board.legal_moves or row['san'] != board.san(move):
            raise ValueError('Illegal move or SAN mismatch')
        if row['actor'] == 'sparring':
            decision = row.get('sparring_decision')
            if (not isinstance(decision, dict) or decision.get('policy') != sparring_play.POLICY
                    or decision.get('ply') != ply or decision.get('before_fen') != row['before_fen']
                    or decision.get('history_uci') != ucis or decision.get('selected_uci') != row['uci']
                    or decision.get('baseline_budget_ms') != row['baseline_budget_ms']
                    or decision.get('phase') != sparring_play.phase(board)):
                raise ValueError('Sparring decision does not match the played move')
            decisions.append(decision)
        else:
            packet = row.get('stockfish_score_packet')
            if not isinstance(packet, dict) or packet.get('before_fen') != row['before_fen'] or packet.get('selected_uci') != row['uci']:
                raise ValueError('Stockfish score packet belongs to a different move')
        board.push(move)
        ucis.append(move.uci())
        if row['after_fen'] != board.fen(en_passant='fen'):
            raise ValueError('Post-move FEN mismatch')
    if ucis != record['uci_moves'] or len(ucis) != record['plies'] or board.fen(en_passant='fen') != record['final_fen']:
        raise ValueError('Final position or full history mismatch')
    stream = io.StringIO(pgn)
    game = chess.pgn.read_game(stream)
    if (game is None or game.errors or chess.pgn.read_game(stream) is not None
            or game.board().fen(en_passant='fen') != chess.STARTING_FEN
            or [move.uci() for move in game.mainline_moves()] != ucis
            or game.headers.get('Result') != record['result']):
        raise ValueError('PGN and JSON disagree')
    if not record['error']:
        host = record['host']
        if (host.get('final_fen') != record['final_fen'] or host.get('uci_moves') != ucis
                or host.get('sparring_play', {}).get('enabled') is not True
                or host['sparring_play'].get('policy') != sparring_play.POLICY
                or host['sparring_play'].get('seed') != record['sparring_seed']
                or host['sparring_play'].get('decisions') != decisions
                or any(host.get(name, {}).get('enabled') is not False for name in ('boss_play', 'dynamic_play', 'opening_variety'))
                or host.get('model_sha256') is not None):
            raise ValueError('Production host mode, model or history metadata mismatch')
        expected = end_state(board, record['max_plies'], 'incomplete')
        if expected is None or any(record.get(key) != value for key, value in expected.items()):
            raise ValueError('Result is not the legal outcome or an unfinished cap')
        if game.headers.get('SparringMode') != '1' or game.headers.get('SparringPolicy') != sparring_play.POLICY:
            raise ValueError('PGN lost the production mode identity')
    elif record['result'] != '*' or record['completed'] or record['legal_result']:
        raise ValueError('An error was incorrectly counted as a completed result')
    return {'passed': True, 'plies': len(ucis), 'sparring_decisions': len(decisions),
            'pgn_history_matches': True, 'result_checked': not bool(record['error'])}


def play_one(config, entry, output, *, sparring_factory, stockfish_factory, on_progress=None):
    record = {**entry, 'started_at': utc(), 'result': '*', 'termination': 'not_started',
              'completed': False, 'legal_result': False, 'capped': False, 'error': None,
              'initial_fen': chess.STARTING_FEN, 'final_fen': chess.STARTING_FEN,
              'uci_moves': [], 'moves': [], 'plies': 0, 'max_plies': config['max_plies'],
              'host': {'engines': [], 'save_directory': f"host/game-{entry['game']:03d}"},
              'stockfish_engine': {}, 'record_file': f"game-{entry['game']:03d}.json",
              'pgn_file': f"game-{entry['game']:03d}.pgn"}
    board, host, opponent, owned = chess.Board(), None, None, []
    started = time.monotonic()

    def failed(message, termination='engine_error'):
        record.update(error=(record['error'] + '; ' if record['error'] else '') + message,
                      result='*', completed=False, legal_result=False, termination=termination)

    try:
        opponent = RecordedEngine(stockfish_factory(Path(config['stockfish'])), record['stockfish_engine'])
        owned.append(opponent)
        configured_sf = stockfish_options(opponent, entry['stockfish_elo'], config['threads'], config['hash_mb'])

        def start_sparring(path):
            details = {}
            record['host']['engines'].append(details)
            engine = RecordedEngine(sparring_factory(path), details)
            owned.append(engine)
            if not {'Hash', 'UsePieceActivity'}.issubset(engine.options):
                raise ValueError('Playing engine must advertise Hash and UsePieceActivity')
            options = {'Hash': config['hash_mb']}
            if 'Threads' in engine.options:
                options['Threads'] = config['threads']
            engine.configure(options)
            return engine

        with patch.object(play_human, 'MoveHints', QuietHints):
            host = play_human.HumanGame(Path(config['binary']), output / record['host']['save_directory'],
                                       engine_factory=start_sparring, neural_model=None,
                                       hint_engine_factory=lambda _: DisplayOnlyPeer(),
                                       evaluation_engine_factory=lambda _: DisplayOnlyPeer(),
                                       opening_variety_enabled=True, dynamic_play_enabled=True)
        host.new_game(entry['stockfish_color'], config['sparring_ms'], host.revision,
                      boss_mode=False, sparring_mode=True)
        host._opening_seed = entry['sparring_seed']
        host._save()
        record['host'].update(game_id=host.game_id, evaluation=host.classical_evaluation_mode,
                              model_sha256=host.neural_sha256)
        token = object()
        with (output / f"game-{entry['game']:03d}.moves.jsonl").open('x', encoding='utf-8') as stream:
            while True:
                if host.board.fen(en_passant='fen') != board.fen(en_passant='fen') or host.board.move_stack != board.move_stack:
                    raise ValueError('Host and independent replay diverged')
                ending = end_state(board, config['max_plies'], 'incomplete')
                if ending:
                    record.update(ending)
                    break
                before = board.copy(stack=True)
                is_sparring = before.turn != host.human_color
                move_started = time.monotonic()
                sf_packet, relay_ms = None, 0.0
                if is_sparring:
                    state = host.engine_move(host.revision)
                    search_ms = (time.monotonic() - move_started) * 1000
                    if state['error']:
                        raise RuntimeError('Sparring host error: ' + state['error'])
                else:
                    reply = opponent.play(before.copy(stack=True), chess.engine.Limit(time=config['stockfish_ms'] / 1000),
                                          game=token, info=chess.engine.INFO_ALL, ponder=False)
                    search_ms = (time.monotonic() - move_started) * 1000
                    if reply.move is None or reply.move not in before.legal_moves:
                        raise ValueError('Stockfish returned an illegal or missing move')
                    sf_packet = stockfish_score_packet(before, reply.info, reply.move, configured_sf)
                    relay_started = time.monotonic()
                    host.human_move(reply.move.uci(), host.revision)
                    relay_ms = (time.monotonic() - relay_started) * 1000
                if len(host.board.move_stack) != len(before.move_stack) + 1:
                    raise ValueError('Host did not append exactly one move')
                move = host.board.peek()
                if move not in before.legal_moves:
                    raise ValueError('Host returned an illegal move')
                board.push(move)
                if host.board.fen(en_passant='fen') != board.fen(en_passant='fen') or host.board.move_stack != board.move_stack:
                    raise ValueError('Host history differs from independent move replay')
                turn = {'ply': len(board.move_stack), 'actor': 'sparring' if is_sparring else 'stockfish',
                        'uci': move.uci(), 'san': before.san(move), 'before_fen': before.fen(en_passant='fen'),
                        'after_fen': board.fen(en_passant='fen'),
                        'elapsed_ms': round((time.monotonic() - move_started) * 1000, 3),
                        'search_elapsed_ms': round(search_ms, 3), 'relay_elapsed_ms': round(relay_ms, 3),
                        'baseline_budget_ms': config['sparring_ms'] if is_sparring else config['stockfish_ms'],
                        'advertised_extra_budget_ms': sparring_play.extra_budget(config['sparring_ms'], ply=len(before.move_stack)) if is_sparring else 0,
                        'sparring_decision': copy.deepcopy(host._sparring_decisions.get(len(board.move_stack))) if is_sparring else None,
                        'stockfish_score_packet': sf_packet}
                record['moves'].append(turn)
                record['uci_moves'].append(move.uci())
                record.update(plies=turn['ply'], final_fen=turn['after_fen'])
                stream.write(json.dumps(turn, ensure_ascii=False, allow_nan=False) + '\n')
                stream.flush()
                if on_progress:
                    on_progress(record, turn)
    except BaseException as error:
        failed(f'{type(error).__name__}: {error}', 'interrupted' if isinstance(error, KeyboardInterrupt) else 'engine_error')
    finally:
        if host is not None:
            try:
                record['host'].update(final_fen=host.board.fen(en_passant='fen'),
                                      uci_moves=[move.uci() for move in host.board.move_stack],
                                      sparring_play=host._sparring_metadata(), boss_play=host._boss_metadata(),
                                      dynamic_play=host._dynamic_metadata(), opening_variety=host._opening_metadata())
                host.close()
                sidecar = host.save_dir / (host.game_id + '.json')
                saved = json.loads(sidecar.read_text(encoding='utf-8'))
                if (saved['final_fen'] != record['host']['final_fen'] or saved['uci_moves'] != record['host']['uci_moves']
                        or saved['sparring_play'] != record['host']['sparring_play']):
                    raise ValueError('Durable production record differs from captured host metadata')
                record['host'].update(sidecar_file=str(sidecar.relative_to(output)), sidecar_sha256=sha(sidecar))
            except Exception as error:
                failed('Host finalization: ' + str(error))
        for engine in owned:
            try:
                engine.close()
            except Exception as error:
                failed('Engine cleanup: ' + str(error))
        try:
            pgn = match_pgn(host, record, config)
            record['audit'] = validate_record(record, pgn)
        except Exception as error:
            failed('Artifact validation: ' + str(error), 'artifact_error')
            record['audit'] = {'passed': False, 'error': str(error)}
            fallback = chess.pgn.Game.from_board(board)
            fallback.headers.update(Event='Sparring diagnostic export failure; see JSON', Result='*')
            pgn = str(fallback) + '\n\n'
        record.update(finished_at=utc(), elapsed_seconds=round(time.monotonic() - started, 3))
        (output / record['pgn_file']).write_text(pgn, encoding='utf-8')
        write_json(output / record['record_file'], record)
    return record, pgn


def run_match(config, *, sparring_factory=None, stockfish_factory=None):
    config = {**config, 'binary': str(Path(config['binary']).resolve(strict=True)),
              'stockfish': str(Path(config['stockfish']).resolve(strict=True)),
              'output_dir': str(Path(config['output_dir']).resolve()),
              'colors': list(config.get('colors', ('white', 'black'))),
              'threads': 1, 'hash_mb': 16, 'cap_policy': 'incomplete', 'classical': True}
    entries = schedule(config['ratings'], config['seed'], config['colors'])
    if any(type(config[key]) is not int or not 600 <= config[key] <= 5000 for key in ('sparring_ms',)):
        raise ValueError('sparring-ms must be 600..5000')
    if any(type(config[key]) is not int or config[key] < 1 for key in ('stockfish_ms', 'max_plies')):
        raise ValueError('stockfish-ms and max-plies must be positive integers')
    source = source_version()
    if config.get('expected_source_commit') and (source['commit'] != config['expected_source_commit'] or source['dirty']):
        raise ValueError('Expected a clean checkout at the frozen source commit')
    binary_hashes = {'regicide': sha(config['binary']), 'stockfish': sha(config['stockfish'])}
    if config.get('expected_binary_sha256') and config['expected_binary_sha256'] != binary_hashes['regicide']:
        raise ValueError('Regicide binary differs from the expected runtime SHA256')
    output = Path(config['output_dir'])
    output.mkdir(parents=True, exist_ok=False)
    target_fullmoves = {'regicide-sparring-play-v1': 50, 'regicide-sparring-play-v2': 40,
                       'regicide-sparring-play-v3': 30, 'regicide-sparring-play-v4': 20,
                       'regicide-sparring-play-v5': 20, 'regicide-sparring-play-v6': 20,
                       'regicide-sparring-play-v7': 15}[sparring_play.POLICY]
    target_plies = 2 * target_fullmoves
    metadata = {'schema': 'regicide.sparring-strength-match.v1', 'started_at': utc(), 'config': config,
                'source': source, 'source_sha256': {path: sha(ROOT / path) for path in SOURCES},
                'catalog_sha256': {path: sha(ROOT / path) for path in CATALOG},
                'binary_sha256': binary_hashes, 'policy': sparring_play.POLICY,
                'python': platform.python_version(), 'python_chess': chess.__version__,
                'platform': platform.platform(), 'logical_cpus': os.cpu_count(),
                'protocol': {'start': 'standard chess position, no forced opening or handicap',
                             'host': 'production HumanGame.new_game(sparring_mode=True), classical activity evaluation',
                             'draws': 'Always claim available threefold repetition or fifty-move draws',
                             'adjudication': 'Legal chess outcomes only; caps/errors remain unfinished and unscored',
                             'pairing': 'One seed per rating shared across colors; global IDs invariant across shards',
                             'stockfish_randomness': 'No exposed seed control; every actual move is preserved',
                             'budget': 'Regicide full baseline plus up to 2000ms extra; Stockfish fixed movetime; unequal CPU use',
                             'timing': 'Wall time, not CPU; host timing includes its own persistence; SF relay recorded separately',
                             'screen_helpers': 'QuietHints and DisplayOnlyPeer; no background display search',
                             'stockfish_scores': 'Supplementary pre-move root evaluations from merged play info, not chosen weakened move values',
                             'close_cp': 100,
                             'target_fullmoves': target_fullmoves,
                             'measurements': f'Actual concessions, {target_plies}-ply reach, pre-{target_plies}-ply abs CP <=100 on known roots, continuous band coverage, delayed recovery, ramp/finish and legal WDL',
                             'claims': 'Small diagnostic only; no Elo or perceived-drama inference', 'ponder': False},
                'schedule': entries, 'games': [], 'status': 'running', 'errors': []}
    write_json(output / 'match.json', metadata)
    pgns = []

    def unchanged():
        return (all(sha(ROOT / path) == value for path, value in metadata['source_sha256'].items()),
                all(sha(ROOT / path) == value for path, value in metadata['catalog_sha256'].items()),
                sha(config['binary']) == binary_hashes['regicide'] and sha(config['stockfish']) == binary_hashes['stockfish'])

    def progress(record, turn):
        if turn['ply'] % 10 == 0:
            print(json.dumps({'event': 'move', 'game': record['game'], 'elo': record['stockfish_elo'],
                              'ply': turn['ply'], 'actor': turn['actor'], 'uci': turn['uci'],
                              'elapsed_ms': turn['elapsed_ms']}, ensure_ascii=False), flush=True)

    for entry in entries:
        if not all(unchanged()):
            metadata['errors'].append('Frozen sources, catalog or binaries changed before a game')
            break
        metadata['active_game'] = entry['game']
        write_json(output / 'match.json', metadata)
        print(json.dumps({'event': 'game_started', **entry}, ensure_ascii=False), flush=True)
        record, pgn = play_one(config, entry, output,
                               sparring_factory=sparring_factory or (lambda path: native_engine(path, boss=True)),
                               stockfish_factory=stockfish_factory or (lambda path: native_engine(path, boss=False)),
                               on_progress=progress)
        metadata['games'].append({key: value for key, value in record.items() if key not in ('moves', 'host', 'uci_moves')})
        pgns.append(pgn)
        (output / 'match.pgn').write_text(''.join(pgns), encoding='utf-8')
        metadata['summary'] = counts(metadata['games'])
        if record['error']:
            metadata['errors'].append(record['error'])
        write_json(output / 'match.json', metadata)
        print(json.dumps({'event': 'game_finished', 'game': entry['game'], 'elo': entry['stockfish_elo'],
                          'result': record['result'], 'termination': record['termination'],
                          'plies': record['plies'], 'error': record['error']}, ensure_ascii=False), flush=True)
        if record['error']:
            break
    source_ok, catalog_ok, binaries_ok = unchanged()
    metadata.update(finished_at=utc(), active_game=None, sources_unchanged=source_ok,
                    catalog_unchanged=catalog_ok, binaries_unchanged=binaries_ok,
                    status='completed' if len(metadata['games']) == len(entries) and not metadata['errors']
                    and source_ok and catalog_ok and binaries_ok else 'failed')
    write_json(output / 'match.json', metadata)
    return metadata


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--stockfish', type=Path, required=True)
    parser.add_argument('--ratings', nargs='+', type=int, default=list(RATINGS))
    parser.add_argument('--colors', nargs='+', choices=('white', 'black'), default=['white', 'black'])
    parser.add_argument('--sparring-ms', type=int, default=1000)
    parser.add_argument('--stockfish-ms', type=int, default=1000)
    parser.add_argument('--max-plies', type=int, default=220)
    parser.add_argument('--seed', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--expected-source-commit')
    parser.add_argument('--expected-binary-sha256')
    args = vars(parser.parse_args(argv))
    try:
        args['binary'] = (args['binary'] or resolve_runtime_engine(ROOT)).expanduser().resolve(strict=True)
        args['stockfish'] = args['stockfish'].expanduser().resolve(strict=True)
        schedule(args['ratings'], args['seed'], args['colors'])
        if not 600 <= args['sparring_ms'] <= 5000 or args['stockfish_ms'] < 1 or args['max_plies'] < 1:
            raise ValueError('sparring-ms 600..5000; opponent time and cap positive')
        if not args['binary'].is_file() or not args['stockfish'].is_file():
            raise ValueError('Both engine paths must be files')
        if args['output_dir'].exists():
            raise ValueError('output-dir must be new; existing records are never overwritten')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return {key: str(value) if isinstance(value, Path) else value for key, value in args.items()}


def main(argv=None):
    return 0 if run_match(arguments(argv))['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
