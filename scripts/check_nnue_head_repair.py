"""One sealed-candidate check of exposed QGA repair positions, never selection.

These positions may have supplied TRAIN comparisons. They measure recovery on
the known example only. The separately reserved gate controls any match.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import chess.engine

from prepare_neural_huber import read, require, sha, write
from stockfish_teacher import source_board, _json_info
from train_neural_stm import ROOT


def check(dataset, candidate, output):
    require(not output.exists(), 'Refuse to repeat exposed repair checks')
    seal = read(candidate.parent / 'gate-seal.json')
    require(seal['candidate_sha256'] == sha(candidate), 'Seal final candidate first')
    validation = read(candidate.parent / 'validation.json')
    require(validation['python_native_full_incremental_mismatches'] == 0
            and validation['input_sha256'].get(str(candidate)) == sha(candidate), 'Candidate validation differs')
    protocol = read(dataset / 'protocol.json')
    choices = read(dataset / 'student-choices.json')
    critical = [r for r in choices if r['root'].get('critical_refutation')]
    require(len(critical) == 3, 'Keep exactly three exposed QGA checks')
    engine_path = Path(protocol['native_engine'])
    inputs = [dataset / 'protocol.json', dataset / 'student-choices.json', candidate,
              candidate.parent / 'gate-seal.json', candidate.parent / 'validation.json',
              engine_path, ROOT / 'config/engine-profiles/classical.json', Path(__file__)]
    bindings = {str(p): sha(p) for p in inputs}
    options = dict(read(ROOT / 'config/engine-profiles/classical.json')['options'],
                   UseNNUEEvaluation=True, NeuralModelFile=str(candidate))
    result = {'started_utc': datetime.now(timezone.utc).isoformat(), 'completed': False,
              'independent_of_training': False, 'used_in_selection_or_retraining': False,
              'teacher_queries': 0, 'input_sha256': bindings, 'cases': []}
    with chess.engine.SimpleEngine.popen_uci([str(engine_path), 'uci'], timeout=30) as engine:
        engine.configure(options)
        for entry in critical:
            root = entry['root']; board = source_board(root)
            depth = (entry['depth_decision']['branch_depth']
                     if root['depth_policy'] == 'critical_parent_completed_depth_minus_one' else 64)
            require(type(depth) is int and 1 <= depth <= 64, 'Invalid fixed development depth')
            engine.configure({'Clear Hash': None})
            last = {}
            with engine.analysis(board, chess.engine.Limit(nodes=10000, depth=depth),
                                 game=object(), info=chess.engine.INFO_ALL) as analysis:
                for packet in analysis:
                    if 'score' in packet:
                        last = packet.copy()
                best = analysis.wait().move
                aggregate = analysis.info
            require(best in board.legal_moves and last.get('pv') and last['pv'][0] == best,
                    'Development bestmove/PV invalid')
            replay = board.copy(stack=True)
            for move in last['pv']:
                require(move in replay.legal_moves, 'Development PV illegal')
                replay.push(move)
            require(last['score'].turn == board.turn, 'Development score POV invalid')
            result['cases'].append({'root_id': root['query_id'], 'warm_move': entry['student_move'],
                'candidate_move': best.uci(), 'candidate_san': board.san(best),
                'teacher_move': entry['teacher_move'], 'requested_depth': depth,
                'completed_depth': last.get('depth'), 'white_cp': last['score'].white().score(),
                'warm_matches_teacher': entry['student_move'] == entry['teacher_move'],
                'candidate_matches_teacher': best.uci() == entry['teacher_move'],
                'raw_info': _json_info(aggregate), 'raw_score_info_packet': _json_info(last)})
    for path, digest in bindings.items():
        require(sha(Path(path)) == digest, 'Input changed during development check')
    result.update(completed=True, native_searches=len(result['cases']))
    write(output, result)
    print(json.dumps({'cases': [{k: c[k] for k in ('root_id', 'warm_move', 'candidate_move', 'teacher_move')}
                               for c in result['cases']]}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path); parser.add_argument('candidate', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    check(args.dataset.resolve(), args.candidate.resolve(), args.output.resolve())
