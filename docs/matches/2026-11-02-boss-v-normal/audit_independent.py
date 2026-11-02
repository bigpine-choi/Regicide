"""Read frozen completed records only; never launch engines or mutate the trial."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import statistics
import sys

import chess
import chess.pgn

TRIAL = Path(__file__).resolve().parent
ROOT = TRIAL.parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from boss_strength_summary import score_statistics

COMMIT = 'dcc40800c9e42f652864dcc145e9a59b46272d2c'
SEED = '20261102boss-v-normal-v1'
SHARDS = ('pairs-1-2', 'pairs-3-4', 'pairs-5-6', 'pairs-7-8')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def expected_entry(pair, color):
    return {'game': 2 * (pair - 1) + (1 if color == 'white' else 2),
            'pair': pair, 'boss_color': color,
            'normal_color': 'black' if color == 'white' else 'white',
            **{mode + '_seed': hashlib.sha256(json.dumps(
                [SEED, 'boss-vs-normal', mode, pair]).encode()).hexdigest()[:32]
               for mode in ('boss', 'normal')}}


def parse_pgn(raw):
    stream = io.StringIO(raw)
    games = []
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            return games
        if game.errors:
            raise ValueError('PGN parser errors: ' + str(game.errors))
        games.append(game)


def timing(values):
    values = sorted(values)
    if not values:
        return {'moves': 0}
    return {'moves': len(values), 'sum_ms': sum(values), 'mean_ms': statistics.mean(values),
            'median_ms': statistics.median(values), 'p95_nearest_rank_ms': values[math.ceil(.95 * len(values)) - 1],
            'max_ms': values[-1]}


def paired_sign_test(values):
    values = list(values)
    positive = sum(value > .5 for value in values)
    negative = sum(value < .5 for value in values)
    ties = len(values) - positive - negative
    n = positive + negative
    pvalue = (min(1., 2 * sum(math.comb(n, k) for k in range(min(positive, negative) + 1)) / 2 ** n)
              if n else None)
    return {'positive_pairs': positive, 'negative_pairs': negative, 'ties_excluded': ties,
            'non_tied_pairs': n, 'two_sided_exact_p': pvalue,
            'method': 'Exact binomial sign test of pair mean above/below 0.5; ties excluded.',
            'assumptions': 'Independent/exchangeable pairs with equal sign probabilities under the null; fixed few seeds and standard starts limit generalization.',
            'scope': 'Supplementary directional evidence, not a calibrated win-rate interval or effect size.'}


def audit(partial=False):
    errors, audited, inputs, timings = [], [], [], defaultdict(list)
    schedules, records, seen = [], [], set()
    all_finished = True
    frozen_hashes = None

    def check(condition, message):
        if not condition:
            errors.append(message)

    for shard_index, name in enumerate(SHARDS):
        directory = TRIAL / name
        meta_path = directory / 'match.json'
        metadata = load(meta_path)
        inputs.append({'path': str(meta_path), 'sha256': sha(meta_path)})
        config = metadata['config']
        check(metadata['schema'] == 'regicide.boss-mode-match.v1', name + ': schema')
        check(metadata['source']['commit'] == COMMIT and metadata['source']['dirty'] is False, name + ': source freeze')
        for key, value in {'pairs': 2, 'pair_start': shard_index * 2 + 1, 'seed': SEED,
                           'boss_ms': 1000, 'normal_ms': 1000, 'threads': 1, 'hash_mb': 16,
                           'max_plies': 300, 'cap_policy': 'incomplete', 'classical': True}.items():
            check(config.get(key) == value, name + ': config ' + key)
        expected_schedule = [expected_entry(pair, color) for pair in range(shard_index * 2 + 1, shard_index * 2 + 3)
                             for color in ('white', 'black')]
        check(metadata['schedule'] == expected_schedule, name + ': schedule differs')
        schedules.extend(metadata['schedule'])
        fingerprint = (metadata['source_sha256'], metadata['catalog_sha256'], metadata['binary_sha256'])
        if frozen_hashes is None:
            frozen_hashes = fingerprint
            for path, digest in {**fingerprint[0], **fingerprint[1]}.items():
                check(sha(ROOT / path) == digest, 'Current frozen input changed: ' + path)
            check(sha(config['binary']) == fingerprint[2], 'Current runtime binary changed')
        check(fingerprint == frozen_hashes, name + ': different frozen inputs')
        finished = metadata.get('status') in ('completed', 'failed') and metadata.get('active_game') is None
        all_finished &= finished
        if finished:
            for key in ('sources_unchanged', 'catalog_unchanged', 'binary_unchanged'):
                check(metadata.get(key) is True, name + ': ' + key)
        elif not partial:
            check(False, name + ': still running; final audit refused')
        shard_games = metadata['games']
        if finished:
            check(len(shard_games) == 4, name + ': scheduled games not all recorded')
        shard_pgn_path = directory / 'match.pgn'
        # A running writer updates the combined PGN just before match.json.
        # Partial audits use immutable individual game files; audit the whole
        # combined file after that shard finishes to avoid a snapshot race.
        combined = (parse_pgn(shard_pgn_path.read_text(encoding='utf-8'))
                    if finished and shard_pgn_path.exists() else [])
        if finished:
            check(len(combined) == len(shard_games), name + ': combined PGN count')
            if shard_pgn_path.exists():
                inputs.append({'path': str(shard_pgn_path), 'sha256': sha(shard_pgn_path)})
        for offset, top in enumerate(shard_games):
            number = top['game']
            label = f'game {number}'
            record_path, pgn_path = directory / top['record_file'], directory / top['pgn_file']
            trace_path = directory / f'game-{number:03d}.moves.jsonl'
            record = load(record_path)
            before_errors = len(errors)
            check(number not in seen, label + ': duplicate global game')
            seen.add(number)
            expected = expected_entry(top['pair'], top['boss_color'])
            for key, value in expected.items():
                check(record.get(key) == value and top.get(key) == value, label + ': identity ' + key)
            for key, value in top.items():
                check(record.get(key) == value, label + ': match/record mismatch ' + key)
            check(record['initial_fen'] == chess.STARTING_FEN, label + ': nonstandard start')
            trace = [json.loads(line) for line in trace_path.read_text(encoding='utf-8').splitlines() if line.strip()]
            check(trace == record['moves'], label + ': JSONL trace mismatch')
            moves = record['moves']
            check(len(moves) == len(record['uci_moves']) == record['plies'], label + ': move counts')
            board = chess.Board()
            legal = True
            for index, row in enumerate(moves):
                ply = index + 1
                check(board.outcome(claim_draw=True) is None, label + f': move after claimable terminal at ply {ply}')
                check(row['ply'] == ply and row['before_fen'] == board.fen(en_passant='fen'), label + f': before position {ply}')
                actor = 'boss' if board.turn == (record['boss_color'] == 'white') else 'normal'
                check(row['actor'] == actor, label + f': actor {ply}')
                move = chess.Move.from_uci(row['uci'])
                if move not in board.legal_moves:
                    check(False, label + f': illegal move {ply}')
                    legal = False
                    break
                check(row['san'] == board.san(move) and record['uci_moves'][index] == row['uci'], label + f': SAN/UCI {ply}')
                board.push(move)
                check(row['after_fen'] == board.fen(en_passant='fen'), label + f': after position {ply}')
                check(row['baseline_budget_ms'] == 1000, label + f': baseline budget {ply}')
                extra = row['advertised_extra_budget_ms']
                check(type(extra) in (float, int) and 0 <= extra <= 1000, label + f': extra budget {ply}')
                for key in ('elapsed_ms', 'relay_elapsed_ms'):
                    value = row[key]
                    check(type(value) in (int, float) and math.isfinite(value) and value >= 0, label + f': invalid {key}')
                    timings[actor + '_' + key].append(value)
                timings[actor + '_extra_budget_ms'].append(extra)
            check(board.fen(en_passant='fen') == record['final_fen'], label + ': final position')
            outcome = board.outcome(claim_draw=True) if legal else None
            if record['error'] is not None:
                category = 'interrupted' if record['termination'] == 'interrupted' else 'failed'
                check(record['result'] == '*' and not record['completed'] and not record['legal_result'], label + ': error scored as result')
            elif outcome is not None:
                category = 'legal_completed'
                check(record['result'] == outcome.result() and record['termination'] == outcome.termination.name.lower(), label + ': wrong legal outcome')
                check(record['completed'] is True and record['legal_result'] is True and record['capped'] is False, label + ': outcome flags')
            elif record['capped']:
                category = 'capped'
                check(record['plies'] == 300 and record['result'] == '*' and not record['completed'] and not record['legal_result'], label + ': cap counted as completed')
            else:
                category = 'other_incomplete'
                check(record['result'] == '*' and not record['completed'] and not record['legal_result'], label + ': incomplete counted as completed')
            individual = parse_pgn(pgn_path.read_text(encoding='utf-8'))
            check(len(individual) == 1, label + ': individual PGN count')
            if individual:
                pgn = individual[0]
                check(pgn.board().fen(en_passant='fen') == chess.STARTING_FEN, label + ': PGN initial position')
                check([move.uci() for move in pgn.mainline_moves()] == record['uci_moves'], label + ': PGN/JSON moves')
                check(pgn.end().board().fen(en_passant='fen') == record['final_fen'], label + ': PGN final position')
                expected_headers = {'Round': str(number), 'Pair': str(record['pair']), 'Result': record['result'],
                                    'BossSeed': record['boss_seed'], 'NormalSeed': record['normal_seed'],
                                    'BossMoveTimeMS': '1000', 'NormalMoveTimeMS': '1000',
                                    'White': 'Regicide ' + ('Boss' if record['boss_color'] == 'white' else 'Normal'),
                                    'Black': 'Regicide ' + ('Boss' if record['boss_color'] == 'black' else 'Normal'),
                                    'TerminationReason': record['termination'],
                                    'Termination': 'normal' if record['legal_result'] else 'unterminated'}
                for key, value in expected_headers.items():
                    check(pgn.headers.get(key) == value, label + ': PGN header ' + key)
                if offset < len(combined):
                    check(str(combined[offset]) == str(pgn), label + ': combined PGN differs')
            mode_flags = {}
            for mode in ('boss', 'normal'):
                host = record['hosts'][mode]
                check(host['final_fen'] == record['final_fen'] and host['uci_moves'] == record['uci_moves'], label + ': host history ' + mode)
                flags = {key: host[key]['enabled'] for key in ('boss_play', 'dynamic_play', 'opening_variety')}
                expected_flags = {'boss_play': mode == 'boss', 'dynamic_play': mode == 'normal', 'opening_variety': False}
                check(flags == expected_flags, label + ': effective policy flags ' + mode)
                check(host['evaluation'] == 'classical_activity_v1' and host['model_sha256'] is None,
                      label + ': evaluator ' + mode)
                mode_flags[mode] = flags
            score = None
            if category == 'legal_completed':
                score = .5 if record['result'] == '1/2-1/2' else float(record['result'] == ('1-0' if record['boss_color'] == 'white' else '0-1'))
            row_audit = {**expected, 'result': record['result'], 'termination': record['termination'],
                         'category': category, 'boss_score': score, 'plies': record['plies'],
                         'effective_mode_flags': mode_flags,
                         'passed': len(errors) == before_errors,
                         'artifacts': [{'path': str(path), 'sha256': sha(path)} for path in (record_path, pgn_path, trace_path)]}
            audited.append(row_audit)
            records.append(record)
    check(sorted(entry['game'] for entry in schedules) == list(range(1, 17)), 'Full schedule IDs must be 1..16')
    if not partial:
        check(seen == set(range(1, 17)), 'Not all 16 scheduled game records are present')
        check(Counter(row['boss_color'] for row in audited) == {'white': 8, 'black': 8}, 'Final boss color counts must be 8 each')
    report = {'schema': 'regicide.boss-v-normal.independent-pgn-audit.v1',
              'checked_at': datetime.now(timezone.utc).isoformat(), 'partial': partial,
              'source_freeze': COMMIT, 'all_shards_finished': all_finished,
              'auditor_sha256': sha(__file__), 'input_matches': inputs,
              'games_checked': len(audited), 'games': audited, 'errors': errors,
              'passed': not errors, 'scope': 'Independent PGN, JSON and JSONL replay, legal outcomes, pairing and frozen-input consistency; no engines run.'}
    completed = [row for row in audited if row['category'] == 'legal_completed' and row['passed']]
    scores = [row['boss_score'] for row in completed]
    pairs = []
    for pair_id in range(1, 9):
        pair = [row for row in audited if row['pair'] == pair_id]
        included = (len(pair) == 2 and {row['boss_color'] for row in pair} == {'white', 'black'}
                    and all(row['category'] == 'legal_completed' and row['passed'] for row in pair))
        pairs.append({'pair': pair_id, 'games': [row['game'] for row in pair], 'included': included,
                      'boss_mean_score': sum(row['boss_score'] for row in pair) / 2 if included else None})
    pair_stats = score_statistics(row['boss_mean_score'] for row in pairs if row['included'])
    sign = paired_sign_test(row['boss_mean_score'] for row in pairs if row['included'])
    interval, mean = pair_stats['hoeffding95_independent_units'], pair_stats['mean']
    conclusion = ('insufficient_completed_pairs' if interval is None else
                  'boss_direction_supported_under_independent_pairs' if interval[0] > .5 else
                  'normal_direction_supported_under_independent_pairs' if interval[1] < .5 else
                  'ordering_not_established')
    directional = (sign['two_sided_exact_p'] is not None and sign['two_sided_exact_p'] <= .05)
    summary = {'schema': 'regicide.boss-v-normal.independent-summary.v1',
               'source_freeze': COMMIT, 'audit_passed': report['passed'], 'partial': partial,
               'scheduled_games': 16, 'recorded_games': len(audited),
               'recorded_boss_color_counts': dict(Counter(row['boss_color'] for row in audited)),
               'legal_completed_games': len(completed), 'categories': dict(Counter(row['category'] for row in audited)),
               'terminations': dict(Counter(row['termination'] for row in audited)),
               'total_plies': sum(row['plies'] for row in audited),
               'boss_wdl': {'wins': scores.count(1.), 'draws': scores.count(.5), 'losses': scores.count(0.)},
               'normal_wdl': {'wins': scores.count(0.), 'draws': scores.count(.5), 'losses': scores.count(1.)},
               'boss_score_statistics': score_statistics(scores), 'pairs': pairs,
               'boss_paired_score_statistics': pair_stats, 'paired_sign_test': sign,
               'bounded_interval_judgment': conclusion,
               'uncertainty_judgment': ('insufficient_completed_pairs' if mean is None else
                                        'directional_evidence_under_pair_assumptions' if directional else
                                        'observed_direction_only'),
               'observed_direction': None if mean is None else ('boss' if mean > .5 else 'normal' if mean < .5 else 'tied'),
               'by_boss_color': {color: {'games': len(color_rows := [row for row in completed if row['boss_color'] == color]),
                                        'wins': sum(row['boss_score'] == 1 for row in color_rows),
                                        'draws': sum(row['boss_score'] == .5 for row in color_rows),
                                        'losses': sum(row['boss_score'] == 0 for row in color_rows)} for color in ('white', 'black')},
               'timing': {key: timing(values) for key, values in timings.items()},
               'conditions': {'baseline_ms_each': 1000, 'same_binary': frozen_hashes[2], 'hash_mb_each': 16,
                              'threads_each': 1, 'standard_start': True, 'forced_openings': False,
                              'fixed_seed_pairs': 8, 'shards_concurrent': 4,
                              'host_configuration_dynamic_and_opening_variety_enabled_each': True,
                              'effective_policy_boss': 'boss path; dynamic and standalone opening metadata disabled by priority',
                              'effective_policy_normal': 'dynamic path, which owns opening variety; standalone opening metadata disabled',
                              'equal_CPU_budget': False},
               'caveats': ['Fixed 16-game screening run; no automatic sample expansion.',
                           'Eight units are seed/color pairs, not fixed opening positions.',
                           'Hoeffding bounds assume independent pairs and are conservative; shared PC load is not fully isolated.',
                           'The exact paired sign test is supplementary: a wide Hoeffding interval alone does not negate consistent pair directions.',
                           'Normal intervals are descriptive; zero sample variance does not establish certainty.',
                           'Only legal completed audited games are scored; incomplete pairs stay out of paired statistics.',
                           'The two app modes have different optional extra search; elapsed values are wall time, not CPU time.',
                           'No human Elo, platform rating, or logistic Elo conversion is estimated.']}
    return report, summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--partial', action='store_true')
    args = parser.parse_args()
    report, summary = audit(args.partial)
    if not args.partial:
        if not report['all_shards_finished']:
            raise SystemExit('Refusing final artifacts while any shard is running')
        for name, data in (('all16-pgn-audit-independent.json', report), ('summary-independent.json', summary)):
            with (TRIAL / name).open('x', encoding='utf-8') as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.write('\n')
    print(json.dumps({'games_checked': report['games_checked'], 'passed': report['passed'],
                      'errors': report['errors'], 'boss_wdl': summary['boss_wdl'],
                      'completed_pairs': summary['boss_paired_score_statistics']['n'],
                      'mean': summary['boss_paired_score_statistics']['mean'],
                      'paired95': summary['boss_paired_score_statistics']['hoeffding95_independent_units'],
                      'paired_sign_test': summary['paired_sign_test'],
                      'judgment': summary['uncertainty_judgment']}, ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)
