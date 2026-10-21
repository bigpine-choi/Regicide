"""One final search-only screen on three already exposed QGA positions.

No training or teacher queries. These examples guide development and are not
held-out evidence. A separately reserved gate controls the conditional match.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import chess
import chess.engine

from nnue_generalization_gate import ROOT, read, require, sha, write
from stockfish_teacher import _json_info, source_board

MODEL = ROOT / 'docs/checks/2026-10-22-nnue-head-repair/attempt-001/model.nnue'
MODEL_SHA = '43958337348ce3d16397ed63ffadcea9bbe9b97473e61489653ffdede02eb5ac'
SOURCE = ROOT / 'docs/checks/2026-10-22-nnue-output-only/attempt-001/move-choice-gate.json'
NODES = 10000


def known_cases():
    source = next(row['source'] for row in read(SOURCE)['cases']
                  if row['source']['query_id'] == 'choice-gate-opening-30')
    definitions = [('original_black_choice', [], 'f6e4'),
                   ('white_reply_after_Bf5', ['c8f5'], 'e5f6'),
                   ('quiet_fork_after_exf6_Bxb1', ['c8f5', 'e5f6', 'f5b1'], 'f3g5')]
    cases = []
    for name, continuation, expected in definitions:
        board = source_board(source)
        for text in continuation:
            require(chess.Move.from_uci(text) in board.legal_moves, 'Illegal exposed line')
            board.push_uci(text)
        require(chess.Move.from_uci(expected) in board.legal_moves, 'Illegal expected move')
        cases.append({'name': name, 'initial_fen': source['initial_fen'],
                      'history': source['history'] + continuation,
                      'fen': board.fen(en_passant='fen'), 'expected_move': expected,
                      'partition': 'exposed_development'})
    return cases


def fingerprint(row):
    return tuple(row[key] if key != 'pv_uci' else tuple(row[key])
                 for key in ('move_uci', 'depth', 'white_cp', 'nodes', 'pv_uci'))


def probe(binary, board, *, threats, incremental):
    started = time.perf_counter()
    options = {'UseNNUEEvaluation': True, 'NeuralModelFile': str(MODEL),
               'UseNeuralEvaluation': False, 'UseIncrementalNeuralEvaluation': incremental,
               'Hash': 16, 'Move Overhead': 20}
    with chess.engine.SimpleEngine.popen_uci([str(binary), 'uci'], timeout=30) as engine:
        if 'UseQuiescenceThreats' in engine.options:
            options['UseQuiescenceThreats'] = threats
        else:
            require(not threats, 'Candidate lacks the tactical search option')
        engine.configure(options)
        packets, last = [], {}
        with engine.analysis(board, chess.engine.Limit(nodes=NODES), game=object(),
                             info=chess.engine.INFO_ALL) as analysis:
            for packet in analysis:
                packets.append(_json_info(packet))
                if 'score' in packet:
                    last = packet.copy()
            best, total = analysis.wait().move, analysis.info
        require(best in board.legal_moves, 'Search returned an illegal move')
        require(1 <= total.get('nodes', 0) <= NODES, 'Missing/overshot total node budget')
        require('score' in last, 'Missing score/fallback packet')
        white_cp = last['score'].white().score()
        require(white_cp is not None, 'Known case unexpectedly returned mate')
        replay = board.copy(stack=True)
        pv = last.get('pv', [])
        require(bool(pv) and pv[0] == best, 'Published PV and best move differ')
        for move in pv:
            require(move in replay.legal_moves, 'Illegal PV')
            replay.push(move)
        return {'move_uci': best.uci(), 'depth': last['depth'], 'white_cp': white_cp,
                'completed_iteration': last['depth'] >= 1,
                'nodes': total['nodes'], 'pv_uci': [move.uci() for move in pv],
                'options': options, 'score_packets': packets, 'raw_info': _json_info(total),
                'elapsed_seconds': time.perf_counter() - started}


def check(baseline, candidate, output):
    require(not output.exists(), 'Refuse to overwrite/repeat final known-position screen')
    require(sha(MODEL) == MODEL_SHA, 'Frozen output-only model differs')
    paths = [SOURCE, MODEL, Path(__file__).resolve(), baseline, candidate,
             ROOT / 'src/search.cpp', ROOT / 'include/regicide/search.hpp', ROOT / 'src/uci.cpp',
             ROOT / 'scripts/nnue_generalization_gate.py', ROOT / 'scripts/stockfish_teacher.py']
    result = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
              'input_sha256': {str(path): sha(path) for path in paths},
              'model_sha256': MODEL_SHA, 'nodes_per_search': NODES,
              'old_binary_sha256': sha(baseline), 'new_binary_sha256': sha(candidate),
              'known_position_policy': 'All three fixed expected moves must match; no tolerance, replacement or teacher queries',
              'source_status': 'Previously exposed development examples; not independent generalization',
              'training_runs': 0, 'teacher_queries': 0, 'cases': []}
    mismatches = 0
    for case in known_cases():
        board = source_board(case)
        searches = {
            'baseline': probe(baseline, board, threats=False, incremental=True),
            'candidate_off': probe(candidate, board, threats=False, incremental=True),
            'candidate': probe(candidate, board, threats=True, incremental=True),
            'candidate_full': probe(candidate, board, threats=True, incremental=False),
        }
        off_equal = fingerprint(searches['baseline']) == fingerprint(searches['candidate_off'])
        oracle_equal = fingerprint(searches['candidate']) == fingerprint(searches['candidate_full'])
        mismatches += int(not off_equal) + int(not oracle_equal)
        passed = (searches['candidate']['completed_iteration']
                  and searches['candidate']['move_uci'] == case['expected_move'])
        result['cases'].append({'source': case, 'searches': searches,
                                'disabled_path_equal': off_equal,
                                'full_incremental_equal': oracle_equal, 'passed': passed})
    result.update(completed=True, native_searches=12, native_mismatches=mismatches,
                  known_position_passed=mismatches == 0 and all(row['passed'] for row in result['cases']))
    output.parent.mkdir(parents=True, exist_ok=True)
    write(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = check(args.baseline.resolve(), args.candidate.resolve(), args.output.resolve())
    print(json.dumps({'known_position_passed': result['known_position_passed'],
                      'native_mismatches': result['native_mismatches'],
                      'choices': [{r['source']['name']: r['searches']['candidate']['move_uci']}
                                  for r in result['cases']]}))


if __name__ == '__main__':
    main()
